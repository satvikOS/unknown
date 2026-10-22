"""Ratios, enterprise-value bridge and multiples with lineage (CAP-CO-07; book ch. 11 and 17).

Every metric has a formula id and version (``gross_margin@1``), a written expression, a unit and a
basis. Its inputs are standardized cells (``statements.Cell``) whose observation ids, accessions
and availability times travel with the result, plus, for market multiples, an explicit
``PriceInput`` with its source and observation time. ``MetricEngine.explain`` is the terminal's
"show calculation"; ``persist`` writes results through ``ResearchStore.record_metric``
(formula id@version and input observation ids, DAT-05).

Arithmetic. Money amounts are exact ``Fixed`` sums and products. Ratios are exact quotients rounded
HALF_EVEN to ``RATIO_SCALE`` (10) decimal places; FCFF is rounded HALF_EVEN to whole currency
units. None of these values is ever posted to the ledger.

Conventions (documented choices, not vendor methodology):

* Basis: flows are trailing twelve months ending at the latest fiscal quarter known at the as-of
  time (``Fundamentals.ttm``); balances are at that quarter's end. "Four quarters earlier" (YoY
  growth, average balances, the FCFF working-capital change) is the fiscal quarter ending one
  fiscal year before, found by date (``Fundamentals.year_ago_quarter``), never by list position, so
  a quarter missing from the calendar cannot turn a YoY comparison into an 18-month one. Averages
  use the balance at the window end and at that year-ago quarter end; when the earlier balance is
  unavailable the ending balance is used and flagged ``average_unavailable_ending_balance_used``.
* Not meaningful: a ratio whose denominator is <= 0 (P/E with negative earnings, EV/EBITDA with
  negative EBITDA, growth from a non-positive base) has status ``NM``, no value, and is excluded from
  peer statistics (CAP-CO-07, CAP-CO-10).
* EV bridge (``ev_standard@1``): market cap + total debt + preferred stock + noncontrolling
  interest - cash and equivalents - short-term investments, all at the latest balance-sheet date.
  Operating lease liabilities are excluded (``ev_with_operating_leases@1`` adds them);
  ``ev_pension_ex_equity_method@1`` adds the net unfunded pension liability (-funded status) and
  subtracts equity-method investments. A component the filer did not report for that date is shown
  as missing (never as a reported zero), is excluded from the sum, and the EV status is
  ``INCOMPLETE``; multiples built on an incomplete EV inherit that status.
* Market cap = price x cover-page shares outstanding (``dei:EntityCommonStockSharesOutstanding``,
  latest at the as-of time; balance-sheet shares as fallback). Multi-class issuers whose cover
  page reports per-class counts are not summed by class here (flagged by the absence of a total).
* P/E (TTM) = price / (net income TTM / diluted weighted-average shares of the period ending at the
  latest quarter). P/E (FY) = price / reported diluted EPS of the latest fiscal year.
* Dividend yield uses dividends paid (cash flow, TTM) / market cap, so per-share values are never
  summed.
* Financial-sector issuers (SIC 6000-6799: banks, insurers, REITs) have EV-based metrics suppressed
  by default with a reason (CAP-CO-07).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ...domain.clock import NS_PER_S, ns_to_iso
from ...domain.fixed import Fixed, Rounding
from ..pit import Observation
from .periods import Period
from .statements import Cell, Fundamentals, pit_reason, sec_index_url

RATIO_SCALE = 10
DAY_NS = 86_400 * NS_PER_S
PRICE_DATASET = "owner.price_input"


@dataclass(frozen=True)
class Formula:
    id: str
    version: int
    expression: str
    unit: str            # "ratio" (fraction), "x" (multiple), "money" (reporting currency)
    basis: str
    nm_rule: str = ""
    needs_price: bool = False
    ev_based: bool = False

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


def _f(id, expression, unit, basis, nm_rule="", needs_price=False, ev_based=False) -> Formula:
    return Formula(id, 1, expression, unit, basis, nm_rule, needs_price, ev_based)


TTM, BAL, FY = "TTM at latest quarter", "balance at latest quarter end", "latest fiscal year"
FORMULAS: dict[str, Formula] = {f.id: f for f in (
    _f("revenue_ttm", "sum of revenue over the four latest discrete fiscal quarters", "money", TTM),
    _f("gross_margin", "gross_profit_TTM / revenue_TTM", "ratio", TTM, "NM if revenue <= 0"),
    _f("operating_margin", "operating_income_TTM / revenue_TTM", "ratio", TTM, "NM if revenue <= 0"),
    _f("ebitda_margin", "EBITDA_TTM / revenue_TTM; EBITDA = operating income + D&A", "ratio", TTM, "NM if revenue <= 0"),
    _f("net_margin", "net_income_TTM / revenue_TTM", "ratio", TTM, "NM if revenue <= 0"),
    _f("fcf_ttm", "CFO_TTM - capex_TTM", "money", TTM),
    _f("fcf_margin", "FCF_TTM / revenue_TTM", "ratio", TTM, "NM if revenue <= 0"),
    _f("effective_tax_rate", "income_tax_TTM / pretax_income_TTM", "ratio", TTM, "NM if pretax income <= 0"),
    _f("fcff_ttm", "EBIT_TTM x (1 - effective tax rate) + D&A_TTM - capex_TTM - (NWC_end - NWC_4q_earlier)", "money", TTM,
       "unavailable if the effective tax rate is NM"),
    _f("revenue_growth_yoy", "revenue_TTM / revenue_TTM(4 quarters earlier) - 1", "ratio", TTM, "NM if prior <= 0"),
    _f("net_income_growth_yoy", "net_income_TTM / net_income_TTM(4 quarters earlier) - 1", "ratio", TTM, "NM if prior <= 0"),
    _f("revenue_growth_fy", "revenue_FY / revenue_FY(prior fiscal year) - 1", "ratio", FY, "NM if prior <= 0"),
    _f("roe", "net_income_TTM / average(equity attributable to parent)", "ratio", TTM, "NM if average equity <= 0"),
    _f("roa", "net_income_TTM / average(total assets)", "ratio", TTM, "NM if average assets <= 0"),
    _f("roic", "EBIT_TTM x (1 - effective tax rate) / average(invested capital); invested capital = total debt + total equity - cash",
       "ratio", TTM, "NM if pretax income <= 0 or average invested capital <= 0"),
    _f("debt_to_equity", "total_debt / equity attributable to parent", "ratio", BAL, "NM if equity <= 0"),
    _f("net_debt", "total_debt - cash and equivalents - short-term investments", "money", BAL),
    _f("net_debt_to_ebitda", "net_debt / EBITDA_TTM", "x", TTM, "NM if EBITDA <= 0"),
    _f("interest_coverage", "EBIT_TTM / interest_expense_TTM", "x", TTM, "NM if interest expense <= 0"),
    _f("current_ratio", "current_assets / current_liabilities", "x", BAL, "NM if current liabilities <= 0"),
    _f("quick_ratio", "(cash + short-term investments + receivables) / current_liabilities", "x", BAL, "NM if current liabilities <= 0"),
    _f("market_cap", "price x shares outstanding (cover page, latest at the as-of time)", "money", "price time", needs_price=True),
    _f("ev_standard", "market cap + total debt + preferred + noncontrolling interest - cash - short-term investments", "money",
       "price time / latest balance", needs_price=True, ev_based=True),
    _f("ev_with_operating_leases", "ev_standard + operating lease liabilities", "money", "price time / latest balance",
       needs_price=True, ev_based=True),
    _f("ev_pension_ex_equity_method", "ev_standard - pension funded status (adds net unfunded liability) - equity-method investments",
       "money", "price time / latest balance", needs_price=True, ev_based=True),
    _f("pe_ttm", "price / (net_income_TTM / diluted weighted-average shares)", "x", TTM, "NM if net income TTM <= 0", needs_price=True),
    _f("pe_fy", "price / diluted EPS (latest fiscal year, as reported)", "x", FY, "NM if EPS <= 0", needs_price=True),
    _f("price_to_book", "market cap / equity attributable to parent", "x", BAL, "NM if equity <= 0", needs_price=True),
    _f("price_to_sales", "market cap / revenue_TTM", "x", TTM, "NM if revenue <= 0", needs_price=True),
    _f("ev_to_sales", "ev_standard / revenue_TTM", "x", TTM, "NM if revenue <= 0", needs_price=True, ev_based=True),
    _f("ev_to_ebitda", "ev_standard / EBITDA_TTM", "x", TTM, "NM if EBITDA <= 0", needs_price=True, ev_based=True),
    _f("ev_to_ebit", "ev_standard / EBIT_TTM", "x", TTM, "NM if EBIT <= 0", needs_price=True, ev_based=True),
    _f("fcf_yield", "FCF_TTM / market cap", "ratio", TTM, needs_price=True),
    _f("dividend_yield", "dividends paid_TTM / market cap (cash basis)", "ratio", TTM, needs_price=True),
)}


def is_financial_sic(sic) -> bool:
    try:
        return 6000 <= int(sic) <= 6799
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class PriceInput:
    """A price supplied with its source and observation time (never fetched implicitly)."""
    value: Fixed
    currency: str
    source: str
    observed_at_ns: int
    obs_id: int | None = None
    listing: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.value, Fixed) or not self.value.is_positive():
            raise ValueError("price must be a positive Fixed")
        if not self.source:
            raise ValueError("price source is required")

    def to_json(self) -> dict:
        return {"value": self.value.to_str(), "currency": self.currency, "source": self.source, "listing": self.listing,
                "observed_at": ns_to_iso(self.observed_at_ns), "obs_id": self.obs_id, "persisted": self.obs_id is not None}


def record_price_input(store, entity_id: str, value: Fixed, currency: str, source: str, observed_at_ns: int,
                       received_at_ns: int, listing: str = "") -> PriceInput:
    """Store an owner-supplied price as an observation (dataset owner.price_input) so persisted
    metrics can cite it by observation id. Available at its observation time."""
    day = datetime.fromtimestamp(observed_at_ns // NS_PER_S, tz=timezone.utc).date().isoformat()
    obs_id, _ = store.record(Observation(
        dataset=PRICE_DATASET, entity_id=entity_id, field=f"price:{listing or 'primary'}", unit=f"{currency}/shares",
        currency=currency, value=value, period_end=day, period_kind="INSTANT", receipt_time_ns=received_at_ns,
        available_time_ns=observed_at_ns, publication_id=f"{source}@{observed_at_ns}", source_ref=f"owner:{source}",
        quality=("owner_supplied",)), received_at_ns)
    return PriceInput(value, currency, source, observed_at_ns, obs_id, listing)


@dataclass(frozen=True)
class Input:
    role: str
    cell: Cell | None = None
    price: PriceInput | None = None
    metric: "MetricResult | None" = None
    value: Fixed | None = None         # for scalar intermediates (e.g. tax rate)

    def obs_ids(self) -> list[int]:
        out = list(self.cell.obs_ids()) if self.cell is not None else []
        if self.price is not None and self.price.obs_id is not None:
            out.append(self.price.obs_id)
        if self.metric is not None:
            out += self.metric.obs_ids()
        return out

    def to_json(self, cik: int | None, depth: int) -> dict:
        d: dict = {"role": self.role}
        if self.cell is not None:
            d["cell"] = self.cell.to_json(cik, depth=depth)
        if self.price is not None:
            d["price"] = self.price.to_json()
        if self.metric is not None:
            d["metric"] = self.metric.to_json(cik, detail=depth > 0)
        if self.value is not None:
            d["value"] = self.value.to_str()
        return d


@dataclass(frozen=True)
class MetricResult:
    name: str
    formula: Formula
    value: Fixed | None
    status: str                    # OK | NM | MISSING | INCOMPLETE | SUPPRESSED | NO_PRICE
    unit: str
    period: str
    inputs: tuple[Input, ...] = ()
    reason: str | None = None
    flags: tuple[str, ...] = ()
    bridge: tuple[dict, ...] = field(default=())

    def obs_ids(self) -> list[int]:
        ids: set[int] = set()
        for i in self.inputs:
            ids.update(i.obs_ids())
        return sorted(ids)

    def source_dates(self) -> list[str]:
        times = set()
        for i in self.inputs:
            if i.cell is not None and i.cell.available_ns() is not None:
                times.add(i.cell.available_ns())
            if i.price is not None:
                times.add(i.price.observed_at_ns)
        return [ns_to_iso(t) for t in sorted(times)]

    def to_json(self, cik: int | None = None, *, detail: bool = False) -> dict:
        d = {"metric": self.name, "formula": self.formula.ref, "expression": self.formula.expression,
             "nm_rule": self.formula.nm_rule or None, "basis": self.formula.basis, "period": self.period,
             "value": self.value.to_str() if self.value is not None else None, "unit": self.unit, "status": self.status,
             "reason": self.reason, "flags": list(self.flags), "input_obs_ids": self.obs_ids(), "source_dates": self.source_dates()}
        if self.bridge:
            d["bridge"] = list(self.bridge)
        d["inputs"] = [i.to_json(cik, depth=2 if detail else 0) for i in self.inputs] if detail else \
            [{"role": i.role, "value": (i.cell.value.to_str() if i.cell is not None and i.cell.value is not None else
                                        i.price.value.to_str() if i.price is not None else
                                        i.metric.value.to_str() if i.metric is not None and i.metric.value is not None else
                                        i.value.to_str() if i.value is not None else None),
              "period": i.cell.period.label if i.cell is not None else None,
              "method": i.cell.method if i.cell is not None else None} for i in self.inputs]
        return d


def _ratio(num: Fixed, den: Fixed) -> Fixed:
    return num.div(den, RATIO_SCALE, Rounding.HALF_EVEN)


def _avg(a: Fixed, b: Fixed) -> Fixed:
    s = a + b
    return s.div(2, min(s.scale + 1, 18), Rounding.HALF_EVEN)


class MetricEngine:
    """Computes the FORMULAS for one issuer as of ``fundamentals.as_of_ns`` (CAP-CO-07)."""

    def __init__(self, fundamentals: Fundamentals, price: PriceInput | None = None, *, sic=None,
                 allow_financial_ev: bool = False, stale_price_days: int = 5, fundamentals_gap_days: int = 135):
        self.f = fundamentals
        if price is not None and price.observed_at_ns > fundamentals.as_of_ns:
            raise ValueError("price observed after the as-of time would be look-ahead")
        self.price = price
        self.sic = sic
        self.suppress_ev = is_financial_sic(sic) and not allow_financial_ev
        self.stale_price_ns = stale_price_days * DAY_NS
        self.gap_ns = fundamentals_gap_days * DAY_NS
        self.q0 = fundamentals.latest_quarter()
        self._memo: dict[str, MetricResult] = {}

    # ------------------------------------------------------------ helpers
    def _result(self, name: str, value: Fixed | None, status: str, inputs=(), reason=None, flags=(), bridge=()) -> MetricResult:
        formula = FORMULAS[name]
        unit = self.f.currency if formula.unit == "money" else formula.unit
        period = self.q0.label if self.q0 is not None else "n/a"
        if formula.basis == FY:
            fy = self.f.latest_fiscal_year()
            period = fy.label if fy else "n/a"
        elif formula.basis == TTM and self.q0 is not None:
            period = f"TTM to {self.q0.end.isoformat()}"
        elif formula.basis == BAL and self.q0 is not None:
            period = self.q0.end.isoformat()
        return MetricResult(name, formula, value, status, unit, period, tuple(inputs), reason, tuple(flags), tuple(bridge))

    def _missing(self, name: str, inputs, reason: str) -> MetricResult:
        return self._result(name, None, "MISSING", inputs, reason)

    def ttm(self, key: str) -> Cell:
        return self.f.ttm(key, self.q0)

    def bal(self, key: str, end: date | None = None) -> Cell:
        end = end or (self.q0.end if self.q0 else date.max)
        return self.f.cell(key, Period("INSTANT", end))

    def avg_bal(self, key: str) -> tuple[Fixed | None, list[Input], list[str]]:
        end = self.bal(key)
        if not end.ok:
            return None, [Input(f"{key} (end)", end)], []
        back = self.f.year_ago_quarter(self.q0) if self.q0 else None
        begin = self.bal(key, back.end) if back else None
        if begin is not None and begin.ok:
            return _avg(begin.value, end.value), [Input(f"{key} (begin)", begin), Input(f"{key} (end)", end)], []
        return end.value, [Input(f"{key} (end)", end)], ["average_unavailable_ending_balance_used"]

    def _quotient(self, name: str, num: Cell, den: Cell, *, num_role: str, den_role: str, extra_flags=()) -> MetricResult:
        inputs = [Input(num_role, num), Input(den_role, den)]
        missing = [i.role for i in inputs if not i.cell.ok]
        if missing:
            return self._missing(name, inputs, "missing input: " + ", ".join(
                f"{i.role} ({i.cell.missing_reason}: {i.cell.detail})" for i in inputs if not i.cell.ok))
        if not den.value.is_positive():
            return self._result(name, None, "NM", inputs, f"not meaningful: {den_role} <= 0 ({den.value})")
        flags = tuple(extra_flags) + tuple(x for c in (num, den) for x in c.all_flags())
        return self._result(name, _ratio(num.value, den.value), "OK", inputs, flags=tuple(dict.fromkeys(flags)))

    def _no_q0(self, name: str) -> MetricResult | None:
        if self.q0 is None:
            return self._missing(name, (), "no fiscal quarter reported at the as-of time")
        return None

    # ------------------------------------------------------------ dispatch
    def compute(self, name: str) -> MetricResult:
        if name not in FORMULAS:
            raise KeyError(f"unknown metric {name}")
        if name not in self._memo:
            early = self._no_q0(name) if FORMULAS[name].basis != FY else None
            if early is None and FORMULAS[name].needs_price:
                early = self._price_gate(name)
            if early is None and FORMULAS[name].ev_based and self.suppress_ev:
                early = self._result(name, None, "SUPPRESSED", (), f"financial-sector issuer (SIC {self.sic}): EV-based "
                                     "metrics suppressed by default (CAP-CO-07)")
            self._memo[name] = early if early is not None else getattr(self, f"_m_{name}")()
        return self._memo[name]

    def all(self, names=None) -> list[MetricResult]:
        return [self.compute(n) for n in (names or FORMULAS)]

    def explain(self, name: str) -> dict:
        """'Show calculation': formula, inputs with values, units, periods, accessions and source dates."""
        r = self.compute(name)
        d = r.to_json(self.f.cik, detail=True)
        d.update({"entity_id": self.f.entity_id, "as_of": ns_to_iso(self.f.as_of_ns), "mapping": "std-map@1",
                  "currency": self.f.currency})
        return d

    # ------------------------------------------------------------ price gate
    def _price_gate(self, name: str) -> MetricResult | None:
        if self.price is None:
            return self._result(name, None, "NO_PRICE", (), "no price input supplied (price, currency, source and time are required)")
        if self.price.currency != self.f.currency:
            return self._result(name, None, "MISSING", (Input("price", price=self.price),),
                                f"price currency {self.price.currency} differs from reporting currency {self.f.currency}; "
                                "FX conversion is not applied to multiples")
        return None

    def _price_flags(self) -> list[str]:
        flags = []
        if self.f.as_of_ns - self.price.observed_at_ns > self.stale_price_ns:
            flags.append("stale_price")
        if self.q0 is not None:
            q_end_ns = int(datetime(self.q0.end.year, self.q0.end.month, self.q0.end.day, tzinfo=timezone.utc).timestamp()) * NS_PER_S
            if self.price.observed_at_ns - q_end_ns > self.gap_ns:
                flags.append("fundamentals_older_than_price_threshold")
        return flags

    # ------------------------------------------------------------ profitability
    def _m_revenue_ttm(self):
        c = self.ttm("revenue")
        if not c.ok:
            return self._missing("revenue_ttm", [Input("revenue_TTM", c)], f"{c.missing_reason}: {c.detail}")
        return self._result("revenue_ttm", c.value, "OK", [Input("revenue_TTM", c)], flags=c.all_flags())

    def _margin(self, name: str, item: str):
        return self._quotient(name, self.ttm(item), self.ttm("revenue"), num_role=f"{item}_TTM", den_role="revenue_TTM")

    def _m_gross_margin(self):
        return self._margin("gross_margin", "gross_profit")

    def _m_operating_margin(self):
        return self._margin("operating_margin", "operating_income")

    def _m_ebitda_margin(self):
        return self._margin("ebitda_margin", "ebitda")

    def _m_net_margin(self):
        return self._margin("net_margin", "net_income")

    def _m_fcf_ttm(self):
        c = self.ttm("free_cash_flow")
        if not c.ok:
            return self._missing("fcf_ttm", [Input("free_cash_flow_TTM", c)], f"{c.missing_reason}: {c.detail}")
        return self._result("fcf_ttm", c.value, "OK", [Input("free_cash_flow_TTM", c)], flags=c.all_flags())

    def _m_fcf_margin(self):
        return self._margin("fcf_margin", "free_cash_flow")

    def _m_effective_tax_rate(self):
        return self._quotient("effective_tax_rate", self.ttm("income_tax"), self.ttm("pretax_income"),
                              num_role="income_tax_TTM", den_role="pretax_income_TTM")

    def _m_fcff_ttm(self):
        etr = self.compute("effective_tax_rate")
        ebit, da, capex = self.ttm("ebit"), self.ttm("depreciation_amortization"), self.ttm("capex")
        nwc_end = self.bal("operating_working_capital")
        back = self.f.year_ago_quarter(self.q0)
        nwc_begin = self.bal("operating_working_capital", back.end) if back else None
        inputs = [Input("effective_tax_rate", metric=etr), Input("EBIT_TTM", ebit), Input("D&A_TTM", da), Input("capex_TTM", capex),
                  Input("NWC (end)", nwc_end)] + ([Input("NWC (4 quarters earlier)", nwc_begin)] if nwc_begin else [])
        if etr.status != "OK":
            return self._missing("fcff_ttm", inputs, f"effective tax rate {etr.status}: {etr.reason}")
        missing = [i.role for i in inputs if i.cell is not None and not i.cell.ok]
        if nwc_begin is None:
            missing.append("NWC (4 quarters earlier)")
        if missing:
            return self._missing("fcff_ttm", inputs, "missing input: " + ", ".join(missing))
        after_tax = ebit.value * (Fixed(1) - etr.value)
        value = (after_tax + da.value - capex.value - (nwc_end.value - nwc_begin.value)).rescale(0, Rounding.HALF_EVEN)
        flags = tuple(dict.fromkeys(x for i in inputs if i.cell is not None for x in i.cell.all_flags()))
        return self._result("fcff_ttm", value, "OK", inputs, flags=flags)

    # ------------------------------------------------------------ growth
    def _growth(self, name: str, cur: Cell, prior: Cell | None, role: str, no_prior: str = "no prior period at the as-of time"):
        inputs = [Input(f"{role} (current)", cur)] + ([Input(f"{role} (prior)", prior)] if prior is not None else [])
        if prior is None:
            return self._missing(name, inputs, no_prior)
        if not cur.ok or not prior.ok:
            return self._missing(name, inputs, "missing input: " + ", ".join(
                f"{i.role} ({i.cell.missing_reason})" for i in inputs if not i.cell.ok))
        if not prior.value.is_positive():
            return self._result(name, None, "NM", inputs, f"not meaningful: prior value <= 0 ({prior.value})")
        flags = tuple(dict.fromkeys(x for c in (cur, prior) for x in c.all_flags()))
        return self._result(name, _ratio(cur.value, prior.value) - 1, "OK", inputs, flags=flags)

    def _ttm_growth(self, name: str, item: str):
        back = self.f.year_ago_quarter(self.q0)
        prior = self.f.ttm(item, back) if back else None
        return self._growth(name, self.ttm(item), prior, f"{item}_TTM",
                            f"no prior period at the as-of time: no fiscal quarter ends one year before {self.q0.end}")

    def _m_revenue_growth_yoy(self):
        return self._ttm_growth("revenue_growth_yoy", "revenue")

    def _m_net_income_growth_yoy(self):
        return self._ttm_growth("net_income_growth_yoy", "net_income")

    def _m_revenue_growth_fy(self):
        fy = self.f.latest_fiscal_year()
        if fy is None:
            return self._missing("revenue_growth_fy", (), "no fiscal year reported at the as-of time")
        prev = self.f.fiscal_year_back(fy, 1)
        if prev is not None and prev.end + date.resolution != fy.start:
            return self._result("revenue_growth_fy", None, "MISSING", (), "fiscal calendar break between the two years "
                                "(transition period): excluded by default (CAP-CO-05)")
        return self._growth("revenue_growth_fy", self.f.cell("revenue", fy), self.f.cell("revenue", prev) if prev else None, "revenue_FY")

    # ------------------------------------------------------------ returns
    def _return_on(self, name: str, num: Cell, num_role: str, base_key: str, extra_inputs=(), num_value: Fixed | None = None):
        avg, bal_inputs, flags = self.avg_bal(base_key)
        inputs = list(extra_inputs) + [Input(num_role, num)] + bal_inputs
        if not num.ok:
            return self._missing(name, inputs, f"missing input: {num_role} ({num.missing_reason}: {num.detail})")
        if avg is None:
            return self._missing(name, inputs, f"missing input: {base_key} at {self.q0.end}")
        if not avg.is_positive():
            return self._result(name, None, "NM", inputs, f"not meaningful: average {base_key} <= 0 ({avg})")
        all_flags = flags + [x for i in inputs if i.cell is not None for x in i.cell.all_flags()]
        value = _ratio(num_value if num_value is not None else num.value, avg)
        return self._result(name, value, "OK", inputs, flags=tuple(dict.fromkeys(all_flags)))

    def _m_roe(self):
        return self._return_on("roe", self.ttm("net_income"), "net_income_TTM", "equity_parent")

    def _m_roa(self):
        return self._return_on("roa", self.ttm("net_income"), "net_income_TTM", "total_assets")

    def _m_roic(self):
        etr = self.compute("effective_tax_rate")
        ebit = self.ttm("ebit")
        if etr.status != "OK":
            status = "NM" if etr.status == "NM" else "MISSING"
            return self._result("roic", None, status, [Input("effective_tax_rate", metric=etr), Input("EBIT_TTM", ebit)],
                                f"effective tax rate {etr.status}: {etr.reason}")
        nopat = ebit.value * (Fixed(1) - etr.value) if ebit.ok else None
        return self._return_on("roic", ebit, "EBIT_TTM", "invested_capital", [Input("effective_tax_rate", metric=etr)], nopat)

    # ------------------------------------------------------------ leverage and liquidity
    def _m_debt_to_equity(self):
        return self._quotient("debt_to_equity", self.bal("total_debt"), self.bal("equity_parent"),
                              num_role="total_debt", den_role="equity_parent")

    def _m_net_debt(self):
        c = self.bal("net_debt")
        if not c.ok:
            return self._missing("net_debt", [Input("net_debt", c)], f"{c.missing_reason}: {c.detail}")
        return self._result("net_debt", c.value, "OK", [Input("net_debt", c)], flags=c.all_flags())

    def _m_net_debt_to_ebitda(self):
        return self._quotient("net_debt_to_ebitda", self.bal("net_debt"), self.ttm("ebitda"), num_role="net_debt", den_role="EBITDA_TTM")

    def _m_interest_coverage(self):
        return self._quotient("interest_coverage", self.ttm("ebit"), self.ttm("interest_expense"),
                              num_role="EBIT_TTM", den_role="interest_expense_TTM")

    def _m_current_ratio(self):
        return self._quotient("current_ratio", self.bal("current_assets"), self.bal("current_liabilities"),
                              num_role="current_assets", den_role="current_liabilities")

    def _m_quick_ratio(self):
        cash, sti, rec, cl = (self.bal(k) for k in ("cash_and_equivalents", "short_term_investments", "receivables", "current_liabilities"))
        inputs = [Input("cash_and_equivalents", cash), Input("short_term_investments", sti), Input("receivables", rec),
                  Input("current_liabilities", cl)]
        if not cash.ok or not cl.ok:
            return self._missing("quick_ratio", inputs, "missing input: cash and current liabilities are required")
        if not cl.value.is_positive():
            return self._result("quick_ratio", None, "NM", inputs, "not meaningful: current liabilities <= 0")
        num = cash.value + (sti.value if sti.ok else 0) + (rec.value if rec.ok else 0)
        flags = [f"not_reported:{k}" for k, c in (("short_term_investments", sti), ("receivables", rec)) if not c.ok]
        return self._result("quick_ratio", _ratio(num, cl.value), "OK", inputs, flags=flags)

    # ------------------------------------------------------------ price based
    def _shares(self) -> Cell:
        c = self.f.latest_instant("shares_outstanding_cover")
        if c.ok:
            return c
        return self.f.latest_instant("shares_outstanding_balance")

    def _m_market_cap(self):
        shares = self._shares()
        inputs = [Input("price", price=self.price), Input("shares_outstanding", shares)]
        if not shares.ok:
            return self._missing("market_cap", inputs, "no shares outstanding reported at the as-of time")
        flags = self._price_flags()
        shares_date_ns = int(datetime(shares.period.end.year, shares.period.end.month, shares.period.end.day,
                                      tzinfo=timezone.utc).timestamp()) * NS_PER_S
        if self.price.observed_at_ns - shares_date_ns > 120 * DAY_NS:
            flags.append("shares_count_older_than_120_days")
        if shares.item == "shares_outstanding_balance":
            flags.append("balance_sheet_share_count_used")
        return self._result("market_cap", self.price.value * shares.value, "OK", inputs, flags=flags)

    def _ev(self, name: str, extra: tuple[tuple[str, int], ...] = ()):
        mc = self.compute("market_cap")
        if mc.status != "OK":
            return self._result(name, None, mc.status if mc.status != "OK" else "MISSING", [Input("market_cap", metric=mc)],
                                f"market cap unavailable: {mc.reason}")
        parts = (("total_debt", 1), ("preferred_stock", 1), ("noncontrolling_interest", 1), ("cash_and_equivalents", -1),
                 ("short_term_investments", -1)) + extra
        total = mc.value
        bridge = [{"component": "market_cap", "sign": "+", "value": mc.value.to_str(), "contribution": mc.value.to_str(), "status": "OK",
                   "source": f"price x shares ({FORMULAS['market_cap'].ref})", "obs_ids": mc.obs_ids()}]
        inputs = [Input("market_cap", metric=mc)]
        missing = []
        for key, sign in parts:
            c = self.bal(key)
            inputs.append(Input(key, c))
            if c.ok:
                # sign -1 on pension_funded_status: funded status is negative when underfunded, so
                # subtracting it adds the net unfunded liability to EV.
                v = c.value if sign > 0 else -c.value
                total = total + v
                facts = c.all_facts()
                bridge.append({"component": key, "sign": "+" if sign > 0 else "-", "value": c.value.to_str(),
                               "contribution": v.to_str(), "status": "OK", "period_end": c.period.end.isoformat(),
                               "method": c.method, "obs_ids": c.obs_ids(),
                               "accessions": sorted({f.accession for f in facts}),
                               "sec_filing_index": sec_index_url(self.f.cik, facts[0].accession) if facts else None})
            else:
                missing.append(key)
                bridge.append({"component": key, "sign": "+" if sign > 0 else "-", "value": None, "status": "MISSING",
                               "missing_reason": c.missing_reason, "detail": c.detail})
        status = "INCOMPLETE" if missing else "OK"
        reason = (f"components not reported by the filer at {self.q0.end}: {', '.join(missing)}; excluded from the sum "
                  "(not treated as reported zeros)") if missing else None
        flags = list(mc.flags) + ([f"missing:{m}" for m in missing])
        return self._result(name, total, status, inputs, reason, flags, bridge)

    def _m_ev_standard(self):
        return self._ev("ev_standard")

    def _m_ev_with_operating_leases(self):
        return self._ev("ev_with_operating_leases", (("operating_lease_liabilities", 1),))

    def _m_ev_pension_ex_equity_method(self):
        return self._ev("ev_pension_ex_equity_method", (("pension_funded_status", -1), ("equity_method_investments", -1)))

    def _over_price_metric(self, name: str, top: MetricResult, den: Cell, den_role: str):
        inputs = [Input(top.name, metric=top), Input(den_role, den)]
        if top.status not in ("OK", "INCOMPLETE"):
            return self._result(name, None, top.status, inputs, f"{top.name} {top.status}: {top.reason}")
        if not den.ok:
            return self._missing(name, inputs, f"missing input: {den_role} ({den.missing_reason}: {den.detail})")
        if not den.value.is_positive():
            return self._result(name, None, "NM", inputs, f"not meaningful: {den_role} <= 0 ({den.value})")
        flags = tuple(dict.fromkeys(list(top.flags) + den.all_flags()))
        return self._result(name, _ratio(top.value, den.value), top.status, inputs, top.reason, flags)

    def diluted_shares_at_latest_quarter(self) -> Cell:
        idx = self.f.index["weighted_shares_diluted"]
        candidates = [f for concept in idx.values() for (s, e), f in concept.items() if e == self.q0.end and s is not None]
        if not candidates:
            return self.f.cell("weighted_shares_diluted", self.q0)
        best = max(candidates, key=lambda f: f.start)      # the shortest period ending at q0 (the quarter if reported)
        flags = () if best.start == self.q0.start else ("weighted_shares_from_ytd_or_annual_period",)
        return Cell("weighted_shares_diluted", Period("Q", best.end, best.start), best.unit, best.value, "REPORTED",
                    best.concept, (best,), flags=flags)

    def _m_pe_ttm(self):
        ni, shares = self.ttm("net_income"), self.diluted_shares_at_latest_quarter()
        inputs = [Input("price", price=self.price), Input("net_income_TTM", ni), Input("diluted_weighted_shares", shares)]
        if not ni.ok or not shares.ok:
            return self._missing("pe_ttm", inputs, "missing input: " + ", ".join(i.role for i in inputs[1:] if not i.cell.ok))
        if not ni.value.is_positive():
            return self._result("pe_ttm", None, "NM", inputs, f"not meaningful: net income TTM <= 0 ({ni.value})")
        eps = ni.value.div(shares.value, 6, Rounding.HALF_EVEN)
        inputs.append(Input("EPS_TTM_diluted (display, 6 dp)", value=eps))
        value = (self.price.value * shares.value).div(ni.value, RATIO_SCALE, Rounding.HALF_EVEN)
        flags = self._price_flags() + ni.all_flags() + shares.all_flags()
        return self._result("pe_ttm", value, "OK", inputs, flags=tuple(dict.fromkeys(flags)))

    def _m_pe_fy(self):
        fy = self.f.latest_fiscal_year()
        if fy is None:
            return self._missing("pe_fy", [Input("price", price=self.price)], "no fiscal year reported at the as-of time")
        eps = self.f.cell("eps_diluted", fy)
        inputs = [Input("price", price=self.price), Input("eps_diluted_FY", eps)]
        if not eps.ok:
            return self._missing("pe_fy", inputs, f"missing input: diluted EPS {fy.label} ({eps.missing_reason})")
        if not eps.value.is_positive():
            return self._result("pe_fy", None, "NM", inputs, f"not meaningful: diluted EPS <= 0 ({eps.value})")
        return self._result("pe_fy", _ratio(self.price.value, eps.value), "OK", inputs, flags=self._price_flags())

    def _m_price_to_book(self):
        return self._over_price_metric("price_to_book", self.compute("market_cap"), self.bal("equity_parent"), "equity_parent")

    def _m_price_to_sales(self):
        return self._over_price_metric("price_to_sales", self.compute("market_cap"), self.ttm("revenue"), "revenue_TTM")

    def _m_ev_to_sales(self):
        return self._over_price_metric("ev_to_sales", self.compute("ev_standard"), self.ttm("revenue"), "revenue_TTM")

    def _m_ev_to_ebitda(self):
        return self._over_price_metric("ev_to_ebitda", self.compute("ev_standard"), self.ttm("ebitda"), "EBITDA_TTM")

    def _m_ev_to_ebit(self):
        return self._over_price_metric("ev_to_ebit", self.compute("ev_standard"), self.ttm("ebit"), "EBIT_TTM")

    def _yield(self, name: str, item: str):
        mc = self.compute("market_cap")
        c = self.ttm(item)
        inputs = [Input(f"{item}_TTM", c), Input("market_cap", metric=mc)]
        if mc.status != "OK":
            return self._result(name, None, mc.status, inputs, f"market cap {mc.status}: {mc.reason}")
        if not c.ok:
            return self._missing(name, inputs, f"missing input: {item}_TTM ({c.missing_reason}: {c.detail})")
        return self._result(name, _ratio(c.value, mc.value), "OK", inputs, flags=tuple(dict.fromkeys(list(mc.flags) + c.all_flags())))

    def _m_fcf_yield(self):
        return self._yield("fcf_yield", "free_cash_flow")

    def _m_dividend_yield(self):
        return self._yield("dividend_yield", "dividends_paid")


# ---------------------------------------------------------------- persistence and record explanation
PERSIST_REASON = {"NM": "NM", "MISSING": "NOT_REPORTED", "INCOMPLETE": "INCOMPLETE", "SUPPRESSED": "SUPPRESSED", "NO_PRICE": "NO_PRICE"}


def persist(store, results: list[MetricResult], entity_id: str, as_of_ns: int, computed_at_ns: int) -> list[dict]:
    """Write results with ``record_metric`` (formula id@version, input observation ids).

    Only ``OK`` values are stored as values. ``INCOMPLETE`` EVs and multiples are stored without a
    value (reason NOT_REPORTED) because the derived_metric table has no quality flag column to carry
    the INCOMPLETE label. Price-based metrics are stored only when the price input is itself a stored
    observation (``record_price_input``), so every input has an observation id."""
    out = []
    for r in results:
        price_inputs = [i.price for i in r.inputs if i.price is not None]
        for i in r.inputs:
            if i.metric is not None:
                price_inputs += [j.price for j in i.metric.inputs if j.price is not None]
        if r.formula.needs_price and r.status != "NO_PRICE" and any(p.obs_id is None for p in price_inputs):
            out.append({"metric": r.name, "persisted": False, "reason": "price input is not a stored observation"})
            continue
        value = r.value if r.status == "OK" else None
        reason = None if value is not None else pit_reason(PERSIST_REASON.get(r.status, "NOT_REPORTED"))
        metric_id = store.record_metric(entity_id, r.name, r.formula.ref, as_of_ns, value, r.unit, r.obs_ids(), computed_at_ns, reason)
        out.append({"metric": r.name, "persisted": True, "metric_id": metric_id, "status": r.status})
    return out


def explain_record(store, metric_id: int) -> dict:
    """'Show calculation' for a persisted derived_metric row: its formula and every input observation."""
    row = store.conn.execute("SELECT * FROM derived_metric WHERE metric_id=?", (metric_id,)).fetchone()
    if row is None:
        raise LookupError(f"no derived metric {metric_id}")
    ids = json.loads(row["inputs"])
    obs = []
    if ids:
        marks = ",".join("?" * len(ids))
        for o in store.conn.execute(f"SELECT * FROM observation WHERE obs_id IN ({marks}) ORDER BY obs_id", ids):
            cik = int(o["entity_id"][8:]) if o["entity_id"].startswith("SEC-CIK-") else None
            obs.append({"obs_id": o["obs_id"], "dataset": o["dataset"], "field": o["field"], "value": o["value"],
                        "value_text": o["value_text"], "unit": o["unit"], "period_start": o["period_start"],
                        "period_end": o["period_end"], "publication_id": o["publication_id"],
                        "available_time": ns_to_iso(o["available_time_ns"]), "source_ref": o["source_ref"],
                        "sec_filing_index": sec_index_url(cik, o["publication_id"])})
    name, _, version = row["formula"].partition("@")
    f = FORMULAS.get(name)
    definition = f.expression if f is not None and str(f.version) == version else None
    return {"metric_id": row["metric_id"], "entity_id": row["entity_id"], "metric": row["metric"], "formula": row["formula"],
            "expression": definition or "formula version not defined in this build",
            "value": row["value"], "unit": row["unit"], "missing_reason": row["missing_reason"],
            "as_of": ns_to_iso(row["as_of_ns"]), "computed_at": ns_to_iso(row["computed_at_ns"]),
            "inputs": obs, "missing_input_ids": sorted(set(ids) - {o["obs_id"] for o in obs})}
