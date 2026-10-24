"""Read-only IBKR PAPER evidence runner (unknown.broker.ibkr.evidence) against the fake IBKR hosts of test_ibkr_check /
test_ibkr_marketdata (market-data connection) and test_ibkr_adapter (account connection, official ibapi classes,
IBKR venv). It must never place, modify or cancel an order, never press start, never write to the core, and write
its dated record only outside the repository. Accounts, conIds, amounts and times are TEST FIXTURES."""
from __future__ import annotations

import io
import json
import logging
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unknown.broker.ibkr import evidence as EV  # noqa: E402
from unknown.broker.ibkr import marketdata as MD  # noqa: E402
from unknown.broker.ibkr.config import config_from_dict  # noqa: E402
from unknown.broker.port import (AccountSnapshot, BrokerOrderStatus, BrokerSessionState, CashBalance,  # noqa: E402
                                 CommissionRow, ExecutionBatch, ExecutionRow, OpenOrderRow, PositionRow)
from unknown.domain.clock import ManualClock  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.service.paths import Paths  # noqa: E402

from test_ibkr_check import READ_ONLY, CheckHost  # noqa: E402
from test_ibkr_marketdata import ACCOUNT, CON_A, CON_B, PURE_SDK, RULE_9001, T0, base_config, details, md_config  # noqa: E402

import test_ibkr_adapter as TA  # noqa: E402

F = Fixed.parse
MD_READ_ONLY = READ_ONLY | {"reqContractDetails", "reqMarketRule"}
ACCOUNT_READ_ONLY = {"connect", "disconnect", "reqCurrentTimeInMillis", "reqAccountSummary", "cancelAccountSummary",
                     "reqPositions", "cancelPositions", "reqOpenOrders", "reqAllOpenOrders", "reqExecutions"}
ADAPTER_READS = ["connect", "account_snapshot", "open_orders", "executions_since", "disconnect"]


def setUpModule():
    logging.getLogger("unknown.broker.ibkr").setLevel(logging.CRITICAL)


class EvidenceHost(CheckHost):
    """CheckHost plus contract details: FXA and FXB resolve, conId 1 answers IBKR 200."""
    listings = {("FXA", "NASDAQ"): CON_A, ("FXB", "NYSE"): CON_B}

    def reqContractDetails(self, reqId, contract):
        self.calls.append(("reqContractDetails", reqId, contract))
        con_id = self.listings.get((contract.symbol, contract.primaryExchange))
        if contract.conId == EV.INVALID_CON_ID or con_id is None:
            self.wrapper.error(reqId, 0, 200, "No security definition has been found for the request", "")
            return
        self.wrapper.contractDetails(reqId, details(con_id=con_id, symbol=contract.symbol,
                                                    primary=contract.primaryExchange))
        self.wrapper.contractDetailsEnd(reqId)

    def reqMarketRule(self, marketRuleId):
        self.calls.append(("reqMarketRule", marketRuleId))
        self.wrapper.marketRule(marketRuleId, RULE_9001)


class FakeAccountAdapter:
    """The order adapter's read surface, recording every call (anything else would raise AttributeError)."""

    def __init__(self, cfg, *, authenticated=True):
        self.cfg, self.calls, self.authenticated = cfg, [], authenticated

    def connect(self):
        self.calls.append("connect")
        return BrokerSessionState(True, self.authenticated, None if self.authenticated else "AUTH_REQUIRED", None, None)

    def account_snapshot(self):
        self.calls.append("account_snapshot")
        return AccountSnapshot(T0, (CashBalance("USD", F("998500.00"), F("999000.00"), "fixture"),),
                               (PositionRow("INS-FXA", str(CON_A), F("10"), F("101.25"), "USD"),
                                PositionRow(None, "4242", F("3"), None, "USD")), True,
                               raw={"account_type": "INDIVIDUAL", "incomplete_reasons": []})

    def open_orders(self):
        self.calls.append("open_orders")
        return [OpenOrderRow(None, "OTHER:1:x", "ibkr:5:3:9200", None, "BUY", BrokerOrderStatus.ACKNOWLEDGED, F("0"),
                             F("5"))], True

    def executions_since(self, watermark):
        self.calls.append("executions_since")
        e = ExecutionRow("ibkr:DU**E1:0000e0d5.01.01", "0000e0d5.01.01", None, None, None, "INS-FXA", str(CON_A),
                         "BUY", F("10"), F("101.25"), "USD", T0, raw={"time": "20261022 10:31:02 America/New_York"})
        return ExecutionBatch((e,), (CommissionRow(e.exec_key, F("1.00"), "USD"),), "wm", True)

    def disconnect(self):
        self.calls.append("disconnect")


def make_core_db(paths: Paths, account: str, rows):
    """A core database with only the two tables the runner reads (registered instruments and ibkr mappings)."""
    paths.data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.db(account))
    conn.executescript("CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, body TEXT NOT "
                       "NULL, metadata_version INTEGER NOT NULL); CREATE TABLE provider_mappings (provider TEXT, "
                       "external_id TEXT, version INTEGER, instrument_id TEXT, row TEXT);")
    for instrument_id, symbol, primary, con_id in rows:
        body = {"symbol": symbol, "currency": "USD", "attributes": {"ibkr.primaryExchange": primary}}
        conn.execute("INSERT INTO instruments VALUES (?,?,?,1)", (instrument_id, symbol, json.dumps(body)))
        conn.execute("INSERT INTO provider_mappings VALUES ('ibkr',?,1,?,'{}')", (str(con_id), instrument_id))
    conn.commit()
    conn.close()


class EvidenceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.paths = Paths(self.home)
        self.clock = ManualClock(T0)
        self.cfg = config_from_dict(base_config(self.home / "state"))
        self.md = MD.market_data_config_from_dict(md_config(requested_market_data_type=3))
        self.hosts: list = []
        self.adapters: list = []
        self.lines: list[str] = []
        self.universe = [{"instrument_id": "INS-FXA", "symbol": "FXA", "currency": "USD", "primary_exchange": "NASDAQ",
                          "registered_con_id": CON_A}]

    def run_evidence(self, accounts=ACCOUNT, universe=None, adapter_factory=None, **host_attrs):
        def factory(wrapper):
            host = EvidenceHost(wrapper, accounts=accounts)
            host.clock = self.clock
            for k, v in host_attrs.items():
                setattr(host, k, v)
            self.hosts.append(host)
            return host

        def fake_adapter(cfg):
            a = FakeAccountAdapter(cfg)
            self.adapters.append(a)
            return a
        return EV.run_evidence(self.cfg, self.md, account_key="IBKR-PAPER-1",
                               universe=self.universe if universe is None else universe, universe_source="fixture",
                               sdk=PURE_SDK, md_client_factory=factory, adapter_factory=adapter_factory or fake_adapter,
                               clock=self.clock, wait_s=0.2, max_clock_skew_ms=2000, out=self.lines.append)

    @staticmethod
    def results(record) -> dict:
        return {i["id"]: i["result"] for i in record["items"]}


class EvidenceTests(EvidenceCase):
    def test_every_read_only_item_passes_and_nothing_but_reads_is_sent(self):
        record = self.run_evidence()
        r = self.results(record)
        self.assertTrue(record["ok"], self.lines)
        self.assertEqual({i: r[i] for i in EV.READ_ONLY_ITEMS}, {i: "PASS" for i in EV.READ_ONLY_ITEMS})
        self.assertEqual(set(self.hosts[0].names()) - MD_READ_ONLY, set())
        self.assertEqual(self.adapters[0].calls, ADAPTER_READS)
        self.assertEqual((self.hosts[0].clientId, self.adapters[0].cfg.client_id), (92, 93))
        self.assertNotEqual(self.adapters[0].cfg.state_dir, self.cfg.state_dir)   # never the real order-id store
        self.assertEqual((record["places_orders"], record["presses_start"], record["writes_to_core"]),
                         (False, False, False))
        items = {i["id"]: i for i in record["items"]}
        self.assertEqual(items["contracts"]["data"]["listings"][0]["matches_registration"], True)
        self.assertEqual(items["market_data_type"]["data"]["instruments"][0]["delivered_type_name"], "live")
        self.assertEqual(items["error_mapping"]["data"]["status"], "UNRESOLVED")
        self.assertEqual(items["account_snapshot"]["data"]["account_type"], "INDIVIDUAL")
        self.assertEqual(items["account_snapshot"]["data"]["unmapped_positions"], ["4242"])
        self.assertTrue(items["open_orders"]["data"]["orders"][0]["foreign"])
        self.assertEqual(items["executions"]["data"]["executions"][0]["time_text"], "20261022 10:31:02 America/New_York")
        self.assertEqual(items["commissions"]["data"]["executions_without_report"], [])
        self.assertIn("| PASS |", record["table"])

    def test_order_items_are_listed_with_owner_steps_and_never_run(self):
        record = self.run_evidence()
        owner = [i for i in record["items"] if i["result"] == "OWNER_SESSION"]
        self.assertEqual([i["id"] for i in owner], [o[0] for o in EV.OWNER_ITEMS])
        self.assertTrue(all(i["owner_steps"] for i in owner))
        self.assertTrue(any("Needs orders" in s for i in owner for s in i["owner_steps"]))
        self.assertEqual(record["summary"]["OWNER_SESSION"], len(EV.OWNER_ITEMS))

    def test_wrong_account_fails_identity_and_requests_nothing_else(self):
        record = self.run_evidence(accounts="DUOTHER1")
        r = self.results(record)
        self.assertEqual(r["identity"], "FAIL")
        self.assertEqual({r[i] for i in EV.READ_ONLY_ITEMS if i != "identity"}, {"NOT_RUN"})
        self.assertEqual(set(self.hosts[0].names()), {"connect", "disconnect"})
        self.assertEqual(self.adapters, [])
        self.assertFalse(record["ok"])

    def test_conid_changed_since_registration_fails_contracts(self):
        universe = [dict(self.universe[0], registered_con_id=CON_B)]
        record = self.run_evidence(universe=universe)
        items = {i["id"]: i for i in record["items"]}
        self.assertEqual(items["contracts"]["result"], "FAIL")
        self.assertIn(f"!= registered {CON_B}", items["contracts"]["detail"])
        self.assertEqual(items["market_data_type"]["result"], "NOT_RUN")

    def test_frozen_or_delayed_data_is_never_a_pass(self):
        self.assertEqual(self.results(self.run_evidence(md_type=2))["market_data_type"], "INCONCLUSIVE")
        self.assertEqual(self.results(self.run_evidence(md_type=3))["market_data_type"], "FAIL")

    def test_empty_universe_is_not_run_with_the_reason(self):
        record = self.run_evidence(universe=[])
        items = {i["id"]: i for i in record["items"]}
        self.assertEqual(items["contracts"]["result"], "NOT_RUN")
        self.assertIn("bootstrap_paper", items["contracts"]["detail"])
        self.assertFalse(record["ok"])

    def test_client_ids_of_other_connections_and_zero_are_refused(self):
        for kw in ({"md_client_id": self.cfg.client_id}, {"account_client_id": self.md.client_id},
                   {"md_client_id": 0}, {"md_client_id": 95, "account_client_id": 95},
                   {"reserved_client_ids": {"market-data bus source": 92}}):
            with self.assertRaises(ValueError):
                EV.run_evidence(self.cfg, self.md, account_key="IBKR-PAPER-1", universe=[], universe_source="x",
                                sdk=PURE_SDK, md_client_factory=lambda w: self.fail("socket opened"), **kw)

    def test_record_is_written_outside_git_with_mode_600_and_a_table(self):
        record = self.run_evidence()
        path = EV.write_record(record, self.home / "evidence")
        self.assertRegex(path.name, r"^ibkr-paper-\d{8}T\d{6}Z\.json$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["schema"], EV.SCHEMA)
        self.assertIn("| Result |", path.with_suffix(".md").read_text())
        self.assertNotEqual(EV.write_record(record, self.home / "evidence"), path)    # never overwrites
        with self.assertRaises(ValueError):
            EV.write_record(record, Path(__file__).resolve().parent / "evidence-never")

    def test_registered_universe_is_read_from_the_core_database(self):
        self.assertEqual(EV.registered_universe(self.paths, "IBKR-PAPER-1")[0], [])
        make_core_db(self.paths, "IBKR-PAPER-1", [("US:FXA", "FXA", "NASDAQ", CON_A), ("US:FXB", "FXB", "NYSE", CON_B)])
        universe, source = EV.registered_universe(self.paths, "IBKR-PAPER-1")
        self.assertEqual([(u["symbol"], u["primary_exchange"], u["registered_con_id"]) for u in universe],
                         [("FXA", "NASDAQ", CON_A), ("FXB", "NYSE", CON_B)])
        self.assertIn("2 registered", source)
        self.assertEqual(EV.universe_from_text("aapl:nasdaq")[0]["symbol"], "AAPL")
        with self.assertRaises(ValueError):
            EV.universe_from_text("AAPL")

    def test_cli_without_configuration_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = EV.main(["--account", "IBKR-PAPER-1", "--config", str(self.home / "missing.json"),
                            "--home", str(self.home)])
        self.assertEqual(code, 2)
        self.assertIn("evidence not run", err.getvalue())


@unittest.skipUnless(TA.HAVE_IBAPI, TA.SKIP_REASON)
class EvidenceWithRealAdapterTests(EvidenceCase):
    """The account items through the real IbkrAdapter over the official ibapi classes [IBKR venv]."""

    def test_account_items_through_the_order_adapter_send_only_reads(self):
        account_hosts = []

        def account_factory(wrapper):
            host = TA.FakeHost(wrapper, server_time_ms=T0 // 1_000_000)
            host.summary = [(ACCOUNT, "AccountType", "INDIVIDUAL", ""), (ACCOUNT, "TotalCashValue", "999000.00", "USD"),
                            (ACCOUNT, "SettledCash", "998500.00", "USD")]
            host.positions = [(ACCOUNT, TA.ib_contract(), Decimal("10"), 101.25)]
            host.other_orders = [(3, TA.ib_contract(TA.MSFT, "MSFT"), TA.ib_order(3, client_id=5, perm_id=9200),
                                  "PreSubmitted", "0", "5")]
            host.executions = [(TA.ib_contract(), TA.ib_execution("0000e0d5.6554a3c6.01.01", 100),
                                TA.ib_commission("0000e0d5.6554a3c6.01.01", 1.0))]
            account_hosts.append(host)
            return host

        from unknown.broker.ibkr import IbkrAdapter
        universe = [dict(self.universe[0], registered_con_id=CON_A)]

        def real_adapter(cfg):
            return IbkrAdapter(cfg, instrument_resolver={TA.AAPL: "US:AAPL"}.get, clock=self.clock,
                               client_factory=account_factory)
        record = self.run_evidence(universe=universe, adapter_factory=real_adapter)
        items = {i["id"]: i for i in record["items"]}
        for name in ("account_snapshot", "open_orders", "executions", "commissions"):
            self.assertEqual(items[name]["result"], "PASS", items[name]["detail"])
        self.assertEqual(set(account_hosts[0].names()) - ACCOUNT_READ_ONLY, set())
        self.assertEqual(account_hosts[0].clientId, 93)
        self.assertEqual(items["account_snapshot"]["data"]["account_type"], "INDIVIDUAL")
        self.assertEqual(len(items["commissions"]["data"]["commissions"]), 1)
        self.assertTrue(items["open_orders"]["data"]["orders"][0]["foreign"])


if __name__ == "__main__":
    unittest.main()
