"""EU issuers' ESEF annual reports from filings.xbrl.org (CAP-CO-03, CAP-DATA-03; registry ``xbrl-filings``).

Source: the filings.xbrl.org JSON:API (``/api/entities/<LEI>``, ``/api/entities/<LEI>/filings``) and
each filing's xBRL-JSON (OIM) document named by ``json_url``. Only filings whose ``country`` is an
EU member state are kept (a UK FCA copy of the same report is not EU data). An LEI the index holds
with no EU filing (German issuers: Germany is not indexed) is recorded as an EU issuer with status
``NO_FILINGS_INDEXED`` and a reason, so search shows why there are no statements.

Facts kept: numeric ``ifrs-full`` facts without extra dimensions (concept, entity, period, unit
only) for the filing's own LEI. Each becomes one observation: field = concept QName, unit ``EUR``
/ ``shares`` / ``EUR/shares`` (ISO units stripped of their prefix), value = the canonical decimal
text exactly, publication = the filing id (``fxo_id``). OIM periods are instants at midnight: a
duration ``2025-01-01T00:00:00/2026-01-01T00:00:00`` is 2025-01-01..2025-12-31 and an instant
``2026-01-01T00:00:00`` is the balance at 2025-12-31. Duplicate facts with equal values are one
observation; duplicates with different values keep the first and are flagged
``duplicate_fact_conflict`` (never averaged).

Availability: the index's ``published`` time read as UTC (EU zones are at or ahead of UTC, so this
is never earlier than the real publication), else ``date_added`` (flag
``availability_from_index_date_added``). A later report's comparative figure for an earlier year is
a separate, later publication, so it never leaks backwards (``ResearchStore.as_of``).

  python -m unknown.data.sources.esef ingest --lei 724500Y6DUVHQD6OXN27 [--lei ...] [--max-filings 3]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timedelta, timezone

from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed
from ..pit import Observation
from .macro_common import EU_MEMBERS, require_storage

PROVIDER = "xbrl-filings"
DATASET = "xbrl_org.esef_facts"
BASE = "https://filings.xbrl.org"
ATTRIBUTION = "Source: filings.xbrl.org (XBRL International) index of ESEF annual financial reports; figures as tagged by the issuer"
ALLOWED_DIMS = frozenset({"concept", "entity", "period", "unit"})
LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def lei_valid(text: str) -> bool:
    """ISO 17442 LEI: 20 characters, ISO 7064 MOD 97-10 check digits."""
    t = (text or "").strip().upper()
    if not LEI.fullmatch(t):
        return False
    return int("".join(str(int(ch, 36)) for ch in t)) % 97 == 1


def entity_id(lei: str) -> str:
    return f"ESEF-LEI-{lei}"


def _utc_ns(text: str | None) -> int | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text.strip().replace(" ", "T")[:26])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(microseconds=1) * 1000


def parse_filings(body: bytes) -> tuple[list[dict], str | None]:
    """(filings, next_link)."""
    doc = json.loads(body)
    out = []
    for f in doc.get("data", []):
        a = f.get("attributes") or {}
        out.append({"id": f.get("id"), "fxo_id": a.get("fxo_id"), "period_end": a.get("period_end"), "country": a.get("country"),
                    "published": a.get("published"), "date_added": a.get("date_added"), "json_url": a.get("json_url"),
                    "viewer_url": a.get("viewer_url"), "report_url": a.get("report_url"), "package_url": a.get("package_url"),
                    "package_sha256": a.get("sha256"), "error_count": a.get("error_count"), "warning_count": a.get("warning_count"),
                    "inconsistency_count": a.get("inconsistency_count")})
    return out, (doc.get("links") or {}).get("next")


def eu_filings(filings: list[dict]) -> list[dict]:
    keep = [f for f in filings if f.get("country") in EU_MEMBERS and f.get("json_url") and f.get("fxo_id") and f.get("period_end")]
    return sorted(keep, key=lambda f: (f["period_end"], f["fxo_id"]))


def availability(filing: dict) -> tuple[int, tuple[str, ...]]:
    pub = _utc_ns(filing.get("published"))
    if pub is not None:
        return pub, ("published_time_read_as_utc",)
    added = _utc_ns(filing.get("date_added"))
    if added is None:
        raise ValueError(f"filing {filing.get('fxo_id')} has neither published nor date_added")
    return added, ("availability_from_index_date_added",)


def _oim_date(text: str, *, end: bool) -> date:
    d = date.fromisoformat(text[:10])
    if end and (len(text) == 10 or text[11:19] == "00:00:00"):
        d -= timedelta(days=1)
    return d


def _unit(u: str | None) -> str | None:
    if not u:
        return None
    parts = [p.split(":", 1)[1] if ":" in p else p for p in u.split("/")]
    return "/".join(parts)


def period_kind(start: date | None, end: date) -> str:
    if start is None:
        return "INSTANT"
    days = (end - start).days + 1
    return "FY" if 350 <= days <= 380 else "Q" if 85 <= days <= 98 else "H" if 175 <= days <= 190 else "DURATION"


def parse_oim(body: bytes, lei: str) -> tuple[list[dict], dict]:
    """(facts, stats). A fact is {fact_id, concept, unit, start, end, value (Fixed), decimals, flags}."""
    doc = json.loads(body)
    if (doc.get("documentInfo") or {}).get("documentType") != "https://xbrl.org/2021/xbrl-json":
        raise ValueError("not an xBRL-JSON document")
    stats = {"facts_total": 0, "kept": 0, "dimensional": 0, "non_ifrs": 0, "non_numeric": 0, "other_entity": 0, "duplicates": 0,
             "conflicts": 0}
    seen: dict[tuple, dict] = {}
    for fid, f in (doc.get("facts") or {}).items():
        stats["facts_total"] += 1
        dims = f.get("dimensions") or {}
        concept = dims.get("concept") or ""
        if set(dims) - ALLOWED_DIMS:
            stats["dimensional"] += 1
            continue
        if not concept.startswith("ifrs-full:"):
            stats["non_ifrs"] += 1
            continue
        if (dims.get("entity") or "").split(":", 1)[-1] != lei:
            stats["other_entity"] += 1
            continue
        raw, unit = f.get("value"), _unit(dims.get("unit"))
        if raw is None or unit is None:
            stats["non_numeric"] += 1
            continue
        try:
            value = Fixed.parse(str(raw))
        except ValueError:
            stats["non_numeric"] += 1
            continue
        period = dims.get("period") or ""
        if "/" in period:
            a, b = period.split("/", 1)
            start, end = _oim_date(a, end=False), _oim_date(b, end=True)
        else:
            start, end = None, _oim_date(period, end=True)
        key = (concept, start, end, unit)
        if key in seen:
            stats["duplicates"] += 1
            if seen[key]["value"] != value:
                stats["conflicts"] += 1
                seen[key]["flags"].append(f"duplicate_fact_conflict:{fid}={raw}")
            continue
        seen[key] = {"fact_id": fid, "concept": concept, "unit": unit, "start": start, "end": end, "value": value,
                     "decimals": f.get("decimals"), "flags": []}
    stats["kept"] = len(seen)
    return list(seen.values()), stats


def observations(facts: list[dict], filing: dict, lei: str, raw_sha: str, receipt_ns: int) -> list[Observation]:
    available, flags = availability(filing)
    out = []
    for f in facts:
        ccy = f["unit"] if re.fullmatch(r"[A-Z]{3}", f["unit"]) else (f["unit"][:3] if re.fullmatch(r"[A-Z]{3}/shares", f["unit"]) else None)
        q = (f"filing_country:{filing['country']}", f"filing_period_end:{filing['period_end']}") + flags + \
            ((f"decimals:{f['decimals']}",) if f["decimals"] is not None else ()) + tuple(f["flags"])
        out.append(Observation(
            dataset=DATASET, entity_id=entity_id(lei), field=f["concept"], unit=f["unit"], currency=ccy, value=f["value"],
            period_start=f["start"].isoformat() if f["start"] else None, period_end=f["end"].isoformat(),
            period_kind=period_kind(f["start"], f["end"]), receipt_time_ns=receipt_ns, available_time_ns=available,
            published_time_ns=available if "published_time_read_as_utc" in flags else None,
            publication_id=filing["fxo_id"], source_ref=f"raw:{raw_sha}#{f['fact_id']}", quality=q))
    return out


def _filing_ingested(store, fxo_id: str) -> bool:
    return store.conn.execute("SELECT 1 FROM observation WHERE dataset=? AND publication_id=? LIMIT 1", (DATASET, fxo_id)).fetchone() is not None


def ingest_issuer(store, fetcher, now_ns, lei: str, *, max_filings: int = 3, refresh: bool = False) -> dict:
    require_storage(DATASET)
    lei = lei.strip().upper()
    if not lei_valid(lei):
        raise ValueError(f"{lei} is not a valid LEI (ISO 17442 check digits)")
    eid = entity_id(lei)
    ent = fetcher.get(f"{BASE}/api/entities/{lei}")
    name = ((json.loads(ent.body).get("data") or {}).get("attributes") or {}).get("name") or lei
    filings, link, pages = [], f"{BASE}/api/entities/{lei}/filings", 0
    while link and pages < 5:
        resp = fetcher.get(link if link.startswith("http") else BASE + link)
        store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url}, receipt_time_ns=resp.receipt_time_ns,
                      media_type=resp.content_type)
        page, link = parse_filings(resp.body)
        filings += page
        pages += 1
    eu = eu_filings(filings)
    chosen = eu[-max_filings:] if max_filings else eu
    summary = {"lei": lei, "name": name, "entity_id": eid, "filings_indexed": len(filings), "filings_eu": len(eu),
               "filings_ingested": 0, "filings_skipped_already_held": 0, "facts": 0, "NEW": 0, "UNCHANGED": 0, "CORRECTED": 0, "filings": []}
    held = []
    for f in chosen:
        if not refresh and _filing_ingested(store, f["fxo_id"]):
            summary["filings_skipped_already_held"] += 1
            held.append({**f, "status": "HELD"})
            continue
        resp = fetcher.get(BASE + f["json_url"])
        sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url, "fxo_id": f["fxo_id"]},
                            receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type or "application/json")
        facts, stats = parse_oim(resp.body, lei)
        counts = {"NEW": 0, "UNCHANGED": 0, "CORRECTED": 0}
        for o in observations(facts, f, lei, sha, resp.receipt_time_ns):
            _, outcome = store.record(o, now_ns())
            counts[outcome] += 1
        for k, v in counts.items():
            summary[k] += v
        summary["facts"] += len(facts)
        summary["filings_ingested"] += 1
        held.append({**f, "status": "INGESTED", "json_sha256": sha})
        summary["filings"].append({"fxo_id": f["fxo_id"], "period_end": f["period_end"], "country": f["country"],
                                   "available_time": ns_to_iso(availability(f)[0]), "raw_sha256": sha, **stats, **counts})
    prior = store.conn.execute("SELECT attributes FROM entity WHERE entity_id=?", (eid,)).fetchone()
    prior_filings = {x["fxo_id"]: x for x in (json.loads(prior["attributes"]).get("filings", []) if prior else [])}
    for f in held:
        prior_filings[f["fxo_id"]] = {k: f.get(k) for k in ("fxo_id", "period_end", "country", "published", "date_added", "json_url",
                                                            "viewer_url", "report_url", "error_count", "warning_count",
                                                            "inconsistency_count", "json_sha256")}
    countries = sorted({f["country"] for f in eu})
    status = "OK" if eu else "NO_FILINGS_INDEXED"
    attrs = {"lei": lei, "source": "filings.xbrl.org", "dataset": DATASET, "countries": countries, "esef_status": status,
             "filings": sorted(prior_filings.values(), key=lambda x: (x["period_end"] or "", x["fxo_id"])),
             "index_checked_at": ns_to_iso(ent.receipt_time_ns)}
    if not eu:
        attrs["reason"] = (f"filings.xbrl.org holds no EU ESEF filing for {lei} ({len(filings)} filings in the index); "
                           "German issuers are not indexed (country list on filings.xbrl.org, 2026-10-23)")
    store.upsert_entity(eid, "eu_issuer", name, attrs)
    store.add_alias("lei", lei, eid, ent.receipt_time_ns, f"filings.xbrl.org:/api/entities/{lei}")
    summary["status"] = status
    if not eu:
        summary["reason"] = attrs["reason"]
    return summary


def main(argv: list[str] | None = None) -> None:
    from .common import data_config, make_fetcher
    from .runner import open_store
    ap = argparse.ArgumentParser(description="Ingest ESEF annual reports from filings.xbrl.org into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--lei", action="append", required=True)
    ing.add_argument("--max-filings", type=int, default=3)
    ing.add_argument("--refresh", action="store_true")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    store, home = open_store(a.home)
    fetcher = make_fetcher(PROVIDER, data_config(home))
    out = []
    for lei in a.lei:
        try:
            out.append(ingest_issuer(store, fetcher, time.time_ns, lei, max_filings=a.max_filings, refresh=a.refresh))
        except Exception as exc:  # one issuer's failure is reported, the others continue
            out.append({"lei": lei, "status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"})
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

