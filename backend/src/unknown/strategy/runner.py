"""Strategy runner process (ADR-0017 #4 and #5; book ch. 15 algorithm A15).

A runner runs ONE registered artifact for ONE account, in ``shadow`` or ``live`` mode. It is a research-side
process like the agent worker: it holds no broker credentials, reads the research registry and the
point-in-time store read-only, reads the core database read-only, and writes to the core only through the
command socket with the owner-issued token bound to its artifact (``secrets/strategy/<artifact>.token``), as
principal ``{id: <artifact id>, role: STRATEGY}``. The core's pretrade, reservation, stop, session and mandate
rules apply unchanged; the runner never talks to the broker.

One cycle (``run_once``):

1. Load the artifact and refuse unless the registry status permits the mode, the bundle is intact, the spec is
   schema v2, the artifact has not expired (its expiry bounded by the retention of its evidence data, BR-DAT-16)
   and this checkout's code hash is the registered one (``artifacts.load``). A refused cycle, or one that fails
   before deciding, decides nothing but still cancels the remainder of intents it sent whose window has ended
   (``_cancel_without_deciding``).
2. At the first cycle of a process, re-warm the strategy from the point-in-time store as of the journal's latest
   decision and compare the feature rows and targets with the journal's snapshot; a mismatch blocks new entries
   for the life of the process, including buys planned before the restart and not yet sent (``_rewarm_check``,
   BR-DAT-18).
3. Find the latest eligible session ``D``: the latest business day of the calendar whose decision instant
   (``D`` at the spec's ``decision_clock_utc``, as in the backtest) has passed. Only ``D`` is ever decided; missed
   sessions are not back-filled. Decisions an earlier cycle left unrecorded (outage, crash) are finished first,
   and an intent past its deadline is never sent (recorded ``BLOCKED`` RUNNER_LATE unless the core holds it).
4. For each instrument of the spec that has no final decision for ``D`` yet (journal, then the core's stored
   commands if the journal was lost): read the bars visible at the decision instant from the point-in-time
   store (``research.bars.load_series`` + ``Cursor``, the engine's as-of reader). An instrument whose latest
   visible bar is not the completed bar of ``D`` is ``SKIPPED_STALE_DATA``; one whose feature rows (validity
   window, enumerated missingness: ``research.features``) are not usable is ``BLOCKED`` FEATURE_INVALID.
5. Compute the strategy's targets with the research code itself (``research.strategies.build(spec).decide`` on a
   ``DecisionContext``, the backtest's code path), read the session's current and pending exposure from the core
   (BR-DAT-02: the leaves of every open order; no intent beside an UNKNOWN or CANCEL_PENDING order, at most one
   live intent per session, artifact and instrument), and map targets to orders with the A15 mapping the backtest
   uses (``research.sizing.plan_orders``: no-trade band, minimum size, the risk term with entry and exit fees
   (BR-DAT-04), cash).
6. Journal every decision (PLANNED) with deterministic ids. In live mode each intent waits for its submission
   window, ``[open + N, close - M]`` of the next regular session (``research.execution``, BR-DAT-19); in the window
   the BR-DAT-02 gates are re-checked against the core's open orders (``pending_gate``; a closed gate keeps it
   PLANNED), its limit is set from a fresh validated quote (min(model bound, collar) for a buy), journaled, and sent
   (``submit_intent``, DAY LIMIT, ``deadline_ns`` = the window's end, ``purpose`` ENTRY/EXIT, ``policy_hash`` of
   the session mandate); at the window's end its remainder is cancelled. Every decision is recorded with
   ``record_strategy_decision``: ``INTENT_SUBMITTED``, ``BLOCKED`` (the core's rejection code, or a runner gate),
   ``NO_TRADE`` or ``SKIPPED_STALE_DATA``. Shadow mode records the same decisions with ``mode: shadow`` and never
   submits; a decision that would have traded is ``NO_TRADE`` with the reason ``SHADOW_WOULD_SUBMIT ...`` (the
   core's outcome vocabulary has no separate shadow outcome).

``run_loop`` repeats ``run_once`` on a schedule: once the decision instant of the latest eligible session plus a
grace period has passed, every ``poll_s`` seconds, re-checking the artifact's status each time (a quarantined or
retired artifact stops making decisions at the next cycle)."""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.state import TERMINAL_ORDER_STATES
from ..domain.calendar import WEEKDAYS_ONLY, BusinessCalendar, SessionTimeUnknown
from ..domain.clock import NS_PER_S, Clock, SystemClock
from ..domain.fixed import Fixed, Rounding
from ..research.bars import Bar, BarSeries, Cursor, DatasetView, load_series
from ..research.common import float_to_fixed, stable_hash
from ..research.engine import DecisionContext, Sizing, StrategyDecision
from ..research.execution import (FIXTURE_SESSION_BASIS, SessionHoursUnknown, SubmissionWindow, WindowTimes,
                                  transmission_limit, window_times)
from ..research.features import FeatureRow, feature_rows
from ..research.registry import ResearchRegistry
from ..research.spec import StrategySpec
from ..research.strategies import build
from ..service.ipc import CoreClient, IpcError
from ..service.paths import Paths
from . import artifacts
from .artifacts import ArtifactRefused, LoadedArtifact
from .coreview import UNRESOLVED_STATES, CoreSnapshot, CoreUnavailable, CoreView
from .journal import FINAL, Journal, cancel_command_id, decision_ids
from .sizing import Holding, InstrumentPlan, RiskTerm, plan

log = logging.getLogger("unknown.strategy")

MAX_REASON = 1000
DEFAULT_GRACE_S = 600
DEFAULT_POLL_S = 60
_HEX64 = re.compile(r"[0-9a-f]{64}")
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
    """(execution session, latest possible deadline): the end of the execution session when the calendar knows its
    hours, and never later than that session's own decision instant, so an intent cannot outlive the next decision.
    The intent's deadline is tighter: the end of its submission window (``submission_window``)."""
    exec_date = calendar.add_business_days(session_date, delay)
    deadline = decision_instant(exec_date, clock_utc)
    close = _session_close_ns(calendar, exec_date)
    if close is not None and close < deadline:
        deadline = close
    return exec_date, deadline


def submission_window(spec: StrategySpec, session_date: date, calendar: BusinessCalendar, *,
                      fixture: bool) -> tuple[date, WindowTimes]:
    """(execution session, its submission window) for the decisions of ``session_date`` (BR-DAT-19): the spec's
    [open + N, close - M] of the next regular session, placed by ``research.execution.window_times``, the function
    the backtest uses. ``SessionHoursUnknown`` when the calendar has no hours and the data is not a FIXTURE."""
    exec_date = calendar.add_business_days(session_date, spec.execution["execution_delay_sessions"])
    window = SubmissionWindow.from_spec(spec)
    if window is None:
        raise ArtifactRefused("SPEC_SCHEMA_V1", "the spec declares no submission window")
    return exec_date, window_times(window, calendar, exec_date, fixture=fixture)


def quote_reference(quote: dict | None, *, max_age_ms: int | None, account_mode: str) -> tuple[Fixed | None, str]:
    """(mid, "") for a fresh, validated quote from the core's ``quotes`` query, else (None, why). The checks are the
    core's own (``risk.pretrade``): present, live (not delayed or frozen), two-sided and not crossed, younger than
    the policy's ``quote_max_age_ms``, and consolidated on a LIVE account."""
    if quote is None:
        return None, "NO_QUOTE"
    if quote.get("delayed") or quote.get("market_data_type") not in ("live", "unreported"):
        return None, f"QUOTE_NOT_LIVE ({quote.get('market_data_type')}, delayed={quote.get('delayed')})"
    if account_mode == "LIVE" and quote.get("consolidation") != "consolidated":
        return None, f"QUOTE_NOT_CONSOLIDATED ({quote.get('consolidation')})"
    if max_age_ms is None:
        return None, "NO_ACTIVE_POLICY (no quote age limit)"
    if quote.get("age_ms") is None or quote["age_ms"] > max_age_ms:
        return None, f"QUOTE_STALE (age {quote.get('age_ms')} ms > {max_age_ms} ms)"
    if quote.get("bid") is None or quote.get("ask") is None:
        return None, "QUOTE_NOT_TWO_SIDED"
    bid, ask = Fixed.parse(quote["bid"]), Fixed.parse(quote["ask"])
    if not bid.is_positive() or bid > ask:
        return None, f"QUOTE_CROSSED_OR_EMPTY (bid {bid} ask {ask})"
    # the core's mid (``market.quotes.Quote.mid``): half the sum at one more decimal, HALF_EVEN
    return (bid + ask).div(2, max(bid.scale, ask.scale) + 1, Rounding.HALF_EVEN), ""


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
    decision, visible, _ = _decide_with(spec, series, session_date, decision_ns, positions=positions, equity_fn=equity_fn)
    return decision, visible


def _decide_with(spec: StrategySpec, series: dict[str, BarSeries], session_date: date, decision_ns: int, *,
                 positions: dict[str, Fixed] | None = None, equity_fn=None):
    """(decision, visible bars, the strategy after deciding). The strategy is built fresh and fed every bar visible at
    the decision instant from the point-in-time store: its feature state is re-warmed deterministically from the
    store on every decision, so a restarted process computes what an uninterrupted one would (BR-DAT-18)."""
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
    return decision, {i: list(c.history) for i, c in engine.cursors.items()}, strategy


def features_valid_until(row: dict) -> int | None:
    """The earliest end of validity of the feature values a journaled decision used (None: no feature rows)."""
    ends = [f["valid_until_ns"] for f in ((row.get("detail") or {}).get("features") or [])
            if f.get("value") is not None and f.get("valid_until_ns") is not None]
    return min(ends) if ends else None


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


def pending_gate(open_orders, artifact_id: str, *, exclude_order_id: str | None = None) -> str:
    """BR-DAT-02, one rule for the decision and the transmission: "" when an intent may be emitted for the instrument,
    else the blocking reason. No intent beside an order whose outcome (UNKNOWN) or cancellation (CANCEL_PENDING) is
    unresolved, whoever placed it (it may still fill), and at most one live intent per session, artifact and
    instrument. ``exclude_order_id``: the intent being transmitted is not its own obstacle."""
    others = [o for o in open_orders if o["order_id"] != exclude_order_id]
    unresolved = [o for o in others if o["state"] in UNRESOLVED_STATES]
    if unresolved:
        return "PENDING_UNRESOLVED: " + ", ".join(f"{o['order_id']} is {o['state']} with leaves {o['leaves_qty']}"
                                                  for o in unresolved) + \
            "; no new intent for this instrument until it resolves (BR-DAT-02)"
    own = [o for o in others if (o["principal_id"], o["principal_role"]) == (artifact_id, "STRATEGY")]
    if own:
        return "OPEN_INTENT: " + ", ".join(f"{o['order_id']} ({o['state']}, leaves {o['leaves_qty']})" for o in own) + \
            " still works: at most one live intent per session, artifact and instrument (BR-DAT-02)"
    return ""


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
    strategy_note: str = ""                     # the strategy's own note (score formula, warm-up), without the plan
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
        # BR-DAT-18: the restart consistency check, made once per process before its first decision
        # (``_rewarm_check``); a MISMATCH blocks new entries for the life of the process.
        self.rewarm: dict | None = None

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
                stored = self._core_commands([command_id]).get(command_id)
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

    def _core_commands(self, command_ids: list[str]) -> dict[str, dict]:
        """``CoreView.commands``; a missing or unreadable core database is ``CoreUnreachable`` (exit 3 for --once)."""
        try:
            return self.core.commands(command_ids)
        except (CoreUnavailable, sqlite3.Error) as exc:
            raise CoreUnreachable("CORE_DB_UNAVAILABLE", str(exc)) from None

    def _core_order(self, order_id: str) -> dict | None:
        try:
            return self.core.order(order_id)
        except (CoreUnavailable, sqlite3.Error) as exc:
            raise CoreUnreachable("CORE_DB_UNAVAILABLE", str(exc)) from None

    def _core_snapshot(self, account_key: str, session_id: str | None, instrument_ids) -> CoreSnapshot:
        try:
            return self.core.snapshot(account_key, session_id, instrument_ids)
        except (CoreUnavailable, sqlite3.Error) as exc:
            raise CoreUnreachable("CORE_DB_UNAVAILABLE", str(exc)) from None

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
        try:
            return self._cycle()
        except CoreUnreachable:
            raise                             # nothing can be cancelled either; the next cycle retries everything
        except Exception as exc:
            # A cycle that cannot decide (artifact refused, calendar or session hours unknown, store missing, ...)
            # still ends the intents it already sent at their window's end (BR-DAT-19).
            self._cancel_without_deciding(exc)
            raise

    def _cycle(self) -> dict:
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
        try:
            exec_date, window = submission_window(spec, day, self.cfg.calendar, fixture=art.fixture_allowed)
        except SessionHoursUnknown as exc:
            raise RunnerError("SESSION_HOURS_UNKNOWN", str(exc)) from None
        # BR-DAT-19: an intent lives until the end of its submission window (close - M), never past the next decision.
        deadline = min(window.end_ns, decision_instant(exec_date, clock_utc))
        summary: dict[str, Any] = {
            "artifact_id": art.artifact_id, "artifact_hash": art.artifact_hash, "status": art.status, "mode": self.cfg.mode,
            "account_key": self.cfg.account_key, "session_date": day.isoformat(), "decision_ns": t,
            "execution_date": exec_date.isoformat(), "deadline_ns": deadline, "window": window.to_json(),
            "runtime_code_hash": self.runtime_code, "evidence_status": art.evidence_status,
            "calendar": f"{self.cfg.calendar.calendar_id}@{self.cfg.calendar.version}", "instruments": {}, "result": "OK"}
        if art.evidence_status != "CURRENT":
            log.warning("%s is %s (its evidence data was deleted) yet loaded, expiry %s: a deletion ends the expiry",
                        art.artifact_id, art.evidence_status, art.expires_on)
        if self.rewarm is None:
            self.rewarm = self._rewarm_check(art, clock_utc)
            if self.rewarm["status"] == "MISMATCH":
                log.error("%s: feature state re-warmed from the point-in-time store differs from the journal's snapshot of "
                          "%s; new entries are blocked: %s", art.artifact_id, self.rewarm["session_date"],
                          self.rewarm["mismatches"][:5])
        summary["rewarm"] = self.rewarm

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
            stored = self._core_commands([c for i in fresh for c in (ids[i]["decision_command_id"], ids[i]["submit_command_id"])])
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
                    o = self._core_order(ids[i]["order_id"]) if s["status"] == "ACCEPTED" else None
                    if o is not None and o.get("principal_id") == art.artifact_id:
                        recovered[i]["intent"] = {"side": o.get("side"), "quantity": o.get("quantity"),
                                                  "limit_price": o.get("limit_price"),
                                                  "inputs_digest": (o.get("decision_ref") or {}).get("inputs_digest")}
                elif s is not None:
                    log.error("%s %s %s: submit command id %s is held by %s %s; the intent will be refused (COMMAND_ID_CLAIMED)",
                              art.artifact_id, day, i, ids[i]["submit_command_id"], s["principal_role"], s["principal_id"])
            if adopted:
                self.journal.plan(adopted, now)
                for r in adopted:
                    summary["instruments"][r["instrument_id"]] = self._row_summary(self.journal.get(day, r["instrument_id"]),
                                                                                    already=True)
        if fresh:
            snap = self._core_snapshot(self.cfg.account_key, self.cfg.session_id, spec.instrument_ids)
            summary["session_id"] = snap.session_id
            if snap.session_id is None:
                summary["result"] = "NO_SESSION"
                summary["reason"] = snap.reason or "no session"
                log.warning("%s: no decision recorded for %s: %s", art.artifact_id, day, summary["reason"])
                return self._finish(summary, todo)
            rows = self._decide(art, day, t, deadline, now, snap, fresh, recovered, window=window)
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
                return summary
            self._put(summary, row)
        try:
            cancels = self._cancel_at_window_end()
        except CoreUnreachable as exc:
            summary["result"] = "CORE_UNREACHABLE"
            summary["reason"] = exc.message
            log.error("core unreachable; window-end cancellations wait for the next cycle: %s", exc)
            return summary
        if cancels:
            summary["window_cancels"] = cancels
        return summary

    # ------------------------------------------------------- window and transmission (BR-DAT-19)
    def _cancel_at_window_end(self) -> dict:
        """Cancel the unfilled remainder of every accepted intent whose submission window has ended (the spec's
        CANCEL_AT_WINDOW_END: a DAY order must not keep working into the closing auction, nor be carried over).
        One deterministic ``request_cancel`` id per decision; an order already terminal is noted, not cancelled."""
        now = self.clock.now_ns()
        out = {}
        for row in self.journal.open_intents():
            window = row["detail"]["window"]
            if now < window["end_ns"]:
                continue
            day, inst = date.fromisoformat(row["session_date"]), row["instrument_id"]
            o = self._core_order(row["order_id"])
            if o is None or o.get("principal_id") != row["artifact_id"]:
                note = {"skipped": "the core holds no order of this artifact under this id"}
            elif o["state"] in TERMINAL_ORDER_STATES:
                note = {"skipped": f"order already {o['state']} at the window's end", "state": o["state"]}
            else:
                cid = cancel_command_id(row["artifact_id"], day, inst)
                res = self._command(cid, "request_cancel", {"order_id": row["order_id"],
                                                            "reason": "submission window ended (BR-DAT-19)"})
                note = {"command_id": cid, "accepted": res["accepted"], "duplicate": bool(res.get("duplicate")),
                        "body": res.get("body"), "state_before": o["state"]}
                log.info("%s %s %s: window ended at %s; cancel %s %s", row["artifact_id"], day, inst, window["end_ns"],
                         row["order_id"], "requested" if res["accepted"] else f"REFUSED {(res.get('body') or {}).get('code')}")
            note["at_ns"] = now
            self.journal.update(day, inst, now, detail={**row["detail"], "window_cancel": note})
            out[f"{row['session_date']}|{inst}"] = note
        return out

    def _cancel_without_deciding(self, why: BaseException) -> None:
        """The window-end cancellations (BR-DAT-19) of a cycle that makes no decision: the artifact is refused
        (quarantined, retired, expired, code drift, ...) or the cycle fails before it decides (calendar horizon,
        session hours, point-in-time store). An intent sent earlier keeps working until it is cancelled, and cancelling
        at the window's end only tightens (quarantine never loosens), so it is done anyway. Best effort: without a
        token or a reachable core it is retried at the next cycle."""
        try:
            cancels = self._cancel_at_window_end()
        except (ArtifactRefused, CoreUnreachable) as exc:
            log.error("%s: no decision this cycle (%s), and its window-end cancellations are not possible now: %s",
                      self.cfg.artifact_id, why, exc)
            return
        if cancels:
            log.warning("%s: no decision this cycle (%s); window-end cancellation of intents it sent: %s",
                        self.cfg.artifact_id, why, {k: v.get("accepted", v.get("skipped")) for k, v in cancels.items()})

    def _price(self, row: dict) -> dict | None:
        """Set the intent's limit at transmission (BR-DAT-19): min(model bound, collar above a fresh validated quote's
        mid) for a buy, max(model bound, collar below it) for a sell, by ``research.execution.transmission_limit``.
        The priced payload is journaled before it is sent (write-ahead), so a resend after a crash is identical.
        Returns the updated row, or None (still PLANNED, retried next cycle until the deadline) when the instrument's
        pending orders block it (``pending_gate``) or no valid quote is available yet."""
        detail = row["detail"] or {}
        if detail.get("transmission") is not None or detail.get("window") is None:
            return row                                         # priced already, or a row planned before windows existed
        day, inst, sub = date.fromisoformat(row["session_date"]), row["instrument_id"], row["submit_payload"]
        # BR-DAT-02 at transmission: what was pending at the decision may have changed overnight (an order gone
        # UNKNOWN or CANCEL_PENDING, a working intent of ours). The intent waits, as for a missing quote.
        try:
            working = self.core.open_orders(self.cfg.account_key, sub["session_id"], inst)
        except (CoreUnavailable, sqlite3.Error) as exc:
            raise CoreUnreachable("CORE_DB_UNAVAILABLE", str(exc)) from None
        gate = pending_gate(working, row["artifact_id"], exclude_order_id=row["order_id"])
        if gate:
            now = self.clock.now_ns()
            self.journal.update(day, inst, now, detail={**detail, "pricing_refusal": {"at_ns": now, "why": gate}})
            log.warning("%s %s %s: not transmitted yet: %s", row["artifact_id"], day, inst, gate)
            return None
        try:
            quotes = self.client().query("quotes")
        except (IpcError, OSError) as exc:
            raise CoreUnreachable("CORE_UNREACHABLE", f"quotes for {inst}: {type(exc).__name__}: {exc}") from None
        try:
            instrument, max_age, mode = self.core.transmission_context(self.cfg.account_key, inst)
        except (CoreUnavailable, sqlite3.Error) as exc:
            raise CoreUnreachable("CORE_DB_UNAVAILABLE", str(exc)) from None
        now = self.clock.now_ns()
        mid, why = quote_reference(quotes.get(inst), max_age_ms=max_age, account_mode=mode)
        if mid is None or instrument is None:
            why = why or "INSTRUMENT_NOT_REGISTERED"
            self.journal.update(day, inst, now, detail={**detail, "pricing_refusal": {"at_ns": now, "why": why}})
            log.warning("%s %s %s: not transmitted yet: %s", row["artifact_id"], day, inst, why)
            return None
        bound = Fixed.parse(detail["model_bound"])
        collar = int(detail["collar_bps"])
        limit = transmission_limit(sub["side"], bound, mid, collar, instrument)
        q = quotes[inst]
        priced = {**sub, "limit_price": limit.to_str()}
        record = {"at_ns": now, "model_bound": bound.to_str(), "reference_mid": mid.to_str(), "bid": q.get("bid"),
                  "ask": q.get("ask"), "quote_age_ms": q.get("age_ms"), "quote_source": q.get("source"),
                  "collar_bps": collar, "limit": limit.to_str(), "rule": "execution.window.br-dat-19@1"}
        relation = "min" if sub["side"] == "BUY" else "max"
        sign = "+" if sub["side"] == "BUY" else "-"
        decision = {**row["decision_payload"],
                    "reason": _join(f"TRANSMITTED in the window: limit {limit} = {relation}(model bound {bound}, mid {mid} "
                                    f"{sign} {collar} bps)", row["decision_payload"].get("reason", ""))}
        self.journal.update(day, inst, now, submit_payload=priced, decision_payload=decision,
                            detail={**detail, "transmission": record})
        return self.journal.get(day, inst)

    def _put(self, summary: dict, row: dict) -> None:
        """A row of the decided session goes under ``instruments``; a row carried over from an earlier session under
        ``carried_over`` (keyed ``<session date>|<instrument>``)."""
        if row["session_date"] == summary["session_date"]:
            summary["instruments"][row["instrument_id"]] = self._row_summary(row)
        else:
            summary.setdefault("carried_over", {})[f"{row['session_date']}|{row['instrument_id']}"] = self._row_summary(row)

    # ------------------------------------------------------------- decisions
    def _decide(self, art: LoadedArtifact, day: date, t: int, deadline: int, now: int, snap: CoreSnapshot,
                fresh: list[str], recovered: dict[str, dict], *, window: WindowTimes | None = None) -> list[dict]:
        spec = art.spec
        if window is None:
            try:
                _, window = submission_window(spec, day, self.cfg.calendar, fixture=art.fixture_allowed)
            except SessionHoursUnknown as exc:
                raise RunnerError("SESSION_HOURS_UNKNOWN", str(exc)) from None
        deadline = min(deadline, window.end_ns)
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
        decision, visible, strategy = _decide_with(spec, series, day, t, positions=positions, equity_fn=snap.equity)
        sizing = Sizing.from_spec(spec)
        close = _session_close_ns(self.cfg.calendar, day)
        # BR-DAT-18: the feature rows of the approved contract (validity window, warm-up, enumerated missingness)
        frows: dict[str, list[FeatureRow]] = {
            i: feature_rows(spec, strategy, i, visible.get(i, []), decision_session=day, session_close_ns=close,
                            calendar=self.cfg.calendar) for i in spec.instrument_ids}
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
                d.reason = (f"{'NOT_REPORTED' if last is None else 'PROVIDER_DELAY'}: no completed bar for {day} visible "
                            f"at the decision instant in {art.dataset_id}; latest is "
                            f"{last.session_date.isoformat() if last else 'none'}")
            elif close is not None and last.end_ns < close:
                d.outcome, d.reason = "SKIPPED_STALE_DATA", f"NOT_REPORTED: bar for {day} ends before the session close (partial bar)"
            elif any(b.fixture for b in bars) and not art.fixture_allowed:
                d.outcome, d.reason = "BLOCKED", "FIXTURE_DATA_NOT_ALLOWED by the spec's data requirements"
            elif any(b.fixture for b in bars) and snap.account_mode != "SIMULATED":
                d.outcome, d.reason = "BLOCKED", f"FIXTURE_DATA_ON_{snap.account_mode}_ACCOUNT: synthetic bars never drive this account"
            elif gate := pending_gate(snap.open_orders.get(i, ()), art.artifact_id):
                # BR-DAT-02: an order whose outcome (UNKNOWN) or cancellation (CANCEL_PENDING) is unresolved may still
                # fill; its leaves count as pending exposure, and no new intent is emitted beside it. Re-checked at
                # transmission (``_price``): the intent is sent hours after this decision.
                d.outcome, d.reason = "BLOCKED", gate
            elif window.basis == FIXTURE_SESSION_BASIS and not all(b.fixture for b in bars):
                d.outcome, d.reason = "BLOCKED", (f"SESSION_HOURS_UNKNOWN: {self.cfg.calendar.calendar_id} has no session "
                                                  "hours, and only FIXTURE data may use the fixture session convention "
                                                  "to place the submission window; run with the venue calendar")
            elif invalid := [r for r in frows[i] if r.missing_reason is not None]:
                # BR-DAT-18: an input vector with a missing feature (enumerated reason) is rejected, not imputed
                d.outcome, d.reason = "BLOCKED", "FEATURE_INVALID: " + ", ".join(
                    f"{r.feature_id} v{r.version} {r.missing_reason}" for r in invalid)
            elif outside := [r for r in frows[i] if r.value is not None and not r.usable_at(t)]:
                d.outcome, d.reason = "BLOCKED", "FEATURE_OUTSIDE_VALIDITY_WINDOW: " + ", ".join(
                    f"{r.feature_id} valid [{r.valid_from_ns}, {r.valid_until_ns})" for r in outside)
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
            # BR-DAT-04: the risk term prices entry and exit fees with the core's reservation function under the
            # active policy's fee model (``risk.fees.envelope``); the backtest prices them the same way.
            risk = RiskTerm.from_spec(spec, snap.fees)
            if risk is not None and snap.currency:
                risk = RiskTerm(risk.loss_fraction_of_equity, risk.adverse_move_fraction, risk.cost_fraction, risk.fee_model,
                                snap.currency)
            plans, plan_notes = plan(targets, sizing=sizing, limit_offset_bps=spec.execution["limit_offset_bps"],
                                     equity=snap.equity(marks), marks=marks, holdings=snap.holdings,
                                     instruments={i: snap.instruments[i] for i in tradable}, budget=snap.available_cash,
                                     fee_envelope=snap.fee_envelope, adv={i: _adv(visible.get(i, []), sizing.adv_window) for i in tradable},
                                     ledger_scale=snap.ledger_scale, tradable=tradable, risk=risk)
        else:
            plans, plan_notes = {}, {}
        for i in eligible:
            d = decisions[i]
            text, d.score = _strategy_note(notes.get(i))
            d.strategy_note = _join(text, "FIXTURE data" if fixture else "")
            if decision.targets is None or i not in plans:
                d.reason = _join(text, f"no target from the strategy: hold ({notes.get('state', 'no targets')})",
                                 "FIXTURE data" if fixture else "")
                continue
            p: InstrumentPlan = plans[i]
            d.target_quantity = p.desired_quantity
            d.detail = {"target_weight": p.target_weight.to_str() if p.target_weight is not None else None,
                        "current_weight": p.current_weight.to_str() if p.current_weight is not None else None,
                        "equity": plan_notes.get("equity"), "budget": plan_notes.get("budget")}
            if p.risk is not None:
                d.detail["risk"] = p.risk
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
            elif window.empty:
                d.outcome = "BLOCKED"
                d.reason = _join(f"NO_SUBMISSION_WINDOW: the {window.session_date} session is shorter than the spec's open "
                                 f"and close offsets; would {what}", text)
            elif o.side == "BUY" and (self.rewarm or {}).get("status") == "MISMATCH":
                d.outcome = "BLOCKED"
                d.reason = _join(f"FEATURE_STATE_MISMATCH: the feature state re-warmed from the point-in-time store at "
                                 f"restart differs from the snapshot of {self.rewarm['session_date']}; no new entries "
                                 f"(BR-DAT-18); would {what}", text)
            else:
                d.outcome, d.order = "INTENT", o
                d.reason = _join(text, p.note, "FIXTURE data" if fixture else "")
                # BR-DAT-19: sent inside [open + N, close - M] of the execution session, its limit set at transmission
                d.detail.update({"window": window.to_json(), "model_bound": o.limit_price.to_str(),
                                 "collar_bps": spec.submission_window["collar_bps"]})
        rows = []
        for i in fresh:
            if i in recovered:          # the core holds this decision's intent: its remainder is cancelled at window end
                decisions[i].detail = {**decisions[i].detail, "window": window.to_json()}
            # BR-DAT-18: the feature rows and the strategy's own target, the snapshot a restarted runner compares with
            decisions[i].detail = {**decisions[i].detail, "features": [r.to_json() for r in frows[i]],
                                   "strategy_target": (repr(decision.targets[i]) if decision.targets is not None
                                                       and i in decision.targets else None)}
            rows.append(self._row(art, day, t, deadline, snap, decisions[i], recovered.get(i)))
        return rows

    def _rewarm_check(self, art: LoadedArtifact, clock_utc: time) -> dict:
        """BR-DAT-18 (book ch. 26 "resume only after warm-up and feature-state consistency checks"). At the first cycle
        of a process, re-warm the strategy from the point-in-time store as of the journal's most recent decision
        instant, by the same deterministic code path every decision uses, and compare the feature rows (values,
        versions, validity windows, warm-up states, missingness) and, for a strategy whose targets depend only on the
        data, the targets with the snapshot the journal holds. A MISMATCH (e.g. history silently corrected in the
        store, or different code) blocks new entries for the life of this process; exits stay possible."""
        rows = [r for r in self.journal.rows() if (r["detail"] or {}).get("features")]
        if not rows:
            return {"status": "NO_SNAPSHOT"}
        last = max(r["session_date"] for r in rows)
        snap_rows = {r["instrument_id"]: r for r in rows if r["session_date"] == last}
        day = date.fromisoformat(last)
        t = decision_instant(day, clock_utc)
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
        decision, visible, strategy = _decide_with(spec, series, day, t)
        close = _session_close_ns(self.cfg.calendar, day)
        stateless = bool(getattr(strategy, "STATELESS_TARGETS", False))
        mismatches = []
        for inst, row in sorted(snap_rows.items()):
            now = [r.to_json() for r in feature_rows(spec, strategy, inst, visible.get(inst, []), decision_session=day,
                                                     session_close_ns=close, calendar=self.cfg.calendar)]
            if now != row["detail"]["features"]:
                mismatches.append({"instrument_id": inst, "what": "features",
                                   "journal": row["detail"]["features"], "rewarmed": now})
            target = repr(decision.targets[inst]) if decision.targets is not None and inst in decision.targets else None
            if stateless and target != row["detail"].get("strategy_target"):
                mismatches.append({"instrument_id": inst, "what": "target", "journal": row["detail"].get("strategy_target"),
                                   "rewarmed": target})
        return {"status": "MISMATCH" if mismatches else "MATCH", "session_date": last, "compared": sorted(snap_rows),
                "targets_compared": stateless, "mismatches": mismatches}

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
            # Journal lost after the core took this intent: never resubmit; record what the core holds. The decision
            # made now sees the state AFTER that intent (its pending quantity counts as current), so its quantities
            # and digest do not describe the decision that produced the intent: the record carries the digest the
            # intent's decision_ref names, and no quantities (they were lost with the journal). Review fix.
            intent = recovered.get("intent") or {}
            digest = intent.get("inputs_digest")
            original = isinstance(digest, str) and _HEX64.fullmatch(digest) is not None
            what = (f"{intent.get('side')} {intent.get('quantity')} @ {intent.get('limit_price')}" if intent
                    else "the intent the core holds")
            # Only a live run submits intents, so this is a live decision even if this runner now runs in shadow mode.
            rebuilt = {**base, "mode": "live", "target_quantity": None, "current_quantity": None,
                       "inputs_digest": digest if original else base["inputs_digest"],
                       "reason": _join(f"RECOVERED after journal loss: rebuilt from {what}; the decision's quantities were "
                                       "lost with the journal" + ("" if original else "; inputs digest recomputed at recovery"),
                                       d.strategy_note)}
            row["submit_payload"] = None
            row["state"] = "SUBMITTED"
            row["submit_result"] = recovered
            row["decision_payload"] = self._finalize(rebuilt, recovered, ids["order_id"])
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
            if sub is not None and self.cfg.mode != "live":
                # Planned by a live run, now executed by a runner the owner started in shadow mode: never sent.
                self._withhold(row, "MODE_CHANGED", f"planned by a {row['mode']} run; this runner runs in {self.cfg.mode} mode")
            elif sub is not None and self.clock.now_ns() >= sub["deadline_ns"]:
                refusal = (row["detail"] or {}).get("pricing_refusal")
                self._withhold(row, "RUNNER_LATE", "the intent deadline passed before the core took it"
                               + (f" (last transmission attempt: {refusal['why']})" if refusal else ""))
            elif sub is not None and (valid_until := features_valid_until(row)) is not None \
                    and self.clock.now_ns() >= valid_until:
                # BR-DAT-18: the decision's feature values are past their validity window; it is never transmitted
                self._withhold(row, "FEATURE_EXPIRED", f"the decision's feature values were valid until {valid_until}")
            elif sub is not None and sub["side"] == "BUY" and (self.rewarm or {}).get("status") == "MISMATCH":
                # BR-DAT-18: this process's re-warm disagrees with the journal's snapshot, so no new entry is sent,
                # including a buy an earlier process planned on that snapshot and did not transmit
                self._withhold(row, "FEATURE_STATE_MISMATCH", "the feature state re-warmed from the point-in-time store at "
                               f"restart differs from the snapshot of {self.rewarm['session_date']}; no new entries")
            elif sub is not None and self.clock.now_ns() < ((row["detail"] or {}).get("window") or {}).get("start_ns", 0):
                return row          # BR-DAT-19: waits for its submission window (open + N of the execution session)
            elif sub is not None:
                priced = self._price(row)
                if priced is None:
                    return self.journal.get(day, inst)     # no valid quote yet: still PLANNED, retried next cycle
                row = priced
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

    def _withhold(self, row: dict, code: str, why: str) -> None:
        """A PLANNED intent this runner may not send (``RUNNER_LATE``: its deadline has passed, the core would refuse
        it as DEADLINE anyway; ``MODE_CHANGED``: a shadow runner never submits) is never sent. If an earlier process
        did send it (a crash before the journal learned), the core's stored result is the truth and is recorded;
        otherwise the decision is recorded BLOCKED with ``code``."""
        day, inst, sub = date.fromisoformat(row["session_date"]), row["instrument_id"], row["submit_payload"]
        stored = self._core_commands([row["submit_command_id"]]).get(row["submit_command_id"])
        if stored is not None and self._own(stored):
            res = {"accepted": stored["status"] == "ACCEPTED", "duplicate": True, "body": stored["result"], "events": [],
                   "recovered": True}
            decision = self._finalize(row["decision_payload"], res, row["order_id"])
            self.journal.update(day, inst, self.clock.now_ns(), state="SUBMITTED", submit_result=res, decision_payload=decision)
            log.info("%s %s %s: %s, but the core already took this intent (%s); recording that, not resending",
                     row["artifact_id"], day, inst, code, stored["status"])
            return
        base = row["decision_payload"]
        decision = {**base, "outcome": "BLOCKED", "order_ids": [],
                    "reason": _join(f"{code}: not submitted; {why}; would {sub['side']} {sub['quantity']} @ {sub['limit_price']}",
                                    base.get("reason", ""))}
        self.journal.update(day, inst, self.clock.now_ns(), state="SUBMITTED", decision_payload=decision)
        log.warning("%s %s %s: intent not submitted (%s: %s); recorded BLOCKED", row["artifact_id"], day, inst, code, why)

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
        detail = row.get("detail") or {}
        if row["state"] == "PLANNED" and row.get("submit_payload") is not None:
            # still to be transmitted: before the window opens, or no valid quote yet (BR-DAT-19)
            out["waiting"] = "PRICING" if detail.get("pricing_refusal") else "WINDOW"
        if detail.get("transmission"):
            out["transmission"] = detail["transmission"]
        if detail.get("window_cancel"):
            out["window_cancel"] = detail["window_cancel"]
        return out

    # ------------------------------------------------------------------- loop
    def run_loop(self, stop: threading.Event, *, grace_s: int = DEFAULT_GRACE_S, poll_s: float = DEFAULT_POLL_S) -> None:
        """Decide each eligible session once its decision instant plus ``grace_s`` has passed; poll every ``poll_s``."""
        while not stop.is_set():
            try:
                now = self.clock.now_ns()
                today = datetime.fromtimestamp(now // NS_PER_S, tz=timezone.utc).date()
                try:
                    art = self._load_artifact(today)
                    hh, mm = (int(x) for x in art.spec.execution["decision_clock_utc"].split(":"))
                    day = latest_eligible_session(now, self.cfg.calendar, time(hh, mm))
                except CoreUnreachable:
                    raise
                except Exception as exc:
                    self._cancel_without_deciding(exc)      # ``run_once`` does the same for its own failures
                    raise
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
