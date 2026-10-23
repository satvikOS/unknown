"""Durable raw broker-event journal (book-review finding BR-ORD-20; ADR-0003, ADR-0005 #5, ADR-0012).

The gateway appends every broker event it takes from an adapter here, in one
fsync'd transaction, BEFORE any of it is applied to the core. Applying an
entry is an ordinary core command whose id is derived from the entry
(``sys.bev.<journal id>.<seq>``), and an entry is marked applied after the
command returns. So:

* a crash between the append and the apply loses nothing: the next process
  applies the unapplied entries in order before any new event;
* a crash between the apply and the mark re-applies the entry under the same
  command id and payload, which the core answers with the stored result
  (``Result.duplicate``) and no new event; the handlers' scoped broker keys
  (``exec_key``, the order's own state) make even a re-application under a new
  command id a duplicate, never a second economic effect;
* the in-memory hand-off is bounded: events wait in process memory only until
  the next append, and if the journal cannot be written the gateway stops
  taking events and the desk fails closed (``GatewayRunner`` latches the stop
  and requires reconciliation).

Storage: ``<UNKNOWN_HOME>/data/<account>.events``, an SQLite file opened with
the core store's durability settings (WAL, ``synchronous=FULL``, F_FULLFSYNC on
macOS; ``store.db.connect``). Tables are append-only (triggers refuse UPDATE
and DELETE). Each entry carries a SHA-256 hash chained to the previous entry;
entries are re-verified before they are applied. A broken chain is never
applied from the broken entry on: the verified entries before it are applied,
and the journal then refuses new appends (they could never be applied behind
it) until the owner stops the core and moves the file aside; events from then
on are recovered by reconciliation. Marks are committed with
``synchronous=NORMAL``: a mark lost to a power failure only causes a duplicate
re-application, never a lost or doubled event.

The core, not the marks, decides what was applied: ``reapply_after`` makes
marked entries unapplied again when the core does not have their commands
(a core database restored from an older backup next to this journal), and an
entry whose command id the core holds for a different command
(``JournalConflict``: a journal older than the core) breaks the journal the
same way a bad checksum does.

What this does not cover: an event the adapter received but the gateway had
not yet appended when the process died (the adapter's own memory) is
recovered only by reconciliation (executions inside the broker's history
window, statuses through the open-order list and lookups). The journal is not
part of the core's online backup and is not pruned; a restore of the core
database (RB-10) leaves it in place, and the next process applies what the
restored core is missing (``GatewayRunner.replay_journal``)."""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from ..domain.ids import validate_id

FORMAT = 1
GENESIS = "0" * 64
COMMAND_ID_PREFIX = "sys.bev."

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY, appended_ns INTEGER NOT NULL, "
    "event_type TEXT NOT NULL, event TEXT NOT NULL, command_kind TEXT NOT NULL, command_payload TEXT NOT NULL, "
    "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS applied (seq INTEGER PRIMARY KEY REFERENCES entries(seq), applied_ns INTEGER NOT NULL, "
    "command_id TEXT NOT NULL, outcome TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries "
    "BEGIN SELECT RAISE(ABORT, 'event journal entries are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries "
    "BEGIN SELECT RAISE(ABORT, 'event journal entries are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS applied_no_update BEFORE UPDATE ON applied "
    "BEGIN SELECT RAISE(ABORT, 'event journal marks are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS applied_no_delete BEFORE DELETE ON applied "
    "BEGIN SELECT RAISE(ABORT, 'event journal marks are append-only'); END",
)


class JournalUnavailable(RuntimeError):
    """The journal cannot be opened, written or trusted. Events are not taken from the broker."""


class JournalCorrupt(JournalUnavailable):
    """An entry fails its checksum or the chain is broken: nothing from it on is applied."""


class JournalConflict(JournalUnavailable):
    """An entry's command id is recorded in the core for a different command: this journal is not the history
    of this core (for example a journal file rolled back to an older copy), so nothing from it on is applied."""


@dataclass(frozen=True)
class JournalEntry:
    journal_id: str
    seq: int
    appended_ns: int
    event_type: str
    event: str                 # the event as the adapter delivered it (JSON; diagnostic and audit)
    command_kind: str          # the core command it is applied as
    command_payload: dict
    hash: str

    @property
    def command_id(self) -> str:
        return f"{COMMAND_ID_PREFIX}{self.journal_id}.{self.seq}"


def _plain(value: Any) -> Any:
    if isinstance(value, Fixed):
        return value.to_str()
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    return {"__repr__": repr(value)}


def event_json(event: Any) -> str:
    """The event with every field, including the adapter's ``raw`` map, as sorted JSON text."""
    return json.dumps(_plain(event), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _entry_hash(prev_hash: str, seq: int, appended_ns: int, event_type: str, event: str, command_kind: str,
                command_payload: str) -> str:
    return sha256_hex(prev_hash + canonical_json({"seq": seq, "appended_ns": appended_ns, "event_type": event_type,
                                                  "event": event, "command_kind": command_kind,
                                                  "command_payload": command_payload}))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EventJournal:
    """One account's broker-event journal. Opened lazily; every operation may raise ``JournalUnavailable``.

    Not thread-safe: the core service uses it from its loop thread only."""

    def __init__(self, path: str | Path, account_key: str, now_ns: Callable[[], int], *, journal_id: str | None = None):
        self.path = Path(path)
        self.account_key = validate_id(account_key, "account key")
        self._now_ns = now_ns
        self._requested_id = journal_id
        self._conn: sqlite3.Connection | None = None
        self.journal_id: str | None = None
        self._applied_through = 0          # every seq <= this is applied (marked, and not to be re-applied)
        self._reapply_through = 0          # marked entries up to this seq are applied again (reapply_after)
        self._broken: JournalUnavailable | None = None   # a corrupt or conflicting entry: no appends, apply stops
        self.failure: str | None = None    # the last failure; cleared by the next successful append
        self.reported_failure: str | None = None   # the failure the gateway recorded in the core (episode)

    # ------------------------------------------------------------ open
    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        from ..store.db import connect
        conn = None
        try:
            created = not self.path.exists()
            conn = connect(self.path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in SCHEMA:
                    conn.execute(statement)
                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                if not meta:
                    journal_id = self._requested_id or os.urandom(8).hex()
                    validate_id(f"{COMMAND_ID_PREFIX}{journal_id}.1", "journal command id")
                    meta = {"journal_id": journal_id, "account_key": self.account_key, "format": str(FORMAT),
                            "created_ns": str(self._now_ns())}
                    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", sorted(meta.items()))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            if created:
                _fsync_dir(self.path.parent)   # the new file's directory entry is durable too
            if meta.get("account_key") != self.account_key:
                raise self._refused(f"it belongs to account {meta.get('account_key')!r}, not {self.account_key!r}")
            if meta.get("format") != str(FORMAT):
                raise self._refused(f"it has journal format {meta.get('format')!r}; expected {FORMAT}")
            if self._requested_id is not None and meta["journal_id"] != self._requested_id:
                raise self._refused(f"it has journal id {meta['journal_id']!r}, not {self._requested_id!r}")
            first = conn.execute("SELECT MIN(e.seq) AS s FROM entries e WHERE NOT EXISTS "
                                 "(SELECT 1 FROM applied a WHERE a.seq = e.seq)").fetchone()["s"]
            last = conn.execute("SELECT MAX(seq) AS s FROM entries").fetchone()["s"] or 0
            through = (first - 1) if first is not None else last
            # Reopened after a dropped connection: keep a rewind (reapply_after) that is still being re-applied.
            self._applied_through = through if self.journal_id is None else min(through, self._applied_through)
            self.journal_id = meta["journal_id"]
            self._conn = conn
            # Every unapplied entry (and its link to the last applied one) is checked before anything is applied.
            # A broken entry leaves the journal open: the verified entries before it can still be applied,
            # and ``append`` refuses from now on.
            try:
                self._verified_rows(self._applied_through, None)
            except (JournalCorrupt, JournalConflict):
                pass   # ``broken`` is set: unapplied() raises once the verified entries are applied
        except JournalUnavailable:
            self._drop(conn)
            raise
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            self._drop(conn)
            raise self._failed(exc) from exc
        return conn

    def _drop(self, conn: sqlite3.Connection | None) -> None:
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _failed(self, exc: BaseException) -> JournalUnavailable:
        self.failure = f"{type(exc).__name__}: {exc}"
        return JournalUnavailable(f"event journal {self.path.name}: {self.failure}")

    def _refused(self, why: str) -> JournalUnavailable:
        self.failure = f"refused: {why}"
        return JournalUnavailable(f"event journal {self.path.name} refused: {why}")

    def close(self) -> None:
        self._drop(self._conn)

    @property
    def broken(self) -> JournalUnavailable | None:
        """The corrupt or conflicting entry that stops this journal (until the file is moved aside), if any."""
        return self._broken

    def _break(self, error: JournalUnavailable) -> JournalUnavailable:
        """Record the first broken entry (sticky for this journal object) and return a fresh error for it."""
        if self._broken is None:
            self._broken = error
        self.failure = f"{type(self._broken).__name__}: {self._broken}"
        return type(self._broken)(str(self._broken))

    def conflict(self, entry: JournalEntry, why: str) -> JournalConflict:
        """Mark the journal broken at ``entry`` (its command id names something else in the core); returns the error."""
        error = JournalConflict(f"event journal {self.path.name}: entry {entry.seq} conflicts with the core: {why}; "
                                "nothing from this entry on is applied and nothing more is appended (stop the core, move "
                                "the journal file aside, restart and reconcile)")
        error.seq = entry.seq
        return self._break(error)

    # ------------------------------------------------------------ write
    def append(self, records: Sequence[tuple[Any, str, dict]]) -> list[JournalEntry]:
        """Append ``(event, command_kind, command_payload)`` records in order, in ONE fsync'd transaction.

        All or nothing: on any failure nothing is appended and ``JournalUnavailable`` is raised."""
        if not records:
            return []
        prepared = [(type(ev).__name__, event_json(ev), kind, canonical_json(payload)) for ev, kind, payload in records]
        conn = self._connection()
        if self._broken is not None:   # appended behind a broken entry, an event could never be applied
            raise self._break(self._broken)
        entries: list[JournalEntry] = []
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                last = conn.execute("SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
                seq, prev = (last["seq"], last["hash"]) if last else (0, GENESIS)
                now = self._now_ns()
                for event_type, event, kind, payload in prepared:
                    seq += 1
                    digest = _entry_hash(prev, seq, now, event_type, event, kind, payload)
                    conn.execute("INSERT INTO entries(seq, appended_ns, event_type, event, command_kind, command_payload, "
                                 "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                                 (seq, now, event_type, event, kind, payload, prev, digest))
                    entries.append(JournalEntry(self.journal_id, seq, now, event_type, event, kind,
                                                json.loads(payload), digest))
                    prev = digest
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        self.failure = None
        return entries

    def mark_applied(self, entry: JournalEntry, outcome: str) -> None:
        """Record that ``entry``'s command returned (``outcome``: applied, duplicate or rejected)."""
        conn = self._connection()
        try:
            conn.execute("PRAGMA synchronous=NORMAL")   # a lost mark only causes a duplicate re-application
            try:
                conn.execute("INSERT OR IGNORE INTO applied(seq, applied_ns, command_id, outcome) VALUES (?,?,?,?)",
                             (entry.seq, self._now_ns(), entry.command_id, outcome))
            finally:
                conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        if entry.seq == self._applied_through + 1:
            self._applied_through = entry.seq

    def marked_newest_first(self, page: int = 500) -> Iterator[tuple[int, str]]:
        """``(seq, command_id)`` of every marked entry, newest first, read a page at a time (no open cursor)."""
        below = None
        while True:
            conn = self._connection()
            try:
                rows = conn.execute("SELECT seq, command_id FROM applied WHERE seq < COALESCE(?, seq + 1) "
                                    "ORDER BY seq DESC LIMIT ?", (below, page)).fetchall()
            except sqlite3.Error as exc:
                raise self._failed(exc) from exc
            if not rows:
                return
            yield from ((r["seq"], r["command_id"]) for r in rows)
            below = rows[-1]["seq"]

    def reapply_after(self, seq: int) -> int:
        """Treat every entry after ``seq`` as unapplied, marked or not: the core does not have their commands
        (it was restored from a backup taken before they were applied). Returns how many marked entries that
        makes unapplied again. Their old marks stay (append-only); a re-application adds no second mark."""
        conn = self._connection()
        try:
            last_mark = conn.execute("SELECT MAX(seq) AS s FROM applied").fetchone()["s"] or 0
            again = conn.execute("SELECT COUNT(*) AS n FROM applied WHERE seq > ?", (seq,)).fetchone()["n"]
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        if seq < self._applied_through:
            self._applied_through = seq
        self._reapply_through = max(self._reapply_through, last_mark)
        return again

    # ------------------------------------------------------------ read
    def unapplied(self, limit: int | None = None) -> list[JournalEntry]:
        """Unapplied entries, oldest first, each verified against its checksum and its predecessor.

        On a broken chain this returns the verified entries before the broken one and raises only when there
        are none (``broken`` stays set)."""
        self._connection()
        return self._verified_rows(self._applied_through, limit)

    def _verified_rows(self, after: int, limit: int | None) -> list[JournalEntry]:
        conn = self._conn
        assert conn is not None
        try:
            if after > 0:
                anchor = conn.execute("SELECT hash FROM entries WHERE seq=?", (after,)).fetchone()
                if anchor is None:
                    raise self._break(self._corrupt(f"entry {after} is missing", after))
                prev = anchor["hash"]
            else:
                prev = GENESIS
            rows = conn.execute("SELECT e.* , EXISTS (SELECT 1 FROM applied a WHERE a.seq = e.seq) AS done "
                                "FROM entries e WHERE e.seq > ? ORDER BY e.seq", (after,))
            out: list[JournalEntry] = []
            expected = after + 1
            for r in rows:
                problem = None
                if self._broken is not None and r["seq"] >= self._broken_seq:
                    problem = self._broken
                elif r["seq"] != expected:
                    problem = self._corrupt(f"entry {expected} is missing (next is {r['seq']})", expected)
                elif r["prev_hash"] != prev or r["hash"] != _entry_hash(
                        prev, r["seq"], r["appended_ns"], r["event_type"], r["event"], r["command_kind"], r["command_payload"]):
                    problem = self._corrupt(f"entry {r['seq']} fails its checksum", r["seq"])
                if problem is not None:
                    error = self._break(problem)
                    if out:
                        return out   # the verified entries before the broken one; the next call raises
                    raise error
                prev = r["hash"]
                expected += 1
                if not r["done"] or r["seq"] <= self._reapply_through:
                    out.append(JournalEntry(self.journal_id, r["seq"], r["appended_ns"], r["event_type"], r["event"],
                                            r["command_kind"], json.loads(r["command_payload"]), r["hash"]))
                    if limit is not None and len(out) >= limit:
                        break
            return out
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    @property
    def _broken_seq(self) -> int:
        return getattr(self._broken, "seq", 0) or 0

    def _corrupt(self, what: str, seq: int) -> JournalCorrupt:
        error = JournalCorrupt(f"event journal {self.path.name}: {what}; nothing from this entry on is applied and "
                               "nothing more is appended (stop the core, move the journal file aside, restart and "
                               "reconcile)")
        error.seq = seq
        return error

    def unapplied_count(self) -> int:
        conn = self._connection()
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM entries e WHERE e.seq > ? AND (e.seq <= ? OR NOT EXISTS "
                                "(SELECT 1 FROM applied a WHERE a.seq = e.seq))",
                                (self._applied_through, self._reapply_through)).fetchone()["n"]
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    def entries(self) -> Iterable[JournalEntry]:
        """Every entry, oldest first (tests and audit)."""
        conn = self._connection()
        for r in conn.execute("SELECT * FROM entries ORDER BY seq"):
            yield JournalEntry(self.journal_id, r["seq"], r["appended_ns"], r["event_type"], r["event"], r["command_kind"],
                               json.loads(r["command_payload"]), r["hash"])

    def marks(self) -> dict[int, dict]:
        conn = self._connection()
        return {r["seq"]: dict(r) for r in conn.execute("SELECT * FROM applied ORDER BY seq")}

    def verify(self) -> list[str]:
        """Recompute the whole chain; every mark must name an entry. Returns problems (empty when sound)."""
        problems: list[str] = []
        conn = self._connection()
        prev, expected = GENESIS, 1
        for r in conn.execute("SELECT * FROM entries ORDER BY seq"):
            if r["seq"] != expected:
                problems.append(f"entry {expected} missing (next is {r['seq']})")
            if r["prev_hash"] != prev:
                problems.append(f"entry {r['seq']} prev_hash mismatch")
            if r["hash"] != _entry_hash(r["prev_hash"], r["seq"], r["appended_ns"], r["event_type"], r["event"],
                                        r["command_kind"], r["command_payload"]):
                problems.append(f"entry {r['seq']} checksum mismatch")
            prev, expected = r["hash"], r["seq"] + 1
        for r in conn.execute("SELECT a.seq FROM applied a LEFT JOIN entries e ON e.seq = a.seq WHERE e.seq IS NULL"):
            problems.append(f"mark for missing entry {r['seq']}")
        return problems

    def status(self) -> dict:
        """For the health query: never raises."""
        out = {"path": str(self.path), "journal_id": self.journal_id, "failure": self.failure, "unapplied": None}
        try:
            out["unapplied"] = self.unapplied_count()
            out["journal_id"] = self.journal_id
        except JournalUnavailable as exc:
            out["failure"] = str(exc)
        return out
