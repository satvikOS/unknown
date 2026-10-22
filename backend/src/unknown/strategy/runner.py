"""Strategy runner process (ADR-0017 #4 and #5; book ch. 15 algorithm A15).

A runner runs ONE registered artifact for ONE account, in ``shadow`` or ``live`` mode. It is a research-side
process like the agent worker: it holds no broker credentials, reads the research registry and the
point-in-time store read-only, reads the core database read-only, and writes to the core only through the
command socket with the owner-issued token bound to its artifact (``secrets/strategy/<artifact>.token``), as
principal ``{id: <artifact id>, role: STRATEGY}``. The core's pretrade, reservation, stop, session and mandate
rules apply unchanged; the runner never talks to the broker.

One cycle (``run_once``):

1. Load the artifact and refuse unless the registry status permits the mode, the bundle is intact, the artifact
   has not expired and this checkout's code hash is the registered one (``artifacts.load``).
2. Find the latest eligible session ``D``: the latest business day of the calendar whose decision instant
   (``D`` at the spec's ``decision_clock_utc``, as in the backtest) has passed. Only ``D`` is ever decided; missed
   sessions are not back-filled. Decisions an earlier cycle left unrecorded (outage, crash) are finished first,
   and an intent past its deadline is never sent (recorded ``BLOCKED`` RUNNER_LATE unless the core holds it).
3. For each instrument of the spec that has no final decision for ``D`` yet (journal, then the core's stored
   commands if the journal was lost): read the bars visible at the decision instant from the point-in-time
   store (``research.bars.load_series`` + ``Cursor``, the engine's as-of reader). An instrument whose latest
   visible bar is not the completed bar of ``D`` is ``SKIPPED_STALE_DATA``.
4. Compute the strategy's targets with the research code itself (``research.strategies.build(spec).decide`` on a
   ``DecisionContext``, the backtest's code path), read the session's current and pending exposure from the core,
   and map targets to orders with the A15 mapping (``sizing.plan``: no-trade band, minimum size, cash).
5. Journal every decision (PLANNED) with deterministic ids, then, in live mode, submit each intent
   (``submit_intent``, DAY LIMIT, ``deadline_ns`` = the end of its execution session, ``purpose`` ENTRY/EXIT,
   ``policy_hash`` of the session mandate) and record every decision with ``record_strategy_decision``:
   ``INTENT_SUBMITTED``, ``BLOCKED`` (the core's rejection code, or a runner gate), ``NO_TRADE`` or
   ``SKIPPED_STALE_DATA``. Shadow mode records the same decisions with ``mode: shadow`` and never submits; a
   decision that would have traded is ``NO_TRADE`` with the reason ``SHADOW_WOULD_SUBMIT ...`` (the core's
   outcome vocabulary has no separate shadow outcome).

``run_loop`` repeats ``run_once`` on a schedule: once the decision instant of the latest eligible session plus a
grace period has passed, every ``poll_s`` seconds, re-checking the artifact's status each time (a quarantined or
retired artifact stops making decisions at the next cycle)."""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ..domain.calendar import WEEKDAYS_ONLY, BusinessCalendar, SessionTimeUnknown
from ..domain.clock import NS_PER_S, Clock, SystemClock
from ..domain.fixed import Fixed, Rounding
from ..research.bars import Bar, BarSeries, Cursor, DatasetView, load_series
from ..research.common import float_to_fixed, stable_hash
from ..research.engine import DecisionContext, Sizing, StrategyDecision
from ..research.registry import ResearchRegistry
from ..research.spec import StrategySpec
from ..research.strategies import build
from ..service.ipc import CoreClient, IpcError
from ..service.paths import Paths
from . import artifacts
from .artifacts import ArtifactRefused, LoadedArtifact
from .coreview import CoreSnapshot, CoreView
from .journal import FINAL, Journal, decision_ids
from .sizing import Holding, InstrumentPlan, plan

log = logging.getLogger("unknown.strategy")

MAX_REASON = 1000
DEFAULT_GRACE_S = 600
DEFAULT_POLL_S = 60
_ACCOUNT_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")    # an account key usable as one path component


class RunnerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CoreUnreachable(RunnerError):
    pass


# ------------------------------------------------------------------ calendar
def decision_instant(day: date, clock_utc: time) -> int:
    """The backtest's decision instant for a session (``research.engine._decision_ns``)."""
    return int(datetime.combine(day, clock_utc, tzinfo=timezone.utc).timestamp()) * NS_PER_S


def latest_eligible_session(now_ns: int, calendar: BusinessCalendar, clock_utc: time) -> date:
    """The latest business day whose decision instant is at or before ``now_ns``.
    ``CalendarHorizonError`` propagates: beyond the calendar's data the answer is unknown, never 'open'."""
    today = datetime.fromtimestamp(now_ns // NS_PER_S, tz=timezone.utc).date()
    for k in range(0, 31):
        day = today - timedelta(days=k)
        if calendar.is_business_day(day) and decision_instant(day, clock_utc) <= now_ns:
            return day
    raise RunnerError("NO_ELIGIBLE_SESSION", f"no business day of {calendar.calendar_id} in the last 31 days")


def _session_close_ns(calendar: BusinessCalendar, day: date) -> int | None:
    try:
        window = calendar.session_utc(day)
    except SessionTimeUnknown:
        return None                     # e.g. the WEEKDAYS fixture calendar has no session hours
    return int(window[1].timestamp()) * NS_PER_S if window else None


def execution_window(session_date: date, calendar: BusinessCalendar, clock_utc: time, delay: int) -> tuple[date, int]:
    """(execution session, intent deadline). The deadline is the end of the execution session when the calendar
    knows its hours, and never later than that session's own decision instant, so an intent cannot outlive the
    next decision."""
    exec_date = calendar.add_business_days(session_date, delay)
    deadline = decision_instant(exec_date, clock_utc)
    close = _session_close_ns(calendar, exec_date)
    if close is not None and close < deadline:
        deadline = close
    return exec_date, deadline


# ------------------------------------------------------------- research code path
class _RuntimeEngine:
    """The part of the backtest engine's ``_Run`` that ``DecisionContext`` reads: the bounded view, one
    point-in-time cursor per instrument, positions and equity at the strategy's marks."""

    def __init__(self, view: DatasetView, positions: dict[str, Fixed], equity_fn):
        self.view = view
        self.cursors = {i: Cursor(view.series[i]) for i in view.instrument_ids}
        self._positions = positions
        self._equity_fn = equity_fn

    def position(self, instrument_id: str) -> Fixed:
        return self._positions.get(instrument_id, Fixed(0))

    def marks(self) -> dict[str, Fixed | None]:
        return {i: (c.history[-1].close if c.history else None) for i, c in self.cursors.items()}

    def equity_at_marks(self) -> Fixed:
        return self._equity_fn(self.marks())


def strategy_decision(spec: StrategySpec, series: dict[str, BarSeries], session_date: date, decision_ns: int, *,
                      positions: dict[str, Fixed] | None = None, equity_fn=None) -> tuple[StrategyDecision, dict[str, list[Bar]]]:
    """The strategy's decision at one decision instant, computed by the research implementation exactly as the
    backtester calls it. Returns (decision, bars visible per instrument)."""
    view = DatasetView(dict(series), until=session_date, label=f"runtime:{spec.strategy_id}:{session_date.isoformat()}")
    engine = _RuntimeEngine(view, positions or {}, equity_fn or (lambda marks: Fixed(0)))
    new, revised = {}, {}
    for i, c in engine.cursors.items():
        new[i], revised[i] = c.advance(decision_ns)
    strategy = build(spec)
    strategy.reset()
    decision = strategy.decide(DecisionContext(engine, decision_ns, session_date, new, revised))
    if not isinstance(decision, StrategyDecision):
        raise TypeError("strategy.decide must return a StrategyDecision")
    return decision, {i: list(c.history) for i, c in engine.cursors.items()}


def _adv(bars: list[Bar], window: int) -> Fixed | None:
    hist = bars[-window:]
    if not hist:
        return None
    return sum((b.volume for b in hist), Fixed(0)).div(Fixed(len(hist)), 0, Rounding.FLOOR)


def _strategy_note(note: Any) -> tuple[str, str | None]:
    """(reason text, score as a decimal string) from the strategy's per-instrument notes."""
    if not isinstance(note, dict):
        return (str(note) if note else ""), None
    if note.get("state") == "WARMING":
        return f"WARMING {note.get('observations')}/{note.get('required', '?')} observations", None
    z = note.get("z")
    if isinstance(z, float):
        w = note.get("w")
        text = f"{note.get('formula', '')} z={z:.6g}" + (f" w={w:.6f}" if isinstance(w, float) else "")
        return text.strip(), float_to_fixed(z, 8, Rounding.HALF_EVEN).to_str()
    return ", ".join(f"{k}={v}" for k, v in sorted(note.items()) if not isinstance(v, (dict, list)))[:200], None


def _claimed(command_id: str, stored: dict) -> dict:
    """The refusal body for one of our command ids that another principal's stored command holds."""
    return {"code": "COMMAND_ID_CLAIMED",
            "message": f"command id {command_id} is held by {stored.get('principal_role')} {stored.get('principal_id')}; "
                       "the core refuses it from this artifact (CommandIdConflict)"}


def _join(*parts: str) -> str:
    text = "; ".join(p for p in parts if p)
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 3] + "..."


# ---------------------------------------------------------------------- runner
@dataclass
class RunnerConfig:
    artifact_id: str
    account_key: str
    mode: str                                   # shadow | live
    paths: Paths
    session_id: str | None = None               # None: the account's single active session, re-read every cycle
    calendar: BusinessCalendar = WEEKDAYS_ONLY
    registry_path: Path | None = None           # default <home>/research/experiments.sqlite
    research_path: Path | None = None           # default <home>/research/research.sqlite (point-in-time store)
    journal_path: Path | None = None            # default <home>/strategy/<account>/<artifact>.journal.sqlite

    def __post_init__(self) -> None:
        artifacts.split_id(self.artifact_id)
        if self.mode not in artifacts.MODES:
            raise ValueError(f"mode must be one of {artifacts.MODES}")
        if not isinstance(self.account_key, str) or not _ACCOUNT_DIR.fullmatch(self.account_key) or ".." in self.account_key:
            raise ValueError(f"account key {self.account_key!r} cannot name a journal directory")
        root = self.paths.root
        self.registry_path = Path(self.registry_path or root / "research" / "experiments.sqlite")
        self.research_path = Path(self.research_path or root / "research" / "research.sqlite")
        # One journal (and one lock) per account AND artifact: decisions, ids and "already decided" are facts of one
        # account's core, so a journal shared across accounts would skip the second account's decisions (review fix).
        self.journal_path = Path(self.journal_path or root / "strategy" / self.account_key / f"{self.artifact_id}.journal.sqlite")


@dataclass
class _Decision:
    instrument_id: str
    outcome: str                                # NO_TRADE | INTENT | BLOCKED | SKIPPED_STALE_DATA
    reason: str
    digest: str
    score: str | None = None
    target_quantity: Fixed | None = None
    current_quantity: Fixed | None = None
    order: Any = None                           # sizing.OrderPlan for an intent
    registered: bool = True
    detail: dict = field(default_factory=dict)


class RunnerLock:
    """One runner per journal, i.e. per account and artifact: an exclusive ``flock`` held for the process's lifetime."""

    def __init__(self, path: Path):
        self.path = Path(str(path) + ".lock")
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise RunnerError("RUNNER_ALREADY_RUNNING", f"another runner holds {self.path}") from None
            raise
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class StrategyRunner:
    def __init__(self, config: RunnerConfig, *, clock: Clock | None = None, client: CoreClient | None = None,
                 code_hash: str | None = None):
        self.cfg = config
        self.clock = clock or SystemClock()
        self._client = client
        self._code_hash = code_hash          # tests may pin it; default: this checkout's research code hash
        self.core = CoreView(config.paths.db(config.account_key))
        self.lock = RunnerLock(config.journal_path)
        self.lock.acquire()
        try:
            self.journal = Journal(config.journal_path)
        except BaseException:
            self.lock.release()
            raise
        self.runtime_code = artifacts.runtime_code_hash()
        self.cycles = 0                       # completed run_loop cycles (each one a run_once)
        self.last_summary: dict | None = None

    def close(self) -> None:
        self.journal.close()
        self.lock.release()

    def __enter__(self) -> "StrategyRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- plumbing
    def _principal(self) -> dict:
        return {"id": self.cfg.artifact_id, "role": "STRATEGY", "authenticated_at_ns": None}

    def client(self) -> CoreClient:
        if self._client is None:
            tokens = self.cfg.paths.strategy_tokens()
            token = tokens.get(self.cfg.artifact_id)
            if token is None:
                raise ArtifactRefused("NO_STRATEGY_TOKEN", f"no owner-issued token secrets/strategy/{self.cfg.artifact_id}.token "
                                                           "(python -m unknown.cli strategy-token)")
            self._client = CoreClient(self.cfg.paths.socket, token)
        return self._client

    def _command(self, command_id: str, kind: str, payload: dict) -> dict:
        try:
            return self.client().command(command_id, kind, payload, self._principal())
        except IpcError as exc:
            if "CommandConflict" in str(exc):
                stored = self.core.commands([command_id]).get(command_id)
                if stored is not None and self._own(stored):
                    # We already hold this id with another payload (only possible after the journal was lost and
                    # recovery raced the core): adopt our stored outcome, never resend.
                    return {"accepted": stored["status"] == "ACCEPTED", "duplicate": True, "body": stored["result"],
                            "events": [], "adopted_after_conflict": True}
                if stored is not None:
                    # Another principal holds the id: the core refused ours, and its command is not our outcome.
                    log.error("%s %s: COMMAND_ID_CLAIMED by %s %s; the core refused it", kind, command_id,
                              stored["principal_role"], stored["principal_id"])
                    return {"accepted": False, "duplicate": False, "body": _claimed(command_id, stored), "events": []}
            raise CoreUnreachable("CORE_ERROR", f"{kind} {command_id}: {exc}") from None
        except OSError as exc:
            raise CoreUnreachable("CORE_UNREACHABLE", f"{kind} {command_id}: {type(exc).__name__}: {exc}") from None

    def _load_artifact(self, today: date) -> LoadedArtifact:
        try:
            reg = ResearchRegistry.open_readonly(self.cfg.registry_path)
        except FileNotFoundError:
            raise ArtifactRefused("REGISTRY_MISSING", f"research registry {self.cfg.registry_path} does not exist") from None
        try:
            return artifacts.load(reg, self.cfg.artifact_id, mode=self.cfg.mode, today=today, code_hash=self._code_hash)
        finally:
            reg.close()

    # ------------------------------------------------------------------ cycle
    def run_once(self) -> dict:
        now = self.clock.now_ns()
        today = datetime.fromtimestamp(now // NS_PER_S, tz=timezone.utc).date()
        art = self._load_artifact(today)
        self.client()                         # both modes record through the core: no token, no decision
        spec = art.spec
        hh, mm = (int(x) for x in spec.execution["decision_clock_utc"].split(":"))
        clock_utc = time(hh, mm)
        day = latest_eligible_session(now, self.cfg.calendar, clock_utc)
        t = decision_instant(day, clock_utc)
        close = _session_close_ns(self.cfg.calendar, day)
        if close is not None and t < close:
            raise RunnerError("DECISION_BEFORE_CLOSE", f"decision clock {spec.execution['decision_clock_utc']} UTC precedes "
                                                       f"the {day} close of {self.cfg.calendar.calendar_id}")
        exec_date, deadline = execution_window(day, self.cfg.calendar, clock_utc, spec.execution["execution_delay_sessions"])
        summary: dict[str, Any] = {
            "artifact_id": art.artifact_id, "artifact_hash": art.artifact_hash, "status": art.status, "mode": self.cfg.mode,
            "account_key": self.cfg.account_key, "session_date": day.isoformat(), "decision_ns": t,
            "execution_date": exec_date.isoformat(), "deadline_ns": deadline, "runtime_code_hash": self.runtime_code,
            "calendar": f"{self.cfg.calendar.calendar_id}@{self.cfg.calendar.version}", "instruments": {}, "result": "OK"}

        # Rows an earlier session left unfinished (a core outage or a crash that outlasted it) are finished first:
        # their decisions are recorded, and an intent past its deadline is never sent (``_execute``). Only the
        # latest eligible session is ever DECIDED; these were decided already (review fix: they stayed PLANNED or
        # SUBMITTED forever, unrecorded, once a later session became eligible).
        todo: list[dict] = self.journal.unfinished(before=day)
        if todo:
            log.warning("%s: %d decision(s) of earlier sessions are not recorded yet; finishing them first: %s",
                        art.artifact_id, len(todo), sorted({r["session_date"] for r in todo}))
        fresh: list[str] = []
        for inst in spec.instrument_ids:
            row = self.journal.get(day, inst)
            if row is None:
                fresh.append(inst)
            elif row["state"] in FINAL:
                summary["instruments"][inst] = self._row_summary(row, already=True)
            else:
                todo.append(row)
        recovered: dict[str, dict] = {}
        if fresh:
            ids = {i: decision_ids(art.artifact_id, day, i) for i in fresh}
            stored = self.core.commands([c for i in fresh for c in (ids[i]["decision_command_id"], ids[i]["submit_command_id"])])
            adopted = []
            for i in list(fresh):
                # Only commands this artifact's principal issued are ours. The ids are predictable and the core stores
                # what any principal authorized for the kind sends under them (even when it rejects it), so another
                # principal's command under our id is never adopted (review fix).
                d = stored.get(ids[i]["decision_command_id"])
                s = stored.get(ids[i]["submit_command_id"])
                if d is not None and not self._own(d):
                    # The core will refuse our record under this id, so nothing is traded: no intent without its record.
                    log.error("%s %s %s: COMMAND_ID_CLAIMED: decision command id %s is held by %s %s; not deciding",
                              art.artifact_id, day, i, ids[i]["decision_command_id"], d["principal_role"], d["principal_id"])
                    adopted.append({"artifact_id": art.artifact_id, "session_date": day, "instrument_id": i, "mode": self.cfg.mode,
                                    "state": "REFUSED", **ids[i],
                                    "decision_payload": {"claimed": "decision command id held by another principal"},
                                    "decision_result": {"accepted": False, "duplicate": False, "body": _claimed(
                                        ids[i]["decision_command_id"], d)},
                                    "detail": {"claimed_by": {"id": d["principal_id"], "role": d["principal_role"]}}})
                    fresh.remove(i)
                elif d is not None:
                    adopted.append({"artifact_id": art.artifact_id, "session_date": day, "instrument_id": i, "mode": self.cfg.mode,
                                    "state": "RECORDED" if d["status"] == "ACCEPTED" else "REFUSED", **ids[i],
                                    "decision_payload": d.get("decision") or {"recovered": "decision command found in the core"},
                                    "decision_result": {"accepted": d["status"] == "ACCEPTED", "duplicate": True,
                                                        "body": d["result"]},
                                    "detail": {"recovered": True}})
                    fresh.remove(i)
                elif s is not None and self._own(s):
                    recovered[i] = {"accepted": s["status"] == "ACCEPTED", "duplicate": True, "body": s["result"],
                                    "events": [], "recovered": True}
                elif s is not None:
                    log.error("%s %s %s: submit command id %s is held by %s %s; the intent will be refused (COMMAND_ID_CLAIMED)",
                              art.artifact_id, day, i, ids[i]["submit_command_id"], s["principal_role"], s["principal_id"])
            if adopted:
                self.journal.plan(adopted, now)
                for r in adopted:
                    summary["instruments"][r["instrument_id"]] = self._row_summary(self.journal.get(day, r["instrument_id"]),
                                                                                    already=True)
        if fresh:
            snap = self.core.snapshot(self.cfg.account_key, self.cfg.session_id, spec.instrument_ids)
            summary["session_id"] = snap.session_id
            if snap.session_id is None:
                summary["result"] = "NO_SESSION"
                summary["reason"] = snap.reason or "no session"
                log.warning("%s: no decision recorded for %s: %s", art.artifact_id, day, summary["reason"])
                return self._finish(summary, todo)
            rows = self._decide(art, day, t, deadline, now, snap, fresh, recovered)
            self.journal.plan(rows, now)
            todo += [self.journal.get(day, r["instrument_id"]) for r in rows]
        return self._finish(summary, todo)

    def _finish(self, summary: dict, todo: list[dict]) -> dict:
        for row in sorted(todo, key=lambda r: (r["session_date"], r["instrument_id"])):
            try:
                row = self._execute(row)
            except CoreUnreachable as exc:
                summary["result"] = "CORE_UNREACHABLE"
                summary["reason"] = exc.message
                log.error("core unreachable; %s %s stays %s for the next cycle: %s", row["session_date"], row["instrument_id"],
                          row["state"], exc)
                self._put(summary, self.journal.get(date.fromisoformat(row["session_date"]), row["instrument_id"]))
                break
            self._put(summary, row)
        return summary

    def _put(self, summary: dict, row: dict) -> None:
        """A row of the decided session goes under ``instruments``; a row carried over from an earlier session under
        ``carried_over`` (keyed ``<session date>|<instrument>``)."""
        if row["session_date"] == summary["session_date"]:
            summary["instruments"][row["instrument_id"]] = self._row_summary(row)
        else:
            summary.setdefault("carried_over", {})[f"{row['session_date']}|{row['instrument_id']}"] = self._row_summary(row)

    # ------------------------------------------------------------- decisions
    def _decide(self, art: LoadedArtifact, day: date, t: int, deadline: int, now: int, snap: CoreSnapshot,
                fresh: list[str], recovered: dict[str, dict]) -> list[dict]:
        spec = art.spec
        from ..data.pit import ResearchStore
        try:
            store = ResearchStore.open_readonly(self.cfg.research_path)
        except FileNotFoundError:
            raise RunnerError("RESEARCH_STORE_MISSING", f"point-in-time store {self.cfg.research_path} does not exist") from None
        try:
            series = load_series(store, art.dataset_id, spec.instrument_ids, t)
        finally:
            store.close()
        positions = {i: h.owned for i, h in snap.holdings.items()}
        decision, visible = strategy_decision(spec, series, day, t, positions=positions, equity_fn=snap.equity)
        sizing = Sizing.from_spec(spec)
        close = _session_close_ns(self.cfg.calendar, day)
        decisions: dict[str, _Decision] = {}
        eligible: list[str] = []
        for i in spec.instrument_ids:
            bars = visible.get(i, [])
            last = bars[-1] if bars else None
            h = snap.holdings.get(i, Holding())
            digest = self._digest(art, day, t, i, bars, h, snap)
            d = _Decision(i, "NO_TRADE", "", digest, current_quantity=h.current, registered=snap.instruments.get(i) is not None)
            decisions[i] = d
            if not d.registered:
                d.outcome, d.reason = "BLOCKED", f"INSTRUMENT_NOT_REGISTERED: {i} is not in the core's instrument master"
            elif self.cfg.mode == "live" and snap.account_mode == "LIVE" and art.status != "active":
                d.outcome, d.reason = "BLOCKED", f"STATUS_NOT_PERMITTED_FOR_LIVE_ACCOUNT: artifact is '{art.status}'"
            elif last is None or last.session_date != day:
                d.outcome = "SKIPPED_STALE_DATA"
                d.reason = (f"no completed bar for {day} visible at the decision instant in {art.dataset_id}; latest is "
                            f"{last.session_date.isoformat() if last else 'none'}")
            elif close is not None and last.end_ns < close:
                d.outcome, d.reason = "SKIPPED_STALE_DATA", f"bar for {day} ends before the session close (partial bar)"
            elif any(b.fixture for b in bars) and not art.fixture_allowed:
                d.outcome, d.reason = "BLOCKED", "FIXTURE_DATA_NOT_ALLOWED by the spec's data requirements"
            elif any(b.fixture for b in bars) and snap.account_mode != "SIMULATED":
                d.outcome, d.reason = "BLOCKED", f"FIXTURE_DATA_ON_{snap.account_mode}_ACCOUNT: synthetic bars never drive this account"
            elif decision.orders:
                d.outcome, d.reason = "BLOCKED", "EXPLICIT_ORDERS_NOT_SUPPORTED by the runtime (target weights only)"
            else:
                eligible.append(i)
        # Only instruments the strategy targets are mapped, as in the engine (no target = hold, never an exit). The
        # weights are capped and scaled over EVERY target, as in the engine; only eligible instruments get orders. An
        # instrument skipped here (stale data, not registered, a runner gate) keeps its share of the aggregate cap,
        # so dropping it cannot inflate the others' weights past the backtest's (review fix: cap and cash reserve).
        targets = ({i: decision.targets[i] for i in spec.instrument_ids if i in decision.targets}
                   if decision.targets is not None else {})
        tradable = [i for i in eligible if i in targets]
        notes = decision.notes if isinstance(decision.notes, dict) else {}
        fixture = any(b.fixture for bars in visible.values() for b in bars)
        if tradable:
            marks = {i: (visible[i][-1].close if visible.get(i) else None) for i in spec.instrument_ids}
            plans, plan_notes = plan(targets, sizing=sizing, limit_offset_bps=spec.execution["limit_offset_bps"],
                                     equity=snap.equity(marks), marks=marks, holdings=snap.holdings,
                                     instruments={i: snap.instruments[i] for i in tradable}, budget=snap.available_cash,
                                     fee_envelope=snap.fee_envelope, adv={i: _adv(visible.get(i, []), sizing.adv_window) for i in tradable},
                                     ledger_scale=snap.ledger_scale, tradable=tradable)
        else:
            plans, plan_notes = {}, {}
        for i in eligible:
            d = decisions[i]
            text, d.score = _strategy_note(notes.get(i))
            if decision.targets is None or i not in plans:
                d.reason = _join(text, f"no target from the strategy: hold ({notes.get('state', 'no targets')})",
                                 "FIXTURE data" if fixture else "")
                continue
            p: InstrumentPlan = plans[i]
            d.target_quantity = p.desired_quantity
            d.detail = {"target_weight": p.target_weight.to_str() if p.target_weight is not None else None,
                        "current_weight": p.current_weight.to_str() if p.current_weight is not None else None,
                        "equity": plan_notes.get("equity"), "budget": plan_notes.get("budget")}
            if p.order is None:
                d.reason = _join(text, p.note, "FIXTURE data" if fixture else "")
                continue
            o = p.order
            what = f"{o.side} {o.quantity} @ {o.limit_price} ({o.reason}) for the session after {day.isoformat()}"
            if self.cfg.mode == "shadow":
                d.reason = _join(f"SHADOW_WOULD_SUBMIT {what}", text, p.note, "FIXTURE data" if fixture else "")
            elif now >= deadline:
                d.outcome = "BLOCKED"
                d.reason = _join(f"RUNNER_LATE: intent deadline passed before submission; would {what}", text)
            else:
                d.outcome, d.order = "INTENT", o
                d.reason = _join(text, p.note, "FIXTURE data" if fixture else "")
        rows = []
        for i in fresh:
            rows.append(self._row(art, day, t, deadline, snap, decisions[i], recovered.get(i)))
        return rows

    def _digest(self, art: LoadedArtifact, day: date, t: int, inst: str, bars: list[Bar], h: Holding, snap: CoreSnapshot) -> str:
        return stable_hash({"artifact_id": art.artifact_id, "artifact_hash": art.artifact_hash, "mode": self.cfg.mode,
                            "session_date": day, "decision_ns": t, "instrument_id": inst, "dataset_id": art.dataset_id,
                            "bars": [b.to_json() for b in bars],
                            "exposure": {"owned": h.owned, "unreserved": h.unreserved, "pending_buy": h.pending_buy,
                                         "pending_sell": h.pending_sell},
                            "session_id": snap.session_id, "policy_hash": snap.policy_hash,
                            "liquid_value": snap.liquid_value, "available_cash": snap.available_cash,
                            "runtime_code_hash": self.runtime_code,
                            "calendar": f"{self.cfg.calendar.calendar_id}@{self.cfg.calendar.version}"})

    def _row(self, art: LoadedArtifact, day: date, t: int, deadline: int, snap: CoreSnapshot, d: _Decision,
             recovered: dict | None) -> dict:
        ids = decision_ids(art.artifact_id, day, d.instrument_id)
        base = {"account_key": self.cfg.account_key, "session_id": snap.session_id, "artifact_id": art.artifact_id,
                "mode": self.cfg.mode, "outcome": None if d.outcome == "INTENT" else d.outcome, "inputs_digest": d.digest,
                "observation_time_ns": t, "instrument_id": d.instrument_id if d.registered else None, "score": d.score,
                "target_quantity": d.target_quantity.to_str() if d.target_quantity is not None else None,
                "current_quantity": d.current_quantity.to_str() if d.current_quantity is not None else None,
                "reason": d.reason, "order_ids": []}
        submit = None
        if d.outcome == "INTENT":
            o = d.order
            submit = {"account_key": self.cfg.account_key, "session_id": snap.session_id, "order_id": ids["order_id"],
                      "instrument_id": d.instrument_id, "side": o.side, "quantity": o.quantity.to_str(),
                      "limit_price": o.limit_price.to_str(), "order_type": "LIMIT", "time_in_force": "DAY",
                      "deadline_ns": deadline, "policy_hash": snap.policy_hash, "strategy_id": art.artifact_id,
                      "purpose": "ENTRY" if o.side == "BUY" else "EXIT",
                      "decision_ref": {"artifact_id": art.artifact_id, "session_date": day.isoformat(),
                                       "inputs_digest": d.digest, "decision_command_id": ids["decision_command_id"]}}
        row = {"artifact_id": art.artifact_id, "session_date": day, "instrument_id": d.instrument_id, "mode": self.cfg.mode,
               "state": "PLANNED", **ids, "submit_payload": submit, "decision_payload": base, "detail": d.detail}
        if recovered is not None:
            # Journal lost after the core took this intent: never resubmit; record what the core holds.
            row["submit_payload"] = None
            row["state"] = "SUBMITTED"
            row["submit_result"] = recovered
            row["decision_payload"] = self._finalize({**base, "reason": _join("RECOVERED after journal loss", base["reason"])},
                                                     recovered, ids["order_id"])
            row["detail"] = {**d.detail, "recovered": True}
        return row

    @staticmethod
    def _finalize(base: dict, submit_result: dict | None, order_id: str) -> dict:
        """The decision record for an intent once the core has answered the submission (deterministic, so a
        retried record carries the same payload as the first attempt)."""
        p = dict(base)
        if submit_result is None:
            return p
        if submit_result["accepted"]:
            p["outcome"] = "INTENT_SUBMITTED"
            p["order_ids"] = [order_id]
        else:
            body = submit_result.get("body") or {}
            code = body.get("code", "REJECTED")
            checks = ((body.get("details") or {}).get("decision") or {}).get("checks") or []
            failed = ", ".join(f"{c.get('check')} ({c.get('detail')})" for c in checks if not c.get("passed"))
            p["outcome"] = "BLOCKED"
            p["reason"] = _join(f"core rejected submit_intent: {code} {body.get('message', '')}".strip(),
                                f"failed checks: {failed}" if failed else "", base.get("reason", ""))
        return p

    def _execute(self, row: dict) -> dict:
        day = date.fromisoformat(row["session_date"])
        inst = row["instrument_id"]
        if row["state"] == "PLANNED":
            sub = row["submit_payload"]
            if sub is not None and self.clock.now_ns() >= sub["deadline_ns"]:
                self._expire(row)
            elif sub is not None:
                res = self._command(row["submit_command_id"], "submit_intent", row["submit_payload"])
                decision = self._finalize(row["decision_payload"], res, row["order_id"])
                self.journal.update(day, inst, self.clock.now_ns(), state="SUBMITTED", submit_result=res, decision_payload=decision)
                log.info("%s %s %s: submit_intent %s%s", row["artifact_id"], day, inst,
                         "accepted" if res["accepted"] else "REJECTED " + str((res.get("body") or {}).get("code")),
                         " (duplicate)" if res.get("duplicate") else "")
            else:
                self.journal.update(day, inst, self.clock.now_ns(), state="SUBMITTED")
            row = self.journal.get(day, inst)
        if row["state"] == "SUBMITTED":
            res = self._command(row["decision_command_id"], "record_strategy_decision", row["decision_payload"])
            state = "RECORDED" if res["accepted"] else "REFUSED"
            self.journal.update(day, inst, self.clock.now_ns(), state=state, decision_result=res)
            if not res["accepted"]:
                log.error("%s %s %s: the core refused the decision record: %s", row["artifact_id"], day, inst,
                          (res.get("body") or {}).get("code"))
            row = self.journal.get(day, inst)
        return row

    def _own(self, stored: dict) -> bool:
        """A command the core stored under one of this runner's ids was issued by this artifact's principal."""
        return (stored.get("principal_id"), stored.get("principal_role")) == (self.cfg.artifact_id, "STRATEGY")

    def _expire(self, row: dict) -> None:
        """A PLANNED intent whose deadline has passed is never sent (the core would refuse it as DEADLINE anyway).
        If an earlier process did send it (a crash before the journal learned), the core's stored result is the
        truth and is recorded; otherwise the decision is recorded BLOCKED (RUNNER_LATE)."""
        day, inst, sub = date.fromisoformat(row["session_date"]), row["instrument_id"], row["submit_payload"]
        stored = self.core.commands([row["submit_command_id"]]).get(row["submit_command_id"])
        if stored is not None and self._own(stored):
            res = {"accepted": stored["status"] == "ACCEPTED", "duplicate": True, "body": stored["result"], "events": [],
                   "recovered": True}
            decision = self._finalize(row["decision_payload"], res, row["order_id"])
            self.journal.update(day, inst, self.clock.now_ns(), state="SUBMITTED", submit_result=res, decision_payload=decision)
            log.info("%s %s %s: intent past its deadline was already taken by the core (%s); recording that, not resending",
                     row["artifact_id"], day, inst, stored["status"])
            return
        base = row["decision_payload"]
        decision = {**base, "outcome": "BLOCKED", "order_ids": [],
                    "reason": _join(f"RUNNER_LATE: not submitted; the intent deadline passed before the core took it; would "
                                    f"{sub['side']} {sub['quantity']} @ {sub['limit_price']}", base.get("reason", ""))}
        self.journal.update(day, inst, self.clock.now_ns(), state="SUBMITTED", decision_payload=decision)
        log.warning("%s %s %s: intent past its deadline, not submitted; recorded BLOCKED (RUNNER_LATE)", row["artifact_id"],
                    day, inst)

    @staticmethod
    def _row_summary(row: dict, already: bool = False) -> dict:
        p = row["decision_payload"] or {}
        sub = row.get("submit_result") or None
        dec = row.get("decision_result") or None
        out = {"state": row["state"], "outcome": p.get("outcome"), "reason": p.get("reason"), "already_decided": already,
               "submitted": sub is not None, "order_id": row["order_id"] if sub and sub.get("accepted") else None,
               "submit_duplicate": bool(sub and sub.get("duplicate")),
               "record_duplicate": bool(dec and dec.get("duplicate")),
               "core_refusal": (dec.get("body") or {}).get("code") if dec and not dec.get("accepted") else None,
               "submit_rejection": (sub.get("body") or {}).get("code") if sub and not sub.get("accepted") else None,
               "recovered": bool((row.get("detail") or {}).get("recovered"))}
        return out

    # ------------------------------------------------------------------- loop
    def run_loop(self, stop: threading.Event, *, grace_s: int = DEFAULT_GRACE_S, poll_s: float = DEFAULT_POLL_S) -> None:
        """Decide each eligible session once its decision instant plus ``grace_s`` has passed; poll every ``poll_s``."""
        while not stop.is_set():
            try:
                now = self.clock.now_ns()
                today = datetime.fromtimestamp(now // NS_PER_S, tz=timezone.utc).date()
                art = self._load_artifact(today)
                hh, mm = (int(x) for x in art.spec.execution["decision_clock_utc"].split(":"))
                day = latest_eligible_session(now, self.cfg.calendar, time(hh, mm))
                if now >= decision_instant(day, time(hh, mm)) + grace_s * NS_PER_S:
                    summary = self.run_once()
                    self.cycles += 1
                    self.last_summary = summary
                    new = summary["result"] != "OK" or not all(s["already_decided"] for s in summary["instruments"].values())
                    log.log(logging.INFO if new else logging.DEBUG, "cycle %s %s: %s %s", summary["artifact_id"],
                            summary["session_date"], summary["result"],
                            {i: s["outcome"] for i, s in summary["instruments"].items()})
            except ArtifactRefused as exc:
                log.warning("artifact %s refused: %s", self.cfg.artifact_id, exc)
            except RunnerError as exc:
                log.error("cycle failed: %s", exc)
            except Exception:
                log.exception("cycle failed")
            stop.wait(poll_s)
