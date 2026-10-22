"""``unknown-core``: the single-writer service process (ADR-0002, ADR-0013).

All database writes happen on the service thread that opened the desk. The
command socket enqueues requests; the loop executes them between broker pumps,
so socket clients never touch the SQLite connection. The loop keeps the
application alive through recoverable failures (OPS-03): a broker outage is
recorded as connectivity and blocks entries through reconciliation; a failing
cycle is logged and retried; nothing here clears a stop."""
from __future__ import annotations

import argparse
import json
import logging
import queue
import shutil
import signal
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

from ..broker.port import BrokerPort
from ..api.queries import command_events, stored_command
from ..core.desk import Command, Context, Desk, HealthSnapshot, Result, open_desk
from ..core.handlers import control as control_handlers
from ..domain.calendar import WEEKDAYS_ONLY, load_calendars
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.clock import Clock, SystemClock, NS_PER_S
from ..domain.errors import CommandConflict, LeaseLost
from ..domain.ids import IdGenerator
from ..domain.principal import Principal, Role
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
        self.journal: EventJournal | None = None   # <data>/<account>.events, opened by run() (BR-ORD-20)

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
            p = req.get("principal") or {}
            role = p.get("role")
            if role not in roles:
                raise PermissionError(f"token may not assert role {role}")
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
                stored = self._stored_after_failure(command, started_ns, exc)
                if stored is None:
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
                        "event_journal": self.journal.status() if self.journal is not None else None}
            if name == "preflight":
                if "OWNER" not in roles:
                    raise PermissionError("preflight dry run requires the owner token")
                auth = (req.get("args") or {}).get("authenticated_at_ns")
                with desk.store.read() as tx:
                    ctx = Context(desk, tx, Command("dry-run", "start", Principal("owner", Role.OWNER, auth), {}), "dry-run")
                    blockers, evidence = control_handlers.preflight(ctx, self.account_key)
                    progress = control_handlers.stop_progress(ctx, self.account_key)
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
        if self.fixture_market is not None:
            self.fixture_market.quotes = self.desk.quotes
        # Every broker event is journaled before it is applied; the reconciler's runner uses the same journal.
        self.journal = EventJournal(self.paths.data / f"{self.account_key}.events", self.account_key, self.clock.now_ns)
        attach_journal(self.broker, self.journal)
        tokens = {self.paths.token(name, create=True): roles for name, roles in SOCKET_ROLES.items()}
        tokens.update({token: Grant({"STRATEGY"}, artifact) for artifact, token in self.paths.strategy_tokens().items()})
        self.server = CoreSocketServer(self.paths.socket, tokens, self._enqueue)
        self.server.start()
        runner = GatewayRunner(self.desk, self.broker, self.account_key, heartbeat=self._heartbeat_if_due,
                               journal=self.journal)
        reconciler = Reconciler(self.desk, self.broker, self.account_key, heartbeat=self._heartbeat_if_due)
        self._replay_journal(runner)
        last_val = last_rec = 0.0
        self.ready.set()
        log.info("core started: account=%s deployment=%s epoch=%s", self.account_key, self.deployment_id, self.desk.store.epoch)
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
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
                        self.desk.run_system("stop", {"account_key": self.account_key,
                                                      "reason": f"CORE_CYCLE_FAILURES:{self.cycle_errors}"})
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
