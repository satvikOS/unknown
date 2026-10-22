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
from ..domain.principal import Principal, Role, permitted
from ..market.quotes import QuoteBook
from ..store.db import Store, Tx
from . import ledger
from .projector import apply_changes

EVENT_SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64


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


class Context:
    """Handler-facing view of the open transaction."""

    def __init__(self, desk: "Desk", tx: Tx, command: Command, payload_hash: str):
        self.desk = desk
        self.tx = tx
        self.command = command
        self.payload_hash = payload_hash
        self.now_ns = desk.clock.now_ns()
        self.event_seqs: list[int] = []
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
        prev = self.tx.one("SELECT chain_hash FROM events ORDER BY seq DESC LIMIT 1")
        prev_hash = prev["chain_hash"] if prev else GENESIS_HASH
        event_id = self.new_id()
        recorded = self.desk.clock.now_ns()
        chain = sha256_hex(prev_hash + event_id + event_type + payload_hash)
        cur = self.tx.run(
            "INSERT INTO events(event_id, type, schema_version, command_id, correlation_id, causation_id, principal_id, "
            "account_key, session_id, order_id, instrument_id, event_time_ns, receipt_time_ns, recorded_time_ns, writer_epoch, "
            "payload, payload_hash, prev_hash, chain_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, event_type, EVENT_SCHEMA_VERSION, self.command.command_id, self.correlation_id,
             self.command.causation_id, self.principal.id, account_key, session_id, order_id, instrument_id,
             event_time_ns, self.now_ns, recorded, self.epoch, text, payload_hash, prev_hash, chain))
        seq = cur.lastrowid
        apply_changes(self.tx, seq, payload["changes"])
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
        self.quotes = quotes or QuoteBook()
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
        with self.store.write() as tx:
            existing = tx.one("SELECT payload_hash, status, result FROM commands WHERE command_id=?", (command.command_id,))
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise CommandConflict(f"command id {command.command_id} reused with a different payload")
                return Result(command.command_id, existing["status"] == "ACCEPTED", True, json.loads(existing["result"]))
            ctx = Context(self, tx, command, payload_hash)
            tx.run("SAVEPOINT handler")
            try:
                if not permitted(command.principal.role, command.kind):
                    raise Rejected("UNAUTHORIZED", f"role {command.principal.role.value} may not issue {command.kind}")
                if command.deadline_ns is not None and ctx.now_ns > command.deadline_ns:
                    raise Rejected("DEADLINE_EXPIRED", "command deadline passed before execution")
                body = self._handlers[command.kind](ctx, command.payload) or {}
                if self.verify_each_command:
                    problems = ledger.verify(tx)
                    if problems:
                        raise InvariantViolation("; ".join(problems[:5]))
                tx.run("RELEASE handler")
                status = "ACCEPTED"
            except Rejected as rejection:
                tx.run("ROLLBACK TO handler")
                tx.run("RELEASE handler")
                ctx.event_seqs.clear()
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
    restored = False
    marker = Path(f"{path}.epoch") if str(path) != ":memory:" else None
    if marker is not None and marker.exists():
        try:
            restored = int(marker.read_text().strip()) >= store.epoch
        except ValueError:
            restored = True
    desk.run_system("process_started", {"epoch": store.epoch, "restored_from_backup": restored},
                    command_id=f"process.{deployment_id}.{store.epoch}")
    if marker is not None:
        tmp = marker.with_suffix(".epoch.tmp")
        tmp.write_text(str(store.epoch))
        os.replace(tmp, marker)
    return desk
