"""Shared FIXTURE builders for the company-research tests, plus checks of the builders and the mapping.

Every company, filing, accession number and value here is SYNTHETIC (labelled FIXTURE); none is
market data or a real filing. Payloads mimic the documented SEC JSON shapes (submissions and
companyfacts) and are ingested through the real ``unknown.data.sec.ingest`` path with a fake
opener, so the tests exercise exactly what live ingestion stores.

Fiscal calendar of the fixture issuer (52-53 week years ending on a Saturday):
  FY2024 2023-10-01..2024-09-28 (364 d)   FY2025 2024-09-29..2025-09-27 (364 d)
  FY2026 2025-09-28..2026-10-03 (371 d: a 53-week year whose Q4 is 98 days)
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from unknown.data.fundamentals.mapping import ITEMS, LINE_ITEMS, MAPPING_REF, validate_mapping
from unknown.data.http import Fetcher
from unknown.data.pit import ResearchStore
from unknown.data.registry import RateLimit
from unknown.data.sec import ingest as sec_ingest
from unknown.domain.clock import NS_PER_S

UA = "Fixture Owner owner@example.test"
CIK = 9000001
SIC = "3571"

FY = {2024: (date(2023, 10, 1), date(2024, 9, 28)), 2025: (date(2024, 9, 29), date(2025, 9, 27)),
      2026: (date(2025, 9, 28), date(2026, 10, 3))}
Q_ENDS = {2024: [date(2023, 12, 30), date(2024, 3, 30), date(2024, 6, 29)],
          2025: [date(2024, 12, 28), date(2025, 3, 29), date(2025, 6, 28)],
          2026: [date(2025, 12, 27), date(2026, 3, 28), date(2026, 6, 27)]}
REVENUE = {2024: [90_000, 95_000, 100_000, 115_000], 2025: [100_000, 110_000, 120_000, 130_000],
           2026: [115_000, 125_000, 135_000, 145_000]}
RESTATED_FY2025_REVENUE = 450_000          # 10-K/A restates FY2025 from 460,000 (FIXTURE)
RESTATED_FY2025_NET_INCOME = 85_500        # from 87,400 (FIXTURE)


def ts(y, m, d, hh=20, mm=0) -> str:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ns(text: str) -> int:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(dt.timestamp()) * NS_PER_S


LATE_FILER = 9000006   # its FY2025 10-K is accepted after its FY2026 Q1 10-Q (FIXTURE)


# (accession, form, acceptance, filing date, fiscal year, quarter or None, items)
def filings_for(cik: int) -> list[tuple]:
    p = f"{cik:010d}"
    rows = [
        (f"{p}-24-000010", "10-K", ts(2024, 11, 1, 20, 30), "2024-11-01", 2024, None, ""),
        (f"{p}-25-000001", "10-Q", ts(2025, 1, 31, 21), "2025-01-31", 2025, 1, ""),
        (f"{p}-25-000002", "10-Q", ts(2025, 5, 2), "2025-05-02", 2025, 2, ""),
        (f"{p}-25-000003", "10-Q", ts(2025, 8, 1), "2025-08-01", 2025, 3, ""),
        (f"{p}-25-000010", "10-K", ts(2025, 10, 31, 20, 30), "2025-10-31", 2025, None, ""),
        (f"{p}-26-000001", "10-Q", ts(2026, 1, 30, 21), "2026-01-30", 2026, 1, ""),
        (f"{p}-26-000004", "8-K", ts(2026, 2, 20, 13), "2026-02-20", None, None, "4.02,9.01"),
        (f"{p}-26-000005", "10-K/A", ts(2026, 3, 2, 21), "2026-03-02", 2025, None, ""),
        (f"{p}-26-000002", "10-Q", ts(2026, 5, 1), "2026-05-01", 2026, 2, ""),
        (f"{p}-26-000003", "10-Q", ts(2026, 7, 31), "2026-07-31", 2026, 3, ""),
        (f"{p}-26-000010", "10-K", ts(2026, 11, 20, 21), "2026-11-20", 2026, None, ""),
    ]
    if cik == LATE_FILER:
        rows = [(a, f, ts(2026, 2, 15, 21), "2026-02-15", y, q, i) if (f, y) == ("10-K", 2025) else (a, f, acc, d, y, q, i)
                for a, f, acc, d, y, q, i in rows]
    return rows


def accepted(cik: int, form: str, year: int | None, quarter: int | None) -> int:
    for accn, f, acc, _, y, q, _ in filings_for(cik):
        if f == form and y == year and q == quarter:
            return ns(acc)
    raise KeyError((form, year, quarter))


class Book:
    """Builds discrete-quarter values for one fixture issuer and emits companyfacts rows."""

    def __init__(self, scale: int = 1, ni_pct: int = 19):
        self.scale, self.ni_pct = scale, ni_pct

    def rev(self, fy: int, q: int) -> int:
        return REVENUE[fy][q - 1] * self.scale

    def flows(self, fy: int, q: int) -> dict[str, int]:
        r = self.rev(fy, q)
        return {"RevenueFromContractWithCustomerExcludingAssessedTax": r, "CostOfGoodsAndServicesSold": r * 60 // 100,
                "OperatingIncomeLoss": r * 25 // 100, "InterestExpense": r // 100,
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": r * 24 // 100,
                "IncomeTaxExpenseBenefit": r * 5 // 100, "NetIncomeLoss": r * self.ni_pct // 100,
                "NetCashProvidedByUsedInOperatingActivities": r * 30 // 100,
                "PaymentsToAcquirePropertyPlantAndEquipment": r * 8 // 100,
                "DepreciationDepletionAndAmortization": r * 4 // 100, "PaymentsOfDividends": r * 5 // 100}

    @staticmethod
    def quarter_bounds(fy: int, q: int) -> tuple[date, date]:
        start, end = FY[fy]
        ends = Q_ENDS[fy] + [end]
        s = start if q == 1 else date.fromordinal(ends[q - 2].toordinal() + 1)
        return s, ends[q - 1]

    def ytd(self, fy: int, q: int, concept: str, restated: bool = False) -> int:
        if q == 4 and fy == 2025 and restated:
            if concept == "RevenueFromContractWithCustomerExcludingAssessedTax":
                return RESTATED_FY2025_REVENUE * self.scale
            if concept == "NetIncomeLoss":
                return RESTATED_FY2025_NET_INCOME * self.scale if self.ni_pct == 19 else sum(self.flows(fy, i)[concept] for i in range(1, 5))
        return sum(self.flows(fy, i)[concept] for i in range(1, q + 1))

    def balance(self, end: date) -> dict[str, int]:
        k = end.toordinal() // 91 % 50
        cash = (50_000 + 1_000 * k) * self.scale
        sti, rec, other = 20_000 * self.scale, 15_000 * self.scale, 10_000 * self.scale
        ca = cash + sti + rec + other
        assets = ca + 300_000 * self.scale
        cl, ltd_c, ltd_n, other_l = 60_000 * self.scale, 5_000 * self.scale, 95_000 * self.scale, 20_000 * self.scale
        equity = assets - cl - ltd_n - other_l
        return {"CashAndCashEquivalentsAtCarryingValue": cash, "MarketableSecuritiesCurrent": sti, "AccountsReceivableNetCurrent": rec,
                "AssetsCurrent": ca, "Assets": assets, "LiabilitiesCurrent": cl, "LongTermDebtCurrent": ltd_c,
                "LongTermDebtNoncurrent": ltd_n, "Liabilities": assets - equity, "StockholdersEquity": equity,
                "LiabilitiesAndStockholdersEquity": assets}


CASH_FLOW_ONLY_YTD = {"NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment",
                      "DepreciationDepletionAndAmortization", "PaymentsOfDividends"}


def companyfacts(cik: int, book: Book, *, revenue_concept_fy2024: str = "SalesRevenueNet") -> str:
    """companyfacts JSON text with the facts each fixture filing would carry (current + comparatives)."""
    facts: dict[str, dict[str, list]] = {}

    def add(concept, unit, start, end, val, accn, fy, fp, form, filed, taxonomy="us-gaap"):
        facts.setdefault(f"{taxonomy}|{concept}", {}).setdefault(unit, []).append(
            {**({"start": start.isoformat()} if start else {}), "end": end.isoformat(), "val": val, "accn": accn, "fy": fy,
             "fp": fp, "form": form, "filed": filed})

    def flow_concepts(fy):
        names = list(book.flows(fy, 1))
        return names

    for accn, form, _acc, filed, fy, q, _items in filings_for(cik):
        if form.startswith("8-K"):
            continue
        if form.startswith("10-K"):
            restated = form == "10-K/A" or fy >= 2026
            years = [fy] if form == "10-K/A" else [fy, fy - 1, fy - 2]
            concepts = ["RevenueFromContractWithCustomerExcludingAssessedTax", "NetIncomeLoss"] if form == "10-K/A" else flow_concepts(fy)
            for y in years:
                if y not in FY:
                    continue
                s, e = FY[y]
                for c in concepts:
                    concept = c
                    if c == "RevenueFromContractWithCustomerExcludingAssessedTax" and y == 2024 and fy == 2024:
                        concept = revenue_concept_fy2024
                    add(concept, "USD", s, e, book.ytd(y, 4, c, restated=restated), accn, fy, "FY", form, filed)
                if form == "10-K":
                    ni = book.ytd(y, 4, "NetIncomeLoss", restated=restated)
                    add("EarningsPerShareDiluted", "USD/shares", s, e, f"__DEC__{Decimal(ni) / Decimal(10_000 * book.scale)}",
                        accn, fy, "FY", form, filed)
                    add("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", s, e, 10_000 * book.scale, accn, fy, "FY", form, filed)
            if form == "10-K":
                for e in (FY[fy][1], FY[fy - 1][1] if fy - 1 in FY else None):
                    if e is None:
                        continue
                    for c, v in book.balance(e).items():
                        add(c, "USD", None, e, v, accn, fy, "FY", form, filed)
                add("EntityCommonStockSharesOutstanding", "shares", None, date.fromisoformat(filed), 10_000 * book.scale,
                    accn, fy, "FY", form, filed, taxonomy="dei")
            continue
        # 10-Q for quarter q of fiscal year fy: current and prior-year comparatives
        for y in (fy, fy - 1):
            if y not in FY:
                continue
            qs, qe = book.quarter_bounds(y, q)
            for c, v in book.flows(y, q).items():
                if c not in CASH_FLOW_ONLY_YTD:
                    add(c, "USD", qs, qe, v, accn, fy, f"Q{q}", form, filed)
                if q > 1 or c in CASH_FLOW_ONLY_YTD:
                    add(c, "USD", FY[y][0], qe, book.ytd(y, q, c), accn, fy, f"Q{q}", form, filed)
            ni = book.flows(y, q)["NetIncomeLoss"]
            add("EarningsPerShareDiluted", "USD/shares", qs, qe, f"__DEC__{Decimal(ni) / Decimal(10_000 * book.scale)}", accn, fy,
                f"Q{q}", form, filed)
            add("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", qs, qe, 10_000 * book.scale, accn, fy, f"Q{q}", form, filed)
        qe = book.quarter_bounds(fy, q)[1]
        for e in (qe, FY[fy - 1][1]):
            for c, v in book.balance(e).items():
                add(c, "USD", None, e, v, accn, fy, f"Q{q}", form, filed)
        add("EntityCommonStockSharesOutstanding", "shares", None, date.fromisoformat(filed), 10_000 * book.scale,
            accn, fy, f"Q{q}", form, filed, taxonomy="dei")
    out: dict = {"cik": cik, "entityName": f"Fixture Issuer {cik} (synthetic)", "facts": {}}
    for key, units in facts.items():
        taxonomy, concept = key.split("|")
        out["facts"].setdefault(taxonomy, {})[concept] = {"label": concept, "units": units}
    text = json.dumps(out)
    import re
    return re.sub(r'"__DEC__([0-9.\-]+)"', r"\1", text)


def submissions(cik: int, name: str, sic: str, *, ticker: str = "FXW") -> dict:
    rows = sorted(filings_for(cik), key=lambda r: r[2], reverse=True)
    return {"cik": str(cik), "name": name, "sic": sic, "sicDescription": "FIXTURE industry", "fiscalYearEnd": "0927",
            "stateOfIncorporation": "DE", "entityType": "operating", "category": "FIXTURE", "tickers": [ticker], "exchanges": ["Nasdaq"],
            "formerNames": [{"name": f"{name} OLD NAME (synthetic)", "from": "2010-01-01T00:00:00.000Z", "to": "2024-12-31T00:00:00.000Z"}],
            "filings": {"recent": {
                "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows], "acceptanceDateTime": [r[2] for r in rows],
                "filingDate": [r[3] for r in rows], "reportDate": ["" for _ in rows], "items": [r[6] for r in rows],
                "primaryDocument": [f"fixture-{r[0]}.htm" for r in rows], "primaryDocDescription": [r[1] for r in rows],
                "isXBRL": [1 for _ in rows], "fileNumber": ["000-00000" for _ in rows]}}}


class FakeResp:
    def __init__(self, body: bytes):
        self.status, self._body, self.headers = 200, body, {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RouteOpener:
    def __init__(self, routes: dict[str, bytes]):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout):
        self.calls.append(req.full_url)
        if req.full_url not in self.routes:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b""))
        return FakeResp(self.routes[req.full_url])


INGESTED_AT = ns("2026-12-01T00:00:00Z")
ISSUERS = [  # cik, name, sic, scale, net-income percent, ticker
    (CIK, "Fixture Widgets Inc (synthetic)", SIC, 1, 19, "FXW"),
    (9000002, "Fixture Peer Two (synthetic)", SIC, 2, 15, "FXP2"),
    (9000003, "Fixture Peer Three (synthetic)", SIC, 3, 10, "FXP3"),
    (9000004, "Fixture Loss Maker (synthetic)", SIC, 1, -5, "FXLM"),
    (9000005, "Fixture Software Co (synthetic)", "7372", 2, 30, "FXSW"),
    (LATE_FILER, "Fixture Late Filer (synthetic)", "3999", 1, 19, "FXLF"),
]


def ingest_issuer(store: ResearchStore, cik: int, name: str, sic: str, scale: int = 1, ni_pct: int = 19, ticker: str = "FXW") -> dict:
    routes = {f"https://data.sec.gov/submissions/CIK{cik:010d}.json": json.dumps(submissions(cik, name, sic, ticker=ticker)).encode(),
              f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json": companyfacts(cik, Book(scale, ni_pct)).encode()}
    fetcher = Fetcher(UA, RateLimit(10, 1, "fixture"), opener=RouteOpener(routes), clock_ns=lambda: INGESTED_AT, sleep=lambda s: None)
    return sec_ingest(cik, store, fetcher, lambda: INGESTED_AT)


def fixture_home() -> tuple[tempfile.TemporaryDirectory, Path, ResearchStore]:
    """A temporary UNKNOWN_HOME with research/research.sqlite and the raw layer populated."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "research").mkdir()
    store = ResearchStore(root / "research" / "research.sqlite")
    store.conn.execute("PRAGMA synchronous=OFF")   # throwaway test database: skip fsync per record
    for cik, name, sic, scale, ni, ticker in ISSUERS:
        ingest_issuer(store, cik, name, sic, scale, ni, ticker)
    return tmp, root, store


class FixtureAndMappingTests(unittest.TestCase):
    def test_mapping_table_is_consistent(self):
        """CAP-CO-05: unique keys, known statements, no concept mapped twice (no double counting)."""
        self.assertEqual(validate_mapping(), [])
        self.assertEqual(MAPPING_REF, "std-map@1")
        self.assertTrue(all(i.rule().startswith("std-map@1:") for i in LINE_ITEMS))
        self.assertFalse(ITEMS["eps_diluted"].summable)
        self.assertFalse(ITEMS["weighted_shares_diluted"].summable)
        self.assertTrue(ITEMS["revenue"].summable)
        self.assertFalse(ITEMS["total_assets"].summable)

    def test_mapping_documented(self):
        """Every line item and formula appears in docs/research/company.md."""
        from unknown.data.fundamentals.metrics import FORMULAS
        doc = (Path(__file__).resolve().parents[2] / "docs" / "research" / "company.md").read_text()
        missing = [i.key for i in LINE_ITEMS if f"`{i.key}`" not in doc] + [f for f in FORMULAS if f"`{f}@1`" not in doc]
        self.assertEqual(missing, [])

    def test_fixture_ingests_through_real_sec_path(self):
        tmp, root, store = fixture_home()
        try:
            n = store.conn.execute("SELECT COUNT(*) AS n FROM observation WHERE entity_id=?", (f"SEC-CIK-{CIK:010d}",)).fetchone()["n"]
            self.assertGreater(n, 300)
            row = store.conn.execute("SELECT attributes FROM entity WHERE entity_id=?", (f"SEC-CIK-{CIK:010d}",)).fetchone()
            attrs = json.loads(row["attributes"])
            self.assertEqual(attrs["sic"], SIC)
            self.assertTrue((root / "research" / "raw" / attrs["submissions_ref"]).exists())
            self.assertEqual(store.conflicts(f"SEC-CIK-{CIK:010d}", "us-gaap:NetIncomeLoss"), [])
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
