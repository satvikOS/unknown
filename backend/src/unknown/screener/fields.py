"""Screen fields: what a criterion can test, and how one issuer's value is obtained point in time (CAP-MKT-10).

Every value comes with a status (``OK``, ``NM``, ``MISSING``, ``NOT_APPLICABLE``, ``NO_PRICE``, ...), a
reason code from the unavailable vocabulary (``domain/unavailable.py``) when it has no value, and its
lineage (formula id@version, input observation ids, period). Nothing is ever replaced by zero.

* SEC metrics: every ``FORMULAS`` entry of ``data/fundamentals/metrics.py`` (TTM / balance / FY basis,
  SEC companyfacts, CAP-CO-07). Price-based ones need a price input.
* FY fields (``*_fy``): the latest fiscal year available at the as-of time, from SEC companyfacts or,
  for EU issuers, ESEF annual reports (``esef-ifrs-map@1``). The same keys on both paths.
* ``price``: the last price used for this run (an owner price input from the PIT store, or a live quote
  from the market-data bus for a run as of now), with its source and observation time.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..data.fundamentals.ifrs import IFRS_BY_KEY, EsefStatements
from ..data.fundamentals.metrics import FORMULAS, RATIO_SCALE, MetricEngine, PriceInput
from ..data.fundamentals.periods import Period
from ..data.fundamentals.statements import Fundamentals
from ..domain.fixed import Fixed, Rounding
from ..domain.unavailable import classify

SEC, ESEF = "sec", "esef"


@dataclass(frozen=True)
class FieldDef:
    name: str
    label: str
    unit: str            # ratio | x | money | price
    sources: tuple[str, ...]
    kind: str            # metric | fy | fy_ratio | quote
    formula: str
    basis: str
    nm_rule: str = ""


def _label(name: str) -> str:
    return name.replace("_", " ").replace(" ttm", " (TTM)").replace(" fy", " (FY)").replace(" yoy", " YoY")


FY_ITEMS = {"revenue_fy": "revenue", "operating_income_fy": "operating_income", "net_income_fy": "net_income", "cfo_fy": "cfo",
            "total_assets_fy": "total_assets", "equity_parent_fy": "equity_parent"}
FY_BALANCE = {"total_assets", "equity_parent"}
FY_RATIOS = {"net_margin_fy": ("net_income", "revenue", "NM if revenue <= 0"),
             "operating_margin_fy": ("operating_income", "revenue", "NM if revenue <= 0"),
             "roe_fy": ("net_income", "equity_parent", "NM if ending equity <= 0")}

FIELDS: dict[str, FieldDef] = {}
for _n, _f in FORMULAS.items():
    FIELDS[_n] = FieldDef(_n, _label(_n), _f.unit, (SEC,), "metric", _f.ref, _f.basis, _f.nm_rule)
for _n, _item in FY_ITEMS.items():
    FIELDS[_n] = FieldDef(_n, _label(_n), "money", (SEC, ESEF), "fy", f"screen_{_n}@1",
                          "balance at latest fiscal year end" if _item in FY_BALANCE else "latest fiscal year")
for _n, (_num, _den, _rule) in FY_RATIOS.items():
    FIELDS[_n] = FieldDef(_n, _label(_n), "ratio", (SEC, ESEF), "fy_ratio", f"screen_{_n}@1", f"{_num}_FY / {_den}_FY (latest fiscal year)", _rule)
FIELDS["price"] = FieldDef("price", "Price", "price", (SEC, ESEF), "quote", "screen_price@1", "last price input at or before the as-of time")


def catalog() -> list[dict]:
    return [{"field": f.name, "label": f.label, "unit": f.unit, "sources": list(f.sources), "kind": f.kind, "formula": f.formula,
             "basis": f.basis, "nm_rule": f.nm_rule or None, "needs_price": f.kind == "quote" or FORMULAS.get(f.name) is not None
             and FORMULAS[f.name].needs_price} for f in FIELDS.values()]


def value(status: str, *, value: Fixed | None = None, unit: str | None = None, reason: str | None = None, detail: str | None = None,
          formula: str | None = None, period: str | None = None, obs_ids=(), flags=(), currency: str | None = None) -> dict:
    out = {"value": value.to_str() if value is not None else None, "status": status, "unit": unit, "currency": currency,
           "formula": formula, "period": period, "input_obs_ids": sorted(set(obs_ids)), "flags": list(flags)}
    if value is None:
        out["reason"] = reason or classify(status)
        out["detail"] = detail
    return out


class IssuerView:
    """One issuer's field values as of a time (computed lazily, memoised)."""

    def __init__(self, store, issuer: dict, as_of_ns: int, knowledge_cutoff_ns: int | None, price: PriceInput | None,
                 price_reason: tuple[str, str] | None):
        self.store, self.issuer, self.as_of_ns, self.cutoff = store, issuer, as_of_ns, knowledge_cutoff_ns
        self.price, self.price_reason = price, price_reason
        self._f = self._engine = self._esef = None
        self._memo: dict[str, dict] = {}

    # SEC path
    def fundamentals(self) -> Fundamentals:
        if self._f is None:
            self._f = Fundamentals(self.store, self.issuer["entity_id"], self.as_of_ns, knowledge_cutoff_ns=self.cutoff)
        return self._f

    def engine(self) -> MetricEngine:
        if self._engine is None:
            self._engine = MetricEngine(self.fundamentals(), self.price, sic=self.issuer.get("sic"))
        return self._engine

    def esef(self) -> EsefStatements:
        if self._esef is None:
            self._esef = EsefStatements(self.store, self.issuer["lei"], self.as_of_ns, knowledge_cutoff_ns=self.cutoff)
        return self._esef

    def has_data(self) -> bool:
        if self.issuer["source"] == SEC:
            return self.fundamentals().fact_count > 0
        return bool(self.esef().fiscal_years)

    def get(self, name: str) -> dict:
        if name not in self._memo:
            self._memo[name] = self._compute(name)
        return self._memo[name]

    def _compute(self, name: str) -> dict:
        fd = FIELDS[name]
        src = self.issuer["source"]
        if src not in fd.sources:
            return value("NOT_APPLICABLE", reason="NOT_APPLICABLE", formula=fd.formula,
                         detail=f"{fd.name} is computed from SEC quarterly XBRL; {src.upper()} issuers file annual reports only")
        if fd.kind == "quote":
            if self.price is None:
                code, detail = self.price_reason or ("NO_DATA", "no price input")
                return value("NO_PRICE", reason=code, detail=detail, formula=fd.formula)
            return value("OK", value=self.price.value, unit="price", currency=self.price.currency, formula=fd.formula,
                         period=self.price.source, obs_ids=[self.price.obs_id] if self.price.obs_id else [])
        if fd.kind == "metric":
            r = self.engine().compute(name)
            unit = r.unit
            currency = unit if FORMULAS[name].unit == "money" else None
            if r.status == "NO_PRICE" and self.price_reason:
                code, detail = self.price_reason
                return value("NO_PRICE", reason=code, detail=f"{r.reason or ''} {detail}".strip(), formula=r.formula.ref, period=r.period)
            return value(r.status, value=r.value if r.status == "OK" else None, unit=FORMULAS[name].unit, currency=currency,
                         reason=_reason(r.status), detail=r.reason, formula=r.formula.ref, period=r.period, obs_ids=r.obs_ids(),
                         flags=r.flags)
        if fd.kind == "fy":
            return self._fy_cell(FY_ITEMS[name], fd.formula)
        num_key, den_key, _ = FY_RATIOS[name]
        num, den = self._fy_cell(num_key, fd.formula), self._fy_cell(den_key, fd.formula)
        ids = num["input_obs_ids"] + den["input_obs_ids"]
        missing = [k for k, c in ((num_key, num), (den_key, den)) if c["value"] is None]
        if missing:
            return value("MISSING", reason="NOT_REPORTED", detail=f"missing {', '.join(missing)} for the latest fiscal year",
                         formula=fd.formula, period=num["period"] or den["period"], obs_ids=ids)
        if num["period"] != den["period"]:
            return value("MISSING", reason="NO_DATA", detail=f"periods differ ({num['period']} vs {den['period']})", formula=fd.formula, obs_ids=ids)
        d = Fixed.parse(den["value"])
        if not d.is_positive():
            return value("NM", reason="NOT_APPLICABLE", detail=FY_RATIOS[name][2], formula=fd.formula, period=num["period"], obs_ids=ids)
        return value("OK", value=Fixed.parse(num["value"]).div(d, RATIO_SCALE, Rounding.HALF_EVEN), unit="ratio", formula=fd.formula,
                     period=num["period"], obs_ids=ids, flags=num["flags"] + den["flags"])

    def _fy_cell(self, key: str, formula: str) -> dict:
        if self.issuer["source"] == SEC:
            f = self.fundamentals()
            fy = f.latest_fiscal_year()
            if fy is None:
                return value("MISSING", reason="NO_DATA", detail="no fiscal year available at the as-of time", formula=formula)
            period = Period("INSTANT", fy.end) if key in FY_BALANCE else fy
            c = f.cell(key, period)
            if c.value is None:
                return value("MISSING", reason=classify(c.missing_reason or "NOT_REPORTED"), detail=c.detail, formula=formula,
                             period=fy.label, obs_ids=c.obs_ids())
            return value("OK", value=c.value, unit="money", currency=f.currency, formula=formula, period=fy.label, obs_ids=c.obs_ids(),
                         flags=c.all_flags())
        e = self.esef()
        if not e.fiscal_years:
            return value("MISSING", reason="NO_DATA", detail="no ESEF annual figures available at the as-of time", formula=formula)
        fy = e.fiscal_years[-1]
        c = e.cell(IFRS_BY_KEY[key], fy) if key in IFRS_BY_KEY else None
        if c is None:
            return value("NOT_APPLICABLE", reason="NOT_APPLICABLE", detail=f"{key} is not in esef-ifrs-map@1", formula=formula)
        label = c["period"]["label"]
        if c["value"] is None:
            return value("MISSING", reason=classify(c.get("missing_reason") or "NOT_REPORTED"), detail=c.get("detail"), formula=formula,
                         period=label)
        return value("OK", value=Fixed.parse(c["value"]), unit="money", currency=e.currency, formula=formula, period=label,
                     obs_ids=c["obs_ids"], flags=c["flags"])


def _reason(status: str) -> str | None:
    return {"OK": None, "NM": "NOT_APPLICABLE", "MISSING": "NOT_REPORTED", "INCOMPLETE": "NOT_REPORTED", "SUPPRESSED": "NOT_APPLICABLE",
            "NO_PRICE": "NO_DATA"}.get(status, classify(status))
