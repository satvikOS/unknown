"""Shared definitions for the global macro sources (CAP-MAC-05, CAP-MAC-06, CAP-MAC-09; ADR-0015).

Countries. The country set is derived from ``config/markets/venues.json``: every venue country
(US, IN and the EU member states with a registered equity venue). A country whose currency is EUR
is a euro-area member and takes the ECB policy rate (BIS series ``XM``).

Columns. A *column* is one indicator from one source with one definition and frequency, e.g.
"CPI inflation, % over 1 year, monthly (OECD)". Two sources' figures for the same indicator are
never merged into one number: they are separate columns, each labelled with its provider, dataset,
frequency and attribution.

Storage. One research-store entity per (column, area), ``MACRO-<column>-<area>``, field ``value``,
unit ``percent`` (or the column's unit, e.g. an index level), period start/end as ISO dates (``period_kind`` YEAR, QUARTER, MONTH or DAY).
Values are the source's decimal text, parsed exactly (never through a float). A value the source
leaves empty is a row with a missing reason, never a zero.

Availability. Statistical releases are stored without upstream vintages, so a value is available
from the moment this system first saw it (``available_from_first_capture``); a changed value on a
later fetch is a new publication at its receipt time flagged ``revised_after_first_seen``
(``common.record_if_changed``). Policy rates in force at the end of a period are public by then,
so their backfill uses the end of the period (see ``bis``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from ...domain.fixed import MAX_SCALE, Fixed
from ..pit import Observation
from ..registry import REPO, dataset, load_registry
from .common import record_if_changed

ISO3 = {"US": "USA", "IN": "IND", "DE": "DEU", "FR": "FRA", "NL": "NLD", "BE": "BEL", "PT": "PRT", "IT": "ITA", "ES": "ESP",
        "AT": "AUT", "IE": "IRL", "FI": "FIN", "GR": "GRC", "LU": "LUX", "DK": "DNK", "SE": "SWE", "PL": "POL"}
EU_MEMBERS = frozenset({"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT",
                        "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"})
NAMES = {"US": "United States", "IN": "India", "DE": "Germany", "FR": "France", "NL": "Netherlands", "BE": "Belgium",
         "PT": "Portugal", "IT": "Italy", "ES": "Spain", "XM": "Euro area"}
EURO_AREA = "XM"


@dataclass(frozen=True)
class Country:
    code: str          # ISO 3166-1 alpha-2
    iso3: str
    name: str
    currency: str
    euro_area: bool
    eu_member: bool
    mics: tuple[str, ...]

    def to_json(self) -> dict:
        return {"code": self.code, "iso3": self.iso3, "name": self.name, "currency": self.currency, "euro_area": self.euro_area,
                "eu_member": self.eu_member, "venues": list(self.mics)}


def countries(path=None) -> list[Country]:
    """Venue countries from the market registry, in registry order (US first)."""
    venues = json.loads((path or REPO / "config" / "markets" / "venues.json").read_text())["venues"]
    out: dict[str, Country] = {}
    for v in venues:
        c = v["country"]
        if c not in ISO3:
            raise ValueError(f"venue country {c} has no ISO3 code in macro_common.ISO3")
        prev = out.get(c)
        mics = (prev.mics if prev else ()) + (v["mic"],)
        out[c] = Country(c, ISO3[c], NAMES.get(c, c), v["currency"], v["currency"] == "EUR", c in EU_MEMBERS, mics)
    return list(out.values())


def eu_equity_mics(path=None) -> frozenset[str]:
    return frozenset(m for c in countries(path) if c.eu_member for m in c.mics)


@dataclass(frozen=True)
class Column:
    column_id: str
    indicator: str           # gdp_growth | cpi_inflation | unemployment | policy_rate
    provider: str
    dataset: str
    series_code: str
    label: str
    frequency: str           # A | Q | M | D
    definition: str
    attribution: str
    areas: tuple[str, ...] | None = None   # None: every venue country
    unit: str = "percent"

    def to_json(self) -> dict:
        return {"column_id": self.column_id, "indicator": self.indicator, "provider": self.provider, "dataset": self.dataset,
                "series_code": self.series_code, "label": self.label, "unit": self.unit, "frequency": self.frequency,
                "definition": self.definition, "attribution": self.attribution}


INDICATORS = {"gdp_growth": "GDP growth", "cpi_inflation": "CPI inflation", "unemployment": "Unemployment rate",
              "policy_rate": "Policy rate"}
WB_ATTR = "Source: World Bank, World Development Indicators (CC BY 4.0)"
OECD_ATTR = "Source: OECD Data Explorer"
BIS_ATTR = "Source: BIS central bank policy rates (WS_CBPOL)"
FRED_ATTR = "This product uses the FRED(R) API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."

COLUMNS: tuple[Column, ...] = (
    Column("wb_gdp_growth", "gdp_growth", "world-bank", "worldbank.wdi", "NY.GDP.MKTP.KD.ZG", "GDP growth, annual % (World Bank)",
           "A", "Annual percentage growth rate of GDP at market prices, constant local currency", WB_ATTR),
    Column("wb_cpi_inflation", "cpi_inflation", "world-bank", "worldbank.wdi", "FP.CPI.TOTL.ZG", "CPI inflation, annual % (World Bank)",
           "A", "Inflation, consumer prices (annual %): change in the annual average CPI", WB_ATTR),
    Column("wb_unemployment", "unemployment", "world-bank", "worldbank.wdi", "SL.UEM.TOTL.ZS",
           "Unemployment, annual % (World Bank, ILO modelled)", "A",
           "Unemployment, total (% of total labor force) (modeled ILO estimate)", WB_ATTR),
    Column("oecd_cpi_yoy", "cpi_inflation", "oecd", "oecd.sdmx_macro", "DF_PRICES_ALL:M.N.CPI.PA._T.N.GY",
           "CPI inflation, % over 1 year, monthly (OECD)", "M", "Consumer price index, national, total, growth rate over 1 year, "
           "not seasonally adjusted", OECD_ATTR),
    Column("oecd_unemployment", "unemployment", "oecd", "oecd.sdmx_macro", "DF_IALFS_UNE_M:UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M",
           "Unemployment rate, % of labour force, SA, monthly (OECD)", "M", "Monthly unemployment rate, age 15 and over, "
           "seasonally adjusted", OECD_ATTR, areas=("US", "DE", "FR", "NL", "BE", "PT", "IT", "ES")),
    Column("bis_policy_rate", "policy_rate", "bis", "bis.cbpol", "WS_CBPOL:M", "Policy rate, % end of month (BIS)", "M",
           "Central bank policy rate at the end of the month (US: mid-point of the Federal Reserve target range; IN: RBI "
           "policy repo rate; euro area: ECB)", BIS_ATTR, areas=("US", "IN", EURO_AREA)),
)
FRED_SERIES = {
    "DFEDTARU": ("policy_rate", "D", "Federal funds target range, upper limit, % (FRED)"),
    "DFEDTARL": ("policy_rate", "D", "Federal funds target range, lower limit, % (FRED)"),
    "UNRATE": ("unemployment", "M", "Unemployment rate, %, SA, monthly (FRED, BLS)"),
    "A191RL1Q225SBEA": ("gdp_growth", "Q", "Real GDP growth, % change, SAAR, quarterly (FRED, BEA)"),
    "CPIAUCSL": ("cpi_index", "M", "CPI for all urban consumers, index 1982-84=100, SA (FRED, BLS)"),
}
FRED_UNITS = {"CPIAUCSL": "index_1982_84_eq_100"}


def fred_column(series_id: str) -> Column:
    ind, freq, label = FRED_SERIES.get(series_id, ("other", "?", f"{series_id} (FRED)"))
    return Column(f"fred_{series_id}", ind, "fred", "fred.series", series_id, label, freq, label, FRED_ATTR, areas=("US",),
                  unit=FRED_UNITS.get(series_id, "percent"))


def column(column_id: str) -> Column:
    for c in COLUMNS:
        if c.column_id == column_id:
            return c
    if column_id.startswith("fred_"):
        return fred_column(column_id[5:])
    raise LookupError(f"unknown macro column {column_id}; one of {', '.join(c.column_id for c in COLUMNS)} or fred_<SERIES>")


def entity(column_id: str, area: str) -> str:
    return f"MACRO-{column_id}-{area}"


def exact(text) -> Fixed:
    """Exact decimal from the source's text. Exponent notation is expanded exactly; more than
    MAX_SCALE fractional digits is refused (never silently rounded)."""
    t = str(text).strip()
    if "e" in t.lower():
        try:
            t = format(Decimal(t), "f")
        except InvalidOperation:
            raise ValueError(f"not a decimal number: {text!r}") from None
    frac = t.split(".", 1)[1] if "." in t else ""
    if len(frac) > MAX_SCALE:
        raise ValueError(f"more than {MAX_SCALE} fractional digits: {text!r}")
    return Fixed.parse(t)


def period_bounds(period: str) -> tuple[str, str, str]:
    """(start, end, kind) for 'YYYY', 'YYYY-Qn', 'YYYY-MM' or 'YYYY-MM-DD'."""
    p = period.strip()
    if len(p) == 4 and p.isdigit():
        return f"{p}-01-01", f"{p}-12-31", "YEAR"
    if len(p) == 7 and p[4] == "-" and p[5] in "Qq":
        y, q = int(p[:4]), int(p[6])
        if not 1 <= q <= 4:
            raise ValueError(f"bad quarter {period!r}")
        start = date(y, 3 * q - 2, 1)
        end = (date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), "QUARTER"
    if len(p) == 7 and p[4] == "-":
        y, m = int(p[:4]), int(p[5:7])
        start = date(y, m, 1)
        end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), "MONTH"
    d = date.fromisoformat(p)
    return d.isoformat(), d.isoformat(), "DAY"


def first_capture(receipt_ns: int, _period_end: date) -> tuple[int, tuple[str, ...]]:
    return receipt_ns, ("available_from_first_capture",)


def require_storage(dataset_id: str):
    ds = dataset(load_registry(), dataset_id)
    if not ds.permits("storage"):
        raise PermissionError(f"{dataset_id}: storage not permitted by the registry ({ds.rights_source[:120]})")
    return ds


def record_points(store, col: Column, area: str, points: list[dict], *, raw_sha: str, receipt_ns: int, now_ns,
                  availability=first_capture, title: str | None = None, extra_attrs: dict | None = None) -> dict:
    """Store one area's points for a column. A point is {period, value (Fixed|None), missing_reason, key, quality}."""
    eid = entity(col.column_id, area)
    store.upsert_entity(eid, "macro_series", f"{col.label}: {NAMES.get(area, area)}",
                        {"column_id": col.column_id, "indicator": col.indicator, "provider": col.provider, "dataset": col.dataset,
                         "series_code": col.series_code, "area": area, "frequency": col.frequency, "unit": col.unit,
                         "definition": col.definition, "title": title or col.label, **(extra_attrs or {})})
    counts = {"NEW": 0, "UNCHANGED": 0, "REVISED": 0, "CORRECTED": 0}
    for p in points:
        start, end, kind = period_bounds(p["period"])
        available, flags = availability(receipt_ns, date.fromisoformat(end))
        obs = Observation(dataset=col.dataset, entity_id=eid, field="value", unit=col.unit, receipt_time_ns=receipt_ns,
                          available_time_ns=available, source_ref=f"raw:{raw_sha}#{p.get('key', col.series_code)}/{p['period']}",
                          value=p.get("value"), missing_reason=None if p.get("value") is not None else (p.get("missing_reason") or "NOT_REPORTED"),
                          period_start=start, period_end=end, period_kind=kind,
                          quality=tuple(flags) + tuple(p.get("quality", ())),
                          publication_id=f"{col.column_id}:{area}:{p['period']}")
        _, outcome = record_if_changed(store, obs, now_ns())
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def merge_counts(total: dict, part: dict) -> dict:
    for k, v in part.items():
        total[k] = total.get(k, 0) + v
    return total
