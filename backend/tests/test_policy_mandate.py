"""Policy documents, policy activation and session mandate snapshots.

Decisions under test: ADR-0007 (principals, policy versions, mandate snapshots)
and the session-creation, successor and protected-proceeds rules of ADR-0008.
Requirements (docs/requirements/register.md): SES-01, SES-02, SES-03, RSK-01,
AUT-01, AUT-04.

Every amount is asserted as an exact ``Fixed``. Every scenario that moves money
ends with ``h.verify() == []``. Hashes are checked against an oracle written
here from the ADR text (SHA-256 over sorted-key, compact UTF-8 JSON); the oracle
does not call the core's canonical encoder.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, POLICY_PATH, REPO, Harness  # noqa: E402
from unknown.core.replay import replay_and_compare  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.domain.instruments import AssetClass  # noqa: E402
from unknown.domain.principal import Principal, Role  # noqa: E402
from unknown.policy.model import BelowTarget, Mode, Policy, PolicyError  # noqa: E402

F = Fixed.parse
PAPER_POLICY_PATH = REPO / "config" / "policies" / "paper-default.v1.json"
DEFAULT = json.loads(POLICY_PATH.read_text())
SECTIONS = ("eligibility", "account_limits", "session_template", "successor", "wind_down", "fees")
DELETE = object()


# ---------------------------------------------------------------- helpers
def oracle_hash(value) -> str:
    """ADR-0007: SHA-256 over the canonical form (sorted keys, no whitespace, UTF-8)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def variant(changes: dict | None = None, base: dict | None = None) -> dict:
    """Deep copy of the shipped simulated policy with dotted-path edits (DELETE removes)."""
    body = copy.deepcopy(DEFAULT if base is None else base)
    for dotted, value in (changes or {}).items():
        target = body
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[part]
        if value is DELETE:
            del target[parts[-1]]
        else:
            target[parts[-1]] = value
    return body


def reversed_keys(value):
    if isinstance(value, dict):
        return {k: reversed_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(v) for v in value]
    return value


def alt_policy(**template) -> dict:
    """A second valid SIMULATED/USD policy whose session template differs from the default."""
    changes = {"name": "simulated-alt-v1"}
    changes.update({f"session_template.{k}": v for k, v in template.items()})
    return variant(changes)


def mandate_record(h: Harness, session_id: str) -> tuple[str, str]:
    """(mandate_hash, stored body text) for the session's current mandate."""
    s = h.row("sessions", "session_id", session_id)
    rows = h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (s["mandate_hash"],))
    return s["mandate_hash"], rows[0]["body"]


def mandate_body(h: Harness, session_id: str) -> dict:
    return json.loads(mandate_record(h, session_id)[1])


def mandates_of(h: Harness, session_id: str) -> dict[str, str]:
    return {r["mandate_hash"]: r["body"] for r in h.desk.store.query(
        "SELECT mandate_hash, body FROM mandates WHERE session_id=? ORDER BY rowid", (session_id,))}


def events(h: Harness, event_type: str, session_id: str | None = None) -> list[dict]:
    sql, params = "SELECT principal_id, session_id, payload FROM events WHERE type=?", [event_type]
    if session_id is not None:
        sql += " AND session_id=?"
        params.append(session_id)
    out = []
    for r in h.desk.store.query(sql + " ORDER BY seq", tuple(params)):
        out.append({"principal_id": r["principal_id"], "session_id": r["session_id"], **json.loads(r["payload"])})
    return out


def policy_count(h: Harness) -> int:
    return h.desk.store.query("SELECT COUNT(*) AS n FROM policies")[0]["n"]


def active_hash(h: Harness, account_key: str = ACCOUNT) -> str | None:
    r = h.desk.store.query("SELECT policy_hash FROM policy_active WHERE account_key=?", (account_key,))
    return r[0]["policy_hash"] if r else None


def successor_of(h: Harness, session_id: str) -> list[str]:
    return [r["successor_id"] for r in h.desk.store.query(
        "SELECT successor_id FROM successors WHERE predecessor_id=?", (session_id,))]


class HarnessCase(unittest.TestCase):
    def harness(self, **kw) -> Harness:
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h

    def publish(self, h: Harness, body: dict) -> str:
        r = h.owner_cmd("publish_policy", body=body)
        self.assertTrue(r.accepted, r.body)
        return r.body["policy_hash"]

    def publish_activate(self, h: Harness, body: dict) -> str:
        policy_hash = self.publish(h, body)
        r = h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=policy_hash)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(active_hash(h), policy_hash)
        return policy_hash

    def assertRejected(self, result, code: str) -> None:
        self.assertFalse(result.accepted, f"expected {code}, got accepted {result.body}")
        self.assertEqual(result.rejected_code, code, result.body)

    def run_to_target(self, h: Harness, session_id: str, qty: str, tag: str) -> None:
        """Buy at 9.00 and sell at 9.80 (fixture fees 1.00 per fill), observe the target,
        reconcile, and let the session complete. Fixture prices, not market data."""
        h.quote("8.99", "9.01")
        r = h.intent(f"{tag}-B", "BUY", qty, "9.00", session_id)
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill(f"{tag}-B", F(qty), F("9.00"))
        h.pump()
        h.quote("9.79", "9.81")
        r = h.intent(f"{tag}-S", "SELL", qty, "9.80", session_id)
        self.assertTrue(r.accepted, r.body)
        h.pump()
        h.broker.fill(f"{tag}-S", F(qty), F("9.80"))
        h.pump()
        h.tick()
        rec = h.reconcile()
        self.assertEqual(rec.body["status"], "CLEAN", rec.body)
        h.tick()


# ====================================================================== parsing
class PolicyParsingTests(unittest.TestCase):
    """ADR-0007: unknown or missing fields fail validation and are never read as
    permissive defaults; RSK-01: unreleased capabilities are refused."""

    def assertRefused(self, body, fragment: str) -> None:
        with self.assertRaises(PolicyError) as cm:
            Policy.parse(body)
        self.assertIn(fragment, str(cm.exception))

    def test_shipped_policies_parse_to_their_written_values(self):
        """SES-02 / RSK-01: the shipped defaults parse strictly and carry the exact example amounts."""
        p = Policy.parse(variant())
        self.assertIs(p.mode, Mode.SIMULATED)
        self.assertEqual(p.currency, "USD")
        self.assertEqual(p.session_template.seed.amount, F("100.00"))
        self.assertEqual(p.session_template.target_terminal_equity.amount, F("105.00"))
        self.assertEqual(p.session_template.session_max_loss.amount, F("5.00"))
        self.assertEqual(p.limits.authorized_capital.amount, F("300.00"))
        self.assertEqual(p.limits.operational_buffer.amount, F("5.00"))
        self.assertIs(p.session_template.below_target_after_target_observed, BelowTarget.CLOSE)
        self.assertEqual(p.session_template.max_resumes, 0)
        self.assertIsNone(p.session_template.expires_after_s)
        self.assertFalse(p.successor.reinvest_protected_proceeds)
        e = p.eligibility
        self.assertEqual(e.asset_classes, ("EQUITY", "ETF"))
        self.assertFalse(any([e.allow_short, e.allow_margin, e.allow_derivatives, e.allow_fx_conversion,
                              e.allow_transfers, e.outside_regular_hours]))
        paper = Policy.parse(json.loads(PAPER_POLICY_PATH.read_text()))
        self.assertIs(paper.mode, Mode.PAPER)

    def test_unknown_field_is_rejected_at_every_level(self):
        """ADR-0007: an unknown field anywhere in the document fails validation."""
        for section in ("",) + SECTIONS:
            with self.subTest(section=section or "<top>"):
                path = f"{section}.unreviewed_extension" if section else "unreviewed_extension"
                where = f"policy.{section}" if section else "policy"
                self.assertRefused(variant({path: False}),
                                   f"{where} has unknown fields: ['unreviewed_extension']")

    def test_every_missing_field_is_rejected_never_defaulted(self):
        """ADR-0007: removing any single field, including null-valued and boolean ones, fails validation."""
        paths = [(k, "policy", k) for k in DEFAULT]
        for section in SECTIONS:
            paths += [(f"{section}.{k}", f"policy.{section}", k) for k in DEFAULT[section]]
        # The sweep must cover the whole document, not a fragment of it.
        self.assertEqual(len(paths), 13 + 12 + 17 + 9 + 3 + 4 + 7)
        for dotted, where, key in paths:
            with self.subTest(missing=dotted):
                self.assertRefused(variant({dotted: DELETE}), f"{where}.{key} is required")

    def test_wrong_types_are_rejected(self):
        """ADR-0007 / ADR-0004: each field accepts only its declared JSON type (no bool-for-int, no number-for-money)."""
        cases = [
            ("schema_version", "1", "policy.schema_version must be an integer"),
            ("schema_version", True, "policy.schema_version must be an integer"),
            ("name", 7, "policy.name must be a non-empty string"),
            ("name", "", "policy.name must be a non-empty string"),
            ("mode", "simulated", "policy.mode must be one of"),
            ("currency", "usd", "unregistered currency"),
            ("eligibility", [], "policy.eligibility must be an object"),
            ("session_template", None, "policy.session_template must be an object"),
            ("eligibility.allow_short", "false", "policy.eligibility.allow_short must be a boolean"),
            ("eligibility.allow_fractional", 0, "policy.eligibility.allow_fractional must be a boolean"),
            ("eligibility.asset_classes", "EQUITY", "policy.eligibility.asset_classes must be a list of strings"),
            ("eligibility.venues", ["XNAS", 1], "policy.eligibility.venues must be a list of strings"),
            ("account_limits.max_orders_per_minute", "10", "policy.account_limits.max_orders_per_minute must be an integer"),
            ("account_limits.max_orders_per_minute", True, "policy.account_limits.max_orders_per_minute must be an integer"),
            ("account_limits.authorized_capital", 300, "policy.account_limits.authorized_capital must be a decimal string"),
            ("account_limits.require_liquidity_data", "no", "policy.account_limits.require_liquidity_data must be a boolean"),
            ("session_template.seed", 100, "policy.session_template.seed must be a decimal string"),
            ("session_template.auto_start", 1, "policy.session_template.auto_start must be a boolean"),
            ("session_template.expires_after_s", "3600", "policy.session_template.expires_after_s must be an integer"),
            ("session_template.max_resumes", False, "policy.session_template.max_resumes must be an integer"),
            ("session_template.strategy_eligibility", "owner-manual", "policy.session_template.strategy_eligibility must be a list of strings"),
            ("successor.create_on_success", "true", "policy.successor.create_on_success must be a boolean"),
            ("fees.per_share", 0, "policy.fees.per_share must be a decimal string"),
            ("auto_resume_after_clean_reconciliation", None, "policy.auto_resume_after_clean_reconciliation must be a boolean"),
            ("settlement_calendar", "", "policy.settlement_calendar must be a non-empty string"),
        ]
        for dotted, value, fragment in cases:
            with self.subTest(field=dotted, value=value):
                self.assertRefused(variant({dotted: value}), fragment)

    def test_binary_floats_are_never_accepted(self):
        """ADR-0004: a float anywhere in a policy is refused (economic numbers are decimal strings)."""
        for dotted, value in [("session_template.seed", 100.0), ("account_limits.price_collar_bps", 100.0),
                              ("schema_version", 1.0), ("fees.per_share", 0.005)]:
            with self.subTest(field=dotted):
                with self.assertRaises((PolicyError, TypeError)):
                    Policy.parse(variant({dotted: value}))

    def test_out_of_range_values_are_rejected(self):
        """ADR-0007: bounded integers, positive or non-negative money, ledger precision, timezone, currency and lists."""
        cases = [
            ("schema_version", 2, "unsupported policy schema_version 2"),
            ("schema_version", 0, "policy.schema_version must be an integer in [1, 1000]"),
            ("currency", "XYZ", "unregistered currency"),
            ("account_limits.max_position_bps_of_session_equity", 0, "max_position_bps_of_session_equity must be an integer in [1, 10000]"),
            ("account_limits.max_position_bps_of_session_equity", 10_001, "max_position_bps_of_session_equity must be an integer in [1, 10000]"),
            ("account_limits.max_orders_per_minute", 0, "max_orders_per_minute must be an integer in [1, 600]"),
            ("account_limits.max_orders_per_minute", 601, "max_orders_per_minute must be an integer in [1, 600]"),
            ("account_limits.price_collar_bps", 2_001, "price_collar_bps must be an integer in [1, 2000]"),
            ("account_limits.quote_max_age_ms", 0, "quote_max_age_ms must be an integer in [1, 86400000]"),
            ("account_limits.reauth_max_age_s", 29, "reauth_max_age_s must be an integer in [30, 86400]"),
            ("account_limits.max_reconciliation_age_s", 86_401, "max_reconciliation_age_s must be an integer in [1, 86400]"),
            ("account_limits.max_clock_skew_ms", 0, "max_clock_skew_ms must be an integer in [1, 600000]"),
            ("account_limits.min_free_disk_mb", -1, "min_free_disk_mb must be an integer in [0, 10000000]"),
            ("account_limits.max_participation_bps_of_adv", 0, "max_participation_bps_of_adv must be an integer in [1, 10000]"),
            ("account_limits.authorized_capital", "-1.00", "policy.account_limits.authorized_capital must be positive"),
            ("account_limits.authorized_capital", "0.00", "policy.account_limits.authorized_capital must be positive"),
            ("account_limits.operational_buffer", "-0.01", "policy.account_limits.operational_buffer must not be negative"),
            ("account_limits.daily_loss_trigger", "3.0000001", "daily_loss_trigger has more precision than the USD ledger scale"),
            ("account_limits.risk_day_timezone", "Mars/Olympus_Mons", "unknown risk_day_timezone"),
            ("session_template.seed", "0.00", "policy.session_template.seed must be positive"),
            ("session_template.seed", "1e2", "policy.session_template.seed: not a plain decimal number"),
            ("session_template.session_drawdown_trigger_bps", 0, "session_drawdown_trigger_bps must be an integer in [1, 10000]"),
            ("session_template.session_drawdown_trigger_bps", 10_001, "session_drawdown_trigger_bps must be an integer in [1, 10000]"),
            ("session_template.expires_after_s", 59, "expires_after_s must be an integer in [60,"),
            ("session_template.max_resumes", 101, "max_resumes must be an integer in [0, 100]"),
            ("wind_down.collar_bps", 0, "collar_bps must be an integer in [1, 2000]"),
            ("wind_down.reprice_after_s", 4, "reprice_after_s must be an integer in [5, 86400]"),
            ("wind_down.max_attempts", 0, "max_attempts must be an integer in [1, 100]"),
            ("fees.maximum_bps_of_notional", 10_001, "maximum_bps_of_notional must be an integer in [0, 10000]"),
            ("fees.estimate_margin_bps", -1, "estimate_margin_bps must be an integer in [0, 100000]"),
            ("fees.minimum", "-1.00", "policy.fees.minimum must not be negative"),
            ("fees.per_share", "-0.001", "fees.per_share must not be negative"),
            ("eligibility.venues", [], "eligibility.venues must name at least one venue"),
            ("eligibility.venues", ["XNAS", "XNAS"], "policy.eligibility.venues has duplicates"),
            ("eligibility.asset_classes", ["EQUITY", "EQUITY"], "policy.eligibility.asset_classes has duplicates"),
        ]
        for dotted, value, fragment in cases:
            with self.subTest(field=dotted, value=value):
                self.assertRefused(variant({dotted: value}), fragment)
        # Seed above the account's authorized capital is a cross-field violation.
        self.assertRefused(variant({"session_template.seed": "301.00", "session_template.target_terminal_equity": "400.00"}),
                           "session seed exceeds authorized capital")

    def test_values_at_the_documented_bounds_are_accepted(self):
        """ADR-0007: the range checks sit exactly at their bounds (controls for the rejections above)."""
        cases = [
            {"account_limits.max_position_bps_of_session_equity": 1},
            {"account_limits.max_position_bps_of_session_equity": 10_000},
            {"account_limits.price_collar_bps": 2_000},
            {"account_limits.reauth_max_age_s": 30},
            {"account_limits.operational_buffer": "0.00"},
            {"account_limits.daily_loss_trigger": "3.000001"},
            {"session_template.expires_after_s": 60},
            {"session_template.below_target_after_target_observed": "RESUME", "session_template.max_resumes": 100},
            {"session_template.seed": "300.00", "session_template.target_terminal_equity": "300.01"},
            {"eligibility.asset_classes": ["ETF"]},
            {"mode": "LIVE"},  # parses: publishing records a document; activation is the gate
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                p = Policy.parse(variant(changes))
                self.assertEqual(p.policy_hash, oracle_hash(variant(changes)))

    def test_target_must_exceed_seed(self):
        """SES-01 / ADR-0008: the target is terminal equity, not profit, so target <= seed is invalid."""
        for target in ("100.00", "100", "99.99", "0.01"):
            with self.subTest(target=target):
                self.assertRefused(variant({"session_template.target_terminal_equity": target}),
                                   "target_terminal_equity must exceed seed")
        p = Policy.parse(variant({"session_template.target_terminal_equity": "100.000001"}))
        self.assertEqual(p.session_template.target_terminal_equity.amount, F("100.000001"))

    def test_unreleased_capabilities_are_refused(self):
        """RSK-01: no borrowing, unowned selling or unapproved products, whatever the owner writes."""
        for flag in ("allow_short", "allow_margin", "allow_derivatives", "allow_fx_conversion", "allow_transfers"):
            with self.subTest(flag=flag):
                self.assertRefused(variant({f"eligibility.{flag}": True}),
                                   "short, margin, derivatives, FX conversion and transfers are not released")
        self.assertRefused(variant({"eligibility.outside_regular_hours": True}),
                           "outside-regular-hours trading is not released")
        unreleased = [a.value for a in AssetClass if a.value not in ("EQUITY", "ETF")]
        self.assertEqual(len(unreleased), 9)
        for asset_class in unreleased:
            with self.subTest(asset_class=asset_class):
                self.assertRefused(variant({"eligibility.asset_classes": ["EQUITY", asset_class]}),
                                   "only EQUITY and ETF are released for trading")
        self.assertRefused(variant({"eligibility.asset_classes": ["WARRANT"]}),
                           "policy.eligibility.asset_classes contains values outside")
        self.assertRefused(variant({"eligibility.order_types": ["LIMIT", "MARKET"]}),
                           "policy.eligibility.order_types contains values outside ['LIMIT']")
        self.assertRefused(variant({"eligibility.time_in_force": ["DAY", "GTC"]}),
                           "policy.eligibility.time_in_force contains values outside ['DAY']")
        self.assertRefused(variant({"on_stop_liquidation": "REDUCE_WITHIN_COLLAR"}),
                           "stop liquidation policies other than NONE are not released")
        self.assertRefused(variant({"on_stop_liquidation": "SELL_EVERYTHING"}), "policy.on_stop_liquidation must be one of")
        self.assertRefused(variant({"successor.mandate_source": "LATEST_TEMPLATE"}),
                           "policy.successor.mandate_source must be one of ['ACTIVE_TEMPLATE_AT_CREATION']")
        self.assertRefused(variant({"wind_down.order_style": "MARKET"}), "policy.wind_down.order_style must be one of")
        self.assertRefused(variant({"fees.model": "FLAT"}), "policy.fees.model must be one of ['PER_SHARE']")

    def test_resume_below_target_requires_positive_max_resumes(self):
        """ADR-0008: RESUME is permitted only with a positive max_resumes written into the mandate."""
        resume = {"session_template.below_target_after_target_observed": "RESUME"}
        self.assertRefused(variant({**resume, "session_template.max_resumes": 0}), "RESUME requires max_resumes >= 1")
        self.assertRefused(variant({**resume, "session_template.max_resumes": DELETE}),
                           "policy.session_template.max_resumes is required")
        self.assertRefused(variant({**resume, "session_template.max_resumes": None}),
                           "policy.session_template.max_resumes must be an integer in [0, 100]")
        self.assertRefused(variant({"session_template.max_resumes": 1}),
                           "max_resumes must be 0 when below-target policy is CLOSE")
        self.assertRefused(variant({"session_template.below_target_after_target_observed": "RETRY"}),
                           "below_target_after_target_observed must be one of")
        p = Policy.parse(variant({**resume, "session_template.max_resumes": 3}))
        self.assertIs(p.session_template.below_target_after_target_observed, BelowTarget.RESUME)
        self.assertEqual(p.session_template.max_resumes, 3)

    def test_reinvesting_protected_proceeds_is_refused(self):
        """ADR-0008 / SES-09: protected proceeds never fund another session; schema 1 offers no reinvestment rule."""
        self.assertRefused(variant({"successor.reinvest_protected_proceeds": True}),
                           "reinvesting protected proceeds is not offered by policy schema 1")

    def test_document_must_be_an_object(self):
        """ADR-0007: anything other than a JSON object is not a policy."""
        for body in (None, [], "policy", 1):
            with self.subTest(body=body):
                self.assertRefused(body, "policy must be an object")


# ====================================================================== hashing
class PolicyHashTests(unittest.TestCase):
    """ADR-0007: policy_hash is SHA-256 over the canonical form of the document."""

    def test_hash_is_sha256_of_the_canonical_document(self):
        """ADR-0007 / SES-01: the hash recorded in every mandate is reproducible from the document alone."""
        p = Policy.parse(variant())
        self.assertEqual(p.policy_hash, oracle_hash(DEFAULT))
        self.assertRegex(p.policy_hash, r"^[0-9a-f]{64}$")

    def test_hash_ignores_key_order_and_parsing_does_not_mutate(self):
        """ADR-0007: canonical form makes the hash independent of key order; parse leaves the body untouched."""
        body = variant()
        snapshot = copy.deepcopy(body)
        shuffled = reversed_keys(body)
        self.assertNotEqual(list(shuffled), list(body))
        self.assertEqual(Policy.parse(shuffled).policy_hash, Policy.parse(body).policy_hash)
        self.assertEqual(body, snapshot)

    def test_any_change_produces_a_different_hash(self):
        """ADR-0007 / SES-01: a mandate's policy_hash identifies exactly one document version."""
        base = Policy.parse(variant()).policy_hash
        seen = {base}
        for changes in ({"name": "simulated-default-v2"}, {"session_template.seed": "99.00"},
                        {"session_template.target_terminal_equity": "105.01"}, {"account_limits.price_collar_bps": 99},
                        {"eligibility.allow_fractional": True}, {"session_template.auto_start": False}):
            with self.subTest(changes=changes):
                h = Policy.parse(variant(changes)).policy_hash
                self.assertEqual(h, oracle_hash(variant(changes)))
                self.assertNotIn(h, seen)
                seen.add(h)


# ====================================================================== publish / activate
class PublishActivateTests(HarnessCase):
    """ADR-0007: publishing is append-only and idempotent; activation is a separate owner
    command that refuses LIVE, mode and currency mismatches."""

    def setUp(self):
        self.h = self.harness()
        self.h.bootstrap()

    def test_publish_is_idempotent_by_hash(self):
        """ADR-0007: re-publishing the same document (any key order) returns the same hash and stores nothing new."""
        h = self.h
        self.assertEqual(h.policy_hash, oracle_hash(DEFAULT))
        again = h.owner_cmd("publish_policy", body=variant())
        self.assertTrue(again.accepted)
        self.assertEqual(again.body, {"policy_hash": h.policy_hash, "already_published": True})
        reordered = h.owner_cmd("publish_policy", body=reversed_keys(variant()))
        self.assertEqual(reordered.body, {"policy_hash": h.policy_hash, "already_published": True})
        retry = h.cmd("publish_policy", h.owner, command_id=again.command_id, body=variant())
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.body, again.body)
        self.assertEqual(policy_count(h), 1)
        self.assertEqual(len(events(h, "PolicyPublished")), 1)
        self.assertEqual(active_hash(h), h.policy_hash)

    def test_invalid_policies_are_refused_at_publish_and_never_stored(self):
        """ADR-0007 / RSK-01: every invalid document is a recorded POLICY_INVALID rejection and leaves no policy row."""
        h = self.h
        cases = {
            "unknown field": variant({"session_template.bonus_seed": "1.00"}),
            "missing field": variant({"eligibility.allow_short": DELETE}),
            "short selling": variant({"eligibility.allow_short": True}),
            "derivatives": variant({"eligibility.asset_classes": ["EQUITY", "OPTION"]}),
            "outside hours": variant({"eligibility.outside_regular_hours": True}),
            "target <= seed": variant({"session_template.target_terminal_equity": "100.00"}),
            "resume without max": variant({"session_template.below_target_after_target_observed": "RESUME"}),
            "reinvest": variant({"successor.reinvest_protected_proceeds": True}),
            "per_share not decimal": variant({"fees.per_share": "five"}),
            "not an object": [],
        }
        before = len(events(h, "CommandRejected"))
        for label, body in cases.items():
            with self.subTest(case=label):
                r = h.owner_cmd("publish_policy", body=body)
                self.assertRejected(r, "POLICY_INVALID")
        self.assertEqual(len(events(h, "CommandRejected")) - before, len(cases))
        # A binary float never becomes a policy, whether refused as a fault or as a rejection.
        try:
            r = h.owner_cmd("publish_policy", body=variant({"session_template.seed": 100.0}))
            self.assertRejected(r, "POLICY_INVALID")
        except TypeError:
            pass
        self.assertEqual(policy_count(h), 1)
        self.assertEqual(len(events(h, "PolicyPublished")), 1)
        self.assertEqual(active_hash(h), h.policy_hash)

    def test_live_policy_publishes_but_activation_is_refused(self):
        """ADR-0007 / AUT-01: a LIVE policy can be recorded but not activated in this release."""
        h = self.h
        live_hash = self.publish(h, variant({"name": "live-candidate", "mode": "LIVE"}))
        self.assertTrue(h.owner_cmd("register_account", account_key="LIVE-1", provider="simulated", mode="LIVE",
                                    base_currency="USD").accepted)
        self.assertRejected(h.owner_cmd("activate_policy", account_key="LIVE-1", policy_hash=live_hash),
                            "LIVE_NOT_RELEASED")
        self.assertIsNone(active_hash(h, "LIVE-1"))
        self.assertRejected(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=live_hash),
                            "POLICY_MODE_MISMATCH")
        self.assertEqual(active_hash(h), h.policy_hash)
        # With no active policy a LIVE account cannot even create a session.
        self.assertRejected(h.owner_cmd("create_session", account_key="LIVE-1", session_id="L1"), "NO_ACTIVE_POLICY")

    def test_activation_refuses_mode_mismatch(self):
        """ADR-0007: a PAPER policy cannot govern a SIMULATED account, nor the reverse."""
        h = self.h
        paper_hash = self.publish(h, json.loads(PAPER_POLICY_PATH.read_text()))
        self.assertRejected(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=paper_hash),
                            "POLICY_MODE_MISMATCH")
        self.assertEqual(active_hash(h), h.policy_hash)
        self.assertTrue(h.owner_cmd("register_account", account_key="PAPER-1", provider="simulated", mode="PAPER",
                                    base_currency="USD").accepted)
        self.assertRejected(h.owner_cmd("activate_policy", account_key="PAPER-1", policy_hash=h.policy_hash),
                            "POLICY_MODE_MISMATCH")
        self.assertTrue(h.owner_cmd("activate_policy", account_key="PAPER-1", policy_hash=paper_hash).accepted)
        self.assertEqual(active_hash(h, "PAPER-1"), paper_hash)

    def test_activation_refuses_currency_mismatch(self):
        """ADR-0007: the policy currency must equal the account base currency."""
        h = self.h
        eur_hash = self.publish(h, variant({"name": "simulated-eur", "currency": "EUR"}))
        self.assertRejected(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=eur_hash),
                            "POLICY_CURRENCY_MISMATCH")
        self.assertEqual(active_hash(h), h.policy_hash)
        self.assertTrue(h.owner_cmd("register_account", account_key="SIM-EUR", provider="simulated", mode="SIMULATED",
                                    base_currency="EUR").accepted)
        self.assertRejected(h.owner_cmd("activate_policy", account_key="SIM-EUR", policy_hash=h.policy_hash),
                            "POLICY_CURRENCY_MISMATCH")
        self.assertIsNone(active_hash(h, "SIM-EUR"))
        self.assertTrue(h.owner_cmd("activate_policy", account_key="SIM-EUR", policy_hash=eur_hash).accepted)

    def test_activation_refuses_unpublished_hash(self):
        """ADR-0007: only a published document can become the default."""
        h = self.h
        unpublished = Policy.parse(alt_policy(seed="60.00", target_terminal_equity="110.00")).policy_hash
        self.assertRejected(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=unpublished), "UNKNOWN_POLICY")
        self.assertEqual(active_hash(h), h.policy_hash)

    def test_activation_refuses_limit_below_already_authorized_capital(self):
        """AUT-01: lowering authorized_capital below what is authorized needs a deauthorization first."""
        h = self.h
        lower = self.publish(h, variant({"name": "simulated-lower", "account_limits.authorized_capital": "200.00",
                                         "account_limits.max_gross_exposure": "200.00"}))
        self.assertRejected(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=lower),
                            "AUTHORIZED_ABOVE_NEW_LIMIT")
        self.assertEqual(active_hash(h), h.policy_hash)
        self.assertTrue(h.owner_cmd("deauthorize_capital", account_key=ACCOUNT, amount="100.00").accepted)
        self.assertTrue(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=lower).accepted)
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertEqual(h.verify(), [])

    def test_policy_and_mandate_commands_are_owner_only(self):
        """AUT-04 / ADR-0007: SYSTEM, STRATEGY and AGENT can never publish, activate, create or amend."""
        h = self.h
        alt = alt_policy(seed="60.00", target_terminal_equity="110.00")
        alt_hash = self.publish(h, alt)
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        others = [h.desk.system(), Principal("strategy.fixture", Role.STRATEGY), Principal("agent.research", Role.AGENT)]
        requests = [("publish_policy", {"body": variant({"name": "agent-wide", "account_limits.max_orders_per_minute": 600})}),
                    ("activate_policy", {"account_key": ACCOUNT, "policy_hash": alt_hash}),
                    ("create_session", {"account_key": ACCOUNT, "session_id": "X", "seed": "250.00",
                                        "target_terminal_equity": "260.00"}),
                    ("amend_session_request", {"session_id": "A", "seed": "250.00", "target_terminal_equity": "260.00"})]
        for principal in others:
            for kind, payload in requests:
                with self.subTest(role=principal.role.value, kind=kind):
                    self.assertRejected(h.cmd(kind, principal, **payload), "UNAUTHORIZED")
        self.assertEqual(policy_count(h), 2)
        self.assertEqual(active_hash(h), h.policy_hash)
        self.assertEqual([s["session_id"] for s in h.sessions()], ["A"])
        self.assertEqual(F(h.row("sessions", "session_id", "A")["seed"]), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_published_policy_is_inert_until_activated(self):
        """SES-02 / ADR-0007: publishing alone changes nothing; new sessions follow the activated default."""
        h = self.h
        alt_hash = self.publish(h, alt_policy(seed="60.00", target_terminal_equity="110.00"))
        self.assertEqual(active_hash(h), h.policy_hash)
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        a = mandate_body(h, "A")
        self.assertEqual((F(a["seed"]), F(a["target_terminal_equity"]), a["policy_hash"]),
                         (F("100.00"), F("105.00"), h.policy_hash))
        self.assertTrue(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=alt_hash).accepted)
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted)
        b = mandate_body(h, "B")
        self.assertEqual((F(b["seed"]), F(b["target_terminal_equity"]), b["policy_hash"]),
                         (F("60.00"), F("110.00"), alt_hash))
        self.assertEqual(h.balance("CASH", "session:A"), F("100.00"))
        self.assertEqual(h.balance("CASH", "session:B"), F("60.00"))
        self.assertEqual(h.available("unallocated"), F("140.00"))
        self.assertEqual(h.verify(), [])

    def test_policy_identity_survives_restart(self):
        """ADR-0007 / SES-01: stored documents re-parse to their hashes, and the default survives a restart."""
        h = self.h
        alt = alt_policy(seed="60.00", target_terminal_equity="110.00")
        alt_hash = self.publish_activate(h, alt)
        h.restart()
        for r in h.desk.store.query("SELECT policy_hash, body FROM policies"):
            self.assertEqual(Policy.parse(json.loads(r["body"])).policy_hash, r["policy_hash"])
            self.assertEqual(oracle_hash(json.loads(r["body"])), r["policy_hash"])
        self.assertEqual(active_hash(h), alt_hash)
        again = h.owner_cmd("publish_policy", body=alt)
        self.assertEqual(again.body, {"policy_hash": alt_hash, "already_published": True})
        self.assertEqual(policy_count(h), 2)


# ====================================================================== mandates
class MandateSnapshotTests(HarnessCase):
    """SES-01..SES-03: mandates are immutable snapshots taken at request creation."""

    def test_mandate_snapshot_records_every_term_and_is_hashed(self):
        """SES-01: seed, target, currency, strategy eligibility, session risk policy and policy version, hashed."""
        h = self.harness()
        h.bootstrap()
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A")
        self.assertTrue(r.accepted, r.body)
        mandate_hash, raw = mandate_record(h, "A")
        self.assertEqual(r.body["mandate_hash"], mandate_hash)
        m = json.loads(raw)
        self.assertEqual(mandate_hash, oracle_hash(m))
        self.assertEqual(m["session_id"], "A")
        self.assertEqual(m["account_key"], ACCOUNT)
        self.assertEqual(m["currency"], "USD")
        self.assertEqual(F(m["seed"]), F("100.00"))
        self.assertEqual(F(m["target_terminal_equity"]), F("105.00"))
        self.assertEqual(m["strategy_eligibility"], ["owner-manual"])
        self.assertEqual(m["session_drawdown_trigger_bps"], 200)
        self.assertEqual(F(m["session_max_loss"]), F("5.00"))
        self.assertIs(m["auto_start"], True)
        self.assertIsNone(m["expires_at_ns"])
        self.assertEqual(m["below_target_after_target_observed"], "CLOSE")
        self.assertEqual(m["max_resumes"], 0)
        self.assertEqual(m["policy_hash"], h.policy_hash)
        self.assertEqual(m["origin"], "OWNER")
        requested = events(h, "SessionRequested", "A")
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["info"]["mandate"], m)
        self.assertEqual(requested[0]["principal_id"], "owner")
        self.assertEqual(h.session_state("A"), "RUNNING")
        self.assertEqual(h.balance("CASH", "session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_resume_rule_is_written_into_the_mandate(self):
        """SES-01 / ADR-0008: the below-target rule and max_resumes are frozen into the mandate, not read live."""
        h = self.harness(policy=variant({"session_template.below_target_after_target_observed": "RESUME",
                                         "session_template.max_resumes": 2}))
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        before = mandate_record(h, "A")
        self.assertEqual((json.loads(before[1])["below_target_after_target_observed"], json.loads(before[1])["max_resumes"]),
                         ("RESUME", 2))
        close_hash = self.publish_activate(h, variant({"name": "simulated-close"}))
        self.assertEqual(mandate_record(h, "A"), before)
        self.assertNotEqual(json.loads(before[1])["policy_hash"], close_hash)
        self.assertEqual(h.verify(), [])

    def test_owner_overrides_apply_to_that_mandate_only(self):
        """SES-02: seed and target are configurable per request; the default template is untouched."""
        h = self.harness()
        h.bootstrap()
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A", seed="80.00", target_terminal_equity="90.00")
        self.assertTrue(r.accepted, r.body)
        a = mandate_body(h, "A")
        self.assertEqual((F(a["seed"]), F(a["target_terminal_equity"]), a["policy_hash"]), (F("80.00"), F("90.00"), h.policy_hash))
        requested = events(h, "SessionRequested", "A")[0]["info"]
        self.assertEqual(F(requested["required_net_return_pct"]), F("12.5"))
        self.assertIn("not a forecast or funding authorization", requested["note"])
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted)
        b = mandate_body(h, "B")
        self.assertEqual((F(b["seed"]), F(b["target_terminal_equity"])), (F("100.00"), F("105.00")))
        self.assertEqual(active_hash(h), h.policy_hash)
        invalid = [
            {"seed": "80.00", "target_terminal_equity": "80.00"},
            {"seed": "80.00", "target_terminal_equity": "79.99"},
            {"seed": "0.00", "target_terminal_equity": "1.00"},
            {"seed": "-10.00", "target_terminal_equity": "1.00"},
            {"seed": "301.00", "target_terminal_equity": "400.00"},
            {"seed": "eighty", "target_terminal_equity": "90.00"},
            {"seed": "80.0000001", "target_terminal_equity": "90.00"},
            {"seed": 80, "target_terminal_equity": "90.00"},
            {"target_terminal_equity": "100.00"},
        ]
        for i, terms in enumerate(invalid):
            with self.subTest(terms=terms):
                self.assertRejected(h.owner_cmd("create_session", account_key=ACCOUNT, session_id=f"X{i}", **terms),
                                    "MANDATE_INVALID")
        self.assertEqual([s["session_id"] for s in h.sessions()], ["A", "B"])
        self.assertEqual(h.balance("CASH", "session:A"), F("80.00"))
        self.assertEqual(h.balance("CASH", "session:B"), F("100.00"))
        self.assertEqual(h.available("unallocated"), F("120.00"))
        self.assertEqual(h.verify(), [])

    def test_new_default_does_not_change_a_running_session(self):
        """SES-03: activating a different template leaves the running session's mandate, cash and policy version intact."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        before = mandate_record(h, "A")
        alt_hash = self.publish_activate(h, alt_policy(seed="60.00", target_terminal_equity="110.00",
                                                       session_drawdown_trigger_bps=300, session_max_loss="4.00",
                                                       strategy_eligibility=["owner-manual", "fixture-alt"]))
        h.tick()
        self.assertEqual(mandate_record(h, "A"), before)
        row = h.row("sessions", "session_id", "A")
        self.assertEqual((row["state"], F(row["seed"]), F(row["target"])), ("RUNNING", F("100.00"), F("105.00")))
        self.assertEqual(json.loads(before[1])["policy_hash"], h.policy_hash)
        self.assertEqual(h.available("session:A"), F("100.00"))
        # Intents for A are still judged against A's own policy version, not the new default.
        h.quote("9.99", "10.01")
        stale = h.cmd("submit_intent", h.owner, account_key=ACCOUNT, session_id="A", order_id="A-NEWPOL",
                      instrument_id="FIX-EQ-1", side="BUY", quantity="1", limit_price="10.00",
                      deadline_ns=h.clock.now_ns() + 60 * 10**9, policy_hash=alt_hash)
        self.assertRejected(stale, "POLICY_VERSION")
        own = h.intent("A-OWNPOL", "BUY", "1", "10.00", "A")
        self.assertTrue(own.accepted, own.body)
        self.assertEqual(h.available("session:A"), F("88.80"))
        # A request created after the change takes the new template.
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted)
        b = mandate_body(h, "B")
        self.assertEqual((F(b["seed"]), F(b["target_terminal_equity"]), b["policy_hash"], b["session_drawdown_trigger_bps"]),
                         (F("60.00"), F("110.00"), alt_hash, 300))
        self.assertEqual(h.session_state("B"), "READY")
        self.assertEqual(h.balance("CASH", "session:B"), F("60.00"))
        self.assertEqual(h.available("unallocated"), F("140.00"))
        self.assertEqual(h.verify(), [])
        # The snapshot survives a restart and a full replay of the event log.
        h.restart()
        self.assertEqual(mandate_record(h, "A"), before)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])

    def test_new_default_does_not_change_a_waiting_request(self):
        """SES-03 / ADR-0007: defaults never propagate silently into a request that is not yet funded."""
        h = self.harness()
        h.bootstrap(authorize="100.00")
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted)
        self.assertEqual(h.session_state("B"), "WAITING_FUNDS")
        funding = h.row("sessions", "session_id", "B")["funding"]
        self.assertEqual((funding["reason"], F(funding["shortfall"])), ("INSUFFICIENT_UNALLOCATED_SETTLED_CASH", F("5.00")))
        before = mandate_record(h, "B")
        # Under this template the request would fund at once (95.00 eligible >= 5.00) if defaults leaked into it.
        self.publish_activate(h, alt_policy(seed="5.00", target_terminal_equity="6.00"))
        h.tick()
        self.assertEqual(h.session_state("B"), "WAITING_FUNDS")
        self.assertEqual(mandate_record(h, "B"), before)
        self.assertEqual(h.balance("CASH", "session:B"), F("0"))
        self.assertTrue(h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="5.00").accepted)
        self.assertEqual(h.balance("CASH", "session:B"), F("100.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("5.00"))
        self.assertEqual(h.session_state("B"), "RUNNING")
        self.assertEqual(mandate_record(h, "B"), before)
        self.assertEqual(h.verify(), [])

    def test_existing_successor_mandate_is_unchanged_by_a_later_default(self):
        """SES-03: an already-created successor keeps the mandate frozen when it was requested."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        self.run_to_target(h, "A", "10", "A")
        self.assertEqual(h.session_state("A"), "COMPLETED_UNSETTLED")
        (succ,) = successor_of(h, "A")
        before = mandate_record(h, succ)
        s = json.loads(before[1])
        self.assertEqual((F(s["seed"]), F(s["target_terminal_equity"]), s["policy_hash"], s["origin"]),
                         (F("100.00"), F("105.00"), h.policy_hash, "SUCCESSOR_OF:A"))
        self.assertEqual(h.balance("CASH", f"session:{succ}"), F("100.00"))
        self.publish_activate(h, alt_policy(seed="60.00", target_terminal_equity="110.00"))
        h.tick()
        h.tick()
        self.assertEqual(mandate_record(h, succ), before)
        row = h.row("sessions", "session_id", succ)
        self.assertEqual((F(row["seed"]), F(row["target"])), (F("100.00"), F("105.00")))
        self.assertEqual(h.balance("CASH", f"session:{succ}"), F("100.00"))
        self.assertEqual(successor_of(h, "A"), [succ])
        self.assertEqual(len(h.sessions()), 2)
        self.assertEqual(h.verify(), [])

    def test_successor_created_after_the_change_uses_the_new_template(self):
        """SES-03 / ADR-0007: the successor snapshots the default active when it is requested; the
        predecessor still completes against its own target."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        a_before = mandate_record(h, "A")
        # The new default's target (110.00) is above what A will realize (106.00): A must still
        # complete, because completion is judged on A's frozen 105.00 target.
        alt_hash = self.publish_activate(h, alt_policy(seed="60.00", target_terminal_equity="110.00",
                                                       session_drawdown_trigger_bps=300))
        self.run_to_target(h, "A", "10", "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("COMPLETED_UNSETTLED", "TARGET_REACHED"))
        self.assertEqual(F(a["protected_value"]), F("106.00"))
        self.assertEqual(mandate_record(h, "A"), a_before)
        (succ,) = successor_of(h, "A")
        s = mandate_body(h, succ)
        self.assertEqual((F(s["seed"]), F(s["target_terminal_equity"]), s["policy_hash"], s["origin"],
                          s["session_drawdown_trigger_bps"]),
                         (F("60.00"), F("110.00"), alt_hash, "SUCCESSOR_OF:A", 300))
        self.assertEqual(h.balance("CASH", f"session:{succ}"), F("60.00"))
        self.assertEqual(h.available("unallocated"), F("140.00"))
        self.assertEqual(h.session_state(succ), "RUNNING")
        self.assertEqual(h.verify(), [])


# ====================================================================== amendment
class AmendRequestTests(HarnessCase):
    """ADR-0007: the owner may amend a not-yet-running request by issuing a new, audited mandate version."""

    def waiting_request(self) -> Harness:
        h = self.harness()
        h.bootstrap(authorize="100.00")
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted)
        self.assertEqual(h.session_state("B"), "WAITING_FUNDS")
        return h

    def test_amending_an_unfunded_request_issues_an_audited_new_version(self):
        """SES-02 / ADR-0007: amendment writes a new mandate version, keeps the old one, and records who and what."""
        h = self.waiting_request()
        old_hash, old_raw = mandate_record(h, "B")
        r = h.owner_cmd("amend_session_request", session_id="B", seed="90.00", target_terminal_equity="95.00")
        self.assertTrue(r.accepted, r.body)
        new_hash, new_raw = mandate_record(h, "B")
        self.assertEqual(r.body["mandate_hash"], new_hash)
        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual(mandates_of(h, "B"), {old_hash: old_raw, new_hash: new_raw})
        m = json.loads(new_raw)
        self.assertEqual(new_hash, oracle_hash(m))
        self.assertEqual((F(m["seed"]), F(m["target_terminal_equity"]), m["policy_hash"], m["origin"]),
                         (F("90.00"), F("95.00"), h.policy_hash, "OWNER"))
        (audit,) = events(h, "SessionMandateAmended", "B")
        self.assertEqual(audit["principal_id"], "owner")
        self.assertEqual(audit["info"]["previous"], old_hash)
        self.assertEqual(audit["info"]["mandate"], m)
        row = h.row("sessions", "session_id", "B")
        self.assertEqual((F(row["seed"]), F(row["target"])), (F("90.00"), F("95.00")))
        # The amended seed fits the eligible cash (100.00 - 5.00 buffer), so the request funds and starts.
        self.assertEqual(row["state"], "RUNNING")
        self.assertEqual(h.balance("CASH", "session:B"), F("90.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("10.00"))
        self.assertEqual(h.verify(), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "replay.sqlite"), [])

    def test_amendment_is_refused_once_funded_or_terminal(self):
        """ADR-0007: only a not-yet-running (unfunded) request can be amended; RUNNING, READY and CANCELLED cannot."""
        h = self.harness()
        h.bootstrap()
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A").accepted)
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted)
        self.assertTrue(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="C").accepted)
        self.assertTrue(h.owner_cmd("cancel_session", session_id="C").accepted)
        self.assertEqual([h.session_state(x) for x in "ABC"], ["RUNNING", "READY", "CANCELLED"])
        before = {x: mandate_record(h, x) for x in "ABC"}
        for sid in "ABC":
            with self.subTest(session=sid):
                self.assertRejected(h.owner_cmd("amend_session_request", session_id=sid, seed="50.00",
                                                target_terminal_equity="55.00"), "INVALID_STATE")
                self.assertEqual(mandate_record(h, sid), before[sid])
        self.assertRejected(h.owner_cmd("amend_session_request", session_id="NOPE", seed="50.00",
                                        target_terminal_equity="55.00"), "UNKNOWN_SESSION")
        self.assertEqual(events(h, "SessionMandateAmended"), [])
        self.assertEqual(h.balance("CASH", "session:A"), F("100.00"))
        self.assertEqual(h.balance("CASH", "session:B"), F("100.00"))
        self.assertEqual(h.balance("CASH", "session:C"), F("0"))
        self.assertEqual(h.available("unallocated"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_amendment_refuses_invalid_terms(self):
        """SES-01 / ADR-0008: an amended mandate must still have a positive seed below a higher target."""
        h = self.waiting_request()
        before = mandate_record(h, "B")
        for terms in ({"seed": "90.00", "target_terminal_equity": "90.00"},
                      {"seed": "90.00", "target_terminal_equity": "89.99"},
                      {"seed": "0.00", "target_terminal_equity": "1.00"},
                      {"seed": "-5.00", "target_terminal_equity": "1.00"},
                      {"seed": "ninety"},
                      {"seed": "90.0000001", "target_terminal_equity": "95.00"},
                      {"target_terminal_equity": "100.00"}):
            with self.subTest(terms=terms):
                self.assertRejected(h.owner_cmd("amend_session_request", session_id="B", **terms), "MANDATE_INVALID")
                self.assertEqual(mandate_record(h, "B"), before)
        self.assertEqual(h.session_state("B"), "WAITING_FUNDS")
        self.assertEqual(events(h, "SessionMandateAmended"), [])
        self.assertEqual(h.balance("CASH", "unallocated"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_amendment_refuses_seed_above_authorized_capital(self):
        """ADR-0008 / ADR-0007: an amended request must be a mandate valid under its policy, exactly as at creation
        (create_session refuses seed > account_limits.authorized_capital)."""
        h = self.waiting_request()
        before = mandate_record(h, "B")
        self.assertRejected(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="X", seed="400.00",
                                        target_terminal_equity="420.00"), "MANDATE_INVALID")
        r = h.owner_cmd("amend_session_request", session_id="B", seed="400.00", target_terminal_equity="420.00")
        self.assertRejected(r, "MANDATE_INVALID")
        self.assertEqual(mandate_record(h, "B"), before)
        self.assertEqual(h.verify(), [])

    def test_amendment_with_a_non_string_amount_is_a_recorded_rejection(self):
        """ADR-0005 #2 / ADR-0007: a malformed owner amendment is a deterministic, recorded refusal (as create_session
        gives for the same input), not an unhandled fault that leaves no decision record."""
        h = self.waiting_request()
        before = mandate_record(h, "B")
        self.assertRejected(h.owner_cmd("create_session", account_key=ACCOUNT, session_id="X", seed=90,
                                        target_terminal_equity="95.00"), "MANDATE_INVALID")
        try:
            r = h.owner_cmd("amend_session_request", session_id="B", seed=90, target_terminal_equity="95.00")
        except TypeError as exc:
            self.fail(f"non-string seed escaped as an unhandled {type(exc).__name__}: {exc}")
        self.assertRejected(r, "MANDATE_INVALID")
        self.assertEqual(mandate_record(h, "B"), before)
        self.assertEqual(h.verify(), [])

    def test_amendment_keeps_the_request_policy_not_the_new_default(self):
        """SES-03 / ADR-0007: an explicit amendment changes only what the owner names; the new default does not ride along."""
        h = self.waiting_request()
        self.publish_activate(h, alt_policy(seed="60.00", target_terminal_equity="110.00",
                                            session_drawdown_trigger_bps=300, session_max_loss="4.00",
                                            strategy_eligibility=["owner-manual", "fixture-alt"]))
        r = h.owner_cmd("amend_session_request", session_id="B", seed="90.00", target_terminal_equity="95.00")
        self.assertTrue(r.accepted, r.body)
        m = mandate_body(h, "B")
        self.assertEqual(m["policy_hash"], h.policy_hash)
        self.assertEqual(m["session_drawdown_trigger_bps"], 200)
        self.assertEqual(F(m["session_max_loss"]), F("5.00"))
        self.assertEqual(m["strategy_eligibility"], ["owner-manual"])
        self.assertEqual((F(m["seed"]), F(m["target_terminal_equity"])), (F("90.00"), F("95.00")))
        self.assertEqual(h.balance("CASH", "session:B"), F("90.00"))
        self.assertEqual(h.verify(), [])

    def test_waiting_successor_is_frozen_until_the_owner_amends_it(self):
        """SES-03 / SES-10 / ADR-0007: a waiting successor keeps its snapshot across a default change; only an explicit,
        audited amendment changes it, and it keeps its successor origin."""
        h = self.harness()
        h.bootstrap(authorize="100.00")
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A", seed="90.00", target_terminal_equity="95.00")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.session_state("A"), "RUNNING")
        self.run_to_target(h, "A", "9", "A")
        a = h.row("sessions", "session_id", "A")
        self.assertEqual((a["state"], a["outcome"]), ("COMPLETED_UNSETTLED", "TARGET_REACHED"))
        self.assertEqual(F(a["protected_value"]), F("95.20"))
        (succ,) = successor_of(h, "A")
        self.assertEqual(h.session_state(succ), "WAITING_FUNDS")
        funding = h.row("sessions", "session_id", succ)["funding"]
        self.assertEqual(F(funding["shortfall"]), F("95.00"))  # 100.00 seed vs 10.00 unallocated - 5.00 buffer
        before = mandate_record(h, succ)
        self.assertEqual(json.loads(before[1])["origin"], "SUCCESSOR_OF:A")
        # A 5.00 default would fit the 5.00 of eligible cash; the frozen request must not pick it up.
        self.publish_activate(h, alt_policy(seed="5.00", target_terminal_equity="6.00"))
        h.tick()
        self.assertEqual(h.session_state(succ), "WAITING_FUNDS")
        self.assertEqual(mandate_record(h, succ), before)
        r = h.owner_cmd("amend_session_request", session_id=succ, seed="5.00", target_terminal_equity="5.50")
        self.assertTrue(r.accepted, r.body)
        m = mandate_body(h, succ)
        self.assertEqual((F(m["seed"]), F(m["target_terminal_equity"]), m["policy_hash"], m["origin"]),
                         (F("5.00"), F("5.50"), h.policy_hash, "SUCCESSOR_OF:A"))
        (audit,) = events(h, "SessionMandateAmended", succ)
        self.assertEqual(audit["info"]["previous"], before[0])
        self.assertEqual(h.balance("CASH", f"session:{succ}"), F("5.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("5.00"))
        self.assertEqual(h.session_state(succ), "RUNNING")
        self.assertEqual(successor_of(h, "A"), [succ])
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
