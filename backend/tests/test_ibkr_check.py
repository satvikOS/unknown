"""Read-only IBKR connectivity check (unknown.broker.ibkr.check) against the fake IBKR host of test_ibkr_marketdata.
Accounts, conIds and times are TEST FIXTURES."""
from __future__ import annotations

import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from unknown.broker.ibkr import check as CK
from unknown.broker.ibkr import marketdata as MD
from unknown.broker.ibkr.config import config_from_dict
from unknown.domain.clock import ManualClock

from test_ibkr_marketdata import (ACCOUNT, MD_CLIENT_ID, ORDER_CLIENT_ID, PURE_SDK, T0, PureHost, base_config,
                                  md_config)

MS = 1_000_000


def setUpModule():
    logging.getLogger("unknown.broker.ibkr").setLevel(logging.ERROR)


READ_ONLY = {"connect", "disconnect", "reqMarketDataType", "reqCurrentTimeInMillis", "reqMktData", "cancelMktData"}


class CheckHost(PureHost):
    """Adds the two calls the check makes beyond the market-data session."""
    version = 200
    skew_ms = 12
    rtt_ms = 4
    md_type: int | None = 1
    md_error: int | None = None
    clock: ManualClock

    def serverVersion(self):
        return self.version

    def reqCurrentTimeInMillis(self):
        self.calls.append(("reqCurrentTimeInMillis",))
        self.clock.advance(self.rtt_ms * MS)
        self.wrapper.currentTimeInMillis((self.clock.now_ns() - self.rtt_ms * MS // 2) // MS + self.skew_ms)

    def reqMktData(self, reqId, contract, *rest):
        self.calls.append(("reqMktData", reqId, contract, *rest))
        if self.md_error is not None:
            self.wrapper.error(reqId, 0, self.md_error, "fixture entitlement error", "")
        elif self.md_type is not None:
            self.wrapper.marketDataType(reqId, self.md_type)


class CheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.clock = ManualClock(T0)
        self.cfg = config_from_dict(base_config(self.home / "state"))
        self.md = MD.market_data_config_from_dict(md_config(requested_market_data_type=3))
        self.hosts: list[CheckHost] = []
        self.lines: list[str] = []

    def run_check(self, accounts=ACCOUNT, **host_attrs):
        def factory(wrapper):
            host = CheckHost(wrapper, accounts=accounts)
            host.clock = self.clock
            for k, v in host_attrs.items():
                setattr(host, k, v)
            self.hosts.append(host)
            return host
        return CK.run_check(self.cfg, self.md, account_key="IBKR-PAPER-1", sdk=PURE_SDK, client_factory=factory,
                            clock=self.clock, wait_s=0.2, max_clock_skew_ms=2000, out=self.lines.append)

    @property
    def calls(self):
        return [c[0] for c in self.hosts[-1].calls]

    def test_live_entitled_paper_account_passes_and_sends_only_read_only_calls(self):
        r = self.run_check()
        self.assertTrue(r["ok"], self.lines)
        self.assertEqual(self.hosts[-1].calls[0], ("connect", "127.0.0.1", 4002, CK.DEFAULT_CHECK_CLIENT_ID))
        self.assertNotIn(CK.DEFAULT_CHECK_CLIENT_ID, (ORDER_CLIENT_ID, MD_CLIENT_ID))
        self.assertEqual(r["server_version"], 200)
        self.assertEqual((r["clock"]["skew_ms"], r["clock"]["rtt_ms"], r["clock"]["ok"]), (12, 4, True))
        md = r["market_data"]
        self.assertEqual((md["delivered_type_name"], md["requested_type"], md["realtime_entitled"]),
                         ("live", "delayed", True))
        self.assertLessEqual(set(self.calls), READ_ONLY)
        self.assertEqual(self.calls[-2:], ["cancelMktData", "disconnect"])
        self.assertIn("PAPER verified", "\n".join(self.lines))
        self.assertEqual(self.lines[-1], "result         PASS")
        self.assertFalse(r["places_orders"] or r["writes_to_core"])
        self.assertEqual([p for p in self.home.rglob("*")], [], "the check writes nothing")
        self.assertNotIn(ACCOUNT, "\n".join(self.lines))                 # the account id is masked

    def test_delayed_data_means_real_time_is_not_entitled(self):
        r = self.run_check(md_type=3)
        self.assertFalse(r["ok"])
        self.assertIs(r["market_data"]["realtime_entitled"], False)
        self.assertIn("share the live account's market data", r["market_data"]["reason"])
        self.assertEqual(self.lines[-1], "result         FAIL")

    def test_entitlement_error_is_reported_by_code(self):
        r = self.run_check(md_error=10089)
        self.assertIs(r["market_data"]["realtime_entitled"], False)
        self.assertIn("unavailable", r["market_data"]["reason"])
        self.assertEqual(r["market_data"]["status"], "UNAVAILABLE")

    def test_frozen_or_silent_data_is_unknown_not_entitled(self):
        self.assertIsNone(self.run_check(md_type=2)["market_data"]["realtime_entitled"])
        r = self.run_check(md_type=None)
        self.assertIsNone(r["market_data"]["realtime_entitled"])
        self.assertFalse(r["ok"])

    def test_wrong_account_latches_unsafe_and_requests_nothing(self):
        r = self.run_check(accounts="DUOTHER9")
        self.assertFalse(r["ok"])
        self.assertFalse(r["identity"]["ok"])
        self.assertIn("DISCONNECTED_UNSAFE", r["identity"]["reason"])
        self.assertEqual(set(self.calls) - {"connect", "disconnect"}, set())

    def test_clock_failures_fail_closed(self):
        old = self.run_check(version=150)
        self.assertFalse(old["clock"]["ok"])
        self.assertIsNone(old["clock"]["skew_ms"])
        self.assertNotIn("reqCurrentTimeInMillis", self.calls)
        far = self.run_check(skew_ms=5000)
        self.assertFalse(far["clock"]["ok"])
        self.assertIn("2000 ms", far["clock"]["reason"])

    def test_a_client_id_in_use_is_refused_before_any_socket(self):
        for cid in (ORDER_CLIENT_ID, MD_CLIENT_ID):
            with self.assertRaisesRegex(ValueError, "dedicated --client-id"):
                CK.run_check(self.cfg, self.md, account_key="IBKR-PAPER-1", client_id=cid, sdk=PURE_SDK,
                             client_factory=lambda w: self.fail("no socket"), out=self.lines.append)

    def test_cli_without_configuration_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = CK.main(["--account", "IBKR-PAPER-1", "--config", str(self.home / "missing.json"),
                            "--home", str(self.home)])
        self.assertEqual(code, 2)
        self.assertIn("check not run", err.getvalue())


if __name__ == "__main__":
    unittest.main()
