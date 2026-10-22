"""Issuer identity and filings list from stored SEC submissions payloads (CAP-CO-01, CAP-CO-02, CAP-CO-12).

The SEC submissions JSON fetched by ``data.sec`` is kept in the raw layer (content-addressed, the
entity's ``submissions_ref``). This module reads it back, verifies its SHA-256, and presents:

* identity: name (and the name in force at the as-of time from ``formerNames``), CIK, SIC, fiscal
  year end, state of incorporation, current tickers/exchanges (no validity dates in the feed, so
  they are labelled current-as-of-retrieval), former names with dates;
* filings with acceptance at or before the as-of time, each with SEC deep links (filing index,
  primary document) and 8-K item flags (2.02 results; 4.02 non-reliance).

Acceptance times are the submissions ``acceptanceDateTime`` values. CAP-CO-06 records that these
'Z' values were later than true UTC by the Eastern offset on three samples, so filtering on them is
conservative (a filing is never shown before its true acceptance). The list covers the main
submissions file ("recent": at least one year or 1,000 filings); older filings live in additional
files that are not listed here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ...domain.clock import NS_PER_S, ns_to_iso

ITEM_FLAGS = {"2.02": "8-K Item 2.02: results of operations", "4.02": "8-K Item 4.02: NON_RELIANCE on previously issued financials"}


def _acceptance_ns(text: str | None) -> int | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * NS_PER_S


def load_submissions(store, entity_id: str) -> tuple[dict | None, dict]:
    """(payload or None, meta with entity row, raw hash, retrieval time or a reason)."""
    row = store.conn.execute("SELECT * FROM entity WHERE entity_id=?", (entity_id,)).fetchone()
    if row is None:
        return None, {"status": "NOT_INGESTED", "reason": f"{entity_id} is not in the research store"}
    attrs = json.loads(row["attributes"] or "{}")
    meta = {"entity_id": entity_id, "name": row["name"], "kind": row["kind"], "attributes": attrs}
    sha = attrs.get("submissions_ref")
    if not sha:
        return None, {**meta, "status": "NOT_AVAILABLE", "reason": "no stored submissions payload for this entity"}
    raw = store.conn.execute("SELECT receipt_time_ns, request FROM raw_object WHERE sha256=?", (sha,)).fetchone()
    if raw is not None:
        meta["retrieved_at"] = ns_to_iso(raw["receipt_time_ns"])
        meta["retrieved_at_ns"] = raw["receipt_time_ns"]
        meta["source_url"] = json.loads(raw["request"]).get("url")
    path = store.raw_dir / sha if store.raw_dir is not None else None
    if path is None or not path.exists():
        return None, {**meta, "status": "NOT_AVAILABLE", "reason": f"raw payload {sha} not present in the raw layer"}
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != sha:
        return None, {**meta, "status": "ERROR", "reason": f"raw payload {sha} failed its SHA-256 check"}
    meta.update({"status": "OK", "raw_sha256": sha})
    return json.loads(body), meta


def identity(submissions: dict, meta: dict, as_of_ns: int) -> dict:
    cik = int(submissions.get("cik") or meta["attributes"].get("cik"))
    as_of_day = ns_to_iso(as_of_ns)[:10]
    former = [{"name": f.get("name"), "from": (f.get("from") or "")[:10] or None, "to": (f.get("to") or "")[:10] or None}
              for f in submissions.get("formerNames", [])]
    name_then = submissions.get("name")
    for f in former:
        if f["from"] and f["to"] and f["from"] <= as_of_day <= f["to"]:
            name_then = f["name"]
    retrieved_ns = meta.get("retrieved_at_ns")
    caveats = ["tickers and exchanges carry no validity dates in the SEC feed: shown as current at retrieval"]
    if retrieved_ns is not None and retrieved_ns > as_of_ns:
        caveats.append("identity snapshot retrieved after the as-of time; SIC and tickers are current values applied retroactively")
    return {"cik": cik, "entity_id": meta["entity_id"], "name": submissions.get("name"), "name_as_of": name_then,
            "former_names": former, "sic": submissions.get("sic"), "sic_description": submissions.get("sicDescription"),
            "fiscal_year_end": submissions.get("fiscalYearEnd"), "state_of_incorporation": submissions.get("stateOfIncorporation"),
            "entity_type": submissions.get("entityType"), "filer_category": submissions.get("category"),
            "lei": submissions.get("lei"),
            "listings": [{"ticker": t, "exchange": e, "validity": "current at retrieval (no dates in source)"}
                         for t, e in zip(submissions.get("tickers", []), submissions.get("exchanges", []))],
            "source": {"dataset": "sec.submissions", "raw_sha256": meta.get("raw_sha256"), "retrieved_at": meta.get("retrieved_at"),
                       "url": meta.get("source_url"), "attribution": "Source: U.S. SEC EDGAR submissions"},
            "links": {"edgar_company": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=&dateb=&owner=include&count=40",
                      "full_text_search": f"https://www.sec.gov/edgar/search/#/ciks={cik:010d}"},
            "caveats": caveats}


def filings(submissions: dict, as_of_ns: int, *, forms: set[str] | None = None, limit: int = 40) -> dict:
    cik = int(submissions.get("cik"))
    recent = submissions.get("filings", {}).get("recent", {})
    n = len(recent.get("accessionNumber", []))

    def col(key: str, i: int):
        values = recent.get(key) or []
        return values[i] if i < len(values) else None

    out, hidden_future = [], 0
    for i in range(n):
        accn, form = col("accessionNumber", i), col("form", i)
        acc_ns = _acceptance_ns(col("acceptanceDateTime", i))
        if acc_ns is None or acc_ns > as_of_ns:
            hidden_future += 1
            continue
        if forms and form not in forms:
            continue
        items = [x.strip() for x in (col("items", i) or "").split(",") if x.strip()]
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}"
        primary = col("primaryDocument", i)
        out.append({"accession": accn, "form": form, "filing_date": col("filingDate", i), "report_date": col("reportDate", i) or None,
                    "acceptance_time_raw": col("acceptanceDateTime", i), "items": items,
                    "flags": [ITEM_FLAGS[x] for x in items if x in ITEM_FLAGS and (form or "").startswith("8-K")],
                    "primary_document_description": col("primaryDocDescription", i) or None,
                    "is_xbrl": bool(col("isXBRL", i)), "file_number": col("fileNumber", i) or None,
                    "links": {"filing_index": f"{folder}/{accn}-index.htm", "primary_document": f"{folder}/{primary}" if primary else None}})
    out.sort(key=lambda r: (r["acceptance_time_raw"] or "", r["accession"]), reverse=True)
    return {"cik": cik, "as_of": ns_to_iso(as_of_ns), "count": len(out), "filings": out[:limit],
            "not_yet_accepted_at_as_of": hidden_future,
            "coverage_note": "Main submissions file only (at least one year or 1,000 most recent filings); older filings are in "
                             "additional SEC submissions files.",
            "acceptance_note": "acceptanceDateTime as published in the submissions JSON; on fetched samples it is later than true "
                               "UTC (CAP-CO-06), so the as-of filter is conservative."}
