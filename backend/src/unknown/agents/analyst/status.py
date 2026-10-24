"""What the terminal shows before a question is asked: whether paid calls are on, the model and effort the analyst
would use (from the owner policy and ``config/models/approved.json``, never from code), the caps and today's spend.
Read-only: the policy and approvals are parsed, the spend ledger is opened ``mode=ro`` and never created. No key is
resolved or read here (the API process imports this module)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ...domain.fixed import Fixed
from ..budget import _day_month, default_ledger_path
from ..catalog import CatalogError, load_approvals
from ..common import CONFIG_MODELS
from ..config import OwnerPolicyError, default_policy_path, load_owner_policy
from .submission import MAX_TOOL_CALLS, MAX_TURNS, ROLE
from .toolspec import TOOL_NAMES


def spent_on(ledger_path: Path, day: str) -> str | None:
    """Consumed spend of one UTC day across every role (OPEN and UNCONFIRMED count their reservation, SETTLED its
    actual amount), read-only; None without a ledger."""
    if not ledger_path.exists():
        return None
    db = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute("SELECT state, reserved, actual FROM reservations WHERE day = ?", (day,)).fetchall()
    finally:
        db.close()
    total = Fixed(0)
    for state, reserved, actual in rows:
        if state in ("OPEN", "UNCONFIRMED"):
            total = total + Fixed.parse(reserved)
        elif state == "SETTLED":
            total = total + Fixed.parse(actual)
    return total.to_str()


def ai_status(home: Path, *, now_ns: int, models_dir: Path | None = None) -> dict:
    models = Path(models_dir) if models_dir else CONFIG_MODELS
    out: dict = {"role": ROLE, "label": "AI output is advisory and has no authority to trade, start sessions or move money.",
                 "tools": list(TOOL_NAMES), "max_turns": MAX_TURNS, "max_tool_calls": MAX_TOOL_CALLS}
    try:
        policy = load_owner_policy(default_policy_path(home))
        problem = None if policy is not None else "no owner policy (<UNKNOWN_HOME>/config/agents.json)"
    except (OwnerPolicyError, OSError, ValueError) as exc:
        policy, problem = None, f"the owner policy is invalid: {exc}"
    try:
        approvals = [a for a in load_approvals(models / "approved.json") if ROLE in a.roles]
    except (CatalogError, OSError, ValueError) as exc:
        approvals, problem = [], problem or f"approved.json is invalid: {exc}"
    models_out = []
    if policy is not None:
        for pid, cfg in sorted(policy.providers.items()):
            if not cfg.enabled:
                continue
            ids = sorted(a.model_id for a in approvals if a.provider == pid)
            if cfg.default_model in ids:
                ids.remove(cfg.default_model)
                ids.insert(0, cfg.default_model)
            effort = cfg.effort.for_role(ROLE) if cfg.effort else None
            models_out.extend({"provider": pid, "model": m, "effort": effort} for m in ids)
        caps = policy.caps
        out["caps"] = {"currency": caps.currency, "per_job": caps.per_job.amount.to_str(), "per_day": caps.per_day.amount.to_str(),
                       "lifetime": caps.lifetime.amount.to_str() if caps.lifetime else None}
        out["concurrency"] = dict(policy.concurrency.per_role).get(ROLE, 0)
        if not caps.per_day.is_positive() or not caps.per_job.is_positive():
            problem = problem or "the owner's per-day or per-job cap is zero"
        elif not models_out:
            problem = problem or f"no model is approved for role '{ROLE}' in config/models/approved.json"
        elif out["concurrency"] < 1:
            problem = problem or f"concurrency.per_role.{ROLE} is not set in the owner policy, so questions never run"
    out["models"] = models_out
    out["spent_today"] = spent_on(default_ledger_path(home), _day_month(now_ns)[0])
    out["enabled"] = problem is None
    out["disabled_reason"] = problem
    return out
