"""DCF model assembly with sourced inputs (CAP-CO-11; book eq. 17.1).

Turns owner assumptions plus as-of fundamentals into a versioned model: every input states its
source (owner input, a standardized cell with observation ids, the Treasury curve observation, or
a metric with its formula), the WACC build-up, eq. 17.1 valuation, the equity bridge at the latest
balance-sheet date, a WACC x g sensitivity surface and a scenario range. Guards (WACC > g,
currency consistency, financial-sector block) refuse rather than compute; an owner override is
echoed in ``overrides``.

Forecast cash flows come only from typed inputs: an explicit list (``fcff``) or a constant growth
path from the issuer's FCFF TTM (``fcff_growth`` + ``years``), which is labelled as an owner
assumption, not a forecast by this system.
"""
from __future__ import annotations

from decimal import Decimal

from ...domain.clock import ns_to_iso
from . import dcf
from .metrics import MetricEngine, PriceInput, is_financial_sic
from .periods import Period
from .statements import Fundamentals


class ModelRefused(ValueError):
    pass


def _bal(f: Fundamentals, key: str, q0) -> dict:
    c = f.cell(key, Period("INSTANT", q0.end))
    return {"value": c.value.to_str() if c.ok else None, "obs_ids": c.obs_ids(), "period_end": q0.end.isoformat(),
            "method": c.method, "missing_reason": c.missing_reason, "detail": None if c.ok else c.detail}


def dcf_model(f: Fundamentals, *, sic=None, g, fcff: list | None = None, fcff_growth=None, years: int | None = None,
              wacc=None, rf=None, rf_point: dict | None = None, beta=None, erp=None, crp=None, kd=None, tax=None,
              we=None, wd=None, rate_currency: str | None = None, price: PriceInput | None = None,
              step_w="0.01", step_g="0.005", allow_financial: bool = False, allow_currency_mismatch: bool = False) -> dict:
    overrides = []
    if is_financial_sic(sic):
        if not allow_financial:
            raise ModelRefused(f"financial-sector issuer (SIC {sic}): the FCFF DCF template is blocked by default (CAP-CO-11); "
                               "banks, insurers and REITs need different valuation structures")
        overrides.append("allow_financial: FCFF template applied to a financial-sector issuer by owner override")
    eng = MetricEngine(f, price, sic=sic, allow_financial_ev=allow_financial)
    q0 = f.latest_quarter()
    if q0 is None:
        raise ModelRefused("no fiscal quarter reported at the as-of time; no balance sheet for the equity bridge")
    inputs: dict = {}

    # ---- cash flows
    if fcff:
        flows = [str(dcf.dec(x, f"FCFF_{i + 1}")) for i, x in enumerate(fcff)]
        inputs["fcff"] = {"values": flows, "source": "owner input (explicit forecast)", "currency": f.currency}
    else:
        if fcff_growth is None or not years:
            raise ModelRefused("give explicit fcff values, or fcff_growth and years to grow FCFF TTM")
        if not 1 <= int(years) <= 30:
            raise ModelRefused("years must be between 1 and 30")
        base = eng.compute("fcff_ttm")
        if base.status != "OK":
            raise ModelRefused(f"FCFF TTM unavailable ({base.status}: {base.reason}); supply explicit fcff values")
        gr = dcf.dec(fcff_growth, "fcff_growth")
        b = Decimal(base.value.to_str())
        flows = [str(dcf.CTX.multiply(b, dcf.CTX.power(dcf.CTX.add(Decimal(1), gr), t))) for t in range(1, int(years) + 1)]
        inputs["fcff"] = {"values": [dcf.s(Decimal(x), 2) for x in flows], "currency": f.currency,
                          "source": f"owner assumption: FCFF TTM x (1 + {gr})^t for t = 1..{years}",
                          "base": base.to_json(f.cik)}

    # ---- discount rate
    if rf_point is not None:
        rf_val = dcf.CTX.divide(Decimal(rf_point["value_pct"]), Decimal(100))
        inputs["rf"] = {"value": str(rf_val), "source": f"US Treasury par yield {rf_point['tenor']} on {rf_point['curve_date']}",
                        "obs_id": rf_point["obs_id"], "available_time": rf_point["available_time"], "unit": "decimal (percent / 100)",
                        "currency": "USD", "fallback": rf_point.get("fallback"), "convention_note": "par yield; compounding convention unverified"}
        rate_ccy = "USD"
    elif rf is not None:
        rf_val = dcf.dec(rf, "rf")
        inputs["rf"] = {"value": str(rf_val), "source": "owner input", "currency": rate_currency}
        rate_ccy = rate_currency
    else:
        rf_val, rate_ccy = None, rate_currency
    if rate_ccy is None:
        rate_ccy = f.currency
        inputs["rate_currency_note"] = f"discount-rate currency not stated; assumed {f.currency} (owner responsibility)"
    if rate_ccy != f.currency:
        if not allow_currency_mismatch:
            raise ModelRefused(f"discount rate in {rate_ccy} but cash flows in {f.currency}: currency-inconsistent inputs are "
                               "rejected (CAP-CO-11) unless the owner records an override")
        overrides.append(f"allow_currency_mismatch: {rate_ccy} discount rate with {f.currency} cash flows")

    if wacc is not None:
        w = dcf.dec(wacc, "wacc")
        inputs["wacc"] = {"value": str(w), "source": "owner input"}
        wacc_detail = None
    else:
        missing = [n for n, v in (("rf or rf_tenor", rf_val), ("beta", beta), ("erp", erp), ("kd", kd)) if v is None]
        if missing:
            raise ModelRefused("give wacc, or its inputs: missing " + ", ".join(missing))
        tax_src = "owner input"
        if tax is None:
            etr = eng.compute("effective_tax_rate")
            if etr.status != "OK":
                raise ModelRefused(f"tax rate not given and effective tax rate TTM is {etr.status}")
            tax, tax_src = etr.value.to_str(), f"effective tax rate TTM ({etr.formula.ref}, obs {etr.obs_ids()})"
        if we is None or wd is None:
            mc, debt = eng.compute("market_cap"), f.cell("total_debt", Period("INSTANT", q0.end))
            if mc.status != "OK" or not debt.ok:
                raise ModelRefused("give we and wd, or a price input so market-value weights can be computed "
                                   f"(market cap {mc.status}; total debt {'ok' if debt.ok else debt.missing_reason})")
            total = Decimal(mc.value.to_str()) + Decimal(debt.value.to_str())
            we_d = dcf.CTX.divide(Decimal(mc.value.to_str()), total)
            we, wd = str(we_d), str(dcf.CTX.subtract(Decimal(1), we_d))
            inputs["weights"] = {"we": we, "wd": wd, "source": "market values: market cap / (market cap + total debt)",
                                 "market_cap": mc.to_json(f.cik), "total_debt": {"value": debt.value.to_str(), "obs_ids": debt.obs_ids()}}
        else:
            inputs["weights"] = {"we": str(we), "wd": str(wd), "source": "owner input"}
        wacc_detail = dcf.wacc(rf_val, beta, erp, kd, tax, we, wd, crp if crp is not None else "0")
        wacc_detail["sources"] = {"rf": inputs.get("rf", {}).get("source"), "beta": "owner input", "erp": "owner input",
                                  "crp": "owner input" if crp is not None else "default 0 (no country risk premium supplied)",
                                  "kd": "owner input", "tax": tax_src, "weights": inputs["weights"]["source"]}
        w = Decimal(wacc_detail["wacc"])   # the displayed WACC (HALF_EVEN, 10 dp) is exactly the one used
    gg = dcf.dec(g, "g")
    inputs["g"] = {"value": str(gg), "source": "owner input (nominal, same currency as cash flows)"}
    if w <= gg:
        raise ModelRefused(f"WACC ({w}) must exceed terminal growth g ({gg}) (book eq. 17.1)")

    # ---- valuation, bridge, per share
    ev = dcf.enterprise_value(flows, w, gg)
    bridge = []
    for key, sign in (("total_debt", -1), ("preferred_stock", -1), ("noncontrolling_interest", -1),
                      ("cash_and_equivalents", 1), ("short_term_investments", 1)):
        bridge.append({"component": key, "sign": sign, **_bal(f, key, q0)})
    shares_cell = eng.diluted_shares_at_latest_quarter()
    shares = shares_cell.value.to_str() if shares_cell.ok else None
    equity = dcf.equity_value(ev["_ev"], bridge, shares)
    sens = dcf.sensitivity(flows, w, gg, step_w=step_w, step_g=step_g, bridge=bridge, shares=shares)
    scen = dcf.scenarios(flows, w, gg, bridge=bridge, shares=shares)
    football = {"dcf_scenarios": scen["range"], "dcf_sensitivity": sens["range"],
                "trading_comps_iqr": {"status": "SEE_PEERS", "note": "computed by the peers route from comparable multiples"},
                "52_week_range": {"status": "NOT_AVAILABLE", "reason": "no price history in the research store"}}
    if price is not None and "per_share" in equity:
        football["current_price"] = price.to_json()
    return dcf.strip_private({
        "model": dcf.FORMULA, "entity_id": f.entity_id, "as_of": ns_to_iso(f.as_of_ns), "currency": f.currency,
        "balance_sheet_date": q0.end.isoformat(), "inputs": inputs, "wacc_build_up": wacc_detail, "valuation": ev,
        "equity_bridge": {**equity, "expression": "EV - total debt - preferred - noncontrolling interest + cash + short-term investments",
                          "shares": {"value": shares, "source": "diluted weighted-average shares of the period ending at the latest "
                                     "quarter", "obs_ids": shares_cell.obs_ids(), "flags": shares_cell.all_flags()}},
        "sensitivity": sens, "scenarios": scen, "football_field": football, "overrides": overrides,
        "arithmetic": "decimal, 34 significant digits, ROUND_HALF_EVEN, explicit context; presentation rounding stated per field",
        "disclaimer": "A valuation under stated assumptions, not a forecast or a recommendation; small changes in WACC or g can dominate the result."})
