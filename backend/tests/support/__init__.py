"""Deterministic test harness. Every number here is a TEST FIXTURE, not market data."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from unknown.broker.simulated import SimulatedBroker
from unknown.core.desk import Command, Desk, HealthSnapshot, open_desk
from unknown.domain.calendar import WEEKDAYS_ONLY
from unknown.domain.clock import ManualClock, utc_ns
from unknown.domain.fixed import Fixed
from unknown.domain.ids import IdGenerator, SequenceRandom
from unknown.domain.instruments import AssetClass, Instrument, TickRule
from unknown.domain.principal import Principal, Role
from unknown.market.quotes import Quote
from unknown.gateway.runner import GatewayRunner
from unknown.reconcile.reconciler import Reconciler

REPO = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO / "config" / "policies" / "simulated-default.v1.json"
ACCOUNT = "SIM-1"
DEPLOYMENT = "mac-dev"
# Tuesday 2026-10-20 14:00 UTC = 10:00 New York (regular session); fixture time only.
T0 = utc_ns(2026, 10, 20, 14, 0, 0)

FIXTURE_INSTRUMENT = Instrument(
    instrument_id="FIX-EQ-1", asset_class=AssetClass.EQUITY, symbol="FIXA", name="Fixture Equity A (test only)",
    currency="USD", settlement_currency="USD", venue_mic="XNAS", quantity_increment=Fixed(1), min_quantity=Fixed(1),
    multiplier=Fixed(1), settlement_days=1,
    tick_rules=(TickRule(Fixed(0), Fixed.parse("0.0001")), TickRule(Fixed(1), Fixed.parse("0.01"))),
    attributes={"fixture": True})


def load_policy(**overrides) -> dict:
    body = json.loads(POLICY_PATH.read_text())
    for dotted, value in overrides.items():
        target = body
        parts = dotted.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return body


class Harness:
    """A desk on a temp SQLite file wired to a SimulatedBroker and a manual clock."""

    def __init__(self, *, initial_cash: str = "300.00", policy: dict | None = None, db_path: Path | None = None,
                 broker: SimulatedBroker | None = None, clock: ManualClock | None = None, seed: int = 1):
        self.tmp = tempfile.TemporaryDirectory() if db_path is None else None
        self.db_path = db_path or Path(self.tmp.name) / "core.sqlite"
        self.clock = clock or ManualClock(T0)
        self.ids = IdGenerator(lambda: self.clock.now_ns() // 1_000_000, SequenceRandom(seed))
        self.broker = broker or SimulatedBroker(self.clock, initial_settled_cash=Fixed.parse(initial_cash),
                                                calendar=WEEKDAYS_ONLY, settlement_days=1)
        self.policy_body = policy or load_policy()
        self.desk = self._open()
        self.owner = Principal("owner", Role.OWNER, authenticated_at_ns=self.clock.now_ns())
        self.strategy = Principal("strategy.fixture", Role.STRATEGY)
        self.agent = Principal("agent.research", Role.AGENT)
        self.n = 0

    def _open(self) -> Desk:
        return open_desk(self.db_path, self.clock, self.ids, deployment_id=DEPLOYMENT, quotes=None,
                         health=self.health, calendars={"WEEKDAYS": WEEKDAYS_ONLY})

    def health(self) -> HealthSnapshot:
        s = self.broker.session()
        return HealthSnapshot(s.connected, s.authenticated, s.server_time_ns, 50_000, "simulated broker",
                              self.broker.capabilities().account_mode.value)

    @property
    def runner(self) -> GatewayRunner:
        return GatewayRunner(self.desk, self.broker, ACCOUNT)

    def restart(self) -> None:
        self.desk.store.close()
        self.desk = self._open()

    def close(self) -> None:
        self.desk.store.close()
        if self.tmp:
            self.tmp.cleanup()

    # ------------------------------------------------------------ commands
    def cmd(self, kind: str, principal: Principal | None = None, command_id: str | None = None, **payload):
        self.n += 1
        return self.desk.execute(Command(command_id or f"test.{kind}.{self.n}", kind, principal or self.owner, payload))

    def owner_cmd(self, kind: str, **payload):
        self.owner = Principal("owner", Role.OWNER, authenticated_at_ns=self.clock.now_ns())
        return self.cmd(kind, self.owner, **payload)

    def quote(self, bid: str, ask: str, instrument_id: str = "FIX-EQ-1") -> None:
        self.desk.quotes.put(Quote(instrument_id, Fixed.parse(bid), Fixed.parse(ask), None, "USD", "fixture",
                                   self.clock.now_ns(), self.clock.now_ns(), fixture=True))

    def reconcile(self):
        return Reconciler(self.desk, self.broker, ACCOUNT).run()

    def pump(self):
        return self.runner.pump()

    def tick(self):
        self.runner.tick()

    # ------------------------------------------------------------ setup
    def bootstrap(self, *, authorize: str = "300.00", start: bool = True) -> None:
        assert self.owner_cmd("register_account", account_key=ACCOUNT, provider="simulated", mode="SIMULATED",
                              base_currency="USD").accepted
        r = self.owner_cmd("publish_policy", body=self.policy_body)
        assert r.accepted, r.body
        self.policy_hash = r.body["policy_hash"]
        assert self.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=self.policy_hash).accepted
        assert self.owner_cmd("register_instrument", instrument=FIXTURE_INSTRUMENT.to_json(),
                              mappings=[{"provider": "simulated", "external_id": "FIX-EQ-1"}]).accepted
        assert self.owner_cmd("acquire_execution_ownership", account_key=ACCOUNT, deployment_id=DEPLOYMENT).accepted
        self.connect()
        rec = self.reconcile()
        assert rec.accepted and rec.body["status"] == "CLEAN", rec.body
        if authorize:
            r = self.owner_cmd("authorize_capital", account_key=ACCOUNT, amount=authorize)
            assert r.accepted, r.body
        if start:
            self.quote("9.99", "10.01")
            r = self.owner_cmd("start", account_key=ACCOUNT)
            assert r.accepted, r.body

    def connect(self) -> None:
        self.desk.run_system("broker_connectivity", {"account_key": ACCOUNT, "connected": True, "authenticated": True})

    def intent(self, order_id: str, side: str, qty: str, limit: str, session_id: str, principal: Principal | None = None,
               **extra):
        sess = self.row("sessions", "session_id", session_id)
        mandate = json.loads(self.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])
        return self.cmd("submit_intent", principal or self.owner, account_key=ACCOUNT, session_id=session_id, order_id=order_id,
                        instrument_id="FIX-EQ-1", side=side, quantity=qty, limit_price=limit,
                        deadline_ns=self.clock.now_ns() + 60 * 1_000_000_000, policy_hash=mandate["policy_hash"],
                        **extra)

    # ------------------------------------------------------------ reads
    def row(self, table: str, key_col: str, key: str) -> dict | None:
        r = self.desk.store.query(f"SELECT row FROM {table} WHERE {key_col}=?", (key,))
        return json.loads(r[0]["row"]) if r else None

    def session_state(self, session_id: str) -> str:
        return self.row("sessions", "session_id", session_id)["state"]

    def sessions(self) -> list[dict]:
        return [json.loads(r["row"]) for r in self.desk.store.query("SELECT row FROM sessions ORDER BY rowid")]

    def balance(self, code: str, owner: str, ccy: str = "USD", instrument_id: str = "") -> Fixed:
        r = self.desk.store.query("SELECT amount FROM balances WHERE account_key=? AND account_code=? AND owner=? AND currency=? AND instrument_id=?",
                                  (ACCOUNT, code, owner, ccy, instrument_id))
        return Fixed.parse(r[0]["amount"]) if r else Fixed(0)

    def available(self, owner: str, ccy: str = "USD") -> Fixed:
        from unknown.core import ledger
        with self.desk.store.read() as tx:
            return ledger.available_cash(tx, ACCOUNT, owner, ccy)

    def open_exceptions(self) -> list[dict]:
        return [json.loads(r["row"]) for r in self.desk.store.query("SELECT row FROM exceptions WHERE status='OPEN'")]

    def control(self) -> dict:
        return self.row("control", "account_key", ACCOUNT)

    def verify(self) -> list[str]:
        return self.desk.verify()
