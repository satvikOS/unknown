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
from dataclasses import dataclass
from pathlib import Path

from ..broker.port import BrokerPort
from ..core.desk import Command, Context, Desk, HealthSnapshot, open_desk
from ..core.handlers import control as control_handlers
from ..domain.calendar import WEEKDAYS_ONLY
from ..domain.clock import Clock, SystemClock, NS_PER_S
from ..domain.errors import LeaseLost
from ..domain.ids import IdGenerator
from ..domain.principal import Principal, Role
from ..gateway.runner import GatewayRunner
from ..reconcile.reconciler import Reconciler
from .ipc import CoreSocketServer
from .paths import Paths

log = logging.getLogger("unknown.core")

SOCKET_ROLES = {"api": frozenset({"OWNER"}), "agent": frozenset({"AGENT"})}


@dataclass
class Intervals:
    loop_s: float = 0.25
    valuation_s: float = 2.0
    reconcile_s: float = 60.0
    reconcile_retry_s: float = 5.0
    heartbeat_s: float = 10.0


class CoreService:
    def __init__(self, *, paths: Paths, account_key: str, deployment_id: str, broker: BrokerPort,
                 clock: Clock | None = None, intervals: Intervals | None = None, fixture_market=None):
        self.paths = paths
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
        self.lease_lost = False
        self._last_heartbeat = float("-inf")

    # ---------------------------------------------------------------- socket
    def _enqueue(self, req: dict, roles: frozenset[str]) -> dict:
        fut: Future = Future()
        try:
            self.requests.put_nowait((req, roles, fut))
        except queue.Full:
            raise RuntimeError("core busy: request queue full") from None
        return fut.result(timeout=30)

    def health(self) -> HealthSnapshot:
        s = self.broker.session()
        free_mb = shutil.disk_usage(self.paths.data).free // (1024 * 1024)
        return HealthSnapshot(s.connected, s.authenticated, s.server_time_ns, free_mb, s.detail,
                              self.broker.capabilities().account_mode.value)

    def _handle(self, req: dict, roles: frozenset[str]) -> dict:
        desk = self.desk
        assert desk is not None
        if req.get("op") == "command":
            p = req.get("principal") or {}
            role = p.get("role")
            if role not in roles:
                raise PermissionError(f"token may not assert role {role}")
            principal = Principal(str(p.get("id")), Role(role), p.get("authenticated_at_ns"))
            result = desk.execute(Command(req["command_id"], req["kind"], principal, req.get("payload") or {}))
            return {"accepted": result.accepted, "duplicate": result.duplicate, "body": result.body, "events": result.events}
        if req.get("op") == "query":
            name = req.get("name")
            if name == "health":
                h = self.health()
                return {"broker_connected": h.broker_connected, "broker_authenticated": h.broker_authenticated,
                        "broker_server_time_ns": h.broker_server_time_ns, "disk_free_mb": h.disk_free_mb,
                        "detail": h.detail, "now_ns": self.clock.now_ns(), "cycle_errors": self.cycle_errors,
                        "provider": self.broker.capabilities().provider, "account_mode": self.broker.capabilities().account_mode.value}
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
            raise ValueError(f"unknown query {name}")
        raise ValueError("op must be command or query")

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

    def _sync_connectivity(self) -> None:
        """Record broker connectivity only when it changes (a change forces reconciliation)."""
        s = self.broker.session()
        observed = (s.connected, s.authenticated)
        if observed != getattr(self, "_last_connectivity", None):
            self.desk.run_system("broker_connectivity", {"account_key": self.account_key, "connected": s.connected,
                                                         "authenticated": s.authenticated, "detail": s.detail})
            self._last_connectivity = observed

    def run(self) -> None:
        self.paths.ensure()
        ids = IdGenerator(lambda: self.clock.now_ns() // 1_000_000)
        self.desk = open_desk(self.paths.db(self.account_key), self.clock, ids, deployment_id=self.deployment_id,
                              health=self.health, calendars={"WEEKDAYS": WEEKDAYS_ONLY})
        if self.fixture_market is not None:
            self.fixture_market.quotes = self.desk.quotes
        tokens = {self.paths.token(name, create=True): roles for name, roles in SOCKET_ROLES.items()}
        self.server = CoreSocketServer(self.paths.socket, tokens, self._enqueue)
        self.server.start()
        runner = GatewayRunner(self.desk, self.broker, self.account_key, heartbeat=self._heartbeat_if_due)
        reconciler = Reconciler(self.desk, self.broker, self.account_key, heartbeat=self._heartbeat_if_due)
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
                    help="broker adapter; 'ibkr' is added with the IBKR paper adapter (I2)")
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
    signal.signal(signal.SIGTERM, lambda *_: svc.shutdown())
    signal.signal(signal.SIGINT, lambda *_: svc.shutdown())
    svc.run()


if __name__ == "__main__":
    main()
