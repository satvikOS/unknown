"""Paper bootstrap (unknown.broker.ibkr.bootstrap_paper) against a real core desk: only VERIFIED contracts are
registered (with their ibkr conId mapping), ownership needs the owner's attestation, nothing starts trading,
and --dry-run sends no command."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, DEPLOYMENT, Harness  # noqa: E402
from unknown.broker.ibkr.bootstrap_paper import Listing, bootstrap, parse_universe  # noqa: E402
from unknown.broker.ibkr.contracts import Resolution  # noqa: E402
from unknown.core.desk import Command, Context  # noqa: E402
from unknown.core.handlers import control as control_handlers  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.instruments import AssetClass, Instrument, TickRule  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402


class DeskClient:
    """CoreClient stand-in that executes on a Harness desk; preflight reconciles once ownership is held."""

    def __init__(self, h: Harness):
        self.h = h
        self.sent = []

    def command(self, command_id, kind, payload, principal):
        self.sent.append(kind)
        r = self.h.desk.execute(Command(command_id, kind, Principal(principal["id"], Role(principal["role"]),
                                                                     principal["authenticated_at_ns"]), payload))
        return {"accepted": r.accepted, "duplicate": r.duplicate, "body": r.body}

    def query(self, name, **args):
        """The service's read-only preflight dry run (never a start)."""
        assert name == "preflight"
        self.h.reconcile()
        with self.h.desk.store.read() as tx:
            ctx = Context(self.h.desk, tx, Command("dry-run", "start",
                                                   Principal("owner", Role.OWNER, args["authenticated_at_ns"]), {}), "dry-run")
            blockers, _ = control_handlers.preflight(ctx, ACCOUNT)
        return {"blockers": blockers}


def verified(symbol: str, con_id: int) -> Resolution:
    inst = Instrument(instrument_id=f"US:{symbol}", asset_class=AssetClass.EQUITY, symbol=symbol, name=f"{symbol} Inc",
                      currency="USD", settlement_currency="USD", venue_mic="XNAS", quantity_increment=Fixed(1),
                      min_quantity=Fixed(1), multiplier=Fixed(1), settlement_days=1,
                      tick_rules=(TickRule(Fixed(0), Fixed.parse("0.0001")), TickRule(Fixed(1), Fixed.parse("0.01"))))
    res = Resolution("VERIFIED", None, 0, con_id=con_id)
    res.instrument = inst.to_json()
    res.mapping = {"provider": "ibkr", "external_id": str(con_id), "instrument_id": inst.instrument_id, "version": 1,
                   "verified_at_ns": 0, "details": {"primaryExchange": "NASDAQ"}}
    return res


class FakeResolver:
    def __init__(self, table):
        self.table = table

    def resolve(self, query, *, instrument_id, settlement_days):
        assert settlement_days == 1 and query.primary_exchange    # never by name, always US T+1
        return self.table[query.symbol]


class BootstrapPaperTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.client = DeskClient(self.h)
        self.resolver = FakeResolver({"AAPL": verified("AAPL", 265598),
                                      "XYZ": Resolution("AMBIGUOUS", None, 0, reasons=["2 IBKR contracts match"])})
        self.lines = []

    def run_bootstrap(self, **kw):
        args = dict(account=ACCOUNT, deployment=DEPLOYMENT, universe=[Listing("AAPL", "NASDAQ"), Listing("XYZ", "NYSE")],
                    authenticated_at_ns=self.h.clock.now_ns(), attest=True, out=self.lines.append, sleep=lambda s: None)
        args.update(kw)
        return bootstrap(self.client, self.resolver, **args)

    def test_registers_only_verified_listings_and_never_starts(self):
        report = self.run_bootstrap()
        self.assertEqual(report["registered"], ["US:AAPL"])
        self.assertEqual(report["skipped"]["US:XYZ"]["status"], "AMBIGUOUS")
        self.assertEqual(self.h.row("accounts", "account_key", ACCOUNT)["mode"], "PAPER")
        m = self.h.desk.store.query("SELECT instrument_id FROM provider_mappings WHERE provider='ibkr' AND external_id='265598'")
        self.assertEqual([r["instrument_id"] for r in m], ["US:AAPL"])
        self.assertEqual(self.h.row("execution_ownership", "account_key", ACCOUNT)["provenance"],
                         {"owner_attestation": "no other deployment trades this broker account"})
        self.assertNotIn("start", self.client.sent)            # green start stays the owner's action
        self.assertNotIn("create_session", self.client.sent)
        self.assertIn("authorize_capital", self.client.sent)
        self.assertNotIn("RECONCILIATION_REQUIRED", report["preflight_blockers"])
        self.assertEqual(self.h.verify(), [])

    def test_dry_run_sends_nothing(self):
        report = self.run_bootstrap(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.client.sent, [])

    def test_no_attestation_no_commands(self):
        with self.assertRaises(RuntimeError):
            self.run_bootstrap(attest=False)
        self.assertEqual(self.client.sent, [])

    def test_universe_must_name_primary_exchanges(self):
        self.assertEqual(parse_universe("aapl:nasdaq, JPM:NYSE"), [Listing("AAPL", "NASDAQ"), Listing("JPM", "NYSE")])
        for bad in ("AAPL", "AAPL:SMART", "SAP:XETRA", ""):
            with self.assertRaises(ValueError):
                parse_universe(bad)


if __name__ == "__main__":
    unittest.main()


class BootstrapClientIdTest(unittest.TestCase):
    def test_the_bootstrap_session_never_reuses_the_core_market_data_client_id(self):
        # The core runs first (runbook) and holds the configured id; IBKR refuses a second connection with it (326).
        from unknown.broker.ibkr.bootstrap_paper import BOOTSTRAP_CLIENT_ID, bootstrap_md_config
        from unknown.broker.ibkr.marketdata import MarketDataConfig
        core = MarketDataConfig(client_id=18)
        self.assertEqual(bootstrap_md_config(core, BOOTSTRAP_CLIENT_ID).client_id, 91)
        self.assertEqual(core.client_id, 18)
        with self.assertRaises(ValueError):
            bootstrap_md_config(core, 18)
