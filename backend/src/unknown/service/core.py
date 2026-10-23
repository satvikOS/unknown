"""``unknown-core``: the single-writer service process (ADR-0002, ADR-0013).

All database writes happen on the service thread that opened the desk. The
command socket enqueues requests; the loop executes them between broker pumps,
so socket clients never touch the SQLite connection. The loop keeps the
application alive through recoverable failures (OPS-03): a broker outage is
recorded as connectivity and blocks entries through reconciliation; a failing
cycle is logged and retried; nothing here clears a stop.

A red stop is never blocked by persistence (BR-SES-13). If the database cannot
record a stop (disk full, an I/O error, a locked or damaged store), the service
latches it in memory: exposure-increasing commands are refused, the gateway
runner never transmits a buy, every open buy gets a best-effort cancel sent to
the broker directly, the failure is logged CRITICAL and shown in the health
query, and the latch is mirrored in an fsync'd file next to the database so a
restart keeps it. As soon as the database accepts writes, the stop is recorded
under its own command id, followed by a SYSTEM stop carrying the evidence of
what the memory latch did; only then is the memory latch released."""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path

from ..broker.port import BrokerPort
from ..api.queries import command_events, stored_command
from ..core.desk import Command, Context, Desk, HealthSnapshot, Result, open_desk
from ..core.handlers import control as control_handlers
from ..domain.calendar import WEEKDAYS_ONLY, load_calendars
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.clock import Clock, SystemClock, NS_PER_S
from ..domain.errors import CommandConflict, LeaseLost
from ..domain.ids import IdGenerator, validate_id
from ..domain.principal import Principal, Role, permitted
from ..gateway.journal import EventJournal
from ..gateway.runner import GatewayRunner, attach_journal, detach_journal
from ..reconcile.reconciler import Reconciler
from .ipc import CoreSocketServer, Grant, OutcomeUnknown, describe_request
from .paths import Paths

log = logging.getLogger("unknown.core")

# Each token may assert only these roles. Strategy runners (ADR-0017) get one token per artifact, created by the
# owner (``unknown.cli strategy-token``) and bound to that artifact id: see ``Paths.strategy_tokens``.
SOCKET_ROLES = {"api": frozenset({"OWNER"}), "agent": frozenset({"AGENT"})}
# Sourced exchange and settlement calendars (config/calendars); policies name them (e.g. settlement_calendar XNYS).
CALENDAR_DIR = Path(__file__).resolve().parents[4] / "config" / "calendars"
RECONNECT_MIN_S = 5.0
RECONNECT_MAX_S = 300.0


@dataclass
class Intervals:
    loop_s: float = 0.25
    valuation_s: float = 2.0
    reconcile_s: float = 60.0
    reconcile_retry_s: float = 5.0
    heartbeat_s: float = 10.0


# Commands that add exposure, directly or through funding and automatic starts: refused while a stop is latched
# only in memory (the database would not know to refuse them).
EXPOSURE_KINDS = frozenset({"start", "start_session", "resume_session", "create_session", "amend_session_request",
                            "authorize_capital"})
LATCH_ALARM_INTERVAL_S = 10.0
OPEN_BUY_STATES = ("SUBMIT_PENDING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING", "UNKNOWN")


@dataclass
class MemoryLatch:
    """A red stop the database could not record yet (BR-SES-13). ``pending`` holds the commands to record, in order:
    each stop that failed, then a SYSTEM stop carrying the evidence (error, cancels sent, the ids it stood in for)."""
    reason: str
    engaged_at_ns: int
    error: str
    pending: list[Command] = field(default_factory=list)
    cancels: list[dict] = field(default_factory=list)
    stands_in_for: list[str] = field(default_factory=list)
    last_persist_error: str | None = None

    def view(self) -> dict:
        return {"reason": self.reason, "engaged_at_ns": self.engaged_at_ns, "error": self.error,
                "pending": [c.command_id for c in self.pending], "stands_in_for": list(self.stands_in_for),
                "cancels": list(self.cancels), "last_persist_error": self.last_persist_error, "persisted": False}


def _command_json(c: Command) -> dict:
    return {"command_id": c.command_id, "kind": c.kind, "payload": c.payload,
            "principal": {"id": c.principal.id, "role": c.principal.role.value,
                          "authenticated_at_ns": c.principal.authenticated_at_ns}}


def _command_from_json(d: dict) -> Command:
    p = d["principal"]
    return Command(d["command_id"], d["kind"], Principal(p["id"], Role(p["role"]), p.get("authenticated_at_ns")),
                   d["payload"])


def _increases_exposure(req: dict) -> bool:
    kind = req.get("kind")
    if kind in EXPOSURE_KINDS:
        return True
    return kind == "submit_intent" and (req.get("payload") or {}).get("side") != "SELL"


class CoreService:
    def __init__(self, *, paths: Paths, account_key: str, deployment_id: str, broker: BrokerPort,
                 clock: Clock | None = None, intervals: Intervals | None = None, fixture_market=None,
                 quotes: QuoteBook | None = None):
        self.paths = paths
        self.quotes = quotes   # a market-data adapter publishing into the desk's book (None: the desk makes one)
        self.account_key = account_key
        self.deployment_id = deployment_id
        self.broker = broker
        self.clock = clock or SystemClock()
        self.intervals = intervals or Intervals()
        self.fixture_market = fixture_market
        self.requests: queue.Queue[tuple[dict, frozenset[str], Future]] = queue.Queue(maxsize=1000)
        self.stop_event = threading.Event()
        self.desk: Desk | None = None
        self.server: CoreSocketServer | None = None
        self.ready = threading.Event()
        self.cycle_errors = 0
        self.broker_mapping_provider: str | None = None   # set for adapters that resolve provider ids (IBKR conId)
        self.instrument_map: dict[str, str] = {}
        self.instrument_details: dict[str, tuple[str, str]] = {}
        self.lease_lost = False
        self.request_wait_s = 30.0   # how long a socket client waits for the loop to execute its request
        self._last_heartbeat = float("-inf")
        self.memory_latch: MemoryLatch | None = None   # a red stop the database has not recorded yet (BR-SES-13)
        self._last_latch_alarm = float("-inf")
        self.journal: EventJournal | None = None   # <data>/<account>.events, opened by run() (BR-ORD-20)

    # ------------------------------------------------------- memory stop latch
    @property
    def latch_path(self) -> Path:
        """The fsync'd mirror of an unrecorded stop, next to the account's database."""
        return self.paths.data / f"{self.account_key}.stop-latch.json"

    def _latchable(self, command: Command) -> bool:
        """Whether a failed ``command`` may stand in memory for a recorded stop: only a stop the desk would accept,
        from a role that may stop (OWNER or SYSTEM, ADR-0007) and for this core's own account. Any other request
        that fails is reported like every failed command; it must never engage the latch, cancel at the broker or
        leave a latch the desk will refuse to record (which would never release). A command id the desk refuses before
        any write (``validate_id``) is a malformed request, not a persistence failure: latched, it could never be
        recorded, so the latch would never release while the database recorded no stop at all."""
        try:
            validate_id(command.command_id, "command id")
        except ValueError:
            return False
        return (command.kind == "stop" and permitted(command.principal.role, "stop")
                and isinstance(command.payload, dict) and command.payload.get("account_key") == self.account_key)

    def _entry_gate(self) -> str | None:
        """Why the gateway may not transmit a buy now, or None (GatewayRunner entry_gate)."""
        latch = self.memory_latch
        return None if latch is None else f"STOP_LATCHED_IN_MEMORY:{latch.reason}"

    def make_runner(self) -> GatewayRunner:
        return GatewayRunner(self.desk, self.broker, self.account_key, heartbeat=self._heartbeat_if_due,
                             entry_gate=self._entry_gate, journal=self.journal)

    def _latch_in_memory(self, command: Command, exc: BaseException) -> dict:
        """The database did not record the stop ``command``: latch it in memory, cancel open buys at the broker,
        mirror the latch in its file, say so loudly, and answer the caller with what was done."""
        error = f"{type(exc).__name__}: {exc}"
        latch = self.memory_latch
        if latch is None:
            reason = (command.payload or {}).get("reason") or (
                "OWNER_STOP" if command.principal.role is Role.OWNER else "SYSTEM_STOP")
            latch = self.memory_latch = MemoryLatch(reason=str(reason), engaged_at_ns=self.clock.now_ns(), error=error)
        if command.command_id not in latch.stands_in_for:
            latch.stands_in_for.append(command.command_id)
            latch.pending.append(command)
        cancels = self._best_effort_cancels()
        latch.cancels.extend(cancels)
        # The evidence travels as its own SYSTEM stop, recorded after the stops it stands in for. It is rebuilt
        # whenever the latch grows, so one record carries everything.
        latch.pending = [c for c in latch.pending if not c.command_id.startswith("sys.memory-latch.")]
        latch.pending.append(Command(f"sys.memory-latch.{latch.engaged_at_ns}.{len(latch.stands_in_for)}", "stop",
                                     Principal(f"system.{self.deployment_id}", Role.SYSTEM),
                                     {"account_key": self.account_key, "reason": "MEMORY_LATCH_PERSISTED",
                                      "memory_latch": {"engaged_at_ns": latch.engaged_at_ns, "error": latch.error,
                                                       "reason": latch.reason, "stands_in_for": list(latch.stands_in_for),
                                                       "cancels": list(latch.cancels)}}))
        self._write_latch_file()
        log.critical("RED STOP NOT RECORDED: the database refused stop %s (%s). The stop is latched IN MEMORY: no "
                     "entry is transmitted, %d best-effort cancel(s) went to the broker, and the stop is recorded as soon "
                     "as the database accepts writes.", command.command_id, error, len(cancels))
        body = {"stop_latched": True, "persisted": False, "error": error, "cancels": cancels,
                "memory_latch": latch.view(),
                "note": "STOP LATCHED IN MEMORY ONLY: the database did not record it; entries are blocked in this "
                        "process and open buys were canceled at the broker; it is recorded when the database recovers"}
        return {"accepted": True, "duplicate": False, "persisted": False, "body": body, "events": [],
                "persist_error": error}

    def _best_effort_cancels(self) -> list[dict]:
        """Cancel every open buy straight at the broker: those the ledger knows (if it can still be read) and those
        the broker lists as ours (client order id set; manual and foreign orders are never touched)."""
        targets: dict[str, str | None] = {}
        try:
            for r in self.desk.store.query(
                    f"SELECT row FROM orders WHERE account_key=? AND side='BUY' AND state IN ({','.join('?' * len(OPEN_BUY_STATES))})",
                    (self.account_key, *OPEN_BUY_STATES)):
                o = json.loads(r["row"])
                targets[o["order_id"]] = o.get("broker_order_id")
        except Exception:
            log.exception("memory latch: the ledger's open buys cannot be read; using the broker's open orders only")
        try:
            rows, _complete = self.broker.open_orders()
            for row in rows:
                if row.side == "BUY" and row.client_order_id:
                    targets.setdefault(row.client_order_id, row.broker_order_id)
        except Exception:
            log.exception("memory latch: the broker's open orders cannot be read")
        out = []
        for cid, bid in sorted(targets.items()):
            try:
                res = self.broker.cancel(cid, bid)
                out.append({"client_order_id": cid, "status": res.status.value, "reason": res.reason})
            except Exception as exc:
                out.append({"client_order_id": cid, "status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"})
        return out

    def _write_latch_file(self) -> None:
        latch = self.memory_latch
        if latch is None:
            return
        data = {"account_key": self.account_key, "reason": latch.reason, "engaged_at_ns": latch.engaged_at_ns,
                "error": latch.error, "stands_in_for": latch.stands_in_for, "cancels": latch.cancels,
                "pending": [_command_json(c) for c in latch.pending]}
        try:
            tmp = self.latch_path.with_suffix(".json.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.latch_path)
            dir_fd = os.open(self.latch_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            log.critical("memory latch: its file %s could not be written either; the latch holds in this process only",
                         self.latch_path, exc_info=True)

    def _load_latch_file(self) -> None:
        """At start: a stop latched in memory by an earlier process that died before recording it."""
        path = self.latch_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            pending = [_command_from_json(c) for c in data["pending"]]
            if data.get("account_key") != self.account_key or not pending or not all(self._latchable(c) for c in pending):
                # The file is replayed as commands: anything but stops of this account is never executed from it.
                raise ValueError("the latch file holds something other than stops of this account")
            self.memory_latch = MemoryLatch(reason=data["reason"], engaged_at_ns=data["engaged_at_ns"], error=data["error"],
                                            pending=pending, cancels=list(data.get("cancels", [])),
                                            stands_in_for=list(data.get("stands_in_for", [])))
        except Exception as exc:
            # Unreadable: fail closed with a stop of our own.
            command = Command(f"sys.stop.latch-file.{self.clock.now_ns()}", "stop",
                              Principal(f"system.{self.deployment_id}", Role.SYSTEM),
                              {"account_key": self.account_key, "reason": "UNREADABLE_STOP_LATCH_FILE"})
            self.memory_latch = MemoryLatch(reason="UNREADABLE_STOP_LATCH_FILE", engaged_at_ns=self.clock.now_ns(),
                                            error=f"{type(exc).__name__}: {exc}", pending=[command],
                                            stands_in_for=[command.command_id])
        log.critical("an earlier process latched a stop in memory that the database never recorded (%s); this process "
                     "starts latched and records it as soon as it can", path)

    def _persist_memory_latch(self) -> bool:
        """Record every pending stop, in order; release the memory latch only when all are recorded. True if no
        memory latch remains."""
        latch = self.memory_latch
        if latch is None:
            return True
        while latch.pending:
            command = latch.pending[0]
            try:
                result = self.desk.execute(command)
            except Exception as exc:
                try:
                    started = self.clock.now_ns()
                    result = self._stored_after_failure(command, started, exc)
                except Exception:
                    result = None
                if result is None:
                    latch.last_persist_error = f"{type(exc).__name__}: {exc}"
                    if time.monotonic() - self._last_latch_alarm >= LATCH_ALARM_INTERVAL_S:
                        self._last_latch_alarm = time.monotonic()
                        log.critical("RED STOP STILL NOT RECORDED (%s): latched in memory since %s; entries stay "
                                     "blocked", latch.last_persist_error, latch.engaged_at_ns)
                    return False
            if not result.accepted:
                # The desk decided this stop and recorded the refusal (e.g. UNKNOWN_ACCOUNT): it is recorded, so it
                # no longer needs the memory latch. Waiting for it to be accepted would hold the latch forever (and
                # refuse every green start); the SYSTEM stop that follows records the latch itself.
                log.critical("stop %s was recorded REJECTED (%s); it is recorded, so the memory latch no longer "
                             "stands in for it", command.command_id, result.body)
            latch.pending.pop(0)
        self.memory_latch = None
        try:
            self.latch_path.unlink(missing_ok=True)
        except OSError:
            log.exception("memory latch recorded, but its file could not be removed: %s", self.latch_path)
        log.warning("the stop latched in memory since %s is now recorded in the database (%s)", latch.engaged_at_ns,
                    ", ".join(latch.stands_in_for))
        return True

    def _system_stop(self, reason: str) -> None:
        """The loop's own stop (repeated cycle failures). A database that cannot record it latches in memory."""
        command = Command(self.desk.ids.new("sys.stop."), "stop", self.desk.system(),
                          {"account_key": self.account_key, "reason": reason})
        started = self.clock.now_ns()
        try:
            self.desk.execute(command)
        except Exception as exc:
            try:
                stored = self._stored_after_failure(command, started, exc)
            except Exception:
                stored = None
            if stored is None:
                self._latch_in_memory(command, exc)

    # ---------------------------------------------------------------- socket
    def _enqueue(self, req: dict, roles: frozenset[str]) -> dict:
        fut: Future = Future()
        try:
            self.requests.put_nowait((req, roles, fut))
        except queue.Full:
            raise RuntimeError("core busy: request queue full") from None   # not queued: never executed
        try:
            return fut.result(timeout=self.request_wait_s)
        except FutureTimeout:
            # The request stays queued and may still run: its outcome is unknown, never "not executed".
            raise OutcomeUnknown(f"the core did not finish {describe_request(req)} within {self.request_wait_s:g} s; "
                                 "it is still queued and may yet run. Query its command id before resending.") from None

    def broker_provider(self) -> str | None:
        try:
            return self.broker.capabilities().provider
        except Exception:
            return None

    def health(self) -> HealthSnapshot:
        s = self.broker.session()
        free_mb = shutil.disk_usage(self.paths.data).free // (1024 * 1024)
        try:
            mode = self.broker.capabilities().account_mode.value
        except Exception:   # e.g. IbkrNotReady before the PAPER identity is verified: unknown, never assumed
            mode = None
        return HealthSnapshot(s.connected, s.authenticated, s.server_time_ns, free_mb, s.detail, mode)

    def _handle(self, req: dict, roles: frozenset[str]) -> dict:
        desk = self.desk
        assert desk is not None
        if req.get("op") == "command":
            if self.memory_latch is not None:
                self._persist_memory_latch()   # record the stop first, whenever the database accepts writes again
            p = req.get("principal") or {}
            role = p.get("role")
            if role not in roles:
                raise PermissionError(f"token may not assert role {role}")
            if self.memory_latch is not None and _increases_exposure(req):
                raise PermissionError(f"refused: a stop is latched in memory and not yet recorded "
                                      f"({self.memory_latch.last_persist_error or self.memory_latch.error}); "
                                      "exposure-increasing commands wait until the database records it")
            bound = getattr(roles, "principal_id", None)
            if role == Role.STRATEGY.value and bound is None:
                raise PermissionError("STRATEGY may be asserted only with a token bound to one artifact")
            if bound is not None and str(p.get("id")) != bound:
                raise PermissionError(f"this token may assert only principal {bound}")
            if req.get("kind") == "broker_admin_action" and (req.get("payload") or {}).get("account_key") != self.account_key:
                raise ValueError(f"broker_admin_action must name this core's account ({self.account_key})")
            principal = Principal(str(p.get("id")), Role(role), p.get("authenticated_at_ns"))
            command = Command(req["command_id"], req["kind"], principal, req.get("payload") or {})
            started_ns = self.clock.now_ns()
            post_commit_error = None
            try:
                result = desk.execute(command)
            except CommandConflict:
                raise   # decided before this command wrote anything
            except Exception as exc:
                latchable = self._latchable(command)
                try:
                    stored = self._stored_after_failure(command, started_ns, exc)
                except OutcomeUnknown:
                    if not latchable:
                        raise
                    stored = None   # a stop is latched in memory either way; recording it again later is idempotent
                if stored is None:
                    if latchable:
                        return self._latch_in_memory(command, exc)   # a red stop is never blocked by persistence
                    raise   # rolled back (or refused before executing): the command was not executed
                result, post_commit_error = stored, f"{type(exc).__name__}: {exc}"
            body = result.body
            if req["kind"] == "broker_admin_action" and result.accepted and not result.duplicate:
                body = {**body, "adapter": self._apply_broker_admin(body, principal.id)}
            reply = {"accepted": result.accepted, "duplicate": result.duplicate, "body": body, "events": result.events}
            if post_commit_error is not None:
                reply["post_commit_error"] = post_commit_error
            return reply
        if req.get("op") == "query":
            name = req.get("name")
            if name == "health":
                h = self.health()
                return {"broker_connected": h.broker_connected, "broker_authenticated": h.broker_authenticated,
                        "broker_server_time_ns": h.broker_server_time_ns, "disk_free_mb": h.disk_free_mb,
                        "detail": h.detail, "now_ns": self.clock.now_ns(), "cycle_errors": self.cycle_errors,
                        "provider": self.broker_provider(), "account_mode": h.broker_account_mode,
                        "memory_latch": self.memory_latch.view() if self.memory_latch is not None else None,
                        "event_journal": self.journal.status() if self.journal is not None else None}
            if name == "preflight":
                if "OWNER" not in roles:
                    raise PermissionError("preflight dry run requires the owner token")
                auth = (req.get("args") or {}).get("authenticated_at_ns")
                with desk.store.read() as tx:
                    ctx = Context(desk, tx, Command("dry-run", "start", Principal("owner", Role.OWNER, auth), {}), "dry-run")
                    blockers, evidence = control_handlers.preflight(ctx, self.account_key)
                    progress = control_handlers.stop_progress(ctx, self.account_key)
                if self.memory_latch is not None:
                    blockers = sorted(set(blockers) | {"STOP_LATCHED_IN_MEMORY"})
                    progress = {**progress, "entries_blocked": True, "memory_latch": self.memory_latch.view()}
                return {"blockers": blockers, "evidence": evidence, "stop_progress": progress}
            if name == "quotes":
                return {k: q.to_json(self.clock.now_ns()) for k, q in desk.quotes.snapshot().items()}
            if name == "broker_alerts":
                if "OWNER" not in roles:
                    raise PermissionError("broker alerts require the owner token")
                anomalies = getattr(self.broker, "anomalies", None)
                blocks = getattr(self.broker, "entry_blocks", None)
                return {"anomalies": anomalies() if anomalies else [], "entry_blocks": blocks() if blocks else {}}
            if name == "command_result":
                # Answered on the loop thread, after every request queued before it (the queue is FIFO), so
                # "not found" here means the core never received the command or refused it before storing it.
                if "OWNER" not in roles:
                    raise PermissionError("command results require the owner token")
                args = req.get("args") or {}
                with desk.store.read() as tx:   # a primary-key read: cheap on the loop thread
                    found = stored_command(tx.conn, str(args.get("command_id")), principal_id=str(args.get("principal_id")),
                                           principal_role="OWNER")
                return {"found": found is not None, "command": found}
            raise ValueError(f"unknown query {name}")
        raise ValueError("op must be command or query")

    def _stored_after_failure(self, command: Command, started_ns: int, exc: Exception) -> Result | None:
        """The stored result of ``command`` after ``desk.execute`` raised ``exc``, or None if it is not stored.

        A failure after the commit (``Store.write`` writes the restore marker after every commit) leaves the
        command executed and recorded; reporting it as an error would tell the client "not executed", and a
        client that resends under a new id would execute it twice. The row must match this principal and
        payload: another command holding the id (a conflict) is not this one's result. Raises OutcomeUnknown
        when the store cannot be read to tell."""
        try:
            payload_hash = sha256_hex(canonical_json({"kind": command.kind, "payload": command.payload}))
        except Exception:
            return None   # Desk.execute hashes the payload (this same way) before any write: nothing was stored
        store = self.desk.store
        try:
            if store.conn.in_transaction:
                raise RuntimeError("the writer connection is still inside a transaction")
            rows = store.query("SELECT principal_id, principal_role, payload_hash, status, result, recorded_time_ns "
                               "FROM commands WHERE command_id=?", (command.command_id,))
        except Exception as read_exc:
            log.error("command %s failed (%s: %s) and the store cannot be read to tell whether it was recorded: %s",
                      command.command_id, type(exc).__name__, exc, read_exc)
            raise OutcomeUnknown(f"the core failed {command.kind} {command.command_id} ({type(exc).__name__}: {exc}) and "
                                 f"cannot read its store to tell whether it was recorded ({type(read_exc).__name__}: "
                                 f"{read_exc}). Query its command id before resending.") from exc
        r = rows[0] if rows else None
        if r is None or (r["principal_id"], r["principal_role"], r["payload_hash"]) != (
                command.principal.id, command.principal.role.value, payload_hash):
            return None
        duplicate = r["recorded_time_ns"] < started_ns   # stored before this request began: an earlier execution
        events: list[int] = []
        if not duplicate:
            try:
                events = command_events(store.conn, command.command_id, principal_id=command.principal.id)
            except Exception as events_exc:   # the result stands; its event seqs stay readable by command id
                log.error("command %s: event seqs unreadable after the failure: %s", command.command_id, events_exc)
        log.error("command %s was recorded (%s) but the core then failed: %s: %s", command.command_id, r["status"],
                  type(exc).__name__, exc, exc_info=exc)
        return Result(command.command_id, r["status"] == "ACCEPTED", duplicate, json.loads(r["result"]), events)

    def _apply_broker_admin(self, body: dict, by: str) -> dict:
        """Apply an owner broker-admin action that the core has just recorded (never on a duplicate)."""
        action, args = body["action"], body["args"]
        fn = getattr(self.broker, action, None)
        if fn is None:
            return {"applied": False, "reason": f"this broker adapter has no {action}"}
        note = f"core-recorded owner action ({by})"
        try:
            if action == "clear_entry_block":
                return {"applied": bool(fn(args["name"], by=by, note=note))}
            from datetime import date
            fn(date.fromisoformat(args["through_day"]), by=by, note=note)
            return {"applied": True}
        except Exception as exc:
            log.exception("broker admin action %s failed", action)
            return {"applied": False, "reason": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- loop
    def _heartbeat_if_due(self) -> None:
        """Refresh the writer lease once ``heartbeat_s`` has passed, whatever the cycle is doing.

        A failing or slow cycle is still the live writer, so the heartbeat runs before the cycle's
        work and between the commands of a long drain, pump or reconciliation, never only after a
        successful cycle; a second instance therefore cannot take the lease without --takeover
        (ADR-0011 #1, review finding 57). Raises LeaseLost once another writer holds the lease."""
        if time.monotonic() - self._last_heartbeat >= self.intervals.heartbeat_s:
            self.desk.store.heartbeat()
            self._last_heartbeat = time.monotonic()

    def _drain_requests(self, budget: int = 50) -> None:
        for _ in range(budget):
            try:
                req, roles, fut = self.requests.get_nowait()
            except queue.Empty:
                return
            try:
                fut.set_result(self._handle(req, roles))
            except Exception as exc:
                fut.set_exception(exc)
            self._heartbeat_if_due()

    def _fail_pending_requests(self, reason: str) -> None:
        while True:
            try:
                _, _, fut = self.requests.get_nowait()
            except queue.Empty:
                return
            fut.set_exception(RuntimeError(reason))

    def _supervise_connection(self) -> None:
        """Reconnect an adapter that owns a socket (IBKR) with capped exponential backoff. Never automates a
        broker login: a host that is not logged in stays AUTH_REQUIRED until the owner logs in (ADR-0012)."""
        connect = getattr(self.broker, "connect", None)
        if connect is None:
            return
        s = self.broker.session()
        was_connected = getattr(self, "_was_connected", False)
        self._was_connected = s.connected
        if s.connected and s.authenticated:
            self._reconnect_backoff_s = RECONNECT_MIN_S
            return
        if (s.auth_required_reason or "").startswith("DISCONNECTED_UNSAFE"):
            return   # identity mismatch is latched in the adapter; only the owner can clear it
        if s.connected:
            return   # socket up but not ready (e.g. AUTH_REQUIRED): reconnecting cannot help; the adapter finishes late
        now = time.monotonic()
        if was_connected:
            # A fresh disconnect: try promptly, whatever backoff an earlier outage had reached.
            self._next_reconnect_at, self._reconnect_backoff_s = 0.0, RECONNECT_MIN_S
        if now < getattr(self, "_next_reconnect_at", 0.0):
            return
        log.info("broker not connected (%s); connecting", s.auth_required_reason)
        try:
            connect()
        except Exception:
            log.exception("broker connect failed")
        backoff = getattr(self, "_reconnect_backoff_s", RECONNECT_MIN_S)
        self._next_reconnect_at = now + backoff
        self._reconnect_backoff_s = min(backoff * 2, RECONNECT_MAX_S)

    def refresh_instrument_map(self, provider: str) -> None:
        """Copy provider mappings into a dict the adapter's message thread can read without SQLite."""
        rows = self.desk.store.query("SELECT m.external_id, m.instrument_id, m.version, i.body FROM provider_mappings m "
                                     "JOIN instruments i ON i.instrument_id = m.instrument_id WHERE m.provider=? "
                                     "ORDER BY m.version", (provider,))
        self.instrument_map = {r["external_id"]: r["instrument_id"] for r in rows}
        # For market-data subscriptions: {external id: (instrument id, trading currency)}.
        self.instrument_details = {r["external_id"]: (r["instrument_id"], json.loads(r["body"])["currency"]) for r in rows}

    def _sync_connectivity(self) -> None:
        """Record broker connectivity only when it changes (a change forces reconciliation)."""
        self._supervise_connection()
        if self.broker_mapping_provider:
            self.refresh_instrument_map(self.broker_mapping_provider)
        s = self.broker.session()
        observed = (s.connected, s.authenticated)
        if observed != getattr(self, "_last_connectivity", None):
            self.desk.run_system("broker_connectivity", {"account_key": self.account_key, "connected": s.connected,
                                                         "authenticated": s.authenticated, "detail": s.detail})
            self._last_connectivity = observed

    def open_desk(self) -> Desk:
        """Open this service's core database with every sourced calendar (config/calendars) and its quote book."""
        self.paths.ensure()
        ids = IdGenerator(lambda: self.clock.now_ns() // 1_000_000)
        return open_desk(self.paths.db(self.account_key), self.clock, ids, deployment_id=self.deployment_id,
                         health=self.health, calendars=load_calendars(CALENDAR_DIR), quotes=self.quotes)

    def _replay_journal(self, runner: GatewayRunner) -> None:
        """Before any request or new broker event, apply what an earlier process journaled but did not
        apply, in journal order (BR-ORD-20). A failure is logged; the loop's drains retry it and fail closed."""
        try:
            if self.desk.store.query("SELECT 1 FROM accounts WHERE account_key=?", (self.account_key,)):
                n = runner.replay_journal()
                if n:
                    log.info("applied %d journaled broker events left unapplied by an earlier process", n)
        except Exception:
            log.exception("replaying the broker event journal failed; the loop retries it")

    def run(self) -> None:
        self.desk = self.open_desk()
        self._load_latch_file()   # a stop an earlier process latched in memory but never recorded (BR-SES-13)
        if self.fixture_market is not None:
            self.fixture_market.quotes = self.desk.quotes
        # Every broker event is journaled before it is applied; the reconciler's runner uses the same journal.
        self.journal = EventJournal(self.paths.data / f"{self.account_key}.events", self.account_key, self.clock.now_ns)
        attach_journal(self.broker, self.journal)
        tokens = {self.paths.token(name, create=True): roles for name, roles in SOCKET_ROLES.items()}
        tokens.update({token: Grant({"STRATEGY"}, artifact) for artifact, token in self.paths.strategy_tokens().items()})
        self.server = CoreSocketServer(self.paths.socket, tokens, self._enqueue)
        self.server.start()
        runner = self.make_runner()
        reconciler = Reconciler(self.desk, self.broker, self.account_key, heartbeat=self._heartbeat_if_due)
        self._replay_journal(runner)
        last_val = last_rec = 0.0
        self.ready.set()
        log.info("core started: account=%s deployment=%s epoch=%s", self.account_key, self.deployment_id, self.desk.store.epoch)
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                if self.memory_latch is not None:
                    self._persist_memory_latch()   # first thing each cycle: record the stop once writes work again
                self._heartbeat_if_due()  # before the work: a cycle that fails below still keeps the lease
                self._drain_requests()
                registered = bool(self.desk.store.query("SELECT 1 FROM accounts WHERE account_key=?", (self.account_key,)))
                if registered:
                    self._sync_connectivity()
                    if self.fixture_market is not None:
                        self.fixture_market.step()
                    runner.pump()
                    now = time.monotonic()
                    ctl = json.loads(self.desk.store.query("SELECT row FROM control WHERE account_key=?", (self.account_key,))[0]["row"])
                    policy_active = bool(self.desk.store.query("SELECT 1 FROM policy_active WHERE account_key=?", (self.account_key,)))
                    if policy_active and (now - last_rec >= self.intervals.reconcile_s or
                                          (ctl["needs_reconciliation"] and now - last_rec >= self.intervals.reconcile_retry_s)):
                        reconciler.run()
                        last_rec = now
                    if policy_active and now - last_val >= self.intervals.valuation_s:
                        runner.tick()
                        last_val = now
                self.cycle_errors = 0
            except LeaseLost:
                # Another process took the lease (--takeover): every write here is fenced. Stop instead of
                # running on as a zombie that still holds the broker connection (ADR-0011 #1, #4).
                self.lease_lost = True
                log.critical("writer lease epoch %s lost to another process; this core stops", self.desk.store.epoch)
                break
            except Exception:
                self.cycle_errors += 1
                log.exception("core cycle failed (%d consecutive)", self.cycle_errors)
                if self.cycle_errors in (3, 10, 100):
                    try:
                        self._system_stop(f"CORE_CYCLE_FAILURES:{self.cycle_errors}")
                    except Exception:
                        log.exception("could not latch stop after repeated cycle failures")
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.0, self.intervals.loop_s - elapsed))
        self.server.shutdown()
        self.server.server_close()
        self._fail_pending_requests("core stopped")
        detach_journal(self.broker)
        self.journal.close()   # unapplied entries stay in the file for the next process
        try:
            self.desk.store.release_lease()
        except LeaseLost:
            pass  # the lease belongs to another writer now; there is nothing of ours to release
        self.desk.store.close()

    def shutdown(self) -> None:
        self.stop_event.set()


def _simulated_broker(clock: Clock, cash: str):
    from ..broker.simulated import SimulatedBroker
    from ..domain.fixed import Fixed
    return SimulatedBroker(clock, initial_settled_cash=Fixed.parse(cash), calendar=WEEKDAYS_ONLY, settlement_days=1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Project Unknown core service (single writer)")
    ap.add_argument("--account", required=True, help="internal account key, e.g. SIM-1 or IBKR-PAPER-1")
    ap.add_argument("--deployment", required=True, help="deployment id, e.g. mac-dev or aws-paper")
    ap.add_argument("--broker", choices=["simulated"], default="simulated",
                    help="this entry point runs the simulator; the IBKR PAPER service is "
                         "'python -m unknown.broker.ibkr.serve' (the core never imports the GPL adapter, ADR-0014)")
    ap.add_argument("--fixture-market", action="store_true", help="run the deterministic FIXTURE market (simulated broker only)")
    ap.add_argument("--sim-cash", default="300.00")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()
    clock = SystemClock()
    broker = _simulated_broker(clock, args.sim_cash)
    market = None
    if args.fixture_market:
        from ..domain.fixed import Fixed
        from ..market.quotes import QuoteBook
        from .fixture_market import FixtureInstrument, FixtureMarket
        market = FixtureMarket(clock, broker, QuoteBook(), [
            FixtureInstrument("FIX-EQ-1", Fixed.parse("10.00"), Fixed.parse("0.02"), Fixed.parse("0.01")),
            FixtureInstrument("FIX-EQ-2", Fixed.parse("42.00"), Fixed.parse("0.04"), Fixed.parse("0.01")),
        ])
    svc = CoreService(paths=paths, account_key=args.account, deployment_id=args.deployment, broker=broker, clock=clock,
                      fixture_market=market)
    serve(svc)


def serve(svc: "CoreService") -> None:
    """Run a configured service until SIGTERM/SIGINT (shared by every broker entry point)."""
    signal.signal(signal.SIGTERM, lambda *_: svc.shutdown())
    signal.signal(signal.SIGINT, lambda *_: svc.shutdown())
    svc.run()


if __name__ == "__main__":
    main()
