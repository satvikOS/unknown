"""SEC Form 13F data sets -> institutional holders per issuer in the point-in-time store (CAP-CO-09;
dataset ``sec.form13f`` in config/providers/sec-edgar.json).

Source. The SEC publishes the information tables of all 13F-HR filings disseminated in a three-month
window as one ZIP (SUBMISSION, COVERPAGE, INFOTABLE, ... tab-separated; field definitions in the
ZIP's FORM13F_readme.htm). One request per window, inside the EDGAR fair-access rate.

Issuer match. A holding row names a CUSIP, not a CIK. The issuer's CUSIPs come from official
filings: the structured Schedule 13D/13G cover pages on the issuer (``edgar_ownership``), or are
passed explicitly by the owner. No name matching is done.

Aggregation (``form13f_holder@1``). Per accession and CUSIP: shares = sum of SSHPRNAMT over rows
with SSHPRNAMTTYPE 'SH' and no PUT/CALL; market value = sum of VALUE (US dollars for filings from
2023-01-03, thousands of dollars before; the unit is stored with the value). Option rows (PUT/CALL)
and principal-amount rows are counted, never added to shares.

Amendments (AMENDMENTTYPE). A RESTATEMENT replaces the manager's report for the period; a manager
it no longer lists for the issuer is recorded as NOT_REPORTED for that accession. NEW HOLDINGS adds
its rows to the report it amends (the value stored is the cumulative position). Each version is its
own publication (accession), so ``as_of`` picks what was filed by then.

Exits. A manager that held the issuer in the prior stored report period and filed an original
13F-HR holdings report for the new period without it is recorded NOT_REPORTED (quality
``filed_report_without_position``); 13F reporting has thresholds, so this means "not reported",
not "zero". A manager that has not filed for the new period is not an exit: it is unknown.

Point in time: available at 23:59:59 America/New_York on FILING_DATE (date only in the data set).

  python -m unknown.data.sources.edgar_13f ingest --cik 320193,789019 [--window 01jun2026-31aug2026,...] [--home ~/.unknown]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed, Rounding
from ..pit import OPEN_END, Observation
from ..registry import dataset, load_registry
from ..sec import end_of_filed_day_ns
from .edgar_ownership import canonical, entity_id, issuer_cusips

DATASET = "sec.form13f"
FORMULA = "form13f_holder@1"
PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
ZIP_LINK = re.compile(r'href="(/files/[^"]*/form-13f-data-sets/(\d{2}[a-z]{3}\d{4}-\d{2}[a-z]{3}\d{4})_form13f\.zip)"')
DOLLARS_FROM = date(2023, 1, 3)          # readme: VALUE in dollars from this date, thousands before
HOLDINGS_TYPES = ("13F-HR", "13F-HR/A")
ATTRIBUTION = "Source: U.S. SEC Form 13F data sets"


def parse_sec_date(text: str) -> date:
    """'31-JUL-2026' -> date(2026, 7, 31)."""
    return datetime.strptime(text.strip().title(), "%d-%b-%Y").date()


def data_set_links(page_html: str) -> list[tuple[str, str]]:
    """(window, absolute URL) for each data set linked from the SEC page, newest first as listed."""
    seen, out = set(), []
    for path, window in ZIP_LINK.findall(page_html):
        if window not in seen:
            seen.add(window)
            out.append((window, "https://www.sec.gov" + path))
    return out


@dataclass
class Holding:
    accession: str
    cusip: str
    shares: Fixed = field(default_factory=lambda: Fixed(0))
    value: Fixed = field(default_factory=lambda: Fixed(0))
    rows: int = 0
    option_rows: int = 0
    principal_rows: int = 0
    names: set = field(default_factory=set)
    classes: set = field(default_factory=set)


def _tsv(z: zipfile.ZipFile, name: str):
    with z.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        for line in text:
            yield header, line.rstrip("\r\n").split("\t")


def read_data_set(data: bytes, cusips: set[str]) -> dict:
    """Parse one data set ZIP: filings (13F-HR, 13F-HR/A) and the holdings rows for the given CUSIPs."""
    z = zipfile.ZipFile(io.BytesIO(data))
    filings: dict[str, dict] = {}
    for header, f in _tsv(z, "SUBMISSION.tsv"):
        r = dict(zip(header, f))
        if r.get("SUBMISSIONTYPE") not in HOLDINGS_TYPES:
            continue
        try:
            filings[r["ACCESSION_NUMBER"]] = {"accession": r["ACCESSION_NUMBER"], "filing_date": parse_sec_date(r["FILING_DATE"]),
                                              "type": r["SUBMISSIONTYPE"], "cik": int(r["CIK"]), "period": parse_sec_date(r["PERIODOFREPORT"])}
        except (KeyError, ValueError):
            continue
    for header, f in _tsv(z, "COVERPAGE.tsv"):
        r = dict(zip(header, f))
        d = filings.get(r.get("ACCESSION_NUMBER", ""))
        if d is not None:
            d.update({"manager": r.get("FILINGMANAGER_NAME") or None, "is_amendment": (r.get("ISAMENDMENT") or "").upper() == "Y",
                      "amendment_type": (r.get("AMENDMENTTYPE") or "").upper() or None, "report_type": r.get("REPORTTYPE") or None})
    wanted = {c.upper() for c in cusips}
    holdings: dict[tuple[str, str], Holding] = {}
    needles = [c.encode() for c in wanted]
    with z.open("INFOTABLE.tsv") as fh:
        header = fh.readline().decode().rstrip("\r\n").split("\t")
        ix = {h: i for i, h in enumerate(header)}
        for raw in fh:
            if not any(n in raw for n in needles):
                continue
            f = raw.decode("utf-8", "replace").rstrip("\r\n").split("\t")
            cusip = f[ix["CUSIP"]].strip().upper()
            acc = f[ix["ACCESSION_NUMBER"]]
            if cusip not in wanted or acc not in filings:
                continue
            h = holdings.setdefault((acc, cusip), Holding(acc, cusip))
            h.rows += 1
            h.names.add(f[ix["NAMEOFISSUER"]].strip())
            h.classes.add(f[ix["TITLEOFCLASS"]].strip())
            if f[ix["PUTCALL"]].strip():
                h.option_rows += 1
                continue
            if f[ix["SSHPRNAMTTYPE"]].strip().upper() != "SH":
                h.principal_rows += 1
                continue
            h.shares = h.shares + Fixed.parse(f[ix["SSHPRNAMT"]].strip() or "0")
            h.value = h.value + Fixed.parse(f[ix["VALUE"]].strip() or "0")
    return {"filings": filings, "holdings": holdings}


def _value_unit(filing_date: date) -> str:
    return "USD" if filing_date >= DOLLARS_FROM else "USD_thousands"


def _stored(store, eid: str, manager_cik: int, period: str, at_ns: int):
    rows = store.conn.execute("SELECT * FROM observation WHERE dataset=? AND entity_id=? AND field=? AND period_end=? AND known_to_ns=? "
                              "AND available_time_ns<=? ORDER BY available_time_ns DESC, publication_id DESC LIMIT 1",
                              (DATASET, eid, f"13f:{manager_cik:010d}", period, OPEN_END, at_ns)).fetchone()
    return rows


def _periods(store, eid: str) -> list[str]:
    return [r[0] for r in store.conn.execute("SELECT DISTINCT period_end FROM observation WHERE dataset=? AND entity_id=? AND known_to_ns=? "
                                             "AND field LIKE '13f:%' AND field NOT LIKE '13f:coverage:%' ORDER BY period_end",
                                             (DATASET, eid, OPEN_END))]


def ingest_data_set(store, data: bytes, window: str, url: str, targets: dict[str, str], receipt_ns: int, now_ns) -> dict:
    """Record the holdings of ``targets`` ({CUSIP: entity_id}) found in one data set ZIP."""
    sha = store.put_raw(data, provider="sec-edgar", dataset=DATASET, request={"url": url}, receipt_time_ns=receipt_ns,
                        media_type="application/zip")
    parsed = read_data_set(data, set(targets))
    filings, holdings = parsed["filings"], parsed["holdings"]
    counts: dict[str, dict] = {}
    by_issuer: dict[str, list[Holding]] = {}
    for (acc, cusip), h in holdings.items():
        by_issuer.setdefault(targets[cusip], []).append(h)
    order = sorted(filings.values(), key=lambda f: (f["period"], f["filing_date"], f["accession"]))
    for eid in sorted(set(targets.values())):
        c = counts.setdefault(eid, {"holdings": 0, "restated_out": 0, "exits": 0, "outcomes": {}, "periods": {}})
        cus = sorted(k for k, v in targets.items() if v == eid)
        per_acc: dict[str, list[Holding]] = {}
        for h in by_issuer.get(eid, []):
            per_acc.setdefault(h.accession, []).append(h)
        prior_periods = _periods(store, eid)
        for f in order:
            hs = per_acc.get(f["accession"], [])
            avail = end_of_filed_day_ns(f["filing_date"].isoformat())
            period = f["period"].isoformat()
            amend = f.get("amendment_type") if f["type"] == "13F-HR/A" else None
            base = dict(dataset=DATASET, entity_id=eid, field=f"13f:{f['cik']:010d}", unit="json", period_end=period, period_kind="QUARTER",
                        receipt_time_ns=receipt_ns, available_time_ns=avail, publication_id=f["accession"])
            prev = _stored(store, eid, f["cik"], period, avail) if amend else None
            if not hs:
                if amend == "RESTATEMENT" and prev is not None and prev["value_text"] is not None:
                    _rec(store, c, Observation(missing_reason="NOT_REPORTED", source_ref=f"raw:{sha}#{f['accession']}",
                                               quality=("restatement_without_position",), **base), now_ns())
                    c["restated_out"] += 1
                continue
            shares = sum((h.shares for h in hs), Fixed(0))
            value = sum((h.value for h in hs), Fixed(0))
            flags = [f"form:{f['type']}", "availability_from_filed_date"]
            if amend == "NEW HOLDINGS" and prev is not None and prev["value_text"] is not None:
                before = json.loads(prev["value_text"])
                shares = shares + Fixed.parse(before["shares"])
                value = value + Fixed.parse(before["value"])
                flags.append(f"new_holdings_added_to:{prev['publication_id']}")
            doc = {"manager_cik": f["cik"], "manager": f.get("manager"), "accession": f["accession"], "type": f["type"],
                   "amendment_type": amend, "report_type": f.get("report_type"), "filing_date": f["filing_date"].isoformat(), "period": period,
                   "shares": shares.to_str(), "value": value.to_str(), "value_unit": _value_unit(f["filing_date"]),
                   "rows": sum(h.rows for h in hs), "option_rows": sum(h.option_rows for h in hs),
                   "principal_rows": sum(h.principal_rows for h in hs), "cusips": sorted({h.cusip for h in hs}),
                   "issuer_names": sorted(set().union(*(h.names for h in hs))), "classes": sorted(set().union(*(h.classes for h in hs))),
                   "window": window, "formula": FORMULA}
            _rec(store, c, Observation(value_text=canonical(doc), source_ref=f"raw:{sha}#INFOTABLE/{f['accession']}",
                                       quality=tuple(flags), **base), now_ns())
            c["holdings"] += 1
            c["periods"][period] = c["periods"].get(period, 0) + 1
        # Exits against the prior stored period: managers that filed an original holdings report for P without the issuer.
        for period in sorted({f["period"].isoformat() for f in order}):
            earlier = [p for p in prior_periods + list(c["periods"]) if p < period]
            if not earlier:
                continue
            prior = max(earlier)
            held = {r["field"]: r for r in store.conn.execute(
                "SELECT field, value_text, publication_id FROM observation WHERE dataset=? AND entity_id=? AND period_end=? AND known_to_ns=? "
                "AND field LIKE '13f:%' AND field NOT LIKE '13f:coverage:%' AND value_text IS NOT NULL", (DATASET, eid, prior, OPEN_END))}
            originals = [f for f in order if f["period"].isoformat() == period and f["type"] == "13F-HR"
                         and (f.get("report_type") or "").upper() in ("13F HOLDINGS REPORT", "13F COMBINATION REPORT")]
            for f in originals:
                fld = f"13f:{f['cik']:010d}"
                if fld not in held or per_acc.get(f["accession"]):
                    continue
                if _stored(store, eid, f["cik"], period, OPEN_END - 1) is not None:
                    continue
                _rec(store, c, Observation(dataset=DATASET, entity_id=eid, field=fld, unit="json", period_end=period, period_kind="QUARTER",
                                           missing_reason="NOT_REPORTED", receipt_time_ns=receipt_ns,
                                           available_time_ns=end_of_filed_day_ns(f["filing_date"].isoformat()), publication_id=f["accession"],
                                           source_ref=f"raw:{sha}#{f['accession']}", quality=("filed_report_without_position",
                                                                                              f"prior_period:{prior}")), now_ns())
                c["exits"] += 1
        for period in sorted({f["period"].isoformat() for f in order}):
            filed = [f for f in order if f["period"].isoformat() == period]
            if len(filed) < 100 and period not in c["periods"]:
                continue   # stray late filings for old periods: not a coverage statement worth recording
            cov = {"window": window, "period": period, "holdings_reports_in_window": sum(1 for f in filed if f["type"] == "13F-HR"),
                   "amendments_in_window": sum(1 for f in filed if f["type"] == "13F-HR/A"), "holders_found": c["periods"].get(period, 0),
                   "cusips": cus, "url": url, "raw_sha256": sha,
                   "filers": sorted({f["cik"] for f in filed if f["type"] == "13F-HR"})}
            last = max(end_of_filed_day_ns(f["filing_date"].isoformat()) for f in filed)
            _rec(store, c, Observation(dataset=DATASET, entity_id=eid, field=f"13f:coverage:{window}", unit="json", period_end=period,
                                       period_kind="QUARTER", value_text=canonical(cov), receipt_time_ns=receipt_ns, available_time_ns=last,
                                       publication_id=f"{window}", source_ref=f"raw:{sha}", quality=("coverage",)), now_ns())
    return {"window": window, "raw_sha256": sha, "bytes": len(data), "filings_in_window": len(filings),
            "holding_rows_matched": len(holdings), "issuers": counts}


def _rec(store, c: dict, obs: Observation, known_at: int) -> None:
    _, outcome = store.record(obs, known_at)
    c["outcomes"][outcome] = c["outcomes"].get(outcome, 0) + 1


# ------------------------------------------------------------------ read model
def _shares_outstanding(store, eid: str, period: str, as_of_ns: int) -> dict:
    """Shares outstanding nearest the report period end (cover page first), available at as_of, within 100 days."""
    target = date.fromisoformat(period)
    best = None
    for concept in ("dei:EntityCommonStockSharesOutstanding", "us-gaap:CommonStockSharesOutstanding"):
        for r in store.as_of(eid, concept, as_of_ns, dataset="sec.companyfacts"):
            if r["unit"] != "shares" or r["value"] is None or not r["period_end"]:
                continue
            gap = abs((date.fromisoformat(r["period_end"]) - target).days)
            if gap <= 100 and (best is None or gap < best[0]):
                best = (gap, r, concept)
        if best is not None:
            break
    if best is None:
        return {"value": None, "reason": "NOT_REPORTED", "detail": f"no shares-outstanding fact within 100 days of {period} available at the "
                "as-of time (SEC companyfacts)"}
    gap, r, concept = best
    return {"value": r["value"], "date": r["period_end"], "concept": concept, "obs_id": r["obs_id"], "gap_days": gap,
            "accession": r["publication_id"], "available_time": ns_to_iso(r["available_time_ns"])}


def _brief(cov: dict) -> dict:
    return {k: v for k, v in cov.items() if k != "filers"} | {"filers_count": len(cov.get("filers", []))}


def institutional(store, eid: str, as_of_ns: int, *, top: int = 50) -> dict:
    rows = store.conn.execute("SELECT * FROM observation WHERE dataset=? AND entity_id=? AND known_to_ns=? AND available_time_ns<=? "
                              "AND field LIKE '13f:%'", (DATASET, eid, OPEN_END, as_of_ns)).fetchall()
    best: dict[tuple[str, str], object] = {}
    coverage: dict[str, list] = {}
    for r in rows:
        if r["field"].startswith("13f:coverage:"):
            coverage.setdefault(r["period_end"], []).append(json.loads(r["value_text"]))
            continue
        if r["field"].count(":") != 1:
            continue
        k = (r["period_end"], r["field"])
        if k not in best or (r["available_time_ns"], r["publication_id"]) > (best[k]["available_time_ns"], best[k]["publication_id"]):
            best[k] = r
    periods = sorted({p for p, _ in best} | set(coverage))
    if not periods:
        return {"status": "NO_DATA", "reason": "NO_DATA", "detail": "no 13F holdings ingested for this issuer at the as-of time "
                "(python -m unknown.data.sources.edgar_13f ingest --cik <CIK>)", "holders": []}
    cur = max(periods)
    prev = max([p for p in periods if p < cur], default=None)

    def holding(period):
        out = {}
        for (p, fld), r in best.items():
            if p != period:
                continue
            out[fld] = (json.loads(r["value_text"]) if r["value_text"] else None, r)
        return out

    now, before = holding(cur), holding(prev) if prev else {}
    prior_filers = {c for cov in coverage.get(prev, []) for c in cov.get("filers", [])} if prev else set()
    so = _shares_outstanding(store, eid, cur, as_of_ns)
    holders, entries, exits, increases, decreases = [], 0, 0, 0, 0
    for fld, (doc, r) in now.items():
        prior_doc = before.get(fld, (None, None))[0]
        prior_seen = fld in before
        if doc is None:
            if prior_doc is not None:
                exits += 1
                holders.append({"manager_cik": int(fld[4:]), "manager": prior_doc.get("manager"), "shares": None, "shares_reason": "NOT_REPORTED",
                                "change": "EXIT", "prior_shares": prior_doc["shares"], "change_shares": None, "accession": r["publication_id"],
                                "filing_date": None, "available_time": ns_to_iso(r["available_time_ns"]), "pct_held": None,
                                "pct_reason": "NOT_APPLICABLE", "flags": json.loads(r["quality"])})
            continue
        shares = Fixed.parse(doc["shares"])
        if prior_doc is not None:
            ps = Fixed.parse(prior_doc["shares"])
            delta = shares - ps
            change = "INCREASE" if delta.is_positive() else "DECREASE" if delta.is_negative() else "UNCHANGED"
            increases += change == "INCREASE"
            decreases += change == "DECREASE"
        elif prior_seen or doc["manager_cik"] in prior_filers:
            delta, change = None, "ENTRY"
            entries += 1
        elif prev is None:
            delta, change = None, "NO_PRIOR_PERIOD"
        else:
            delta, change = None, "NO_PRIOR_REPORT"
        pct = shares.div(Fixed.parse(so["value"]), 10, Rounding.HALF_EVEN) \
            if so.get("value") and Fixed.parse(so["value"]).is_positive() else None
        holders.append({"manager_cik": doc["manager_cik"], "manager": doc.get("manager"), "shares": doc["shares"], "value": doc["value"],
                        "value_unit": doc["value_unit"], "change": change, "prior_shares": prior_doc["shares"] if prior_doc else None,
                        "change_shares": delta.to_str() if delta is not None else None, "accession": doc["accession"],
                        "type": doc["type"], "amendment_type": doc.get("amendment_type"), "filing_date": doc["filing_date"],
                        "available_time": ns_to_iso(r["available_time_ns"]), "pct_held": pct.to_str() if pct is not None else None,
                        "pct_reason": None if pct is not None else so.get("reason", "NOT_REPORTED"), "obs_id": r["obs_id"],
                        "option_rows": doc["option_rows"], "flags": json.loads(r["quality"])})
    held = [h for h in holders if h["shares"] is not None]
    held.sort(key=lambda h: Fixed.parse(h["shares"]), reverse=True)
    total = sum((Fixed.parse(h["shares"]) for h in held), Fixed(0))
    conc = {}
    for n in (5, 10, 20):
        part = [h for h in held[:n]]
        if so.get("value") and len(part) == min(n, len(held)) and all(h["pct_held"] is not None for h in part):
            conc[f"top{n}"] = sum((Fixed.parse(h["pct_held"]) for h in part), Fixed(0)).to_str()
        else:
            conc[f"top{n}"] = None
    return {"status": "OK", "period": cur, "prior_period": prev, "holders": held[:top] + [h for h in holders if h["shares"] is None],
            "holder_count": len(held), "total_shares_reported": total.to_str(), "shares_outstanding": so,
            "concentration": conc, "concentration_formula": "sum of pct_held of the N largest holders (fraction of shares outstanding)",
            "changes": {"entries": entries, "exits": exits, "increases": increases, "decreases": decreases},
            "coverage": [_brief(c) for c in coverage.get(cur, [])], "prior_coverage": [_brief(c) for c in coverage.get(prev, [])] if prev else [],
            "notes": ["Report period and filing date are shown per holder; % held uses shares outstanding nearest the report period.",
                      "Managers that have not filed for the period yet are absent (unknown), not exits.",
                      "EXIT = an original 13F-HR for the period that does not list the issuer (below 13F thresholds or sold)."],
            "source": {"dataset": DATASET, "attribution": ATTRIBUTION, "formula": FORMULA}}


def main(argv: list[str] | None = None) -> None:
    from .common import data_config, make_fetcher
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest SEC Form 13F data sets for issuers into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--cik", required=True, help="comma-separated CIKs (their CUSIPs come from ingested 13D/13G cover pages)")
    ing.add_argument("--cusip", default="", help="optional CIK=CUSIP pairs, comma-separated, to add to the official-filing CUSIPs")
    ing.add_argument("--window", default="", help="comma-separated windows (e.g. 01mar2026-31may2026); default the two latest")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    if not dataset(load_registry(), DATASET).permits("storage"):
        raise PermissionError(f"{DATASET} storage not permitted by the registry")
    store, home = open_store(a.home)
    fetcher = make_fetcher("sec-edgar", data_config(home))
    targets: dict[str, str] = {}
    unresolved = []
    for c in a.cik.split(","):
        eid = entity_id(int(c))
        found = issuer_cusips(store, eid, time.time_ns())
        if found:
            targets[found[0]["cusip"]] = eid
        else:
            unresolved.append(int(c))
    for pair in filter(None, a.cusip.split(",")):
        c, _, cus = pair.partition("=")
        targets[cus.strip().upper()] = entity_id(int(c))
    page = fetcher.get(PAGE).body.decode("utf-8", "replace")
    links = data_set_links(page)
    wanted = [w for w in a.window.split(",") if w] or [w for w, _ in links[:2]]
    chosen = [(w, u) for w, u in links if w in wanted]
    report = {"targets": targets, "unresolved_ciks": unresolved, "windows": []}
    for w, u in sorted(chosen, key=lambda x: datetime.strptime(x[0][:9], "%d%b%Y")):
        resp = fetcher.get(u)
        report["windows"].append(ingest_data_set(store, resp.body, w, u, targets, resp.receipt_time_ns, time.time_ns))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
