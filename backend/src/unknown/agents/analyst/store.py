"""Conversation threads, stream events and cited tool results of the AI analyst (``<UNKNOWN_HOME>/agents/analyst.sqlite``).

Tables
  thread   one conversation: id, title (the first question), times
  turn     one question in a thread and, once the worker has run its job, the answer (text, citations, checks, model,
           effort, tokens, cost, status). ``job_id`` links it to the analyst job store (``analyst_jobs.sqlite``).
  event    the job's stream, append-only: status, text deltas, tool calls, tool results, usage, the answer, the end.
           ``seq`` is global and increasing, so it is the SSE event id.
  result   every tool result a job produced, by its citation ref (R1, R2, ...): tool, arguments, gateway decision,
           datasets and the exact content the model saw, so the terminal can open each citation.

Writers: the terminal API adds threads and questions (``add_question``); the analyst worker adds events, results and
answers. Both use WAL with ``synchronous=FULL`` (``common.connect_durable``); the stream reader opens it read-only.
The file holds the owner's questions and model output, never a key, and is mode 0600.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ..common import connect_durable

MAX_HISTORY_TURNS = 6
MAX_HISTORY_ANSWER = 8000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread(
  thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS turn(
  thread_id TEXT NOT NULL REFERENCES thread(thread_id), seq INTEGER NOT NULL, job_id TEXT NOT NULL UNIQUE,
  question TEXT NOT NULL, security TEXT, asked_ns INTEGER NOT NULL, answer TEXT, answered_ns INTEGER,
  PRIMARY KEY (thread_id, seq));
CREATE TABLE IF NOT EXISTS event(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, kind TEXT NOT NULL, at_ns INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_job ON event(job_id, seq);
CREATE TABLE IF NOT EXISTS result(
  job_id TEXT NOT NULL, ref TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL, decision TEXT NOT NULL,
  rule_id TEXT NOT NULL, datasets TEXT NOT NULL, content TEXT NOT NULL, at_ns INTEGER NOT NULL, PRIMARY KEY (job_id, ref));
"""


class ThreadBusy(RuntimeError):
    """The thread's previous question has no answer yet (HTTP 409)."""


def default_analyst_path(home: Path) -> Path:
    return Path(home) / "agents" / "analyst.sqlite"


def default_analyst_jobs_path(home: Path) -> Path:
    return Path(home) / "agents" / "analyst_jobs.sqlite"


def new_thread_id() -> str:
    return "thr-" + uuid.uuid4().hex


def new_ask_job_id() -> str:
    return "ask-" + uuid.uuid4().hex


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True, default=str)


class AnalystStore:
    def __init__(self, path: Path, *, clock_ns: Callable[[], int], readonly: bool = False):
        self.path = Path(path)
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self.readonly = readonly
        self._db = connect_durable(self.path, readonly=readonly)
        if not readonly:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, fn: Callable[[], Any]) -> Any:
        if self.readonly:
            raise RuntimeError("analyst store opened read-only")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn()
                self._db.execute("COMMIT")
                return out
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ threads and questions (API)
    def add_question(self, *, job_id: str, question: str, security: str | None, thread_id: str | None) -> dict:
        """Start a thread (``thread_id`` None) or continue one; returns {thread_id, seq, history, history_datasets}.
        A thread whose last question is still unanswered is busy."""
        def tx() -> dict:
            now = self._clock_ns()
            tid = thread_id
            if tid is None:
                tid = new_thread_id()
                self._db.execute("INSERT INTO thread(thread_id, title, created_ns, updated_ns) VALUES (?,?,?,?)",
                                 (tid, question.strip()[:120], now, now))
            elif self._db.execute("SELECT 1 FROM thread WHERE thread_id=?", (tid,)).fetchone() is None:
                raise LookupError(f"no thread {tid}")
            rows = self._db.execute("SELECT seq, question, answer FROM turn WHERE thread_id=? ORDER BY seq", (tid,)).fetchall()
            if rows and rows[-1]["answer"] is None:
                raise ThreadBusy("the previous question in this thread has no answer yet; wait for it or cancel it")
            history, datasets = [], set()
            for r in rows[-MAX_HISTORY_TURNS:]:
                ans = json.loads(r["answer"])
                if ans.get("text"):
                    history.append({"question": r["question"], "answer": ans["text"][:MAX_HISTORY_ANSWER]})
                    datasets.update(ans.get("datasets") or [])
            seq = (rows[-1]["seq"] + 1) if rows else 1
            self._db.execute("INSERT INTO turn(thread_id, seq, job_id, question, security, asked_ns) VALUES (?,?,?,?,?,?)",
                             (tid, seq, job_id, question, security, now))
            self._db.execute("UPDATE thread SET updated_ns=? WHERE thread_id=?", (now, tid))
            return {"thread_id": tid, "seq": seq, "history": history, "history_datasets": sorted(datasets)}
        return self._write(tx)

    def drop_question(self, job_id: str) -> None:
        """Undo ``add_question`` when the job could not be queued (and the thread, if it has no other turn)."""
        def tx() -> None:
            row = self._db.execute("SELECT thread_id FROM turn WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return
            self._db.execute("DELETE FROM turn WHERE job_id=? AND answer IS NULL", (job_id,))
            if self._db.execute("SELECT 1 FROM turn WHERE thread_id=?", (row["thread_id"],)).fetchone() is None:
                self._db.execute("DELETE FROM thread WHERE thread_id=?", (row["thread_id"],))
        self._write(tx)

    def threads(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.thread_id, t.title, t.created_ns, t.updated_ns, COUNT(u.seq) AS turns FROM thread t "
                "LEFT JOIN turn u ON u.thread_id = t.thread_id GROUP BY t.thread_id ORDER BY t.updated_ns DESC LIMIT ?",
                (max(1, min(int(limit), 500)),)).fetchall()
        return [dict(r) for r in rows]

    def thread(self, thread_id: str) -> dict | None:
        with self._lock:
            t = self._db.execute("SELECT * FROM thread WHERE thread_id=?", (thread_id,)).fetchone()
            if t is None:
                return None
            turns = self._db.execute("SELECT * FROM turn WHERE thread_id=? ORDER BY seq", (thread_id,)).fetchall()
        return {**dict(t), "turns": [self._turn(r) for r in turns]}

    def turn_for_job(self, job_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM turn WHERE job_id=?", (job_id,)).fetchone()
        return self._turn(r) if r is not None else None

    @staticmethod
    def _turn(r) -> dict:
        d = dict(r)
        d["answer"] = json.loads(d["answer"]) if d["answer"] is not None else None
        return d

    # ------------------------------------------------------------------ worker writes
    def append_event(self, job_id: str, kind: str, data: Any) -> int:
        def tx() -> int:
            cur = self._db.execute("INSERT INTO event(job_id, kind, at_ns, data) VALUES (?,?,?,?)",
                                   (job_id, kind, self._clock_ns(), _dumps(data)))
            return int(cur.lastrowid)
        return self._write(tx)

    def put_result(self, job_id: str, ref: str, *, tool: str, arguments: Any, decision: str, rule_id: str,
                   datasets: list[str], content: str) -> None:
        self._write(lambda: self._db.execute(
            "INSERT OR REPLACE INTO result(job_id, ref, tool, arguments, decision, rule_id, datasets, content, at_ns) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, ref, tool, _dumps(arguments), decision, rule_id, _dumps(datasets), content, self._clock_ns())))

    def record_answer(self, job_id: str, answer: dict) -> None:
        """Store a job's answer on its turn (the turn row exists from submission; it is never overwritten)."""
        def tx() -> None:
            cur = self._db.execute("UPDATE turn SET answer=?, answered_ns=? WHERE job_id=? AND answer IS NULL",
                                   (_dumps(answer), self._clock_ns(), job_id))
            if cur.rowcount == 1:
                self._db.execute("UPDATE thread SET updated_ns=? WHERE thread_id=(SELECT thread_id FROM turn WHERE job_id=?)",
                                 (self._clock_ns(), job_id))
        self._write(tx)

    # ------------------------------------------------------------------ reads
    def unanswered_jobs(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT job_id FROM turn WHERE answer IS NULL ORDER BY asked_ns")]

    def events_after(self, job_id: str, after_seq: int | None, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT seq, kind, at_ns, data FROM event WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                                    (job_id, after_seq or 0, limit)).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "at_ns": r["at_ns"], "data": json.loads(r["data"])} for r in rows]

    def result(self, job_id: str, ref: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM result WHERE job_id=? AND ref=?", (job_id, ref)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["arguments"], d["datasets"] = json.loads(d["arguments"]), json.loads(d["datasets"])
        return d

    def results(self, job_id: str) -> list[dict]:
        with self._lock:
            refs = [r[0] for r in self._db.execute("SELECT ref FROM result WHERE job_id=? ORDER BY at_ns, ref", (job_id,))]
        return [x for x in (self.result(job_id, ref) for ref in refs) if x is not None]
