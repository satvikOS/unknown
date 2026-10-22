"""Owner administration and process lifecycle: accounts, policies, instruments,
execution ownership, capital authorization, process start (ADR-0007/0010/0011)."""
from __future__ import annotations

import json

from ...domain.errors import Rejected
from ...domain.fixed import Fixed
from ...domain.ids import validate_id
from ...domain.instruments import Instrument
from ...domain.money import Money, currency
from ...policy.model import Mode, Policy, PolicyError
from .. import ledger, state
from ..desk import Context
from .common import RESTORED_FROM_BACKUP, UNKNOWN_ORDER, control_update, new_exception


def register_account(ctx: Context, p: dict) -> dict:
    key = validate_id(p.get("account_key"), "account key")
    provider = validate_id(p.get("provider"), "provider")
    mode = p.get("mode")
    if mode not in ("SIMULATED", "PAPER", "LIVE"):
        raise Rejected("INVALID_MODE", str(mode))
    base = p.get("base_currency")
    try:
        currency(base)
    except ValueError as exc:
        raise Rejected("INVALID_CURRENCY", str(exc)) from None
    if ctx.tx.one("SELECT 1 FROM accounts WHERE account_key=?", (key,)):
        raise Rejected("ACCOUNT_EXISTS", key)
    account_row = {"account_key": key, "provider": provider, "mode": mode, "base_currency": base,
                   "registered_at_ns": ctx.now_ns, "registered_by": ctx.principal.id}
    control_row = {"account_key": key, "stop_latched": True, "latch_reason": "NEW_ACCOUNT", "latched_by": ctx.principal.id,
                   "latched_at_ns": ctx.now_ns, "latch_command_id": ctx.command.command_id,
                   "resume_requires_owner": True, "resume_reason": "NEW_ACCOUNT", "needs_reconciliation": True,
                   "reconciled_epoch": 0, "last_clean_reconciliation_ns": None, "broker_connected": False,
                   "broker_authenticated": False, "last_start": None, "imported": False}
    risk_row = {"account_key": key, "currency": base, "authorized_total": Money.zero(base).amount.to_str(),
                "cumulative_pnl": Money.zero(base).amount.to_str(), "pnl_high_water": Money.zero(base).amount.to_str(),
                "risk_day": None, "day_start_pnl": Money.zero(base).amount.to_str(), "breach": None,
                "breach_at_ns": None, "last_valuation_ns": None}
    ctx.emit("AccountRegistered", {"account_key": key, "provider": provider, "mode": mode},
             {"accounts": [account_row], "control": [control_row], "risk_state": [risk_row]}, account_key=key)
    return {"account_key": key, "stop_latched": True}


def publish_policy(ctx: Context, p: dict) -> dict:
    body = p.get("body")
    try:
        policy = Policy.parse(body)
    except (PolicyError, ValueError, TypeError) as exc:
        raise Rejected("POLICY_INVALID", str(exc)) from None
    if ctx.tx.one("SELECT 1 FROM policies WHERE policy_hash=?", (policy.policy_hash,)):
        return {"policy_hash": policy.policy_hash, "already_published": True}
    ctx.emit("PolicyPublished", {"policy_hash": policy.policy_hash, "name": policy.name, "mode": policy.mode.value},
             {"policies": [{"policy_hash": policy.policy_hash, "name": policy.name, "body": body,
                            "published_by": ctx.principal.id, "published_at_ns": ctx.now_ns}]})
    return {"policy_hash": policy.policy_hash}


def activate_policy(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    acct = state.account(ctx.tx, key)
    policy = state.policy_by_hash(ctx.tx, p.get("policy_hash"))
    if policy.mode.value != acct["mode"]:
        raise Rejected("POLICY_MODE_MISMATCH", f"policy mode {policy.mode.value} vs account mode {acct['mode']}")
    if policy.currency != acct["base_currency"]:
        raise Rejected("POLICY_CURRENCY_MISMATCH", f"{policy.currency} vs {acct['base_currency']}")
    if policy.mode is Mode.LIVE:
        raise Rejected("LIVE_NOT_RELEASED", "live policies cannot be activated in this release (GATE-01)")
    r = state.risk_state(ctx.tx, key)
    if Fixed.parse(r["authorized_total"]) > policy.limits.authorized_capital.amount:
        raise Rejected("AUTHORIZED_ABOVE_NEW_LIMIT", "deauthorize capital before lowering authorized_capital")
    row = {"account_key": key, "policy_hash": policy.policy_hash, "activated_at_ns": ctx.now_ns,
           "activated_by": ctx.principal.id}
    ctx.emit("PolicyActivated", {"account_key": key, "policy_hash": policy.policy_hash,
                                 "note": "applies to future sessions; existing mandates are unchanged"},
             {"policy_active": [row]}, account_key=key)
    return {"policy_hash": policy.policy_hash}


def register_instrument(ctx: Context, p: dict) -> dict:
    try:
        inst = Instrument.from_json(p["instrument"])
    except (KeyError, ValueError, TypeError) as exc:
        raise Rejected("INSTRUMENT_INVALID", str(exc)) from None
    existing = state.instrument(ctx.tx, inst.instrument_id)
    if existing is not None and existing.metadata_version >= inst.metadata_version:
        if existing.to_json() == inst.to_json():
            return {"instrument_id": inst.instrument_id, "unchanged": True}
        raise Rejected("INSTRUMENT_VERSION", "a changed record needs a higher metadata_version")
    mappings = []
    for m in p.get("mappings", []):
        provider = validate_id(m.get("provider"), "provider")
        external = str(m.get("external_id"))
        prev = ctx.tx.one("SELECT MAX(version) AS v FROM provider_mappings WHERE provider=? AND external_id=?", (provider, external))
        version = (prev["v"] or 0) + 1
        mappings.append({"provider": provider, "external_id": external, "version": version,
                         "instrument_id": inst.instrument_id, "verified_at_ns": m.get("verified_at_ns"),
                         "details": m.get("details", {})})
    ctx.emit("InstrumentRegistered", {"instrument_id": inst.instrument_id, "metadata_version": inst.metadata_version},
             {"instruments": [inst.to_json()], "mappings": mappings}, instrument_id=inst.instrument_id)
    return {"instrument_id": inst.instrument_id}


def acquire_execution_ownership(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    state.account(ctx.tx, key)
    deployment = p.get("deployment_id")
    if deployment != ctx.desk.deployment_id:
        raise Rejected("WRONG_DEPLOYMENT", "ownership can only be acquired by the deployment executing the command")
    current = state.ownership(ctx.tx, key)
    if current and current["status"] == "HELD" and current["deployment_id"] != deployment:
        raise Rejected("OWNED_ELSEWHERE", f"held by {current['deployment_id']}; release with a reconciled certificate first")
    epoch = (current["epoch"] + 1) if current else 1
    acct = state.account(ctx.tx, key)
    provenance = None
    if acct["mode"] != Mode.SIMULATED.value and not (current and current["deployment_id"] == deployment):
        # Another deployment may trade this broker account from its own database. Hand-over needs either
        # the previous holder's release certificate or the owner's explicit attestation (ADR-0011).
        cert = p.get("release_certificate")
        if isinstance(cert, dict) and {"deployment_id", "epoch", "reconciliation_run"} <= set(cert):
            provenance = {"release_certificate": cert}
        elif p.get("attest_no_other_deployment") is True:
            provenance = {"owner_attestation": "no other deployment trades this broker account"}
        else:
            raise Rejected("RELEASE_CERTIFICATE_REQUIRED",
                           "provide the previous deployment's release certificate or attest_no_other_deployment")
    row = {"account_key": key, "deployment_id": deployment, "epoch": epoch, "status": "HELD",
           "granted_at_ns": ctx.now_ns, "granted_by": ctx.principal.id, "provenance": provenance}
    c = control_update(ctx, key, needs_reconciliation=True)
    ctx.emit("ExecutionOwnershipAcquired", {"account_key": key, "deployment_id": deployment, "epoch": epoch},
             {"ownership": [row], "control": [c]}, account_key=key)
    return {"epoch": epoch}


def release_execution_ownership(ctx: Context, p: dict) -> dict:
    key = p.get("account_key")
    current = state.ownership(ctx.tx, key)
    if not current or current["status"] != "HELD" or current["deployment_id"] != ctx.desk.deployment_id:
        raise Rejected("NOT_OWNER", "this deployment does not hold execution ownership")
    c = state.control(ctx.tx, key)
    if not c["stop_latched"]:
        raise Rejected("STOP_REQUIRED", "latch the stop before releasing ownership")
    if state.orders(ctx.tx, key, open_only=True):
        raise Rejected("OPEN_ORDERS", "resolve open orders before releasing ownership")
    last = state.latest_reconciliation(ctx.tx, key)
    if not last or last["status"] != "CLEAN" or last["epoch"] != ctx.epoch:
        raise Rejected("RECONCILIATION_REQUIRED", "a clean reconciliation in this process is required")
    row = {**current, "status": "RELEASED", "released_at_ns": ctx.now_ns, "release_certificate": {
        "reconciliation_run": last["run_id"], "deployment_id": ctx.desk.deployment_id, "epoch": current["epoch"]}}
    ctx.emit("ExecutionOwnershipReleased", {"account_key": key, "certificate": row["release_certificate"]},
             {"ownership": [row]}, account_key=key)
    return {"certificate": row["release_certificate"]}


def authorize_capital(ctx: Context, p: dict) -> dict:
    return _capital(ctx, p, authorize=True)


def deauthorize_capital(ctx: Context, p: dict) -> dict:
    return _capital(ctx, p, authorize=False)


def _capital(ctx: Context, p: dict, *, authorize: bool) -> dict:
    key = p.get("account_key")
    acct = state.account(ctx.tx, key)
    policy = state.active_policy(ctx.tx, key)
    ccy = p.get("currency", acct["base_currency"])
    if ccy != policy.currency:
        raise Rejected("CURRENCY", "capital must be in the policy currency")
    try:
        amount = Money(ccy, Fixed.parse(p.get("amount", ""))).at_ledger_scale()
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise Rejected("INVALID_AMOUNT", str(exc)) from None
    if not amount.is_positive():
        raise Rejected("INVALID_AMOUNT", "amount must be positive")
    risk = state.risk_state(ctx.tx, key)
    total = Fixed.parse(risk["authorized_total"])
    src, dst = (ledger.OUTSIDE, ledger.UNALLOCATED) if authorize else (ledger.UNALLOCATED, ledger.OUTSIDE)
    available = ledger.available_cash(ctx.tx, key, src, ccy)
    if amount.amount > available:
        raise Rejected("INSUFFICIENT_SOURCE_CASH", f"{src} available {available} < {amount.amount}")
    new_total = total + amount.amount if authorize else total - amount.amount
    if authorize and new_total > policy.limits.authorized_capital.amount:
        raise Rejected("ABOVE_AUTHORIZED_CAPITAL", f"would authorize {new_total} > {policy.limits.authorized_capital.amount}")
    # authorized_total is the signed net capital contributed to the system. It may go
    # negative when the owner deauthorizes realized gains; it is the baseline that
    # separates P&L from capital flows in loss-budget accounting (valuation.py).
    entry = ledger.entry(ctx.new_id("je-"), key, "OWNER_TRANSFER",
                         f"{'authorize' if authorize else 'deauthorize'} capital {amount}",
                         ledger.transfer(ledger.CASH, ccy, amount.amount, src, dst))
    risk = {**risk, "authorized_total": new_total.rescale(currency(ccy).ledger_scale).to_str()}
    ctx.emit("CapitalAuthorized" if authorize else "CapitalDeauthorized",
             {"account_key": key, "amount": amount.to_json(), "authorized_total": risk["authorized_total"]},
             {"journal": [entry], "risk_state": [risk]}, account_key=key)
    if authorize:
        from .sessions import retry_waiting_funding
        retry_waiting_funding(ctx, key)
    return {"authorized_total": risk["authorized_total"]}


def process_started(ctx: Context, p: dict) -> dict:
    """Restart semantics (ADR-0010): never clears a stop; requires reconciliation;
    resumption needs the owner unless the active policy allows auto-resume. A
    restored database also latches the stop and treats in-flight orders as unknown."""
    restored = bool(p.get("restored_from_backup"))
    accounts = [json.loads(r["row"]) for r in ctx.tx.all("SELECT row FROM accounts ORDER BY account_key")]
    touched = []
    for acct in accounts:
        key = acct["account_key"]
        c = state.control(ctx.tx, key)
        try:
            auto = state.active_policy(ctx.tx, key).auto_resume_after_clean_reconciliation
        except Rejected:
            auto = False
        c.update(needs_reconciliation=True, broker_connected=False, broker_authenticated=False)
        if not auto and not c["resume_requires_owner"]:
            c.update(resume_requires_owner=True, resume_reason="PROCESS_RESTART")
        changes: dict = {"control": [c], "orders": [], "outbox": [], "exceptions": []}
        for o in state.orders(ctx.tx, key, open_only=True):
            outbox = state.outbox_for_order(ctx.tx, o["order_id"])
            submit = next((b for b in outbox if b["action"] == "SUBMIT"), None)
            in_flight = o["state"] == "SUBMIT_PENDING" or (submit and submit["state"] == "SENDING")
            maybe_sent_after_backup = restored and o["state"] == "RESERVED"
            new = dict(o)
            if in_flight or maybe_sent_after_backup:
                new.update(prev_state=o["state"], state="UNKNOWN",
                           reason="restored from backup" if maybe_sent_after_backup else "restart during submission")
                if submit:
                    changes["outbox"].append({**submit, "state": "NEEDS_LOOKUP"})
                case = new_exception(ctx, key, UNKNOWN_ORDER, blocking=True, severity="high",
                                     detail={"order_id": o["order_id"], "cause": "restart" if not restored else "restore"},
                                     key=f"unknown-order:{o['order_id']}", session_id=o.get("session_id"),
                                     order_id=o["order_id"])
                if case:
                    changes["exceptions"].append(case)
            for b in outbox:
                if b["action"] == "CANCEL" and (b["state"] == "SENDING" or (restored and b["state"] == "PENDING")):
                    # The cancel may never have reached the broker: it must stay re-issuable (a cancel is idempotent).
                    changes["outbox"].append({**b, "state": "NEEDS_LOOKUP"})
                    new["cancel_state"] = "UNKNOWN"
            if new != o:
                changes["orders"].append(new)
        if restored:
            case = new_exception(ctx, key, RESTORED_FROM_BACKUP, blocking=True, severity="critical",
                                 detail={"epoch": ctx.epoch, "note": "reconcile all broker activity since the backup",
                                         "evidence": p.get("restore_evidence")},
                                 key=f"restored:{ctx.epoch}")
            if case:
                changes["exceptions"].append(case)
        ctx.emit("ProcessStartedForAccount", {"account_key": key, "epoch": ctx.epoch, "restored": restored},
                 changes, account_key=key)
        if restored:
            from .control import latch_stop
            latch_stop(ctx, key, "RESTORED_FROM_BACKUP")
        touched.append(key)
    if not accounts:
        ctx.emit("ProcessStarted", {"epoch": ctx.epoch, "restored": restored})
    return {"epoch": ctx.epoch, "accounts": touched, "restored": restored}
