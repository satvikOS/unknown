"""Desk command contract: idempotency, stored rejections, the role permission
matrix, command deadlines, the writer lease and the event hash chain.

Decisions under test: ADR-0005 (event-sourced core, command idempotency, hash
chain), ADR-0007 (principals, permission table, mandates), ADR-0011 (writer
lease with fencing epoch). Requirements: ORD-01, ORD-08, AUT-01, AUT-02,
AUT-04, EXE-01, DOM-04. Every number is a test fixture.
"""
from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ACCOUNT, DEPLOYMENT, Harness, load_policy  # noqa: E402

from unknown.core.desk import Command, open_desk  # noqa: E402
from unknown.core.handlers import HANDLERS
from unknown.core.replay import chain_hash, replay_and_compare, verify_chain
from unknown.domain.canonical import sha256_hex
from unknown.domain.errors import CommandConflict, InvariantViolation, LeaseLost
from unknown.domain.fixed import Fixed
from unknown.domain.principal import Principal, Role, permitted
from unknown.store.db import LeaseHeld, Store
from unknown.store.schema import PROJECTION_TABLES

F = Fixed.parse
NS = 1_000_000_000
STRATEGY_POLICY = {"session_template__strategy_eligibility": ["owner-manual", "strategy.fixture"]}

# Hand-written oracle of ADR-0007's role table (deliberately NOT derived from
# domain/principal.py). Broker observations are SYSTEM's alone (ADR-0005 #5).
OWNER_KINDS = frozenset({
    "register_account", "publish_policy", "activate_policy", "register_instrument",
    "acquire_execution_ownership", "release_execution_ownership", "authorize_capital", "deauthorize_capital",
    "create_session", "amend_session_request", "cancel_session", "start_session", "suspend_session",
    "resume_session", "end_session", "submit_intent", "request_cancel", "stop", "start", "resolve_exception",
    "accept_fee_estimate", "submit_research_proposal",
})
SYSTEM_KINDS = frozenset({
    "process_started", "outbox_begin", "outbox_result", "broker_order_status", "broker_execution",
    "broker_commission", "broker_connectivity", "reconciliation_result", "valuation", "session_tick",
    "stop", "start_session", "request_cancel",
})  # SYSTEM never submits intents; wind-down sells go through create_order directly (review finding 40)
STRATEGY_KINDS = frozenset({"submit_intent", "request_cancel"})
AGENT_KINDS = frozenset({"submit_research_proposal"})
EXPECTED = {Role.OWNER: OWNER_KINDS, Role.SYSTEM: SYSTEM_KINDS, Role.STRATEGY: STRATEGY_KINDS, Role.AGENT: AGENT_KINDS}


# ----------------------------------------------------------------- helpers
def event_count(h: Harness) -> int:
    return h.desk.store.query("SELECT COUNT(*) AS n FROM events")[0]["n"]


def command_count(h: Harness) -> int:
    return h.desk.store.query("SELECT COUNT(*) AS n FROM commands")[0]["n"]


def last_seq(h: Harness) -> int:
    return h.desk.store.query("SELECT COALESCE(MAX(seq), 0) AS s FROM events")[0]["s"]


def events_after(h: Harness, seq: int) -> list[dict]:
    return [dict(r) for r in h.desk.store.query(
        "SELECT seq, type, command_id, principal_id, account_key, writer_epoch, payload FROM events WHERE seq>? ORDER BY seq",
        (seq,))]


def event(h: Harness, seq: int) -> dict:
    return dict(h.desk.store.query("SELECT * FROM events WHERE seq=?", (seq,))[0])


def stored_command(h: Harness, command_id: str) -> dict | None:
    rows = h.desk.store.query("SELECT * FROM commands WHERE command_id=?", (command_id,))
    return dict(rows[0]) if rows else None


def projections(h: Harness) -> dict:
    """Every projection table, row for row: the whole observable state."""
    q = h.desk.store.query
    return {t: [tuple(r) for r in q(f"SELECT * FROM {t} ORDER BY rowid")] for t in PROJECTION_TABLES}


def count(h: Harness, table: str) -> int:
    return h.desk.store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def intent_payload(h: Harness, order_id: str, side: str, qty: str, limit: str, session_id: str = "A", **extra) -> dict:
    """The same payload Harness.intent builds, returned so a retry can reuse it byte for byte."""
    sess = h.row("sessions", "session_id", session_id)
    mandate = json.loads(h.desk.store.query("SELECT body FROM mandates WHERE mandate_hash=?", (sess["mandate_hash"],))[0]["body"])
    return dict(account_key=ACCOUNT, session_id=session_id, order_id=order_id, instrument_id="FIX-EQ-1", side=side,
                quantity=qty, limit_price=limit, deadline_ns=h.clock.now_ns() + 60 * NS,
                policy_hash=mandate["policy_hash"], **extra)


def hold_amount(h: Harness, order_id: str) -> Fixed:
    rows = h.desk.store.query("SELECT amount, status FROM holds WHERE order_id=?", (order_id,))
    return sum((F(r["amount"]) for r in rows if r["status"] == "ACTIVE"), Fixed(0))


def valid_proposal(**overrides) -> dict:
    body = {"schema_version": 1, "job_id": "job-fixture-1", "hypothesis": "fixture hypothesis (test only)",
            "evidence": ["fixture evidence"], "horizon_seconds": 3600, "assumptions": ["fixture assumption"],
            "abstain": False, "reason": "fixture reason"}
    body.update(overrides)
    return body


def open_second_store(path, clock, holder: str, *, takeover: bool = False) -> Store:
    return Store(path, clock, holder=holder, takeover=takeover)


class DeskTestCase(unittest.TestCase):
    policy_overrides: dict = {}

    def setUp(self):
        self.h = Harness(policy=load_policy(**self.policy_overrides) if self.policy_overrides else None)
        self.addCleanup(self.h.close)

    def running_session(self, authorize: str = "300.00") -> Harness:
        h = self.h
        h.bootstrap(authorize=authorize)
        r = h.owner_cmd("create_session", account_key=ACCOUNT, session_id="A")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.session_state("A"), "RUNNING")
        self.assertEqual(h.available("session:A"), F("100.00"))
        h.quote("8.99", "9.01")
        return h

    def expect_lease_held(self, holder: str) -> str:
        """A second Store on the live file must refuse; drop its half-open connection quietly."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaises(LeaseHeld) as cm:
                open_second_store(self.h.db_path, self.h.clock, holder)
            message = str(cm.exception)
            del cm
            gc.collect()
        return message


# ======================================================================= ORD-01
class IdempotencyTests(DeskTestCase):

    def test_identical_retry_of_accepted_intent_returns_stored_result_without_new_events(self):
        """ORD-01 / ADR-0005 #4: an identical retry returns the stored result with
        duplicate=True, appends no event and reserves no more cash."""
        h = self.running_session()
        payload = intent_payload(h, "B1", "BUY", "1", "9.00")
        first = h.cmd("submit_intent", h.owner, command_id="cmd-intent-1", **payload)
        self.assertTrue(first.accepted, first.body)
        self.assertFalse(first.duplicate)
        self.assertTrue(first.events)
        # 1 x 9.00 notional + fee envelope max(0.005, 1.00) * 1.20 = 1.20 -> 10.20 held.
        self.assertEqual(hold_amount(h, "B1"), F("10.20"))
        self.assertEqual(h.available("session:A"), F("89.80"))
        events, commands, state = event_count(h), command_count(h), projections(h)

        retry = h.cmd("submit_intent", h.owner, command_id="cmd-intent-1", **payload)

        self.assertTrue(retry.duplicate)
        self.assertTrue(retry.accepted)
        self.assertEqual(retry.command_id, "cmd-intent-1")
        self.assertEqual(retry.body, first.body)
        self.assertEqual(retry.events, [])
        self.assertEqual(event_count(h), events)
        self.assertEqual(command_count(h), commands)
        self.assertEqual(projections(h), state)
        self.assertEqual(count(h, "orders"), 1)
        self.assertEqual(hold_amount(h, "B1"), F("10.20"))
        self.assertEqual(h.available("session:A"), F("89.80"))
        self.assertEqual(h.verify(), [])

    def test_identical_retry_of_capital_authorization_moves_money_once(self):
        """ORD-01: duplicates never duplicate money. A retried authorize_capital moves
        50.00 once; only a NEW command id moves it again."""
        h = self.h
        h.bootstrap(authorize="100.00")
        self.assertEqual(h.available("unallocated"), F("100.00"))
        r1 = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-50", account_key=ACCOUNT, amount="50.00")
        self.assertTrue(r1.accepted, r1.body)
        self.assertEqual(r1.body["authorized_total"], "150.000000")
        r2 = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-50", account_key=ACCOUNT, amount="50.00")
        self.assertTrue(r2.duplicate)
        self.assertEqual(r2.body, r1.body)
        self.assertEqual(r2.events, [])
        self.assertEqual(h.available("unallocated"), F("150.00"))
        self.assertEqual(h.balance("CASH", "unallocated"), F("150.00"))
        r3 = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-50-b", account_key=ACCOUNT, amount="50.00")
        self.assertTrue(r3.accepted, r3.body)
        self.assertFalse(r3.duplicate)
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertEqual(h.verify(), [])

    def test_command_identity_survives_restart(self):
        """ORD-01 'persist command identities': after a process restart an identical
        retry is still recognised and returns the stored result."""
        h = self.h
        h.bootstrap(authorize="100.00")
        r1 = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-restart", account_key=ACCOUNT, amount="25.00")
        self.assertTrue(r1.accepted, r1.body)
        h.restart()
        events = event_count(h)
        r2 = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-restart", account_key=ACCOUNT, amount="25.00")
        self.assertTrue(r2.duplicate)
        self.assertTrue(r2.accepted)
        self.assertEqual(r2.body, r1.body)
        self.assertEqual(event_count(h), events)
        self.assertEqual(h.available("unallocated"), F("125.00"))
        self.assertEqual(h.verify(), [])

    def test_changed_payload_under_reused_id_raises_conflict_and_changes_nothing(self):
        """ORD-01 / ADR-0005 #4: reusing a command id with a changed payload (or a changed
        kind) is refused with CommandConflict; the stored result is untouched."""
        h = self.h
        h.bootstrap(authorize="100.00")
        r1 = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-x", account_key=ACCOUNT, amount="50.00")
        self.assertTrue(r1.accepted, r1.body)
        stored = stored_command(h, "cmd-auth-x")
        state = projections(h)

        with self.assertRaises(CommandConflict):
            h.cmd("authorize_capital", h.owner, command_id="cmd-auth-x", account_key=ACCOUNT, amount="60.00")
        with self.assertRaises(CommandConflict):  # same payload, different kind: still a different command
            h.cmd("deauthorize_capital", h.owner, command_id="cmd-auth-x", account_key=ACCOUNT, amount="50.00")
        with self.assertRaises(CommandConflict):  # an added field changes the payload too
            h.cmd("authorize_capital", h.owner, command_id="cmd-auth-x", account_key=ACCOUNT, amount="50.00", currency="USD")

        self.assertEqual(stored_command(h, "cmd-auth-x"), stored)
        self.assertEqual(projections(h), state)
        self.assertEqual(h.available("unallocated"), F("150.00"))
        again = h.cmd("authorize_capital", h.owner, command_id="cmd-auth-x", account_key=ACCOUNT, amount="50.00")
        self.assertTrue(again.duplicate)
        self.assertEqual(again.body, r1.body)
        self.assertEqual(h.verify(), [])

    def test_conflicting_reuse_is_recorded_as_a_security_relevant_event(self):
        """ADR-0005 #4 (ORD-01): 'Reusing a command_id with a changed payload is rejected
        AND recorded as a security-relevant event.' The refusal must leave a trace
        in the event log that names the reused command id."""
        h = self.h
        h.bootstrap(authorize="100.00")
        self.assertTrue(h.cmd("authorize_capital", h.owner, command_id="cmd-reuse", account_key=ACCOUNT,
                              amount="10.00").accepted)
        before = last_seq(h)
        with self.assertRaises(CommandConflict):
            h.cmd("authorize_capital", h.owner, command_id="cmd-reuse", account_key=ACCOUNT, amount="99.00")
        recorded = [e for e in events_after(h, before) if e["command_id"] == "cmd-reuse" or "cmd-reuse" in e["payload"]]
        self.assertTrue(recorded, "no event records the conflicting reuse of command id cmd-reuse")
        self.assertEqual(h.available("unallocated"), F("110.00"))
        self.assertEqual(h.verify(), [])

    def test_reused_id_from_a_different_principal_is_not_reported_as_accepted(self):
        """ORD-01 / ADR-0005 #4 (commands store the principal) and AUT-04: an AGENT that
        replays an OWNER's command id and payload is not the same command; it must
        not be told that authorize_capital was accepted for it."""
        h = self.h
        h.bootstrap(authorize="100.00")
        owner_result = h.cmd("authorize_capital", h.owner, command_id="cmd-owner-auth", account_key=ACCOUNT, amount="20.00")
        self.assertTrue(owner_result.accepted, owner_result.body)
        state = projections(h)
        try:
            replay = h.cmd("authorize_capital", h.agent, command_id="cmd-owner-auth", account_key=ACCOUNT, amount="20.00")
        except CommandConflict:
            replay = None
        if replay is not None:
            self.assertFalse(replay.accepted, f"AGENT replay of an OWNER command id returned {replay}")
        self.assertEqual(projections(h), state)
        self.assertEqual(h.available("unallocated"), F("120.00"))
        self.assertEqual(h.verify(), [])


# =========================================================== stored rejections
class RejectionRecordTests(DeskTestCase):

    def test_rejected_command_is_stored_with_a_command_rejected_event(self):
        """ORD-08 / ADR-0005 #4: a deterministic rejection is a decision record: the
        command row has status REJECTED with the rejection as its result, and exactly
        one CommandRejected event carries the same body; nothing is reserved."""
        h = self.running_session()
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        state = projections(h)
        payload = intent_payload(h, "B1", "BUY", "1", "9.00")
        r = h.cmd("submit_intent", h.owner, command_id="cmd-rej-1", **payload)

        self.assertFalse(r.accepted)
        self.assertFalse(r.duplicate)
        self.assertEqual(r.rejected_code, "STOP_LATCH")
        self.assertEqual(r.body["details"]["decision"]["code"], "STOP_LATCH")
        row = stored_command(h, "cmd-rej-1")
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["kind"], "submit_intent")
        self.assertEqual(row["principal_role"], "OWNER")
        self.assertEqual(json.loads(row["result"]), r.body)
        self.assertEqual(len(r.events), 1)
        ev = event(h, r.events[0])
        self.assertEqual(ev["type"], "CommandRejected")
        self.assertEqual(ev["command_id"], "cmd-rej-1")
        self.assertEqual(ev["account_key"], ACCOUNT)
        info = json.loads(ev["payload"])["info"]
        self.assertEqual(info["kind"], "submit_intent")
        self.assertEqual(info["rejection"], r.body)
        self.assertEqual(info["payload_hash"], row["payload_hash"])
        self.assertEqual(json.loads(ev["payload"])["changes"], {})
        self.assertEqual(projections(h), state)
        self.assertIsNone(h.row("orders", "order_id", "B1"))
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_identical_retry_returns_the_same_rejection_after_state_changes(self):
        """ORD-01 / ORD-08: a rejection is the command's result. After the stop is
        cleared, an identical retry still returns the stored STOP_LATCH rejection
        (duplicate, no event, no order); only a new command id is re-evaluated."""
        h = self.running_session()
        self.assertTrue(h.owner_cmd("stop", account_key=ACCOUNT).accepted)
        payload = intent_payload(h, "B1", "BUY", "1", "9.00")
        first = h.cmd("submit_intent", h.owner, command_id="cmd-rej-2", **payload)
        self.assertEqual(first.rejected_code, "STOP_LATCH")

        started = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(started.accepted, started.body)
        self.assertFalse(h.control()["stop_latched"])
        events = event_count(h)

        retry = h.cmd("submit_intent", h.owner, command_id="cmd-rej-2", **payload)
        self.assertTrue(retry.duplicate)
        self.assertFalse(retry.accepted)
        self.assertEqual(retry.rejected_code, "STOP_LATCH")
        self.assertEqual(retry.body, first.body)
        self.assertEqual(retry.events, [])
        self.assertEqual(event_count(h), events)
        self.assertIsNone(h.row("orders", "order_id", "B1"))
        self.assertEqual(h.available("session:A"), F("100.00"))

        fresh = h.cmd("submit_intent", h.owner, command_id="cmd-rej-2-new", **payload)
        self.assertTrue(fresh.accepted, fresh.body)
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "RESERVED")
        self.assertEqual(h.available("session:A"), F("89.80"))
        self.assertEqual(h.verify(), [])

    def test_unauthorized_attempt_is_a_recorded_decision_but_never_claims_the_id(self):
        """AUT-04 / ORD-08: an AGENT's attempt to authorize capital is rejected
        UNAUTHORIZED and recorded as an event with the agent's identity, but is not
        stored under the presented command id: authorization precedes dedup, so no
        principal can pre-claim another principal's ids (review findings 5, 19).
        A retry is rejected again, identically, and nothing moves."""
        h = self.h
        h.bootstrap(authorize="100.00")
        state = projections(h)
        r = h.cmd("authorize_capital", h.agent, command_id="cmd-agent-auth", account_key=ACCOUNT, amount="50.00")
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertIsNone(stored_command(h, "cmd-agent-auth"))
        (seq,) = r.events
        ev = event(h, seq)
        self.assertEqual((ev["type"], ev["principal_id"], ev["account_key"]), ("CommandRejected", "agent.research", ACCOUNT))
        retry = h.cmd("authorize_capital", h.agent, command_id="cmd-agent-auth", account_key=ACCOUNT, amount="50.00")
        self.assertFalse(retry.duplicate)
        self.assertEqual(retry.body, r.body)
        self.assertEqual(event(h, retry.events[0])["type"], "CommandRejected")
        self.assertEqual(projections(h), state)
        self.assertEqual(h.available("unallocated"), F("100.00"))
        self.assertEqual(h.verify(), [])
        # The owner can still use that id afterwards: the agent's attempt claimed nothing.
        own = h.cmd("authorize_capital", h.owner, command_id="cmd-agent-auth", account_key=ACCOUNT, amount="50.00")
        self.assertTrue(own.accepted, own.body)
        self.assertEqual(h.verify(), [])


# ============================================================ permission matrix
class PermissionMatrixTests(DeskTestCase):

    def test_oracle_names_only_real_command_kinds(self):
        """ADR-0007: the hand-written role table covers real handlers (guards the oracle itself)."""
        for role, kinds in EXPECTED.items():
            with self.subTest(role=role.value):
                self.assertLessEqual(kinds, set(HANDLERS), kinds - set(HANDLERS))

    def test_permission_table_matches_adr_0007_for_every_role_and_kind(self):
        """AUT-01 / AUT-02 / AUT-04 / ADR-0007 'tests cover every role/command pair':
        permitted(role, kind) equals the ADR table for all 4 x |HANDLERS| pairs."""
        for role in Role:
            for kind in sorted(HANDLERS):
                with self.subTest(role=role.value, kind=kind):
                    self.assertEqual(permitted(role, kind), kind in EXPECTED[role])

    def test_every_forbidden_pair_is_rejected_unauthorized_by_the_desk(self):
        """AUT-01 / AUT-04 / ADR-0007: end to end through Desk.execute, every role x kind
        pair outside the table is rejected UNAUTHORIZED before any handler runs; the
        only trace is one CommandRejected event per attempt; no projection changes."""
        h = self.running_session()
        principals = {Role.OWNER: h.owner, Role.SYSTEM: h.desk.system(), Role.STRATEGY: h.strategy, Role.AGENT: h.agent}
        state = projections(h)
        before = last_seq(h)
        attempts = 0
        for role, principal in principals.items():
            for kind in sorted(set(HANDLERS) - EXPECTED[role]):
                with self.subTest(role=role.value, kind=kind):
                    r = h.cmd(kind, principal, command_id=f"forbidden.{role.value}.{kind}", account_key=ACCOUNT)
                    attempts += 1
                    self.assertFalse(r.accepted)
                    self.assertEqual(r.rejected_code, "UNAUTHORIZED")
                    self.assertIsNone(stored_command(h, f"forbidden.{role.value}.{kind}"))
        new = events_after(h, before)
        self.assertEqual(len(new), attempts)
        self.assertEqual({e["type"] for e in new}, {"CommandRejected"})
        self.assertEqual(projections(h), state)
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertEqual(h.verify(), [])

    def test_agent_cannot_mutate_the_core_with_well_formed_payloads(self):
        """AUT-04: no agent may raise its budget, widen policy, stop/start, order, or
        bypass the gateway; every well-formed attempt is UNAUTHORIZED and changes nothing."""
        h = self.running_session()
        widened = load_policy(account_limits__max_order_notional="1000.00", name="agent-widened")
        attempts = {
            "authorize_capital": dict(account_key=ACCOUNT, amount="1.00"),
            "deauthorize_capital": dict(account_key=ACCOUNT, amount="1.00"),
            "publish_policy": dict(body=widened),
            "activate_policy": dict(account_key=ACCOUNT, policy_hash=h.policy_hash),
            "create_session": dict(account_key=ACCOUNT, session_id="AGENT-S"),
            "stop": dict(account_key=ACCOUNT),
            "start": dict(account_key=ACCOUNT),
            "submit_intent": intent_payload(h, "AG1", "BUY", "1", "9.00", strategy_id="owner-manual"),
            "outbox_begin": dict(outbox_id="AG1:submit"),
            "broker_execution": dict(account_key=ACCOUNT, exec_key="sim:exec:forged", quantity="1", price="9.00"),
            "resolve_exception": dict(case_id="case-x", note="self-approved"),
            "acquire_execution_ownership": dict(account_key=ACCOUNT, deployment_id=DEPLOYMENT),
        }
        state = projections(h)
        for kind, payload in attempts.items():
            with self.subTest(kind=kind):
                r = h.cmd(kind, h.agent, **payload)
                self.assertEqual(r.rejected_code, "UNAUTHORIZED", r.body)
        self.assertEqual(projections(h), state)
        self.assertIsNone(h.row("orders", "order_id", "AG1"))
        self.assertFalse(h.control()["stop_latched"])
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_system_cannot_start_authorize_publish_or_activate(self):
        """ADR-0007 SYSTEM row / RSK-05: SYSTEM may latch a stop but never clear it,
        authorize capital or widen policy; the owner can."""
        h = self.h
        h.bootstrap(authorize="100.00")
        system = h.desk.system()
        latched = h.cmd("stop", system, account_key=ACCOUNT, reason="RISK_TRIGGER_FIXTURE")
        self.assertTrue(latched.accepted, latched.body)
        self.assertTrue(h.control()["stop_latched"])
        self.assertEqual(h.control()["latched_by"], system.id)

        state = projections(h)
        widened = load_policy(account_limits__max_order_notional="1000.00", name="system-widened")
        for kind, payload in {
            "start": dict(account_key=ACCOUNT),
            "authorize_capital": dict(account_key=ACCOUNT, amount="50.00"),
            "publish_policy": dict(body=widened),
            "activate_policy": dict(account_key=ACCOUNT, policy_hash=h.policy_hash),
            "resolve_exception": dict(case_id="case-x", note="system self-clear"),
        }.items():
            with self.subTest(kind=kind):
                self.assertEqual(h.cmd(kind, system, **payload).rejected_code, "UNAUTHORIZED")
        self.assertEqual(projections(h), state)
        self.assertTrue(h.control()["stop_latched"])
        self.assertEqual(h.available("unallocated"), F("100.00"))

        cleared = h.owner_cmd("start", account_key=ACCOUNT)
        self.assertTrue(cleared.accepted, cleared.body)
        self.assertFalse(h.control()["stop_latched"])
        self.assertEqual(h.verify(), [])


class StrategyScopeTests(DeskTestCase):
    policy_overrides = STRATEGY_POLICY

    def test_strategy_may_submit_for_a_session_whose_mandate_lists_it(self):
        """ADR-0007 STRATEGY row / AUT-02: a strategy listed in the session mandate may
        submit an intent; the order records the strategy principal and reserves 10.20."""
        h = self.running_session()
        r = h.cmd("submit_intent", h.strategy, **intent_payload(h, "S1", "BUY", "1", "9.00", strategy_id="strategy.fixture"))
        self.assertTrue(r.accepted, r.body)
        o = h.row("orders", "order_id", "S1")
        self.assertEqual((o["principal_id"], o["principal_role"], o["strategy_id"]), ("strategy.fixture", "STRATEGY", "strategy.fixture"))
        self.assertEqual(hold_amount(h, "S1"), F("10.20"))
        self.assertEqual(h.available("session:A"), F("89.80"))
        self.assertEqual(h.verify(), [])

    def test_strategy_outside_the_mandate_is_rejected(self):
        """ADR-0007 / AUT-02: a strategy not listed in the mandate is rejected
        STRATEGY_PERMITTED; a strategy intent must name its strategy id."""
        h = self.running_session()
        other = Principal("strategy.other", Role.STRATEGY)
        r = h.cmd("submit_intent", other, **intent_payload(h, "X1", "BUY", "1", "9.00", strategy_id="strategy.other"))
        self.assertEqual(r.rejected_code, "STRATEGY_PERMITTED")
        r = h.cmd("submit_intent", h.strategy, **intent_payload(h, "X2", "BUY", "1", "9.00"))
        self.assertEqual(r.rejected_code, "INVALID_FIELD")
        self.assertEqual(count(h, "orders"), 0)
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_strategy_cannot_claim_another_strategys_mandated_id(self):
        """ADR-0007 'the core never accepts identity from a caller-controlled payload
        field' / AUT-04: a STRATEGY principal that is not in the mandate must not gain
        the mandate's permission by writing a listed strategy_id into its payload."""
        h = self.running_session()
        rogue = Principal("strategy.rogue", Role.STRATEGY)
        for n, claimed in enumerate(("strategy.fixture", "owner-manual")):
            with self.subTest(claimed=claimed):
                r = h.cmd("submit_intent", rogue, **intent_payload(h, f"R{n}", "BUY", "1", "9.00", strategy_id=claimed))
                self.assertFalse(r.accepted, f"strategy.rogue ordered as {claimed}: {r.body}")
                self.assertIsNone(h.row("orders", "order_id", f"R{n}"))
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_strategy_permission_follows_the_session_mandate_not_the_active_default(self):
        """ADR-0007 mandates / SES-03: a mandate is a frozen snapshot, so a strategy
        added to a NEW default does not gain permission on an existing session."""
        h = self.h
        h.policy_body = load_policy()  # default template lists only owner-manual
        self.running_session()
        listed = load_policy(name="with-strategy", session_template__strategy_eligibility=["owner-manual", "strategy.fixture"])
        pub = h.owner_cmd("publish_policy", body=listed)
        self.assertTrue(pub.accepted, pub.body)
        self.assertTrue(h.owner_cmd("activate_policy", account_key=ACCOUNT, policy_hash=pub.body["policy_hash"]).accepted)
        r = h.cmd("submit_intent", h.strategy, **intent_payload(h, "S1", "BUY", "1", "9.00", strategy_id="strategy.fixture"))
        self.assertEqual(r.rejected_code, "STRATEGY_PERMITTED")
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])

    def test_strategy_may_cancel_only_its_own_orders(self):
        """ADR-0007 STRATEGY row / ORD-04: a strategy cancels its own order (hold
        released) but not the owner's or another strategy's (hold kept)."""
        h = self.running_session()
        self.assertTrue(h.cmd("submit_intent", h.strategy,
                              **intent_payload(h, "S1", "BUY", "1", "9.00", strategy_id="strategy.fixture")).accepted)
        self.assertTrue(h.cmd("submit_intent", h.owner, **intent_payload(h, "O1", "BUY", "1", "9.00")).accepted)
        self.assertEqual(h.available("session:A"), F("79.60"))

        r = h.cmd("request_cancel", h.strategy, order_id="O1")
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        r = h.cmd("request_cancel", Principal("strategy.other", Role.STRATEGY), order_id="S1")
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertEqual(h.row("orders", "order_id", "O1")["state"], "RESERVED")
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "RESERVED")
        self.assertEqual(h.available("session:A"), F("79.60"))

        r = h.cmd("request_cancel", h.strategy, order_id="S1")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.row("orders", "order_id", "S1")["state"], "CANCELED")
        self.assertEqual(hold_amount(h, "S1"), F("0"))
        self.assertEqual(hold_amount(h, "O1"), F("10.20"))
        self.assertEqual(h.available("session:A"), F("89.80"))
        self.assertEqual(h.verify(), [])


# ===================================================================== deadline
class DeadlineTests(DeskTestCase):

    def execute(self, command_id: str, kind: str, payload: dict, deadline_ns: int | None, principal=None):
        return self.h.desk.execute(Command(command_id, kind, principal or self.h.owner, payload, deadline_ns=deadline_ns))

    def test_expired_command_deadline_is_a_stored_rejection_with_no_order(self):
        """ADR-0005 command pipeline / ORD-08: a command executed after its deadline_ns
        is rejected DEADLINE_EXPIRED, stored, and reserves nothing; a retry returns it."""
        h = self.running_session()
        payload = intent_payload(h, "D1", "BUY", "1", "9.00")
        r = self.execute("cmd-late", "submit_intent", payload, deadline_ns=h.clock.now_ns() - 1)
        self.assertEqual(r.rejected_code, "DEADLINE_EXPIRED")
        self.assertEqual(stored_command(h, "cmd-late")["status"], "REJECTED")
        self.assertEqual([event(h, s)["type"] for s in r.events], ["CommandRejected"])
        self.assertIsNone(h.row("orders", "order_id", "D1"))
        self.assertEqual(h.available("session:A"), F("100.00"))
        retry = self.execute("cmd-late", "submit_intent", payload, deadline_ns=h.clock.now_ns() + 60 * NS)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.rejected_code, "DEADLINE_EXPIRED")
        self.assertIsNone(h.row("orders", "order_id", "D1"))
        self.assertEqual(h.verify(), [])

    def test_deadline_is_inclusive_at_the_current_instant(self):
        """ADR-0005 command pipeline: a deadline equal to now has not passed (strict >)."""
        h = self.h
        h.bootstrap(authorize="100.00")
        r = self.execute("cmd-on-time", "authorize_capital", dict(account_key=ACCOUNT, amount="40.00"),
                         deadline_ns=h.clock.now_ns())
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.available("unallocated"), F("140.00"))
        self.assertEqual(h.verify(), [])

    def test_accepted_command_retried_after_its_deadline_returns_the_stored_result(self):
        """ORD-01 / ADR-0005 #4: deduplication precedes the deadline, so a late identical
        retry of an accepted command is a duplicate, not a DEADLINE_EXPIRED rejection."""
        h = self.h
        h.bootstrap(authorize="100.00")
        deadline = h.clock.now_ns() + 5 * NS
        payload = dict(account_key=ACCOUNT, amount="30.00")
        first = self.execute("cmd-dl", "authorize_capital", payload, deadline_ns=deadline)
        self.assertTrue(first.accepted, first.body)
        h.clock.advance(10 * NS)
        retry = self.execute("cmd-dl", "authorize_capital", payload, deadline_ns=deadline)
        self.assertTrue(retry.accepted)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.body, first.body)
        self.assertEqual(h.available("unallocated"), F("130.00"))
        self.assertEqual(h.verify(), [])


# ================================================================= writer lease
class WriterLeaseTests(DeskTestCase):

    def lease(self) -> dict:
        return dict(self.h.desk.store.query("SELECT holder, epoch, heartbeat_at_ns FROM writer_lease WHERE id=1")[0])

    def test_second_writer_is_refused_while_the_lease_is_live(self):
        """EXE-01 / ADR-0011 #1: a second process on the same database cannot become a
        writer while the first holds a live lease; the first keeps writing."""
        h = self.h
        h.bootstrap(authorize="100.00")
        lease = self.lease()
        message = self.expect_lease_held("aws-standby:1")
        self.assertIn(lease["holder"], message)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaises(LeaseHeld):
                open_desk(h.db_path, h.clock, h.ids, deployment_id="aws-standby", holder="aws-standby:1")
            gc.collect()
        self.assertEqual(self.lease(), lease)
        r = h.owner_cmd("authorize_capital", account_key=ACCOUNT, amount="10.00")
        self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.available("unallocated"), F("110.00"))
        self.assertEqual(h.verify(), [])

    def test_heartbeat_keeps_the_lease_live_past_the_stale_window(self):
        """EXE-01 / ADR-0011 #1: a heartbeating writer is never treated as stale."""
        h = self.h
        h.clock.advance(20 * NS)
        h.desk.store.heartbeat()
        h.clock.advance(20 * NS)  # 40 s since acquisition, 20 s since the heartbeat
        self.expect_lease_held("aws-standby:1")

    def test_takeover_fences_the_previous_writer(self):
        """EXE-01 / ADR-0011 #1: takeover=True raises the epoch; the old process's next
        write raises LeaseLost, commits nothing, and cannot transmit an order."""
        h = self.running_session()
        payload = intent_payload(h, "B1", "BUY", "1", "9.00")
        self.assertTrue(h.cmd("submit_intent", h.owner, **payload).accepted)
        old_epoch = h.desk.store.epoch
        events_before = event_count(h)

        new = open_desk(h.db_path, h.clock, h.ids, deployment_id="aws-standby", holder="aws-standby:1", takeover=True,
                        health=h.health)
        self.addCleanup(new.store.close)
        self.assertEqual(new.store.epoch, old_epoch + 1)
        takeover_events = events_after(h, last_seq(h) - (event_count(h) - events_before))
        self.assertTrue(takeover_events)
        self.assertEqual({e["writer_epoch"] for e in takeover_events}, {old_epoch + 1})

        events = event_count(h)
        with self.assertRaises(LeaseLost):
            h.desk.store.heartbeat()
        with self.assertRaises(LeaseLost):
            h.cmd("authorize_capital", h.owner, command_id="cmd-fenced", account_key=ACCOUNT, amount="1.00")
        with self.assertRaises(LeaseLost):
            h.pump()
        self.assertIsNone(stored_command(h, "cmd-fenced"))
        self.assertEqual(event_count(h), events)
        orders, complete = h.broker.open_orders()
        self.assertTrue(complete)
        self.assertEqual(orders, [])
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "RESERVED")
        self.assertEqual(h.available("session:A"), F("89.80"))
        self.assertEqual(new.verify(), [])

    def test_stale_lease_is_taken_without_takeover_and_fences_the_old_writer(self):
        """EXE-01 / ADR-0011 #1: a lease without a heartbeat for 30 s is stale; a new
        holder acquires it at epoch+1 and the old writer is fenced."""
        h = self.h
        old_epoch = h.desk.store.epoch
        h.clock.advance(31 * NS)
        second = open_second_store(h.db_path, h.clock, "aws-standby:1")
        self.addCleanup(second.close)
        self.assertEqual(second.epoch, old_epoch + 1)
        with self.assertRaises(LeaseLost):
            h.desk.store.heartbeat()
        second.heartbeat()


# =================================================================== hash chain
class HashChainTests(DeskTestCase):

    def active_history(self) -> Harness:
        h = self.running_session()
        self.assertTrue(h.cmd("submit_intent", h.owner, **intent_payload(h, "B1", "BUY", "2", "9.00")).accepted)
        h.pump()
        h.broker.fill("B1", Fixed(2), F("9.00"))
        h.pump()
        self.assertEqual(h.row("orders", "order_id", "B1")["state"], "FILLED")
        self.assertEqual(h.verify(), [])
        return h

    def capital_event_seq(self, h: Harness) -> int:
        return h.desk.store.query("SELECT seq FROM events WHERE type='CapitalAuthorized' ORDER BY seq LIMIT 1")[0]["seq"]

    def test_chain_verifies_and_every_event_is_linked(self):
        """ADR-0005 #1 / #6 / DOM-04: every event carries identity, schema version and
        correlation; each links to its predecessor from the genesis hash."""
        h = self.active_history()
        self.assertEqual(verify_chain(h.desk.store.conn), [])
        rows = [dict(r) for r in h.desk.store.query("SELECT * FROM events ORDER BY seq")]
        self.assertGreater(len(rows), 10)
        self.assertEqual(rows[0]["prev_hash"], "0" * 64)
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur["prev_hash"], prev["chain_hash"])
        for r in rows:
            self.assertEqual(r["payload_hash"], sha256_hex(r["payload"]))
            self.assertEqual(r["schema_version"], 1)
            self.assertTrue(r["event_id"] and r["command_id"] and r["correlation_id"])
        self.assertEqual(len({r["event_id"] for r in rows}), len(rows))

    def test_tampered_payload_is_detected(self):
        """ADR-0005 #6: editing an event payload in place breaks its payload hash."""
        h = self.active_history()
        seq = self.capital_event_seq(h)
        original = event(h, seq)["payload"]
        forged = original.replace('"300.000000"', '"3000.000000"')
        self.assertNotEqual(forged, original)
        h.desk.store.conn.execute("UPDATE events SET payload=? WHERE seq=?", (forged, seq))
        self.assertEqual(verify_chain(h.desk.store.conn), [f"event {seq} payload hash mismatch"])

    def test_tamper_with_recomputed_payload_hash_breaks_the_chain(self):
        """ADR-0005 #6: re-hashing a forged payload is caught by the chain hash; also
        rewriting the chain hash is caught at the next event's link."""
        h = self.active_history()
        seq = self.capital_event_seq(h)
        forged = event(h, seq)["payload"].replace('"300.000000"', '"3000.000000"')
        h.desk.store.conn.execute("UPDATE events SET payload=?, payload_hash=? WHERE seq=?", (forged, sha256_hex(forged), seq))
        self.assertEqual(verify_chain(h.desk.store.conn), [f"event {seq} chain hash mismatch"])

        ev = event(h, seq)
        rechained = chain_hash(ev["prev_hash"], ev)
        h.desk.store.conn.execute("UPDATE events SET chain_hash=? WHERE seq=?", (rechained, seq))
        problems = verify_chain(h.desk.store.conn)
        self.assertIn(f"event {seq + 1} prev_hash mismatch", problems)
        self.assertNotIn(f"event {seq} chain hash mismatch", problems)

    def test_deleted_event_is_detected(self):
        """ADR-0005 #6: removing an event from the middle of the log breaks the next link."""
        h = self.active_history()
        seq = self.capital_event_seq(h)
        h.desk.store.conn.execute("DELETE FROM events WHERE seq=?", (seq,))
        self.assertIn(f"event {seq + 1} prev_hash mismatch", verify_chain(h.desk.store.conn))

    def test_replay_audit_reports_a_balanced_forgery(self):
        """ADR-0005 #3 / #6: the replay audit is clean on the true log; after a
        self-consistent forgery (capital inflated 300 -> 3000 on BOTH legs, so the
        journal still balances) it reports the payload-hash break AND the balance
        projections that no longer reproduce."""
        h = self.active_history()
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(replay_and_compare(h.db_path, Path(d) / "clean.sqlite"), [])
            seq = self.capital_event_seq(h)
            forged = (event(h, seq)["payload"].replace('"300.000000"', '"3000.000000"')
                      .replace('"-300.000000"', '"-3000.000000"'))
            h.desk.store.conn.execute("UPDATE events SET payload=? WHERE seq=?", (forged, seq))
            problems = replay_and_compare(h.db_path, Path(d) / "tampered.sqlite")
        self.assertIn(f"event {seq} payload hash mismatch", problems)
        self.assertTrue(any(p.startswith("balances:") for p in problems), problems)
        self.assertEqual(h.balance("CASH", "unallocated"), F("200.00"))  # the live projection is untouched
        self.assertEqual(h.verify(), [])

    def test_replay_audit_never_reports_clean_on_an_unbalanced_forgery(self):
        """ADR-0005 #3: a forged event that unbalances a journal entry must not pass the
        audit: it either raises InvariantViolation or returns problems (fail closed)."""
        h = self.active_history()
        seq = self.capital_event_seq(h)
        forged = event(h, seq)["payload"].replace('"300.000000"', '"3000.000000"')  # one leg only
        h.desk.store.conn.execute("UPDATE events SET payload=? WHERE seq=?", (forged, seq))
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            try:
                problems = replay_and_compare(h.db_path, Path(d) / "tampered.sqlite")
            except InvariantViolation:
                problems = ["raised InvariantViolation"]
            gc.collect()
        self.assertNotEqual(problems, [])


# ============================================================ research proposals
class ResearchProposalTests(DeskTestCase):

    def test_valid_proposal_is_recorded_and_moves_no_money(self):
        """AUT-02 / AUT-04 / ADR-0007 AGENT row: an agent's only write is a typed,
        non-executable proposal: one ResearchProposalRecorded event whose only change
        is the proposals row; balances, holds, orders and outbox are untouched."""
        h = self.running_session()
        state = projections(h)
        body = valid_proposal()
        r = h.cmd("submit_research_proposal", h.agent, proposal=body)
        self.assertTrue(r.accepted, r.body)
        (seq,) = r.events
        ev = event(h, seq)
        self.assertEqual(ev["type"], "ResearchProposalRecorded")
        self.assertEqual(ev["principal_id"], "agent.research")
        self.assertEqual(list(json.loads(ev["payload"])["changes"]), ["proposals"])
        row = h.row("proposals", "proposal_id", r.body["proposal_id"])
        self.assertEqual(row["author"], "agent.research")
        self.assertEqual(row["body"], body)
        after = projections(h)
        self.assertEqual(len(after["proposals"]), len(state["proposals"]) + 1)
        for table in PROJECTION_TABLES:
            if table != "proposals":
                self.assertEqual(after[table], state[table], table)
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.available("unallocated"), F("200.00"))
        self.assertEqual(h.verify(), [])

    def test_proposal_boundaries_are_accepted(self):
        """AUT-02 / contracts/research_proposal.schema.json: values at the schema's limits are valid."""
        h = self.h
        h.bootstrap()
        for name, body in {
            "hypothesis_4000": valid_proposal(hypothesis="h" * 4000),
            "evidence_50x500": valid_proposal(evidence=["e" * 500] * 50),
            "horizon_min": valid_proposal(horizon_seconds=1),
            "horizon_max": valid_proposal(horizon_seconds=31_536_000),
            "abstain_true": valid_proposal(abstain=True),
            "empty_lists": valid_proposal(evidence=[], assumptions=[]),
        }.items():
            with self.subTest(case=name):
                r = h.cmd("submit_research_proposal", h.agent, proposal=body)
                self.assertTrue(r.accepted, r.body)
        self.assertEqual(h.verify(), [])

    def test_malformed_proposals_are_rejected(self):
        """AUT-02 / AUT-04: shape violations (missing or extra keys, an embedded order,
        wrong version, oversize or mistyped fields) are rejected PROPOSAL_INVALID and
        record nothing."""
        h = self.h
        h.bootstrap()
        missing = valid_proposal()
        del missing["reason"]
        cases = {
            "not_an_object": "buy FIXA",
            "missing_key": missing,
            "extra_key_order": valid_proposal(order={"side": "BUY", "quantity": "1", "limit_price": "9.00"}),
            "extra_key_budget": valid_proposal(budget="1000000.00"),
            "schema_version_2": valid_proposal(schema_version=2),
            "hypothesis_4001": valid_proposal(hypothesis="h" * 4001),
            "hypothesis_not_str": valid_proposal(hypothesis=["h"]),
            "evidence_51": valid_proposal(evidence=["e"] * 51),
            "evidence_item_501": valid_proposal(evidence=["e" * 501]),
            "evidence_not_list": valid_proposal(evidence="e"),
            "horizon_zero": valid_proposal(horizon_seconds=0),
            "horizon_too_long": valid_proposal(horizon_seconds=31_536_001),
            "horizon_bool": valid_proposal(horizon_seconds=True),
            "horizon_string": valid_proposal(horizon_seconds="3600"),
            "abstain_not_bool": valid_proposal(abstain="no"),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                r = h.cmd("submit_research_proposal", h.agent, proposal=body)
                self.assertEqual(r.rejected_code, "PROPOSAL_INVALID", r.body)
        self.assertEqual(count(h, "proposals"), 0)
        self.assertEqual(h.verify(), [])

    def test_proposal_fields_are_validated_against_the_contract_schema(self):
        """AUT-02 / contracts/research_proposal.schema.json (the contract the handler
        cites): job_id is a 1..160 char string, assumptions <= 30 strings of <= 500,
        reason a string of <= 2000, schema_version the integer 1 (not true)."""
        h = self.h
        h.bootstrap()
        cases = {
            "job_id_int": valid_proposal(job_id=5),
            "job_id_empty": valid_proposal(job_id=""),
            "job_id_161": valid_proposal(job_id="j" * 161),
            "assumptions_not_list": valid_proposal(assumptions="a"),
            "assumptions_31": valid_proposal(assumptions=["a"] * 31),
            "assumptions_item_501": valid_proposal(assumptions=["a" * 501]),
            "reason_null": valid_proposal(reason=None),
            "reason_2001": valid_proposal(reason="r" * 2001),
            "schema_version_true": valid_proposal(schema_version=True),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                r = h.cmd("submit_research_proposal", h.agent, proposal=body)
                self.assertEqual(r.rejected_code, "PROPOSAL_INVALID", f"{name} accepted: {r.body}")
        self.assertEqual(count(h, "proposals"), 0)
        self.assertEqual(h.verify(), [])

    def test_proposal_cannot_reserve_or_order(self):
        """AUT-04 'bypass the gateway': order fields riding alongside a proposal are
        ignored (no order, hold or outbox row), and the same fields sent as an intent
        by the agent are UNAUTHORIZED."""
        h = self.running_session()
        order_fields = intent_payload(h, "AG1", "BUY", "1", "9.00", strategy_id="owner-manual")
        r = h.cmd("submit_research_proposal", h.agent, proposal=valid_proposal(), **order_fields)
        self.assertTrue(r.accepted, r.body)
        self.assertEqual([event(h, s)["type"] for s in r.events], ["ResearchProposalRecorded"])
        self.assertIsNone(h.row("orders", "order_id", "AG1"))
        self.assertEqual((count(h, "orders"), count(h, "holds"), count(h, "outbox")), (0, 0, 0))
        r = h.cmd("submit_intent", h.agent, **order_fields)
        self.assertEqual(r.rejected_code, "UNAUTHORIZED")
        self.assertIsNone(h.row("orders", "order_id", "AG1"))
        self.assertEqual(h.pump().sent, 0)
        self.assertEqual(h.broker.open_orders()[0], [])
        self.assertEqual(h.available("session:A"), F("100.00"))
        self.assertEqual(h.verify(), [])


if __name__ == "__main__":
    unittest.main()
