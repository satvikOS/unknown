"""Cost and quality dashboard for the agent layer (CAP-AGT-13), computed read-only from the records that exist:

* the spend ledger (``agents/ledger.sqlite``): consumed per UTC day and role with the ledger's own rule (OPEN and
  UNCONFIRMED rows count their reservation, SETTLED rows their actual cost, RELEASED rows nothing), against the
  owner caps (``agents.json``) and each process's own day cap (``ai_processes.json``);
* the process runs (``agents/processes.sqlite``): runs, outcomes, failures, latency p50/p95, tokens and cost
  per process, plus the verification status of every note line (the unsupported-line rate);
* the approval queue: proposals by state;
* the audit trail (``agents/audit.sqlite``): its hash-chain check.

Nothing here writes, and no prompt or evidence text is included (counts and amounts only).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...domain.fixed import Fixed
from ..audit import AuditTrail, default_audit_path
from ..budget import default_ledger_path
from ..config import OwnerPolicyError, default_policy_path, load_owner_policy
from .config import ROLES, ProcessConfigError, default_config_path, load_processes_config
from .store import ProcessStore, default_store_path

NS = 1_000_000_000


def _consumed(row: sqlite3.Row) -> Fixed:
    if row["state"] in ("OPEN", "UNCONFIRMED"):
        return Fixed.parse(row["reserved"])
    if row["state"] == "SETTLED" and row["actual"]:
        return Fixed.parse(row["actual"])
    return Fixed(0)


def _rate(part: int, total: int) -> str | None:
    """Percentage with one decimal, from integers (no float)."""
    if total == 0:
        return None
    tenths = part * 1000 // total
    return f"{tenths // 10}.{tenths % 10}"


def _pct(values: list[int], q: int) -> int | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, (len(v) * q + 99) // 100 - 1)]


def ledger_summary(path: Path, *, days: int, today: str) -> dict:
    if not Path(path).exists():
        return {"ledger": "absent", "by_day": [], "by_role_today": {}, "today": "0", "month": "0", "lifetime": "0",
                "unconfirmed": "0", "overruns": 0, "currency": None, "calls": 0}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT role, provider, model_id, purpose, currency, reserved, actual, state, day, month, overrun"
                          " FROM reservations").fetchall()
    finally:
        db.close()
    first = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    by_day: dict[str, dict[str, Fixed]] = defaultdict(lambda: defaultdict(lambda: Fixed(0)))
    by_model: dict[str, Fixed] = defaultdict(lambda: Fixed(0))
    total = month = today_total = unconfirmed = Fixed(0)
    overruns = calls = 0
    month_key = today[:7]
    for r in rows:
        c = _consumed(r)
        total = total + c
        if r["month"] == month_key:
            month = month + c
        if r["day"] == today:
            today_total = today_total + c
        if r["state"] == "UNCONFIRMED":
            unconfirmed = unconfirmed + c
        overruns += r["overrun"]
        if r["purpose"] == "generate" and r["state"] != "RELEASED":
            calls += 1
            by_model[f"{r['provider']}/{r['model_id']}"] = by_model[f"{r['provider']}/{r['model_id']}"] + c
        if r["day"] >= first:
            by_day[r["day"]][r["role"]] = by_day[r["day"]][r["role"]] + c
    currencies = sorted({r["currency"] for r in rows})
    day_list = []
    d = datetime.strptime(first, "%Y-%m-%d")
    for _ in range(days):
        key = d.strftime("%Y-%m-%d")
        roles = by_day.get(key, {})
        day_list.append({"day": key, "total": sum(roles.values(), Fixed(0)).to_str(),
                         "by_role": {k: v.to_str() for k, v in sorted(roles.items())}})
        d += timedelta(days=1)
    return {"ledger": "present", "currency": currencies[0] if len(currencies) == 1 else None, "by_day": day_list,
            "by_role_today": {k: v.to_str() for k, v in sorted(by_day.get(today, {}).items())},
            "by_model": {k: v.to_str() for k, v in sorted(by_model.items())},
            "today": today_total.to_str(), "month": month.to_str(), "lifetime": total.to_str(),
            "unconfirmed": unconfirmed.to_str(), "overruns": overruns, "calls": calls}


def runs_summary(store: ProcessStore, *, limit: int = 2000) -> dict:
    per: dict[str, dict[str, Any]] = {}
    for r in store.runs(limit=limit):
        p = per.setdefault(r["process"], {"runs": 0, "outcomes": defaultdict(int), "failed": 0, "latencies_ms": [],
                                          "prompt_tokens": 0, "output_tokens": 0, "cost": Fixed(0), "last_run": None})
        p["runs"] += 1
        p["outcomes"][r["outcome"] or r["state"].lower()] += 1
        p["failed"] += r["state"] == "FAILED"
        if r["started_ns"] and r["finished_ns"]:
            p["latencies_ms"].append((r["finished_ns"] - r["started_ns"]) // 1_000_000)
        p["prompt_tokens"] += r["prompt_tokens"] or 0
        p["output_tokens"] += r["output_tokens"] or 0
        if r["cost"]:
            p["cost"] = p["cost"] + Fixed.parse(r["cost"])
        if p["last_run"] is None:
            p["last_run"] = {"run_id": r["run_id"], "state": r["state"], "outcome": r["outcome"], "reason": r["reason"],
                             "requested_ns": r["requested_ns"], "finished_ns": r["finished_ns"], "note_id": r["note_id"]}
    out = {}
    for name, p in per.items():
        lat = p.pop("latencies_ms")
        out[name] = {**p, "outcomes": dict(p["outcomes"]), "cost": p["cost"].to_str(),
                     "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "n": len(lat)}}
    return out


def quality_summary(store: ProcessStore, *, limit: int = 500) -> dict:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"verified": 0, "uncertain": 0, "unsupported": 0})
    for n in store.notes(limit=limit):
        full = store.note(n["note_id"]) or {}
        for line in (full.get("body") or {}).get("lines") or []:
            st = line.get("status")
            if st in counts[n["process"]]:
                counts[n["process"]][st] += 1
    out = {}
    for proc, c in counts.items():
        total = sum(c.values())
        out[proc] = {**c, "lines": total,
                     "unsupported_rate": _rate(c["unsupported"], total), "verified_rate": _rate(c["verified"], total)}
    return out


def dashboard(root: Path, *, now_ns: int, days: int = 14) -> dict:
    today = datetime.fromtimestamp(now_ns // NS, tz=timezone.utc).strftime("%Y-%m-%d")
    problems: list[str] = []
    policy = cfg = None
    try:
        policy = load_owner_policy(default_policy_path(root))
    except (OwnerPolicyError, OSError, ValueError) as exc:
        problems.append(f"owner policy invalid: {exc}")
    try:
        cfg = load_processes_config(default_config_path(root))
    except ProcessConfigError as exc:
        problems.append(f"ai_processes.json invalid: {exc}")
    ledger = ledger_summary(default_ledger_path(root), days=days, today=today)
    store_path = default_store_path(root)
    store = ProcessStore(store_path, clock_ns=lambda: now_ns, readonly=True)
    try:
        runs = runs_summary(store)
        quality = quality_summary(store)
        approvals = defaultdict(int)
        for a in store.approvals(limit=1000):
            approvals[a["state"]] += 1
    finally:
        store.close()
    trail = AuditTrail(default_audit_path(root), clock_ns=lambda: now_ns, readonly=True)
    try:
        chain = trail.verify()
        head = trail.head()
    finally:
        trail.close()
    owner_caps = None
    if policy is not None:
        c = policy.caps
        owner_caps = {"currency": c.currency, "per_day": c.per_day.amount.to_str(), "per_job": c.per_job.amount.to_str(),
                      "lifetime": c.lifetime.amount.to_str() if c.lifetime is not None else None,
                      "per_month": c.per_month.amount.to_str() if c.per_month is not None else None}
    processes = {}
    for proc in ("brief", "summaries", "explainer"):
        s = cfg.get(proc) if cfg is not None else None
        role = ROLES[proc]
        processes[proc] = {"role": role, "enabled": bool(s and s.enabled),
                           "per_day": s.per_day.amount.to_str() if s else None, "per_run": s.per_run.amount.to_str() if s else None,
                           "spent_today": ledger["by_role_today"].get(role, "0"),
                           **runs.get(proc, {"runs": 0, "outcomes": {}, "failed": 0, "prompt_tokens": 0, "output_tokens": 0,
                                             "cost": "0", "latency_ms": {"p50": None, "p95": None, "n": 0}, "last_run": None}),
                           "quality": quality.get(proc)}
    return {"as_of_ns": now_ns, "today": today, "owner_caps": owner_caps, "spend": ledger, "processes": processes,
            "approvals": dict(approvals), "audit_chain": {"ok": not chain, "problems": chain[:20], "head_seq": head[0]},
            "config": cfg.view() if cfg is not None else None, "problems": problems,
            "note": "Spend follows the ledger rule; UNCONFIRMED amounts stay counted until reconciled."}
