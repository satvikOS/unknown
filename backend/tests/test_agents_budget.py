"""Spend ledger and owner policy (CAP-AGT-02, CAP-MODEL-05; ADR-0016 section 5)."""
from __future__ import annotations

import json
import random
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_fixtures import DELETE, SECOND, TempDirCase, owner, owner_doc, write_private  # noqa: E402
from unknown.agents.budget import (BudgetError, BudgetRefused, PaidCallsDisabled, ReservationStateError,  # noqa: E402
                                   SpendLedger, read_job_spend)
from unknown.agents.config import (OwnerPolicyError, load_owner_policy, parse_owner_policy,  # noqa: E402
                                   resolve_api_key)
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.money import Money  # noqa: E402


def usd(text: str) -> Money:
    return Money("USD", Fixed.parse(text))


class OwnerPolicyTest(TempDirCase, unittest.TestCase):
    def test_absent_file_means_no_policy(self):
        self.assertIsNone(load_owner_policy(self.tmp / "config" / "agents.json"))

    def test_loads_private_file_and_refuses_unsafe_modes(self):
        p = write_private(self.tmp / "agents.json", json.dumps(owner_doc()))
        pol = load_owner_policy(p)
        self.assertEqual(pol.caps.lifetime, usd("200.00"))
        self.assertEqual(pol.caps.per_provider["anthropic"].per_day, usd("15.00"))
        write_private(p, json.dumps(owner_doc()), 0o622)
        with self.assertRaises(OwnerPolicyError):
            load_owner_policy(p)

    def test_strict_parsing(self):
        cases = [dict(unknown_key=1), dict(caps__per_day=15.0), dict(caps__per_day="-1"), dict(caps__per_job=DELETE),
                 dict(currency="XXX"), dict(providers__anthropic__enabled="yes"),
                 dict(providers__anthropic__api_key_env="lower_case"), dict(providers__anthropic__region="US EAST"),
                 dict(providers__anthropic__refusal_fallback="always"),
                 dict(providers__anthropic__effort={"default": "extreme"}),
                 dict(providers__anthropic__account={"zdr": True}), dict(schema_version=2),
                 dict(retry={"max_attempts": 1}), dict(providers={"mistral": {}})]
        for kw in cases:
            with self.subTest(kw=list(kw)), self.assertRaises(OwnerPolicyError):
                parse_owner_policy(owner_doc(**kw))
        with self.assertRaises(OwnerPolicyError):
            write_private(self.tmp / "f.json", '{"schema_version": 1, "x": 1.5}')
            load_owner_policy(self.tmp / "f.json")

    def test_api_key_resolution(self):
        cfg = owner(providers__anthropic__api_key_file="anthropic_api_key").provider("anthropic")
        self.assertEqual(resolve_api_key(cfg, environ={"UNKNOWN_TEST_ANTHROPIC_KEY": "k-env"}, secrets_dir=self.tmp), "k-env")
        self.assertIsNone(resolve_api_key(cfg, environ={}, secrets_dir=self.tmp))
        write_private(self.tmp / "anthropic_api_key", "k-file\n")
        self.assertEqual(resolve_api_key(cfg, environ={}, secrets_dir=self.tmp), "k-file")
        write_private(self.tmp / "anthropic_api_key", "k-file\n", 0o644)
        with self.assertRaises(OwnerPolicyError) as ctx:
            resolve_api_key(cfg, environ={}, secrets_dir=self.tmp)
        self.assertNotIn("k-file", str(ctx.exception))


class LedgerTest(TempDirCase, unittest.TestCase):
    def ledger(self, policy="default", name="ledger.sqlite"):
        led = SpendLedger(owner() if policy == "default" else policy, self.tmp / name, clock_ns=self.clock.now_ns)
        self.addCleanup(led.close)
        return led

    def reserve(self, led, amount, job="job-1", role="research", provider="anthropic", **kw):
        return led.reserve(job_id=job, role=role, provider=provider, model_id="m", amount=usd(amount), **kw)

    def test_paid_calls_disabled_without_policy_or_with_zero_budget(self):
        for led in (self.ledger(None), self.ledger(owner(caps__per_day="0"), "z.sqlite")):
            self.assertFalse(led.paid_calls_enabled())
            with self.assertRaises(PaidCallsDisabled):
                self.reserve(led, "0.01")

    def test_reserve_settle_release_and_exact_totals(self):
        led = self.ledger()
        r1 = self.reserve(led, "1.000000")
        r2 = self.reserve(led, "0.50", job="job-2")
        self.assertEqual(led.consumed(), Fixed.parse("1.5"))
        led.settle(r1, usd("0.0123456789"))
        led.release(r2)
        self.assertEqual(led.consumed(), Fixed.parse("0.0123456789"))
        led.settle(r1, usd("0.0123456789"))  # idempotent repeat
        with self.assertRaises(ReservationStateError):
            led.settle(r1, usd("0.02"))
        with self.assertRaises(ReservationStateError):
            led.release(r1)

    def test_refuses_at_each_cap(self):
        # per job (owner 2.00) and the envelope cap can only lower it
        led = self.ledger()
        self.reserve(led, "1.50")
        with self.assertRaises(BudgetRefused) as c:
            self.reserve(led, "0.51")
        self.assertEqual(c.exception.level, "job")
        with self.assertRaises(BudgetRefused):
            self.reserve(led, "0.10", job="job-9", job_cap=usd("0.05"))
        self.reserve(led, "0.10", job="job-9", job_cap=usd("50.00"))  # a larger envelope cap does not raise the owner cap
        with self.assertRaises(BudgetRefused):
            self.reserve(led, "1.91", job="job-9", job_cap=usd("50.00"))
        # per day
        led = self.ledger(owner(caps__per_day="3.00", caps__per_provider=DELETE), "d.sqlite")
        for i in range(3):
            self.reserve(led, "1.00", job=f"j{i}")
        with self.assertRaises(BudgetRefused) as c:
            self.reserve(led, "0.01", job="j9")
        self.assertEqual(c.exception.level, "day")
        self.clock.advance(24 * 3600 * SECOND)
        self.reserve(led, "1.00", job="j10")  # next UTC day
        # lifetime
        led = self.ledger(owner(caps__lifetime="2.50", caps__per_provider=DELETE), "l.sqlite")
        self.reserve(led, "2.00", job="a")
        self.clock.advance(48 * 3600 * SECOND)
        with self.assertRaises(BudgetRefused) as c:
            self.reserve(led, "0.51", job="b")
        self.assertEqual(c.exception.level, "lifetime")
        # per role per day: unlisted roles have no budget
        led = self.ledger(owner(caps__per_role_per_day={"research": "0.30"}), "r.sqlite")
        self.reserve(led, "0.30")
        with self.assertRaises(BudgetRefused) as c:
            self.reserve(led, "0.01", job="other")
        self.assertEqual(c.exception.level, "role:research:day")
        with self.assertRaises(BudgetRefused):
            self.reserve(led, "0.01", role="critic")
        # per provider lifetime
        led = self.ledger(owner(caps__per_provider={"anthropic": {"lifetime": "0.40"}}), "p.sqlite")
        self.reserve(led, "0.40")
        with self.assertRaises(BudgetRefused) as c:
            self.reserve(led, "0.01", job="x")
        self.assertEqual(c.exception.level, "provider:anthropic:lifetime")
        self.reserve(led, "0.01", job="x", provider="openai")
        # month
        led = self.ledger(owner(caps__per_month="1.00", caps__per_provider=DELETE), "m.sqlite")
        self.reserve(led, "1.00", job="m1")
        with self.assertRaises(BudgetRefused):
            self.reserve(led, "0.01", job="m2")

    def test_unconfirmed_stays_consumed_until_reconciled_and_overrun_is_recorded(self):
        led = self.ledger(owner(caps__per_job="1.00", caps__per_day="1.00", caps__per_provider=DELETE))
        r = self.reserve(led, "0.80")
        led.mark_unconfirmed(r, note="timeout")
        with self.assertRaises(BudgetRefused):
            self.reserve(led, "0.30", job="job-2")
        with self.assertRaises(BudgetError):
            led.reconcile(r, usd("0.10"), evidence="")
        led.reconcile(r, usd("0.10"), evidence="cost report 2026-10-23")
        self.reserve(led, "0.30", job="job-2")
        r3 = self.reserve(led, "0.10", job="job-3")
        led.settle(r3, usd("0.20"))  # actual above the reservation: recorded, never hidden
        self.assertEqual(led.state(r3.reservation_id)["overrun"], 1)
        self.assertTrue(any(e[1] == "overrun" for e in led.events()))
        with self.assertRaises(BudgetRefused):
            self.reserve(led, "0.41", job="job-4")

    def test_durable_across_reopen(self):
        led = self.ledger(owner(caps__per_day="1.00", caps__per_provider=DELETE))
        r = self.reserve(led, "0.60")
        led.close()
        led2 = self.ledger(owner(caps__per_day="1.00", caps__per_provider=DELETE))
        self.assertTrue(led2.is_open(r.reservation_id))
        with self.assertRaises(BudgetRefused):
            self.reserve(led2, "0.41", job="job-2")
        led2.settle(r.reservation_id, usd("0.25"))
        led2.close()
        led3 = self.ledger(owner(caps__per_day="1.00", caps__per_provider=DELETE))
        self.assertEqual(led3.consumed(), Fixed.parse("0.25"))
        self.reserve(led3, "0.75", job="job-3")

    def test_amount_validation(self):
        led = self.ledger()
        with self.assertRaises(BudgetError):
            led.reserve(job_id="j", role="research", provider="anthropic", model_id="m", amount=Money("EUR", Fixed(1)))
        with self.assertRaises(BudgetError):
            self.reserve(led, "-0.01")
        self.assertFalse(hasattr(led, "set_cap"))
        self.assertFalse(any(name.startswith(("raise", "increase", "set_")) for name in dir(led)))

    def test_concurrent_workers_never_exceed_budget(self):
        """CAP-AGT-02 property: 10 workers sharing one daily budget; at every admission
        spent + outstanding <= budget and the final settled spend <= budget."""
        pol = owner(caps__per_day="5.00", caps__per_job="5.00", caps__per_provider=DELETE, caps__lifetime=DELETE)
        path = self.tmp / "shared.sqlite"
        SpendLedger(pol, path, clock_ns=self.clock.now_ns).close()
        violations = []
        rng_lock = threading.Lock()
        rng = random.Random(7)

        def worker(n):
            led = SpendLedger(pol, path, clock_ns=self.clock.now_ns)
            for i in range(120):
                with rng_lock:
                    amount = Fixed(rng.randint(1, 40), 2)
                    actual = Fixed(rng.randint(0, amount.mantissa), 2)
                    outcome = rng.random()
                try:
                    r = led.reserve(job_id=f"w{n}-{i}", role="research", provider="anthropic", model_id="m",
                                    amount=Money("USD", amount))
                except BudgetRefused:
                    continue
                if led.consumed() > Fixed.parse("5.00"):
                    violations.append(str(led.consumed()))
                if outcome < 0.6:
                    led.settle(r, Money("USD", actual))
                elif outcome < 0.8:
                    led.release(r)
                else:
                    led.mark_unconfirmed(r, note="timeout")
            led.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = SpendLedger(pol, path, clock_ns=self.clock.now_ns)
        self.addCleanup(final.close)
        self.assertEqual(violations, [])
        self.assertLessEqual(final.consumed(), Fixed.parse("5.00"))


class LedgerReportingTest(TempDirCase, unittest.TestCase):
    """Per-job listing (worker crash recovery) and the read-only report the terminal API uses."""

    def test_reservations_and_read_only_report_follow_the_consumption_rule(self):
        path = self.tmp / "ledger.sqlite"
        led = SpendLedger(owner(), path, clock_ns=self.clock.now_ns)
        self.addCleanup(led.close)
        a = led.reserve(job_id="j1", role="research", provider="anthropic", model_id="m", amount=usd("0.50"))
        led.settle(a, usd("0.12"), evidence="req_1")
        b = led.reserve(job_id="j1", role="research", provider="anthropic", model_id="m", amount=usd("0.40"))
        led.mark_unconfirmed(b, note="timeout")
        c = led.reserve(job_id="j1", role="research", provider="anthropic", model_id="m", amount=usd("0.30"))
        led.release(c)
        led.reserve(job_id="j2", role="research", provider="anthropic", model_id="m", amount=usd("0.20"))
        rows = led.reservations("j1")
        self.assertEqual([r["state"] for r in rows], ["SETTLED", "UNCONFIRMED", "RELEASED"])
        self.assertEqual(rows[0]["actual"], "0.12")
        report = read_job_spend(path, "j1")
        self.assertEqual((report["consumed"], report["currency"]), ("0.52", "USD"))   # 0.12 settled + 0.40 unconfirmed
        self.assertEqual(Fixed.parse(report["consumed"]), led.consumed(job_id="j1"))
        self.assertEqual(read_job_spend(path, "nope")["reservations"], [])
        missing = self.tmp / "absent.sqlite"
        self.assertEqual(read_job_spend(missing, "j1")["ledger"], "absent")
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
