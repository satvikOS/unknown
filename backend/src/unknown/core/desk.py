"""The single-writer command executor (ADR-0002, ADR-0005).

``Desk.execute`` runs one command in one SQLite transaction:
dedup by command id and payload hash -> authorization -> deadline -> handler
(which emits events that are applied immediately) -> invariant checks ->
store result -> commit. A deterministic ``Rejected`` is stored as the
command's result together with a ``CommandRejected`` decision event; any other
exception rolls everything back and nothing is stored.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..domain.calendar import BusinessCalendar, WEEKDAYS_ONLY
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.clock import Clock
from ..domain.errors import CommandConflict, InvariantViolation, Rejected
from ..domain.ids import IdGenerator, validate_id
from ..domain.principal import Principal, Role, permitted, strategy_id_reserved
from ..market.quotes import QuoteBook
from ..store.db import Store, Tx
from . import ledger, state
from .projector import apply_changes
from .replay import CHAINED_COLUMNS, chain_hash

EVENT_SCHEMA_VERSION = 1
# Commands whose effects must be recorded even when a ledger invariant check fails afterwards:
# genuine broker facts (never discarded) and the red stop (never blocked) — ADR-0006, ADR-0010.
RECORD_DESPITE_INVARIANTS = frozenset({"stop", "broker_execution", "broker_commission", "broker_order_status",
                                       "broker_connectivity", "reconciliation_result", "process_started"})
# Every command checks the ledger keys it touched (ledger.verify_touched, review finding 39). These also re-check
# the whole ledger, so damage that no command made (an edit outside the core) is found at process start, by
# every reconciliation result and before a green start, as the per-command full check used to find it.
FULL_VERIFY_KINDS = frozenset({"process_started", "reconciliation_result", "start"})
GENESIS_HASH = "0" * 64
RESERVED_ID_PREFIXES = ("process.", "sys.", "conflict.")


@dataclass(frozen=True)
class Command:
    command_id: str
    kind: str
    principal: Principal
    payload: dict
    correlation_id: str | None = None
    causation_id: str | None = None
    deadline_ns: int | None = None


@dataclass
class Result:
    command_id: str
    accepted: bool
    duplicate: bool
    body: dict
    events: list[int] = field(default_factory=list)

    @property
    def rejected_code(self) -> str | None:
        return None if self.accepted else self.body.get("code")


@dataclass(frozen=True)
class HealthSnapshot:
    """Facts gathered outside the core for preflight; recorded as evidence."""
    broker_connected: bool
    broker_authenticated: bool
    broker_server_time_ns: int | None
    disk_free_mb: int
    detail: str = ""
    broker_account_mode: str | None = None   # as the broker connection reports it, never the registered value


class Context:
    """Handler-facing view of the open transaction."""

    def __init__(self, desk: "Desk", tx: Tx, command: Command, payload_hash: str):
        self.desk = desk
        self.tx = tx
        self.command = command
        self.payload_hash = payload_hash
        self.now_ns = desk.clock.now_ns()
        self.event_seqs: list[int] = []
        self.touched = ledger.Touched()  # what this command's events changed, for the per-command ledger check
        self.correlation_id = command.correlation_id or command.command_id

    @property
    def principal(self) -> Principal:
        return self.command.principal

    @property
    def epoch(self) -> int:
        return self.desk.store.epoch

    def new_id(self, prefix: str = "") -> str:
        return self.desk.ids.new(prefix)

    def emit(self, event_type: str, info: dict | None = None, changes: dict | None = None, *,
             account_key: str | None = None, session_id: str | None = None, order_id: str | None = None,
             instrument_id: str | None = None, event_time_ns: int | None = None) -> int:
        payload = {"info": info or {}, "changes": changes or {}}
        text = canonical_json(payload)
        payload_hash = sha256_hex(text)
        prev = self.tx.one("SELECT seq, chain_hash FROM events ORDER BY seq DESC LIMIT 1")
        prev_hash = prev["chain_hash"] if prev else GENESIS_HASH
        fields = {"seq": (prev["seq"] + 1) if prev else 1, "event_id": self.new_id(), "type": event_type,
                  "schema_version": EVENT_SCHEMA_VERSION, "command_id": self.command.command_id,
                  "correlation_id": self.correlation_id, "causation_id": self.command.causation_id,
                  "principal_id": self.principal.id, "account_key": account_key, "session_id": session_id,
                  "order_id": order_id, "instrument_id": instrument_id, "event_time_ns": event_time_ns,
                  "receipt_time_ns": self.now_ns, "recorded_time_ns": self.desk.clock.now_ns(), "writer_epoch": self.epoch,
                  "payload_hash": payload_hash}
        chain = chain_hash(prev_hash, fields)
        cols = ", ".join(CHAINED_COLUMNS)
        self.tx.run(f"INSERT INTO events({cols}, payload, prev_hash, chain_hash) VALUES "
                    f"({', '.join('?' * (len(CHAINED_COLUMNS) + 3))})",
                    tuple(fields[c] for c in CHAINED_COLUMNS) + (text, prev_hash, chain))
        seq = fields["seq"]
        apply_changes(self.tx, seq, payload["changes"], self.touched)
        self.event_seqs.append(seq)
        return seq


Handler = Callable[[Context, dict], dict]


class Desk:
    """Owns the store and dispatches commands to handlers."""

    def __init__(self, store: Store, clock: Clock, ids: IdGenerator, *, deployment_id: str,
                 quotes: QuoteBook | None = None, health: Callable[[], HealthSnapshot] | None = None,
                 calendars: dict[str, BusinessCalendar] | None = None, verify_each_command: bool = True):
        self.store = store
        self.clock = clock
        self.ids = ids
        self.deployment_id = validate_id(deployment_id, "deployment id")
        self.quotes = quotes if quotes is not None else QuoteBook()
        self.health = health or (lambda: HealthSnapshot(False, False, None, 0, "no health source configured"))
        self.calendars = {"WEEKDAYS": WEEKDAYS_ONLY, **(calendars or {})}
        self.verify_each_command = verify_each_command
        from .handlers import HANDLERS  # late import: handlers import this module
        self._handlers: dict[str, Handler] = HANDLERS

    # ------------------------------------------------------------- execute
    def execute(self, command: Command) -> Result:
        validate_id(command.command_id, "command id")
        if command.kind not in self._handlers:
            raise ValueError(f"unknown command kind {command.kind!r}")
        payload_hash = sha256_hex(canonical_json({"kind": command.kind, "payload": command.payload}))
        reserved = command.command_id.startswith(RESERVED_ID_PREFIXES) and command.principal.role is not Role.SYSTEM
        reserved_principal = command.principal.role is Role.STRATEGY and strategy_id_reserved(command.principal.id)
        if reserved or reserved_principal or not permitted(command.principal.role, command.kind):
            # Authorization precedes dedup, and a refused command is never stored under its id, so no
            # principal can pre-claim (or probe) another principal's command ids.
            why = ("command id prefix reserved for the system" if reserved else
                   f"principal id {command.principal.id!r} is reserved and may not be asserted by a strategy"
                   if reserved_principal else f"role {command.principal.role.value} may not issue {command.kind}")
            body = Rejected("UNAUTHORIZED", why).to_json()
            with self.store.write() as tx:
                key = command.payload.get("account_key") if isinstance(command.payload, dict) else None
                seq = Context(self, tx, command, payload_hash).emit("CommandRejected", {
                    "kind": command.kind, "rejection": body, "payload_hash": payload_hash, "stored": False},
                    account_key=key if isinstance(key, str) else None)
            return Result(command.command_id, False, False, body, [seq])
        conflict: str | None = None
        with self.store.write() as tx:
            existing = tx.one("SELECT payload_hash, status, result, principal_id, principal_role FROM commands WHERE command_id=?",
                              (command.command_id,))
            if existing is not None:
                same_principal = (existing["principal_id"], existing["principal_role"]) == (command.principal.id, command.principal.role.value)
                if existing["payload_hash"] == payload_hash and same_principal:
                    return Result(command.command_id, existing["status"] == "ACCEPTED", True, json.loads(existing["result"]))
                # A reused id with different content or from another principal is security-relevant:
                # record it durably, then refuse (ADR-0005 #4). The stored result is never disclosed.
                conflict = "payload" if existing["payload_hash"] != payload_hash else "principal"
                Context(self, tx, Command(f"conflict.{command.command_id}"[:160], command.kind, command.principal, {}),
                        payload_hash).emit("CommandIdConflict", {
                            "command_id": command.command_id, "kind": command.kind, "difference": conflict,
                            "presented_payload_hash": payload_hash, "principal_id": command.principal.id,
                            "principal_role": command.principal.role.value})
        if conflict is not None:
            raise CommandConflict(f"command id {command.command_id} reused with a different {conflict}")
        return self._execute_new(command, payload_hash)

    def _execute_new(self, command: Command, payload_hash: str) -> Result:
        with self.store.write() as tx:
            ctx = Context(self, tx, command, payload_hash)
            tx.run("SAVEPOINT handler")
            try:
                if not permitted(command.principal.role, command.kind):
                    raise Rejected("UNAUTHORIZED", f"role {command.principal.role.value} may not issue {command.kind}")
                if command.deadline_ns is not None and ctx.now_ns > command.deadline_ns:
                    raise Rejected("DEADLINE_EXPIRED", "command deadline passed before execution")
                body = self._handlers[command.kind](ctx, command.payload) or {}
                if command.kind in RECORD_DESPITE_INVARIANTS and ctx.event_seqs:
                    # A broker fact that leaves working SELL holds above the shares owned is an exception
                    # naming the owner, not a ledger-invariant alarm (ADR-0006, review finding 16).
                    self._open_quantity_shortfalls(ctx, command)
                if self.verify_each_command and ctx.event_seqs:
                    if command.kind in FULL_VERIFY_KINDS or ctx.touched.full:
                        problems = ledger.verify(tx)
                    else:
                        problems = ledger.verify_touched(tx, ctx.touched)
                    if problems:
                        if command.kind not in RECORD_DESPITE_INVARIANTS:
                            raise InvariantViolation("; ".join(problems[:5]))
                        # Observation and safety path: the economic fact and the stop must be recorded even
                        # if a projection invariant fails; open a blocking case and latch the stop instead.
                        self._invariant_alarm(ctx, command, problems)
                tx.run("RELEASE handler")
                status = "ACCEPTED"
            except Rejected as rejection:
                tx.run("ROLLBACK TO handler")
                tx.run("RELEASE handler")
                ctx.event_seqs.clear()
                ctx.touched = ledger.Touched()
                body = rejection.to_json()
                ctx.emit("CommandRejected", {"kind": command.kind, "rejection": body,
                                             "payload_hash": payload_hash},
                         account_key=command.payload.get("account_key") if isinstance(command.payload.get("account_key"), str) else None)
                status = "REJECTED"
            tx.run("INSERT INTO commands(command_id, kind, principal_id, principal_role, payload_hash, status, result, recorded_time_ns) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (command.command_id, command.kind, command.principal.id, command.principal.role.value, payload_hash,
                    status, canonical_json(body), self.clock.now_ns()))
            return Result(command.command_id, status == "ACCEPTED", False, body, list(ctx.event_seqs))

    def _invariant_alarm(self, ctx: Context, command: Command, problems: list[str]) -> None:
        accounts = [r["account_key"] for r in ctx.tx.all("SELECT account_key FROM accounts ORDER BY account_key")]
        for key in accounts:
            c = json.loads(ctx.tx.one("SELECT row FROM control WHERE account_key=?", (key,))["row"])
            c = c if c["stop_latched"] else {**c, "stop_latched": True, "latch_reason": "LEDGER_INVARIANT",
                                             "latched_by": ctx.principal.id, "latched_at_ns": ctx.now_ns,
                                             "latch_command_id": command.command_id}
            case = {"case_id": ctx.new_id("case-"), "account_key": key, "kind": "LEDGER_INVARIANT", "blocking": True,
                    "status": "OPEN", "severity": "critical", "opened_at_ns": ctx.now_ns, "session_id": None, "order_id": None,
                    "owner_hint": None, "detail": {"command_kind": command.kind, "problems": problems[:20]},
                    "key": f"ledger-invariant:{command.command_id}", "resolution": None}
            ctx.emit("LedgerInvariantViolation", {"problems": problems[:20], "command_kind": command.kind},
                     {"control": [c], "exceptions": [case]}, account_key=key)

    def _open_quantity_shortfalls(self, ctx: Context, command: Command) -> None:
        """Open one blocking EXECUTION_ANOMALY per (owner, instrument) whose ACTIVE quantity holds now exceed
        the shares it owns after this observation changed its lots or holds (review finding 16). The case
        names the owner (owner_hint), so the ledger check treats the shortfall as covered: the fact is
        recorded, new exposure is blocked, and the account is not latched by the generic invariant alarm."""
        for s in ledger.quantity_shortfalls(ctx.tx, ctx.touched):
            key = f"quantity-shortfall:{s['owner']}:{s['instrument_id']}"
            if state.exception_by_key(ctx.tx, s["account_key"], key) is not None:
                continue
            owner = s["owner"]
            session_id = owner.split(":", 1)[1] if owner.startswith("session:") else None
            detail = {"issue": "active sell quantity holds exceed owned quantity", "instrument_id": s["instrument_id"],
                      "held": s["held"], "owned": s["owned"], "orders": s["order_ids"], "command_kind": command.kind,
                      "resolution": "cancel or reduce the working sells, then resolve after reconciliation"}
            case = {"case_id": ctx.new_id("case-"), "account_key": s["account_key"], "kind": "EXECUTION_ANOMALY",
                    "blocking": True, "status": "OPEN", "severity": "critical", "opened_at_ns": ctx.now_ns,
                    "session_id": session_id, "order_id": s["order_ids"][0] if len(s["order_ids"]) == 1 else None,
                    "owner_hint": owner, "detail": detail, "key": key, "resolution": None}
            ctx.emit("QuantityHoldShortfall", {"owner": owner, "instrument_id": s["instrument_id"], "held": s["held"],
                                               "owned": s["owned"]},
                     {"exceptions": [case]}, account_key=s["account_key"], session_id=session_id,
                     instrument_id=s["instrument_id"])

    # ------------------------------------------------------------- helpers
    def system(self) -> Principal:
        return Principal(f"system.{self.deployment_id}", Role.SYSTEM)

    def run_system(self, kind: str, payload: dict, command_id: str | None = None, **kw: Any) -> Result:
        return self.execute(Command(command_id or self.ids.new(f"sys.{kind}."), kind, self.system(), payload, **kw))

    def verify(self) -> list[str]:
        with self.store.read() as tx:
            return ledger.verify(tx)


def open_desk(path: str | Path, clock: Clock, ids: IdGenerator, *, deployment_id: str, holder: str | None = None,
              takeover: bool = False, **kwargs: Any) -> Desk:
    """Open the store, detect a restored database, and record the process start.

    Restore detection (book-review resolution): the highest writer epoch ever
    granted is also written to ``<db>.epoch``. If the database's epoch is lower
    than that file, the database was restored from an older copy, so its stop
    latch, outbox and dedup history may be stale; the start command then
    latches the stop and marks in-flight orders for lookup (ADR-0010)."""
    store = Store(path, clock, holder=holder or f"{deployment_id}:{os.getpid()}", takeover=takeover)
    desk = Desk(store, clock, ids, deployment_id=deployment_id, **kwargs)
    evidence: list[str] = []
    marker = Path(f"{path}.epoch") if str(path) != ":memory:" else None
    origin = store.backup_origin()
    if origin is not None:
        check = store.conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            store.close()
            raise RuntimeError(f"restored database fails quick_check ({check}); refusing to start")
        evidence.append(f"database is a backup copy taken at epoch {origin['source_epoch']}, seq {origin['source_max_seq']}")
    if marker is not None:
        if marker.exists():
            try:
                if int(marker.read_text().strip()) >= store.epoch:
                    evidence.append("writer epoch not above the last recorded start")
            except ValueError:
                evidence.append("epoch marker unreadable")
        elif store.event_count() > 0:
            evidence.append("non-empty database without its epoch marker (moved or restored)")
    restored = bool(evidence)
    result = desk.run_system("process_started", {"epoch": store.epoch, "restored_from_backup": restored,
                                                 "restore_evidence": evidence},
                             command_id=f"process.{deployment_id}.{store.epoch}.{ids.new()}")
    if not result.accepted:
        raise RuntimeError(f"process start was not recorded: {result.body}")
    if origin is not None:
        store.clear_backup_origin()  # the restore is now an event; later restarts are ordinary
    if marker is not None:
        tmp = marker.with_suffix(".epoch.tmp")
        with open(tmp, "w") as fh:
            fh.write(str(store.epoch))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, marker)
    return desk
