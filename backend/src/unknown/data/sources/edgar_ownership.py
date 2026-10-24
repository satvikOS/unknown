"""SEC EDGAR insider (Forms 3/4/5) and beneficial-ownership (Schedules 13D/13G) reports into the
point-in-time store (CAP-CO-09; ADR-0015; datasets ``sec.form345`` and ``sec.schedule13dg`` in
config/providers/sec-edgar.json).

Source. The issuer's submissions JSON lists every filing the issuer is a party to: its insiders'
Forms 3/4/5, the 13D/13G reports holders file on it, and also reports the issuer files *as a holder*
of other companies. Each filing's XML document is fetched (the submissions ``primaryDocument`` minus
its ``xsl*`` rendering folder) and kept only when its ``issuerCik`` is the issuer; a schedule whose
accession prefix is the issuer's own CIK is skipped without a fetch (an issuer does not report its
own stock on Schedule 13D/13G; it files those as a holder of other companies).

Point in time. Every row is available at the EDGAR acceptance time of its accession and is stored
as one observation (``value_text`` is canonical JSON with exact decimal strings; never a float):

* ``sec.form345`` field ``form345:<accession>:filing`` (form, owners, relationship, footnotes) and
  ``form345:<accession>:<nnn>`` (one transaction or holding row, period_end = transaction date);
* ``sec.schedule13dg`` field ``13dg:<accession>`` (cover page: reporting persons, aggregate amount,
  percent of class, CUSIP, event date). Pre-December-2024 free-text schedules are stored with their
  form and link only (``parsed: false``), never with guessed numbers.

The ``aff10b5One`` element is stored as published; it is not presented as a Rule 10b5-1 flag until
its definition is confirmed from SEC documentation (CAP-CO-09 acceptance criterion).

  python -m unknown.data.sources.edgar_ownership ingest --cik 320193,789019 [--lookback-days 365] [--home ~/.unknown]
"""
from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from ...domain.clock import NS_PER_S, ns_to_iso
from ...domain.fixed import Fixed
from ..http import FetchError, Fetcher
from ..pit import OPEN_END, Observation
from ..registry import dataset, load_registry
from .common import guard_xml

DATASET_345 = "sec.form345"
DATASET_13DG = "sec.schedule13dg"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
INSIDER_FORMS = frozenset({"3", "4", "5", "3/A", "4/A", "5/A"})
STRUCTURED_SCHEDULES = frozenset({"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"})
LEGACY_SCHEDULES = frozenset({"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"})
ATTRIBUTION = "Source: U.S. SEC EDGAR (Forms 3/4/5, Schedules 13D/13G)"

# Form 4 General Instructions, item 8 (transaction codes). Only P and S are open-market trades.
TRANSACTION_CODES = {
    "P": "open market or private purchase", "S": "open market or private sale", "V": "voluntarily reported earlier than required",
    "A": "grant or award (Rule 16b-3(d))", "D": "disposition to the issuer (Rule 16b-3(e))",
    "F": "payment of exercise price or tax by delivering or withholding securities", "I": "discretionary transaction (Rule 16b-3(f))",
    "M": "exercise or conversion of an exempt derivative", "C": "conversion of a derivative", "E": "expiration of a short derivative position",
    "H": "expiration or cancellation of a long derivative position with value received", "O": "exercise of an out-of-the-money derivative",
    "X": "exercise of an in-the-money or at-the-money derivative", "G": "bona fide gift", "L": "small acquisition (Rule 16a-6)",
    "W": "acquisition or disposition by will or the laws of descent", "Z": "deposit into or withdrawal from a voting trust",
    "J": "other acquisition or disposition", "K": "equity swap or similar instrument", "U": "disposition in a change-of-control tender",
}
OPEN_MARKET = {"P": "open_market_purchase", "S": "open_market_sale"}


def entity_id(cik: int) -> str:
    return f"SEC-CIK-{cik:010d}"


def acceptance_ns(text: str | None) -> int | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * NS_PER_S


def submission_rows(submissions: dict) -> list[dict]:
    """The submissions 'recent' columns as one dict per filing."""
    recent = submissions.get("filings", {}).get("recent", {})
    cols = ("accessionNumber", "form", "filingDate", "reportDate", "acceptanceDateTime", "primaryDocument", "items")
    n = len(recent.get("accessionNumber", []))
    out = []
    for i in range(n):
        v = {c: ((recent.get(c) or [])[i] if i < len(recent.get(c) or []) else None) for c in cols}
        out.append({"accession": v["accessionNumber"], "form": v["form"], "filing_date": v["filingDate"],
                    "report_date": v["reportDate"] or None, "acceptance_raw": v["acceptanceDateTime"],
                    "acceptance_ns": acceptance_ns(v["acceptanceDateTime"]), "primary_document": v["primaryDocument"] or "",
                    "items": [x.strip() for x in (v["items"] or "").split(",") if x.strip()]})
    return out


def folder_url(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"


def document_url(cik: int, accession: str, primary: str) -> str:
    """The raw XML behind an EDGAR rendering: 'xslF345X06/form4.xml' -> '<folder>/form4.xml'."""
    head, _, tail = primary.partition("/")
    name = tail if tail and head.lower().startswith("xsl") else primary
    return f"{folder_url(cik, accession)}/{name}"


def index_url(cik: int, accession: str) -> str:
    return f"{folder_url(cik, accession)}/{accession}-index.htm"


# ------------------------------------------------------------------ parsing (pure)
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _path(el, *names: str):
    for n in names:
        el = _child(el, n)
        if el is None:
            return None
    return el


def _text(el, *names: str) -> str | None:
    """Text at a path; ownership documents wrap most values in <value>."""
    node = _path(el, *names)
    if node is None:
        return None
    inner = _child(node, "value")
    t = (inner.text if inner is not None else node.text) or ""
    t = t.strip()
    return t or None


def _footnotes(el, *names: str) -> list[str]:
    node = _path(el, *names)
    return [] if node is None else [c.get("id") for c in node.iter() if _local(c.tag) == "footnoteId" and c.get("id")]


def decimal_text(text: str | None) -> str | None:
    """An exact decimal string, or None when absent or not a finite number (never a float)."""
    if text is None:
        return None
    try:
        d = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return Fixed.from_decimal(d).to_str()


def _flag(text: str | None) -> bool:
    return (text or "").strip().lower() in ("1", "true", "y", "yes")


def parse_ownership_xml(body: bytes) -> dict:
    """A Form 3/4/5 ownership document as plain data (strings for every number)."""
    guard_xml(body)
    root = ET.fromstring(body)
    if _local(root.tag) != "ownershipDocument":
        raise ValueError(f"not an ownership document: <{_local(root.tag)}>")
    owners = []
    for ro in root:
        if _local(ro.tag) != "reportingOwner":
            continue
        rel = _child(ro, "reportingOwnerRelationship")
        owners.append({"cik": _text(ro, "reportingOwnerId", "rptOwnerCik"), "name": _text(ro, "reportingOwnerId", "rptOwnerName"),
                       "is_director": _flag(_text(rel, "isDirector")), "is_officer": _flag(_text(rel, "isOfficer")),
                       "is_ten_percent_owner": _flag(_text(rel, "isTenPercentOwner")), "is_other": _flag(_text(rel, "isOther")),
                       "officer_title": _text(rel, "officerTitle"), "other_text": _text(rel, "otherText")})
    rows = []
    for table_name, table in (("non_derivative", "nonDerivativeTable"), ("derivative", "derivativeTable")):
        t = _child(root, table)
        if t is None:
            continue
        for el in t:
            name = _local(el.tag)
            kind = "transaction" if name.endswith("Transaction") else "holding" if name.endswith("Holding") else None
            if kind is None:
                continue
            raw_shares = _text(el, "transactionAmounts", "transactionShares")
            raw_price = _text(el, "transactionAmounts", "transactionPricePerShare")
            raw_after = _text(el, "postTransactionAmounts", "sharesOwnedFollowingTransaction")
            row = {"table": table_name, "kind": kind, "security": _text(el, "securityTitle"),
                   "date": _text(el, "transactionDate"), "code": _text(el, "transactionCoding", "transactionCode"),
                   "form_type": _text(el, "transactionCoding", "transactionFormType"),
                   "equity_swap": _flag(_text(el, "transactionCoding", "equitySwapInvolved")),
                   "shares": decimal_text(raw_shares), "price": decimal_text(raw_price),
                   "price_footnotes": _footnotes(el, "transactionAmounts", "transactionPricePerShare"),
                   "acquired_disposed": _text(el, "transactionAmounts", "transactionAcquiredDisposedCode"),
                   "shares_after": decimal_text(raw_after),
                   "value_after": decimal_text(_text(el, "postTransactionAmounts", "valueOwnedFollowingTransaction")),
                   "direct_indirect": _text(el, "ownershipNature", "directOrIndirectOwnership"),
                   "nature": _text(el, "ownershipNature", "natureOfOwnership"),
                   "footnotes": sorted({c.get("id") for c in el.iter() if _local(c.tag) == "footnoteId" and c.get("id")})}
            if table_name == "derivative":
                row.update({"conversion_price": decimal_text(_text(el, "conversionOrExercisePrice")),
                            "exercise_date": _text(el, "exerciseDate"), "expiration_date": _text(el, "expirationDate"),
                            "underlying_title": _text(el, "underlyingSecurity", "underlyingSecurityTitle"),
                            "underlying_shares": decimal_text(_text(el, "underlyingSecurity", "underlyingSecurityShares"))})
            unparsed = [k for k, raw in (("shares", raw_shares), ("price", raw_price), ("shares_after", raw_after))
                        if raw is not None and row[k] is None]
            if unparsed:
                row["unparsed"] = unparsed
            rows.append(row)
    notes = _child(root, "footnotes")
    footnotes = {c.get("id"): " ".join((c.text or "").split()) for c in (notes if notes is not None else []) if c.get("id")}
    return {"schema_version": _text(root, "schemaVersion"), "document_type": _text(root, "documentType"),
            "period_of_report": _text(root, "periodOfReport"), "date_of_original_submission": _text(root, "dateOfOriginalSubmission"),
            "issuer_cik": _text(root, "issuer", "issuerCik"), "issuer_name": _text(root, "issuer", "issuerName"),
            "issuer_symbol": _text(root, "issuer", "issuerTradingSymbol"), "aff10b5one_as_published": _text(root, "aff10b5One"),
            "owners": owners, "rows": rows, "footnotes": footnotes}


AGGREGATE_NAMES = ("reportingPersonBeneficiallyOwnedAggregateNumberOfShares", "aggregateAmountOwned")
PERCENT_NAMES = ("classPercent", "percentOfClass")


def _first_desc(el, names: tuple[str, ...]) -> str | None:
    for n in names:
        for c in el.iter():
            if _local(c.tag) == n and (c.text or "").strip():
                return c.text.strip()
    return None


def parse_schedule_xml(body: bytes) -> dict:
    """A structured Schedule 13D/13G cover page: issuer, CUSIPs, event date and reporting persons."""
    guard_xml(body)
    root = ET.fromstring(body)
    if _local(root.tag) != "edgarSubmission":
        raise ValueError(f"not an EDGAR schedule submission: <{_local(root.tag)}>")
    persons, seen = [], set()
    for el in root.iter():
        if id(el) in seen or _child(el, "reportingPersonName") is None:
            continue
        seen.add(id(el))
        name = (_child(el, "reportingPersonName").text or "").strip()
        if _first_desc(el, AGGREGATE_NAMES) is None and _first_desc(el, PERCENT_NAMES) is None:
            continue    # e.g. the signature block also names the reporting person
        persons.append({"name": name or None, "citizenship": _first_desc(el, ("citizenshipOrOrganization", "citizenship")),
                        "aggregate_shares": decimal_text(_first_desc(el, AGGREGATE_NAMES)),
                        "percent_of_class": decimal_text(_first_desc(el, PERCENT_NAMES)),
                        "type": _first_desc(el, ("typeOfReportingPerson",)),
                        "sole_voting": decimal_text(_first_desc(el, ("soleVotingPower",))),
                        "shared_voting": decimal_text(_first_desc(el, ("sharedVotingPower",))),
                        "sole_dispositive": decimal_text(_first_desc(el, ("soleDispositivePower",))),
                        "shared_dispositive": decimal_text(_first_desc(el, ("sharedDispositivePower",)))})
    cusips = sorted({(c.text or "").strip().upper() for c in root.iter() if _local(c.tag) == "issuerCusipNumber" and (c.text or "").strip()})
    event = _first_desc(root, ("eventDateRequiresFilingThisStatement", "dateOfEvent"))
    return {"submission_type": _first_desc(root, ("submissionType",)), "issuer_cik": _first_desc(root, ("issuerCik",)),
            "issuer_name": _first_desc(root, ("issuerName",)), "cusips": cusips, "class_title": _first_desc(root, ("securitiesClassTitle",)),
            "event_date": _us_date(event), "event_date_raw": event, "rule": _first_desc(root, ("designateRulePursuantThisScheduleFiled",)),
            "filer_cik": _first_desc(root, ("cik",)), "persons": persons, "parsed": True}


def _us_date(text: str | None) -> str | None:
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _same_cik(a, b: int) -> bool:
    try:
        return int(str(a).strip()) == b
    except (TypeError, ValueError):
        return False


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ ingestion
def _fetch_raw(store, fetcher: Fetcher, url: str, ds_id: str) -> tuple[bytes, str, int]:
    resp = fetcher.get(url)
    sha = store.put_raw(resp.body, provider="sec-edgar", dataset=ds_id, request={"url": resp.url},
                        receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
    return resp.body, sha, resp.receipt_time_ns


def _record(store, counts: dict, obs: Observation, known_at_ns: int) -> None:
    _, outcome = store.record(obs, known_at_ns)
    counts[outcome] = counts.get(outcome, 0) + 1


def ingest_insiders(store, cik: int, submissions: dict, fetcher: Fetcher, now_ns, *, lookback_days: int = 365,
                    max_filings: int = 400) -> dict:
    """Fetch and record the issuer's Forms 3/4/5 accepted within the lookback (newest first, bounded)."""
    eid = entity_id(cik)
    cutoff = now_ns() - lookback_days * 86_400 * NS_PER_S
    rows = [r for r in submission_rows(submissions) if r["form"] in INSIDER_FORMS and r["acceptance_ns"] and r["acceptance_ns"] >= cutoff
            and r["primary_document"].lower().endswith(".xml")]
    rows.sort(key=lambda r: r["acceptance_ns"], reverse=True)
    out = {"filings_listed": len(rows), "fetched": 0, "kept": 0, "other_issuer": 0, "errors": [], "rows": 0,
           "truncated": len(rows) > max_filings, "outcomes": {}}
    for r in rows[:max_filings]:
        url = document_url(cik, r["accession"], r["primary_document"])
        try:
            body, sha, receipt = _fetch_raw(store, fetcher, url, DATASET_345)
            doc = parse_ownership_xml(body)
        except (FetchError, ValueError, ET.ParseError) as exc:
            out["errors"].append({"accession": r["accession"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        out["fetched"] += 1
        if not _same_cik(doc["issuer_cik"], cik):
            out["other_issuer"] += 1      # the issuer is the reporting owner here, not the subject
            continue
        out["kept"] += 1
        meta = {k: doc[k] for k in ("schema_version", "document_type", "period_of_report", "date_of_original_submission",
                                    "issuer_name", "issuer_symbol", "aff10b5one_as_published", "owners", "footnotes")}
        meta.update({"form": r["form"], "accession": r["accession"], "filing_date": r["filing_date"], "acceptance_raw": r["acceptance_raw"],
                     "document_url": url, "index_url": index_url(cik, r["accession"]), "rows": len(doc["rows"])})
        base = dict(dataset=DATASET_345, entity_id=eid, unit="json", receipt_time_ns=receipt, available_time_ns=r["acceptance_ns"],
                    published_time_ns=r["acceptance_ns"], publication_id=r["accession"])
        _record(store, out["outcomes"], Observation(field=f"form345:{r['accession']}:filing", value_text=canonical(meta),
                                                    period_end=doc["period_of_report"] or r["filing_date"], period_kind="FILING",
                                                    source_ref=f"raw:{sha}#filing", quality=(f"form:{r['form']}",), **base), now_ns())
        for i, row in enumerate(doc["rows"]):
            _record(store, out["outcomes"], Observation(field=f"form345:{r['accession']}:{i:03d}", value_text=canonical(row),
                                                        period_end=row["date"] or doc["period_of_report"] or r["filing_date"],
                                                        period_kind="EVENT", source_ref=f"raw:{sha}#row{i}",
                                                        quality=(f"form:{r['form']}",) + (("unparsed_number",) if row.get("unparsed") else ()),
                                                        **base), now_ns())
            out["rows"] += 1
    return out


def ingest_schedules(store, cik: int, submissions: dict, fetcher: Fetcher, now_ns, *, lookback_days: int = 1095,
                     max_filings: int = 200) -> dict:
    """Fetch and record Schedule 13D/13G reports on the issuer accepted within the lookback."""
    eid = entity_id(cik)
    cutoff = now_ns() - lookback_days * 86_400 * NS_PER_S
    own_prefix = f"{cik:010d}-"
    rows = [r for r in submission_rows(submissions) if r["form"] in STRUCTURED_SCHEDULES | LEGACY_SCHEDULES
            and r["acceptance_ns"] and r["acceptance_ns"] >= cutoff]
    rows.sort(key=lambda r: r["acceptance_ns"], reverse=True)
    out = {"filings_listed": len(rows), "skipped_filed_by_issuer": 0, "fetched": 0, "kept": 0, "other_issuer": 0, "legacy_listed": 0,
           "errors": [], "truncated": False, "outcomes": {}}
    kept = 0
    for r in rows:
        if r["accession"].startswith(own_prefix):
            out["skipped_filed_by_issuer"] += 1
            continue
        if kept >= max_filings:
            out["truncated"] = True
            break
        base = dict(dataset=DATASET_13DG, entity_id=eid, unit="json", available_time_ns=r["acceptance_ns"],
                    published_time_ns=r["acceptance_ns"], publication_id=r["accession"], period_kind="FILING")
        common = {"form": r["form"], "accession": r["accession"], "filing_date": r["filing_date"], "acceptance_raw": r["acceptance_raw"],
                  "index_url": index_url(cik, r["accession"])}
        if r["form"] in LEGACY_SCHEDULES:
            # Free-text filings before the structured-data rule: listed with their link; numbers are not parsed.
            doc = {**common, "parsed": False, "persons": [], "cusips": [],
                   "document_url": f"{folder_url(cik, r['accession'])}/{r['primary_document']}" if r["primary_document"] else None,
                   "reason": "free-text schedule (before structured XML): amounts and percent are in the document, not parsed"}
            _record(store, out["outcomes"], Observation(field=f"13dg:{r['accession']}", value_text=canonical(doc), period_end=r["filing_date"],
                                                        receipt_time_ns=now_ns(), source_ref=f"sec:submissions#{r['accession']}",
                                                        quality=("not_parsed_free_text",), **base), now_ns())
            out["legacy_listed"] += 1
            kept += 1
            continue
        url = document_url(cik, r["accession"], r["primary_document"])
        try:
            body, sha, receipt = _fetch_raw(store, fetcher, url, DATASET_13DG)
            doc = parse_schedule_xml(body)
        except (FetchError, ValueError, ET.ParseError) as exc:
            out["errors"].append({"accession": r["accession"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        out["fetched"] += 1
        if not _same_cik(doc["issuer_cik"], cik):
            out["other_issuer"] += 1
            continue
        doc.update({**common, "document_url": url})
        _record(store, out["outcomes"], Observation(field=f"13dg:{r['accession']}", value_text=canonical(doc),
                                                    period_end=doc["event_date"] or r["filing_date"], receipt_time_ns=receipt,
                                                    source_ref=f"raw:{sha}", quality=(f"form:{r['form']}",), **base), now_ns())
        out["kept"] += 1
        kept += 1
    return out


def load_or_fetch_submissions(store, cik: int, fetcher: Fetcher) -> dict:
    """Fresh submissions JSON for the issuer, kept in the raw layer and made the entity's ``submissions_ref`` (filings
    views filter by acceptance time, so a newer snapshot never shows a filing before it was accepted)."""
    body, sha, _ = _fetch_raw(store, fetcher, SUBMISSIONS.format(cik=cik), "sec.submissions")
    subs = json.loads(body)
    eid = entity_id(cik)
    row = store.conn.execute("SELECT kind, name, attributes FROM entity WHERE entity_id=?", (eid,)).fetchone()
    attrs = json.loads(row["attributes"] or "{}") if row else {"cik": cik, "sic": subs.get("sic")}
    attrs["submissions_ref"] = sha
    store.upsert_entity(eid, row["kind"] if row else "issuer", (row["name"] if row else None) or subs.get("name", ""), attrs)
    return subs


def ingest(store, cik: int, fetcher: Fetcher, now_ns, *, lookback_days: int = 365, schedule_lookback_days: int = 1095) -> dict:
    registry = load_registry()
    for ds in (DATASET_345, DATASET_13DG):
        if not dataset(registry, ds).permits("storage"):
            raise PermissionError(f"{ds} storage not permitted by the registry")
    started = now_ns()
    submissions = load_or_fetch_submissions(store, cik, fetcher)
    result = {"cik": cik, "entity_id": entity_id(cik), "name": submissions.get("name"),
              "insiders": ingest_insiders(store, cik, submissions, fetcher, now_ns, lookback_days=lookback_days),
              "schedules": ingest_schedules(store, cik, submissions, fetcher, now_ns, lookback_days=schedule_lookback_days)}
    status = "OK" if not (result["insiders"]["errors"] or result["schedules"]["errors"]) else "PARTIAL"
    store.conn.execute("INSERT OR REPLACE INTO ingestion_run VALUES (?,?,?,?,?,?,?)",
                       (f"ownership:{cik}:{started}", "sec-edgar", f"{DATASET_345}+{DATASET_13DG}", started, now_ns(), status,
                        canonical({k: v for k, v in result.items() if k not in ("insiders", "schedules")} |
                                  {"insider_rows": result["insiders"]["rows"], "schedules": result["schedules"]["kept"]})))
    result["status"] = status
    return result


# ------------------------------------------------------------------ read model
def _current(store, ds: str, eid: str, as_of_ns: int) -> list:
    return store.conn.execute("SELECT * FROM observation WHERE dataset=? AND entity_id=? AND known_to_ns=? AND available_time_ns<=? "
                              "ORDER BY available_time_ns DESC, field", (ds, eid, OPEN_END, as_of_ns)).fetchall()


def _role(owner: dict) -> str:
    parts = []
    if owner.get("is_director"):
        parts.append("Director")
    if owner.get("is_officer"):
        parts.append(owner.get("officer_title") or "Officer")
    if owner.get("is_ten_percent_owner"):
        parts.append("10% owner")
    if owner.get("is_other"):
        parts.append(owner.get("other_text") or "Other")
    return ", ".join(parts) or "not stated"


def insiders(store, eid: str, as_of_ns: int, *, limit: int = 300) -> dict:
    rows = _current(store, DATASET_345, eid, as_of_ns)
    if not rows:
        return {"status": "NO_DATA", "reason": "NO_DATA", "detail": "no Forms 3/4/5 ingested for this issuer at the as-of time "
                "(python -m unknown.data.sources.edgar_ownership ingest --cik <CIK>)", "transactions": [], "summary": None}
    filings, txns = {}, []
    for r in rows:
        if r["field"].endswith(":filing"):
            filings[r["publication_id"]] = (json.loads(r["value_text"]), r)
    # A 4/A restates the original it names by date of original submission (same reporting owner): the original's rows are
    # shown but marked superseded and left out of the flow summary, so a trade is never counted twice.
    amended = set()
    for acc, (meta, _) in filings.items():
        if meta["form"].endswith("/A") and meta.get("date_of_original_submission"):
            owners = {o.get("cik") for o in meta["owners"]}
            for acc2, (m2, _) in filings.items():
                if acc2 != acc and not m2["form"].endswith("/A") and m2["filing_date"] == meta["date_of_original_submission"] \
                        and owners & {o.get("cik") for o in m2["owners"]}:
                    amended.add(acc2)
    for r in rows:
        if r["field"].endswith(":filing"):
            continue
        acc = r["publication_id"]
        meta = filings.get(acc, ({}, None))[0]
        row = json.loads(r["value_text"])
        code = row.get("code")
        owners = meta.get("owners", [])
        txns.append({"obs_id": r["obs_id"], "accession": acc, "form": meta.get("form"), "filing_date": meta.get("filing_date"),
                     "available_time": ns_to_iso(r["available_time_ns"]), "insider": "; ".join(o.get("name") or "?" for o in owners),
                     "insider_ciks": [o.get("cik") for o in owners], "role": "; ".join(_role(o) for o in owners), **row,
                     "code_label": TRANSACTION_CODES.get(code or "", "not coded" if row["kind"] == "holding" else "unknown code"),
                     "category": OPEN_MARKET.get(code or "", "holding" if row["kind"] == "holding" else "other"),
                     "price_reason": None if row.get("price") is not None else ("NOT_REPORTED" if row["kind"] == "transaction" else "NOT_APPLICABLE"),
                     "superseded_by_amendment": acc in amended, "links": {"filing_index": meta.get("index_url"), "document": meta.get("document_url")}})
    txns.sort(key=lambda t: (t.get("date") or "", t["available_time"], t["obs_id"]), reverse=True)
    return {"status": "OK", "transactions": txns[:limit], "count": len(txns), "truncated": len(txns) > limit,
            "filings": len(filings), "summary": {w: flow(txns, as_of_ns, w) for w in (90, 365)},
            "ten_b5_one": "not displayed: the aff10b5One element is stored as published, but its reading as a Rule 10b5-1 flag is not "
                          "confirmed from SEC documentation (CAP-CO-09)",
            "source": {"dataset": DATASET_345, "attribution": ATTRIBUTION}}


def flow(txns: list[dict], as_of_ns: int, window_days: int) -> dict:
    """Insider net open-market flow over a window: sum(shares x price, P) - sum(shares x price, S), exact decimals."""
    end = datetime.fromtimestamp(as_of_ns // NS_PER_S, tz=timezone.utc).date()
    start = end - timedelta(days=window_days)
    buy, sell, n_buy, n_sell, no_price = Fixed(0), Fixed(0), 0, 0, 0
    for t in txns:
        if t["category"] not in ("open_market_purchase", "open_market_sale") or t["superseded_by_amendment"] or not t.get("date"):
            continue
        try:
            d = date.fromisoformat(t["date"][:10])
        except ValueError:
            continue
        if not start < d <= end:
            continue
        if t.get("price") is None or t.get("shares") is None:
            no_price += 1
            continue
        amount = Fixed.parse(t["shares"]) * Fixed.parse(t["price"])
        if t["category"] == "open_market_purchase":
            buy, n_buy = buy + amount, n_buy + 1
        else:
            sell, n_sell = sell + amount, n_sell + 1
    return {"window_days": window_days, "from": start.isoformat(), "to": end.isoformat(), "purchases": buy.to_str(), "sales": sell.to_str(),
            "net": (buy - sell).to_str(), "purchase_count": n_buy, "sale_count": n_sell, "excluded_without_price": no_price,
            "unit": "USD (Form 4 prices carry no currency tag)", "formula": "insider_net_open_market_flow@1"}


def major_holders(store, eid: str, as_of_ns: int) -> dict:
    rows = _current(store, DATASET_13DG, eid, as_of_ns)
    if not rows:
        return {"status": "NO_DATA", "reason": "NO_DATA", "detail": "no Schedule 13D/13G reports on this issuer ingested at the as-of time",
                "filings": []}
    out = []
    for r in rows:
        doc = json.loads(r["value_text"])
        out.append({"obs_id": r["obs_id"], "available_time": ns_to_iso(r["available_time_ns"]), **doc})
    out.sort(key=lambda d: (d.get("acceptance_raw") or "", d["accession"]), reverse=True)
    return {"status": "OK", "filings": out, "count": len(out), "source": {"dataset": DATASET_13DG, "attribution": ATTRIBUTION},
            "note": "Each reporting person is shown as filed; members of a group report the same shares, so rows are never summed."}


def issuer_cusips(store, eid: str, as_of_ns: int) -> list[dict]:
    """CUSIPs named for the issuer on its structured 13D/13G cover pages (official filings), most cited first."""
    counts: dict[str, list[str]] = {}
    for r in _current(store, DATASET_13DG, eid, as_of_ns):
        doc = json.loads(r["value_text"])
        for c in doc.get("cusips") or []:
            counts.setdefault(c, []).append(doc["accession"])
    return [{"cusip": c, "filings": len(accs), "accessions": accs[:5]} for c, accs in sorted(counts.items(), key=lambda kv: -len(kv[1]))]


def main(argv: list[str] | None = None) -> None:
    from .common import data_config, make_fetcher
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest SEC Forms 3/4/5 and Schedules 13D/13G for issuers into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--cik", required=True, help="comma-separated CIKs")
    ing.add_argument("--lookback-days", type=int, default=365)
    ing.add_argument("--schedule-lookback-days", type=int, default=1095)
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher("sec-edgar", data_config(home))
    for c in a.cik.split(","):
        res = ingest(store, int(c), fetcher, time.time_ns, lookback_days=a.lookback_days, schedule_lookback_days=a.schedule_lookback_days)
        print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
