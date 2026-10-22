"""Discounted cash flow valuation (book eq. 17.1; CAP-CO-11).

    EV = sum_{t=1..T} FCFF_t / (1 + WACC)^t + TV_T / (1 + WACC)^T,   TV_T = FCFF_{T+1} / (WACC - g)

The terminal-growth form requires WACC > g; the model refuses otherwise and the sensitivity surface
marks such cells invalid. FCFF_{T+1} defaults to FCFF_T x (1 + g). WACC = w_e k_e + w_d k_d (1 - tau)
with k_e = r_f + beta x ERP (+ CRP): a modelling convention, not an observable truth (book ch. 17).

Arithmetic convention: this is a statistical valuation, never posted as money. It uses Python
``decimal`` with an explicit context (34 significant digits, ROUND_HALF_EVEN), passed to every
operation, so a re-run with the same inputs is exactly reproducible. Outputs are decimal strings.
Floats are refused as inputs.

Guards: WACC <= g is rejected; discount-rate currency must equal cash-flow currency unless the
owner records an override; financial-sector issuers (SIC 6000-6799: banks, insurers, REITs) are
blocked from the FCFF template unless overridden (CAP-CO-11). Every input carries its source.
Default scenarios are illustrative sensitivities (+/-1pp WACC, +/-0.5pp g, +/-10% cash flows), not
forecasts.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

from ...domain.fixed import Fixed

CTX = Context(prec=34, rounding=ROUND_HALF_EVEN)
FORMULA = "dcf_fcff_gordon@1"
WACC_FORMULA = "wacc_capm@1"


class DcfInputError(ValueError):
    pass


def dec(x, name: str = "value") -> Decimal:
    if isinstance(x, float):
        raise DcfInputError(f"{name}: floats are refused; pass a decimal string")
    if isinstance(x, Fixed):
        return Decimal(x.to_str())
    if isinstance(x, (int, Decimal)) and not isinstance(x, bool):
        return Decimal(x)
    if isinstance(x, str):
        try:
            d = Decimal(x.strip())
        except InvalidOperation:
            raise DcfInputError(f"{name}: not a decimal number: {x!r}") from None
        if not d.is_finite():
            raise DcfInputError(f"{name}: must be finite")
        return d
    raise DcfInputError(f"{name}: unsupported type {type(x).__name__}")


def s(d: Decimal, places: int = 10) -> str:
    """Presentation rounding (HALF_EVEN) at a declared number of decimal places."""
    return str(CTX.quantize(d, Decimal(1).scaleb(-places)))


def _disc(rate: Decimal, t: int) -> Decimal:
    return CTX.power(CTX.add(Decimal(1), rate), t)


def enterprise_value(fcff: list, wacc, g, fcff_next=None) -> dict:
    """Book eq. 17.1. Raises DcfInputError when WACC <= g or inputs are invalid."""
    flows = [dec(x, f"FCFF_{i + 1}") for i, x in enumerate(fcff)]
    if not flows:
        raise DcfInputError("at least one forecast FCFF is required")
    w, gg = dec(wacc, "WACC"), dec(g, "g")
    if w <= -1:
        raise DcfInputError("WACC must be greater than -100%")
    if w <= gg:
        raise DcfInputError(f"WACC ({w}) must exceed terminal growth g ({gg}) (book eq. 17.1)")
    T = len(flows)
    nxt = dec(fcff_next, "FCFF_T+1") if fcff_next is not None else CTX.multiply(flows[-1], CTX.add(Decimal(1), gg))
    rows, pv_sum = [], Decimal(0)
    for t, f in enumerate(flows, start=1):
        factor = _disc(w, t)
        pv = CTX.divide(f, factor)
        pv_sum = CTX.add(pv_sum, pv)
        rows.append({"t": t, "fcff": str(f), "discount_factor": s(CTX.divide(Decimal(1), factor), 12), "pv": s(pv, 2)})
    tv = CTX.divide(nxt, CTX.subtract(w, gg))
    pv_tv = CTX.divide(tv, _disc(w, T))
    ev = CTX.add(pv_sum, pv_tv)
    return {"formula": FORMULA, "wacc": str(w), "g": str(gg), "T": T, "fcff_next": s(nxt, 2), "projections": rows,
            "pv_forecast": s(pv_sum, 2), "terminal_value": s(tv, 2), "pv_terminal_value": s(pv_tv, 2),
            "enterprise_value": s(ev, 2), "terminal_share_of_ev": s(CTX.divide(pv_tv, ev), 6) if ev != 0 else None,
            "_ev": ev}


def wacc(rf, beta, erp, kd, tax, we, wd, crp="0") -> dict:
    """WACC = w_e (r_f + beta ERP + CRP) + w_d k_d (1 - tau); weights must sum to 1 exactly."""
    rf_, b, e, kd_, t, we_, wd_, c = (dec(rf, "rf"), dec(beta, "beta"), dec(erp, "erp"), dec(kd, "kd"), dec(tax, "tax"),
                                      dec(we, "we"), dec(wd, "wd"), dec(crp, "crp"))
    if we_ < 0 or wd_ < 0 or CTX.add(we_, wd_) != 1:
        raise DcfInputError(f"weights must be non-negative and sum to 1 (got {we_} + {wd_})")
    if not 0 <= t <= 1:
        raise DcfInputError("tax rate must be in [0, 1]")
    ke = CTX.add(CTX.add(rf_, CTX.multiply(b, e)), c)
    kd_after = CTX.multiply(kd_, CTX.subtract(Decimal(1), t))
    w = CTX.add(CTX.multiply(we_, ke), CTX.multiply(wd_, kd_after))
    return {"formula": WACC_FORMULA, "expression": "w_e x (r_f + beta x ERP + CRP) + w_d x k_d x (1 - tax)",
            "cost_of_equity": s(ke, 10), "after_tax_cost_of_debt": s(kd_after, 10), "wacc": s(w, 10), "_wacc": w,
            "inputs": {"rf": str(rf_), "beta": str(b), "erp": str(e), "crp": str(c), "kd": str(kd_), "tax": str(t),
                       "we": str(we_), "wd": str(wd_)}}


def equity_value(ev: Decimal, bridge: list[dict], shares) -> dict:
    """Equity = EV - debt - preferred - noncontrolling interest + cash + short-term investments.
    ``bridge`` items: {component, sign (+1/-1 applied to EV), value (str|None), source...}. A missing
    component is excluded and listed; the status is INCOMPLETE (never a silent zero)."""
    eq = ev
    missing = []
    lines = []
    for b in bridge:
        if b.get("value") is None:
            missing.append(b["component"])
            lines.append({**b, "status": "MISSING"})
            continue
        v = dec(b["value"], b["component"])
        eq = CTX.add(eq, v if b["sign"] > 0 else CTX.minus(v))
        lines.append({**b, "status": "OK"})
    out = {"equity_value": s(eq, 2), "_equity": eq, "bridge": lines, "status": "INCOMPLETE" if missing else "OK",
           "missing_components": missing}
    if shares is not None:
        sh = dec(shares, "shares")
        if sh <= 0:
            raise DcfInputError("share count must be positive")
        out["per_share"] = s(CTX.divide(eq, sh), 4)
        out["_per_share"] = CTX.divide(eq, sh)
        out["shares"] = str(sh)
    return out


def sensitivity(fcff: list, wacc0, g0, *, step_w="0.01", step_g="0.005", n: int = 2, bridge=None, shares=None, fcff_next=None) -> dict:
    """value(WACC_i, g_j) for WACC_i = WACC0 + k x step_w, g_j = g0 + l x step_g, k, l in [-n, n];
    cells with WACC_i <= g_j are invalid (CAP-CO-11)."""
    w0, g0_, sw, sg = dec(wacc0, "WACC"), dec(g0, "g"), dec(step_w, "step_w"), dec(step_g, "step_g")
    waccs = [CTX.add(w0, CTX.multiply(Decimal(k), sw)) for k in range(-n, n + 1)]
    gs = [CTX.add(g0_, CTX.multiply(Decimal(k), sg)) for k in range(-n, n + 1)]
    grid, values = [], []
    for w in waccs:
        row = []
        for g in gs:
            if w <= g:
                row.append({"wacc": str(w), "g": str(g), "valid": False, "reason": "WACC <= g"})
                continue
            r = enterprise_value(fcff, w, g, fcff_next)
            cell = {"wacc": str(w), "g": str(g), "valid": True, "enterprise_value": r["enterprise_value"]}
            if bridge is not None:
                e = equity_value(r["_ev"], bridge, shares)
                cell["equity_value"] = e["equity_value"]
                if "per_share" in e:
                    cell["per_share"] = e["per_share"]
                    values.append(e["_per_share"])
            else:
                values.append(r["_ev"])
            row.append(cell)
        grid.append(row)
    return {"wacc_axis": [str(w) for w in waccs], "g_axis": [str(g) for g in gs], "grid": grid,
            "measure": "per_share" if bridge is not None and shares is not None else "enterprise_value",
            "range": {"min": s(min(values), 4), "max": s(max(values), 4)} if values else None,
            "invalid_cells": sum(1 for row in grid for c in row if not c["valid"])}


DEFAULT_SCENARIOS = (
    {"name": "low", "wacc_shift": "0.01", "g_shift": "-0.005", "fcff_scale": "0.9"},
    {"name": "base", "wacc_shift": "0", "g_shift": "0", "fcff_scale": "1"},
    {"name": "high", "wacc_shift": "-0.01", "g_shift": "0.005", "fcff_scale": "1.1"},
)


def scenarios(fcff: list, wacc0, g0, specs=DEFAULT_SCENARIOS, *, bridge=None, shares=None) -> dict:
    w0, g0_ = dec(wacc0, "WACC"), dec(g0, "g")
    rows, values = [], []
    for sp in specs:
        w = CTX.add(w0, dec(sp.get("wacc_shift", "0"), "wacc_shift"))
        g = CTX.add(g0_, dec(sp.get("g_shift", "0"), "g_shift"))
        k = dec(sp.get("fcff_scale", "1"), "fcff_scale")
        flows = [CTX.multiply(dec(f, "FCFF"), k) for f in fcff]
        row = {"name": sp["name"], "wacc": str(w), "g": str(g), "fcff_scale": str(k)}
        try:
            r = enterprise_value(flows, w, g)
        except DcfInputError as exc:
            row.update({"valid": False, "reason": str(exc)})
            rows.append(row)
            continue
        row.update({"valid": True, "enterprise_value": r["enterprise_value"]})
        if bridge is not None:
            e = equity_value(r["_ev"], bridge, shares)
            row["equity_value"] = e["equity_value"]
            if "per_share" in e:
                row["per_share"] = e["per_share"]
                values.append(e["_per_share"])
        else:
            values.append(r["_ev"])
        rows.append(row)
    return {"scenarios": rows, "range": {"min": s(min(values), 4), "max": s(max(values), 4)} if values else None,
            "note": "Scenario shifts are illustrative sensitivities chosen by the caller (defaults +/-1pp WACC, "
                    "+/-0.5pp g, +/-10% FCFF); they are not forecasts."}


def strip_private(d):
    """Remove internal Decimal fields (keys starting with '_') before JSON output."""
    if isinstance(d, dict):
        return {k: strip_private(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [strip_private(v) for v in d]
    return d
