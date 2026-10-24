"""Durable spend ledger for paid model calls (ADR-0016 section 5; CAP-AGT-02, CAP-MODEL-05).

Protocol for every paid call:
  1. ``reserve(worst_case)`` BEFORE the network call. The check against every cap and
     the insert happen in one SQLite ``BEGIN IMMEDIATE`` transaction, so several
     worker processes sharing the file cannot both pass the same headroom
     (CAP-AGT-02: "a shared store that checks and reserves in a single operation").
  2. ``settle(actual)`` after a response with a usable usage object.
  3. ``release()`` only when the request provably never left this process.
  4. ``mark_unconfirmed()`` when the outcome or the bill cannot be confirmed
     (timeout, interrupted transfer, provider error whose billing is not documented;
     CAP-MODEL-06 records error billing as unverified). The full reservation then stays
     consumed until ``reconcile()`` against a provider cost report.

Caps come only from the owner policy (``config.OwnerPolicy``). There is no method that
changes a cap. If no owner policy exists, every reservation is refused.

Consumption per row: OPEN and UNCONFIRMED rows count their reserved amount; SETTLED
rows count the actual amount (even when it exceeds the reservation: real spend is
never hidden; the row is flagged ``overrun``); RELEASED rows count nothing.
Amounts are exact decimal text summed with ``Fixed`` (never SQL floats). Days and
months are UTC.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..domain.fixed import Fixed
from ..domain.money import Money
from ..service.paths import Paths
from .config import OwnerPolicy

STATES = ("OPEN", "SETTLED", "RELEASED", "UNCONFIRMED")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations(
  reservation_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  role TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  currency TEXT NOT NULL,
  reserved TEXT NOT NULL,
  actual TEXT,
  state TEXT NOT NULL CHECK (state IN ('OPEN','SETTLED','RELEASED','UNCONFIRMED')),
  day TEXT NOT NULL,
  month TEXT NOT NULL,
  created_ns INTEGER NOT NULL,
  closed_ns INTEGER,
  overrun INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS reservations_day ON reservations(day);
CREATE INDEX IF NOT EXISTS reservations_job ON reservations(job_id);
CREATE TABLE IF NOT EXISTS ledger_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  reservation_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  at_ns INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""


class BudgetError(RuntimeError):
    pass


class PaidCallsDisabled(BudgetError):
    """No owner policy (or a zero budget): every paid call is refused (AI-05)."""


class BudgetRefused(BudgetError):
    def __init__(self, level: str, cap: Money, consumed: Money, requested: Money):
        self.level, self.cap, self.consumed, self.requested = level, cap, consumed, requested
        super().__init__(f"budget refused at {level}: consumed {consumed} + requested {requested} > cap {cap}")


class ReservationStateError(BudgetError):
    pass


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    job_id: str
    role: str
    provider: str
    model_id: str
    purpose: str
    amount: Money
    created_ns: int


def default_ledger_path(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "agents" / "ledger.sqlite"


def _day_month(ns: int) -> tuple[str, str]:
    d = datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc)
    return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")


class SpendLedger:
    def __init__(self, policy: OwnerPolicy | None, db_path: Path | None = None, *,
                 clock_ns: Callable[[], int], new_id: Callable[[], str] | None = None):
        self._policy = policy
        self._clock_ns = clock_ns
        self._new_id = new_id or (lambda: uuid.uuid4().hex)
        path = Path(db_path) if db_path else default_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except PermissionError:  # pragma: no cover - directory owned elsewhere
            pass
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        os.chmod(path, 0o600)

    # ------------------------------------------------------------------ policy view (read only)
    @property
    def currency(self) -> str | None:
        return self._policy.caps.currency if self._policy else None

    def paid_calls_enabled(self) -> bool:
        p = self._policy
        return p is not None and p.caps.per_day.is_positive() and p.caps.per_job.is_positive()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ internals
    def _consumed(self, rows) -> Fixed:
        total = Fixed(0)
        for state, reserved, actual in rows:
            if state in ("OPEN", "UNCONFIRMED"):
                total = total + Fixed.parse(reserved)
            elif state == "SETTLED":
                total = total + Fixed.parse(actual)
        return total

    def _sum(self, where: str, args: tuple) -> Fixed:
        rows = self._db.execute(f"SELECT state, reserved, actual FROM reservations WHERE {where}", args).fetchall()
        return self._consumed(rows)

    def _event(self, rid: str, kind: str, detail: str = "") -> None:
        self._db.execute("INSERT INTO ledger_events(reservation_id, kind, at_ns, detail) VALUES (?,?,?,?)",
                         (rid, kind, self._clock_ns(), detail))

    # ------------------------------------------------------------------ reserve / settle
    def reserve(self, *, job_id: str, role: str, provider: str, model_id: str, amount: Money,
                job_cap: Money | None = None, purpose: str = "generate", note: str = "",
                role_day_cap: Money | None = None) -> Reservation:
        """Reserve ``amount`` against every cap atomically, or raise.

        ``job_cap`` (the envelope's spend cap) can only lower the owner's per-job cap.
        ``role_day_cap`` (an AI process's own per-day cap, ``agents/processes``) adds one more check on this
        role's spend today, in the same transaction; it can only refuse, never widen an owner cap.
        A zero amount is an admission record (used for calls whose billing is free or
        undocumented); it is still refused when any cap is already exhausted.
        """
        p = self._policy
        if p is None:
            raise PaidCallsDisabled("no owner policy at ~/.unknown/config/agents.json; paid model calls are disabled")
        if not self.paid_calls_enabled():
            raise PaidCallsDisabled("owner budget is zero; paid model calls are disabled")
        caps = p.caps
        if not isinstance(amount, Money) or amount.currency != caps.currency:
            raise BudgetError(f"amount must be Money in {caps.currency}")
        if amount.is_negative():
            raise BudgetError("reservation amount must not be negative")
        if job_cap is not None and job_cap.currency != caps.currency:
            raise BudgetError("job cap currency differs from the owner policy currency")
        if role_day_cap is not None and (not isinstance(role_day_cap, Money) or role_day_cap.currency != caps.currency):
            raise BudgetError("role day cap must be Money in the owner policy currency")
        role_cap = None
        if caps.per_role_per_day is not None:
            role_cap = caps.per_role_per_day.get(role)
            if role_cap is None:
                raise BudgetRefused(f"role:{role}", Money.zero(caps.currency), Money.zero(caps.currency), amount)
        effective_job_cap = caps.per_job if job_cap is None or caps.per_job <= job_cap else job_cap
        now = self._clock_ns()
        day, month = _day_month(now)
        pcaps = caps.per_provider.get(provider)
        checks: list[tuple[str, Money, str, tuple]] = [("day", caps.per_day, "day = ?", (day,)),
                                                        ("job", effective_job_cap, "job_id = ?", (job_id,))]
        if caps.lifetime is not None:
            checks.append(("lifetime", caps.lifetime, "1 = 1", ()))
        if caps.per_month is not None:
            checks.append(("month", caps.per_month, "month = ?", (month,)))
        if role_cap is not None:
            checks.append((f"role:{role}:day", role_cap, "role = ? AND day = ?", (role, day)))
        if role_day_cap is not None:
            checks.append((f"process:{role}:day", role_day_cap, "role = ? AND day = ?", (role, day)))
        if pcaps is not None and pcaps.lifetime is not None:
            checks.append((f"provider:{provider}:lifetime", pcaps.lifetime, "provider = ?", (provider,)))
        if pcaps is not None and pcaps.per_day is not None:
            checks.append((f"provider:{provider}:day", pcaps.per_day, "provider = ? AND day = ?", (provider, day)))
        rid = self._new_id()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for level, cap, where, args in checks:
                    consumed = self._sum(where, args)
                    if consumed + amount.amount > cap.amount:
                        raise BudgetRefused(level, cap, Money(caps.currency, consumed), amount)
                self._db.execute(
                    "INSERT INTO reservations(reservation_id, job_id, role, provider, model_id, purpose, currency, reserved,"
                    " state, day, month, created_ns, note) VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)",
                    (rid, job_id, role, provider, model_id, purpose, caps.currency, amount.amount.to_str(), day, month, now, note))
                self._event(rid, "reserved", amount.amount.to_str())
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return Reservation(rid, job_id, role, provider, model_id, purpose, amount, now)

    def _transition(self, rid: str, allowed_from: tuple[str, ...], to: str, *, actual: Money | None = None,
                    note: str = "", evidence: str = "") -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT state, reserved, currency, actual FROM reservations WHERE reservation_id = ?",
                                       (rid,)).fetchone()
                if row is None:
                    raise ReservationStateError(f"unknown reservation {rid}")
                state, reserved, ccy, prev_actual = row
                if state == to and to == "SETTLED" and actual is not None and prev_actual == actual.amount.to_str():
                    self._db.execute("COMMIT")
                    return  # idempotent repeat of the same settlement
                if state not in allowed_from:
                    raise ReservationStateError(f"reservation {rid} is {state}; cannot become {to}")
                overrun = 0
                actual_text = None
                if actual is not None:
                    if actual.currency != ccy or actual.is_negative():
                        raise BudgetError("actual must be non-negative Money in the reservation currency")
                    actual_text = actual.amount.to_str()
                    overrun = 1 if actual.amount > Fixed.parse(reserved) else 0
                self._db.execute(
                    "UPDATE reservations SET state = ?, actual = COALESCE(?, actual), closed_ns = ?, overrun = MAX(overrun, ?),"
                    " note = CASE WHEN ? = '' THEN note ELSE ? END, evidence = CASE WHEN ? = '' THEN evidence ELSE ? END"
                    " WHERE reservation_id = ?",
                    (to, actual_text, self._clock_ns(), overrun, note, note, evidence, evidence, rid))
                self._event(rid, to.lower(), actual_text or note)
                if overrun:
                    self._event(rid, "overrun", f"actual {actual_text} > reserved {reserved}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def settle(self, reservation: Reservation | str, actual: Money, *, evidence: str = "") -> None:
        rid = reservation if isinstance(reservation, str) else reservation.reservation_id
        self._transition(rid, ("OPEN",), "SETTLED", actual=actual, evidence=evidence)

    def release(self, reservation: Reservation | str, *, note: str = "not sent") -> None:
        """Only for a request that never left this process (nothing can have been billed)."""
        rid = reservation if isinstance(reservation, str) else reservation.reservation_id
        self._transition(rid, ("OPEN",), "RELEASED", note=note)

    def mark_unconfirmed(self, reservation: Reservation | str, *, note: str) -> None:
        rid = reservation if isinstance(reservation, str) else reservation.reservation_id
        self._transition(rid, ("OPEN",), "UNCONFIRMED", note=note)

    def reconcile(self, reservation: Reservation | str, actual: Money, *, evidence: str) -> None:
        """Owner/system reconciliation of an UNCONFIRMED row against a provider cost report."""
        if not evidence:
            raise BudgetError("reconciliation needs evidence (cost report reference)")
        rid = reservation if isinstance(reservation, str) else reservation.reservation_id
        self._transition(rid, ("UNCONFIRMED",), "SETTLED", actual=actual, evidence=evidence)

    # ------------------------------------------------------------------ reporting
    def is_open(self, reservation: Reservation | str) -> bool:
        rid = reservation if isinstance(reservation, str) else reservation.reservation_id
        with self._lock:
            row = self._db.execute("SELECT state FROM reservations WHERE reservation_id = ?", (rid,)).fetchone()
        return row is not None and row[0] == "OPEN"

    def state(self, reservation_id: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT state, reserved, actual, overrun, note, evidence, job_id, provider, model_id FROM reservations"
                " WHERE reservation_id = ?", (reservation_id,)).fetchone()
        if row is None:
            raise ReservationStateError(f"unknown reservation {reservation_id}")
        keys = ("state", "reserved", "actual", "overrun", "note", "evidence", "job_id", "provider", "model_id")
        return dict(zip(keys, row))

    def consumed(self, *, day: str | None = None, job_id: str | None = None, role: str | None = None,
                 provider: str | None = None) -> Fixed:
        clauses, args = [], []
        for col, val in (("day", day), ("job_id", job_id), ("role", role), ("provider", provider)):
            if val is not None:
                clauses.append(f"{col} = ?")
                args.append(val)
        with self._lock:
            return self._sum(" AND ".join(clauses) or "1 = 1", tuple(args))

    def events(self) -> list[tuple]:
        with self._lock:
            return self._db.execute("SELECT reservation_id, kind, at_ns, detail FROM ledger_events ORDER BY seq").fetchall()

    def reservations(self, job_id: str) -> list[dict]:
        """Every reservation row of one job, oldest first (reporting and worker crash recovery)."""
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(_ROW_COLUMNS)} FROM reservations WHERE job_id = ?"
                                    " ORDER BY created_ns, rowid", (job_id,)).fetchall()
        return [dict(zip(_ROW_COLUMNS, r)) for r in rows]


_ROW_COLUMNS = ("reservation_id", "job_id", "role", "provider", "model_id", "purpose", "currency", "reserved", "actual",
                "state", "created_ns", "closed_ns", "overrun", "note", "evidence")


def read_job_spend(db_path: Path, job_id: str) -> dict:
    """Read-only spend report for one job (the terminal API uses this; it opens the file read-only and never
    creates it). ``consumed`` follows the ledger rule: OPEN and UNCONFIRMED rows count their reservation,
    SETTLED rows their actual amount, RELEASED rows nothing."""
    path = Path(db_path)
    if not path.exists():
        return {"ledger": "absent", "consumed": None, "currency": None, "reservations": []}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute(f"SELECT {', '.join(_ROW_COLUMNS)} FROM reservations WHERE job_id = ? ORDER BY created_ns, rowid",
                          (job_id,)).fetchall()
    finally:
        db.close()
    out = [dict(zip(_ROW_COLUMNS, r)) for r in rows]
    total = Fixed(0)
    for r in out:
        if r["state"] in ("OPEN", "UNCONFIRMED"):
            total = total + Fixed.parse(r["reserved"])
        elif r["state"] == "SETTLED":
            total = total + Fixed.parse(r["actual"])
    currencies = sorted({r["currency"] for r in out})
    return {"ledger": "present", "consumed": total.to_str(), "currency": currencies[0] if len(currencies) == 1 else None,
            "reservations": out}
