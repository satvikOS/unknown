"""Corporate events calendar from official SEC filings (CAP-NEWS-10, partial; dataset
``sec.event_announcements`` in config/providers/sec-edgar.json).

Three kinds of entry, each labelled with its basis:

* ``occurred`` (basis ``filing``): an 8-K with Item 2.02 (results of operations) is a results
  release; a 10-Q / 10-K (or 20-F, 40-F) is a periodic report. Date = EDGAR acceptance time, from the
  issuer's stored submissions JSON, shown only when accepted at or before the as-of time.
* ``announced`` (basis ``announced``): a date the issuer itself stated in an 8-K (Item 7.01 or
  8.01) for an upcoming results release or call. The sentence is kept as evidence; the entry is
  available from the acceptance time of that 8-K. Many issuers announce dates only on their own
  websites, which this platform does not read, so the absence of an announced date is normal.
* ``projected`` (basis ``derived``): one year (52 weeks, same weekday) after the same event of the
  prior year, labelled "estimated (internal)" with the filing it was derived from (the capability
  record forbids inferring dates from prior-year patterns without that label). An announced date
  within 30 days replaces the projection; an occurrence within 35 days closes it.

  python -m unknown.data.sources.edgar_events ingest --cik 320193,19617 [--lookback-days 200] [--home ~/.unknown]
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone

from ...domain.clock import NS_PER_S, ns_to_iso
from ..http import FetchError, Fetcher
from ..pit import OPEN_END, Observation
from ..registry import dataset, load_registry
from .edgar_ownership import canonical, entity_id, folder_url, index_url, submission_rows

DATASET = "sec.event_announcements"
PERIODIC = {"10-Q": "periodic_report", "10-K": "periodic_report", "20-F": "periodic_report", "40-F": "periodic_report",
            "10-Q/A": "periodic_report", "10-K/A": "periodic_report"}
EVENT_TYPES = {"results_release": "Results release (8-K Item 2.02)", "periodic_report": "Periodic report (10-Q / 10-K)",
               "earnings_call": "Results call or webcast"}
PROJECTION_DAYS = 364
MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september",
                                          "october", "november", "december"))}
_MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sept?\.?|Oct\.?|Nov\.?|Dec\.?)"
DATE_RE = re.compile(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+)?(\d{4})?", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.m\.|p\.m\.|am|pm))(?:\s*\(?\s*(Eastern|Pacific|Central|ET|EST|EDT|PT|PST|PDT|CT|CST|CDT)\b)?", re.I)
RESULTS_RE = re.compile(r"\b(results|earnings)\b", re.I)
SCHEDULE_RE = re.compile(r"\b(will|plans to|expects to|to be held|scheduled)\b", re.I)
CALL_RE = re.compile(r"\b(conference call|call|webcast)\b", re.I)
SENTENCE_SPLIT = re.compile(r"(?<!p\.m\.)(?<!a\.m\.)(?<!Inc\.)(?<!Corp\.)(?<!Co\.)(?<!No\.)(?<=[.!?])\s+(?=[A-Z])")
RELEASE_RE = re.compile(r"\b(report|release|announce|publish)", re.I)
PAST_CONTEXT = re.compile(r"\b(ended|ending|as of|through|dated|since)\s*$", re.I)


# ------------------------------------------------------------------ announced dates (pure)
def html_to_text(body: bytes) -> str:
    t = body.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return " ".join(html_lib.unescape(t).split())


def _month(text: str) -> int | None:
    key = text.lower().rstrip(".")
    return next((n for name, n in MONTHS.items() if len(key) >= 3 and name.startswith(key)), None)


def find_announced_dates(text: str, filed: date, *, horizon_days: int = 120) -> list[dict]:
    """Future dates that a filing schedules for a results release or call: each with the sentence it came from."""
    out, seen = [], set()
    for sentence in SENTENCE_SPLIT.split(text):
        if len(sentence) > 600 or not RESULTS_RE.search(sentence) or not SCHEDULE_RE.search(sentence):
            continue
        for m in DATE_RE.finditer(sentence):
            if PAST_CONTEXT.search(sentence[max(0, m.start() - 20):m.start()]):
                continue
            month, day = _month(m.group(1)), int(m.group(2))
            if month is None:
                continue
            years = [int(m.group(3))] if m.group(3) else [filed.year, filed.year + 1]
            for y in years:
                try:
                    d = date(y, month, day)
                except ValueError:
                    continue
                if filed < d <= filed + timedelta(days=horizon_days):
                    kind = "results_release" if RELEASE_RE.search(sentence) or not CALL_RE.search(sentence) else "earnings_call"
                    tm = TIME_RE.search(sentence[m.end():m.end() + 120])
                    key = (d, kind)
                    if key not in seen:
                        seen.add(key)
                        out.append({"date": d.isoformat(), "event_type": kind, "time_text": " ".join(x for x in tm.groups() if x) if tm else None,
                                    "year_inferred": not m.group(3), "call_mentioned": bool(CALL_RE.search(sentence)),
                                    "sentence": sentence[:400]})
                    break
    return out


def ingest_announcements(store, cik: int, submissions: dict, fetcher: Fetcher, now_ns, *, lookback_days: int = 200,
                         max_docs: int = 20) -> dict:
    if not dataset(load_registry(), DATASET).permits("storage"):
        raise PermissionError(f"{DATASET} storage not permitted by the registry")
    eid = entity_id(cik)
    cutoff = now_ns() - lookback_days * 86_400 * NS_PER_S
    rows = [r for r in submission_rows(submissions) if (r["form"] or "").startswith("8-K") and r["acceptance_ns"]
            and r["acceptance_ns"] >= cutoff and {"7.01", "8.01"} & set(r["items"]) and r["primary_document"]]
    rows.sort(key=lambda r: r["acceptance_ns"], reverse=True)
    out = {"candidates": len(rows), "fetched": 0, "announced": 0, "errors": [], "truncated": len(rows) > max_docs, "outcomes": {}}
    for r in rows[:max_docs]:
        url = f"{folder_url(cik, r['accession'])}/{r['primary_document']}"
        try:
            resp = fetcher.get(url)
        except FetchError as exc:
            out["errors"].append({"accession": r["accession"], "error": str(exc)[:300]})
            continue
        out["fetched"] += 1
        sha = store.put_raw(resp.body, provider="sec-edgar", dataset=DATASET, request={"url": resp.url},
                            receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
        found = find_announced_dates(html_to_text(resp.body), date.fromisoformat(r["filing_date"]))
        for i, ev in enumerate(found):
            doc = {**ev, "accession": r["accession"], "form": r["form"], "items": r["items"], "filing_date": r["filing_date"],
                   "acceptance_raw": r["acceptance_raw"], "document_url": url, "index_url": index_url(cik, r["accession"])}
            _, outcome = store.record(Observation(
                dataset=DATASET, entity_id=eid, field=f"announced:{r['accession']}:{i}", unit="json", value_text=canonical(doc),
                period_end=ev["date"], period_kind="EVENT", receipt_time_ns=resp.receipt_time_ns, available_time_ns=r["acceptance_ns"],
                published_time_ns=r["acceptance_ns"], publication_id=r["accession"], source_ref=f"raw:{sha}",
                quality=("announced_in_8k",) + (("year_inferred",) if ev["year_inferred"] else ())), now_ns())
            out["outcomes"][outcome] = out["outcomes"].get(outcome, 0) + 1
            out["announced"] += 1
    return out


# ------------------------------------------------------------------ calendar (read model)
def _day(ns: int) -> date:
    return datetime.fromtimestamp(ns // NS_PER_S, tz=timezone.utc).date()


def occurred_events(submissions: dict, as_of_ns: int) -> list[dict]:
    cik = int(submissions.get("cik"))
    out = []
    for r in submission_rows(submissions):
        if r["acceptance_ns"] is None or r["acceptance_ns"] > as_of_ns:
            continue
        form = r["form"] or ""
        kind = "results_release" if form.startswith("8-K") and "2.02" in r["items"] else PERIODIC.get(form)
        if kind is None:
            continue
        out.append({"event_type": kind, "status": "occurred", "basis": "filing", "date": _day(r["acceptance_ns"]).isoformat(),
                    "time": ns_to_iso(r["acceptance_ns"]), "form": form, "accession": r["accession"], "items": r["items"],
                    "report_date": r["report_date"], "amendment": form.endswith("/A"),
                    "link": index_url(cik, r["accession"]), "label": f"{form}{' Item 2.02' if kind == 'results_release' else ''} accepted"})
    return out


def announced_events(store, eid: str, as_of_ns: int) -> list[dict]:
    rows = store.conn.execute("SELECT * FROM observation WHERE dataset=? AND entity_id=? AND known_to_ns=? AND available_time_ns<=?",
                              (DATASET, eid, OPEN_END, as_of_ns)).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["value_text"])
        out.append({"event_type": d["event_type"], "status": "announced", "basis": "announced", "date": d["date"], "time": None,
                    "time_text": d.get("time_text"), "form": d["form"], "accession": d["accession"], "items": d["items"],
                    "link": d["index_url"], "evidence": d["sentence"], "announced_at": ns_to_iso(r["available_time_ns"]),
                    "year_inferred": d.get("year_inferred", False), "obs_id": r["obs_id"],
                    "label": f"announced in {d['form']} of {d['filing_date']}"})
    return out


def projected_events(occurred: list[dict], announced: list[dict], as_of_ns: int) -> list[dict]:
    today = _day(as_of_ns)
    out = []
    for e in occurred:
        if e["amendment"]:
            continue
        base = date.fromisoformat(e["date"])
        if not today - timedelta(days=400) <= base <= today:
            continue
        target = base + timedelta(days=PROJECTION_DAYS)
        if target <= today - timedelta(days=35):
            continue
        def same(x, e=e):
            return x["event_type"] == e["event_type"] or {x["event_type"], e["event_type"]} == {"results_release", "earnings_call"}
        if any(same(o) and abs((date.fromisoformat(o["date"]) - target).days) <= 35 and o["date"] > e["date"] for o in occurred):
            continue
        if any(same(a) and abs((date.fromisoformat(a["date"]) - target).days) <= 30 for a in announced):
            continue
        out.append({"event_type": e["event_type"], "status": "projected", "basis": "derived", "date": target.isoformat(), "time": None,
                    "form": None, "accession": None, "derived_from": {"accession": e["accession"], "form": e["form"], "date": e["date"]},
                    "overdue": target < today, "link": None,
                    "label": f"estimated (internal): {PROJECTION_DAYS} days after {e['form']} of {e['date']}"})
    return out


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def calendar(store, issuers: list[dict], start: date, end: date, as_of_ns: int, *, statuses: set[str] | None = None,
             types: set[str] | None = None) -> dict:
    """Events for ``issuers`` ([{entity_id, name, submissions, meta}]) between start and end (inclusive), by ISO week."""
    from ..fundamentals.filings import load_submissions
    events, per_issuer = [], []
    for iss in issuers:
        eid = iss["entity_id"]
        subs, meta = load_submissions(store, eid)
        if subs is None:
            per_issuer.append({"entity_id": eid, "name": meta.get("name"), "status": "NO_DATA", "reason": "NO_DATA",
                               "detail": meta.get("reason")})
            continue
        occ = occurred_events(subs, as_of_ns)
        ann = announced_events(store, eid, as_of_ns)
        for a in ann:
            hits = [o for o in occ if o["event_type"] == "results_release" and abs((date.fromisoformat(o["date"]) - date.fromisoformat(a["date"])).days) <= 3]
            a["confirmed_by"] = hits[0]["accession"] if hits else None
        proj = projected_events(occ, ann, as_of_ns)
        tickers = subs.get("tickers") or []
        n = 0
        for e in occ + ann + proj:
            d = date.fromisoformat(e["date"])
            if not start <= d <= end or (statuses and e["status"] not in statuses) or (types and e["event_type"] not in types):
                continue
            events.append({**e, "entity_id": eid, "cik": int(subs["cik"]), "name": subs.get("name"), "ticker": tickers[0] if tickers else None,
                           "event_label": EVENT_TYPES[e["event_type"]], "week": week_start(d).isoformat()})
            n += 1
        per_issuer.append({"entity_id": eid, "name": subs.get("name"), "status": "OK", "events": n, "announced_held": len(ann),
                           "submissions_retrieved_at": meta.get("retrieved_at")})
    events.sort(key=lambda e: (e["date"], e["time"] or "", e["name"] or "", e["event_type"]))
    weeks: dict[str, list] = {}
    for e in events:
        weeks.setdefault(e["week"], []).append(e)
    return {"as_of": ns_to_iso(as_of_ns), "from": start.isoformat(), "to": end.isoformat(), "count": len(events),
            "weeks": [{"week_start": w, "events": evs} for w, evs in sorted(weeks.items())], "issuers": per_issuer,
            "legend": {"occurred": "filed on EDGAR (acceptance time)", "announced": "date stated by the issuer in an 8-K",
                       "projected": "estimated (internal): one year after the same event; not a company announcement"},
            "event_types": EVENT_TYPES, "source": "Source: U.S. SEC EDGAR submissions and 8-K filings"}


def main(argv: list[str] | None = None) -> None:
    from .common import data_config, make_fetcher
    from .edgar_ownership import load_or_fetch_submissions
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Scan recent 8-K filings (Items 7.01/8.01) for announced results dates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--cik", required=True)
    ing.add_argument("--lookback-days", type=int, default=200)
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher("sec-edgar", data_config(home))
    for c in a.cik.split(","):
        subs = load_or_fetch_submissions(store, int(c), fetcher)
        print(json.dumps({"cik": int(c), **ingest_announcements(store, int(c), subs, fetcher, time.time_ns,
                                                                lookback_days=a.lookback_days)}, indent=1))


if __name__ == "__main__":
    main()
