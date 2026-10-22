"""SEC EDGAR ingestion into the point-in-time store (DAT-03; capability records
company-research / data-providers). Free official data, fair-access rate limit,
declared User-Agent from local config.

Availability rule (config/providers/sec-edgar.json): a reported value is
available at the EDGAR acceptance timestamp of its accession, joined from the
submissions feed; when that is unknown, at 23:59:59 America/New_York on the
'filed' date, which is conservative (never earlier than true availability).

Values are parsed from JSON with ``parse_float=Decimal`` so no binary float
ever touches a reported number.

  python -m unknown.data.sec ingest --cik 320193 [--home ~/.unknown]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from ..domain.clock import NS_PER_S
from ..domain.fixed import Fixed
from .http import Fetcher
from .pit import Observation, ResearchStore
from .registry import dataset, load_registry, local_config

NY = ZoneInfo("America/New_York")
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def entity_id(cik: int) -> str:
    return f"SEC-CIK-{cik:010d}"


def parse_json(body: bytes) -> dict:
    return json.loads(body, parse_float=Decimal)


def acceptance_times(submissions: dict) -> dict[str, int]:
    """accessionNumber -> acceptance time (UTC ns) from a submissions 'recent' block, or from an
    additional submissions file (which carries the same columns at top level)."""
    recent = submissions.get("filings", {}).get("recent", submissions)
    out = {}
    for accn, acc in zip(recent.get("accessionNumber", []), recent.get("acceptanceDateTime", [])):
        if acc:
            dt = datetime.fromisoformat(acc.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[accn] = int(dt.timestamp()) * NS_PER_S
    return out


def end_of_filed_day_ns(filed: str) -> int:
    d = datetime.fromisoformat(filed).replace(hour=23, minute=59, second=59, tzinfo=NY)
    return int(d.timestamp()) * NS_PER_S


def companyfacts_observations(cik: int, facts: dict, accepted: dict[str, int], raw_ref: str, receipt_ns: int) -> list[Observation]:
    out: list[Observation] = []
    for taxonomy, concepts in facts.get("facts", {}).items():
        for concept, body in concepts.items():
            for unit, rows in body.get("units", {}).items():
                for r in rows:
                    val = r.get("val")
                    if isinstance(val, Decimal):
                        value = Fixed.from_decimal(val)
                    elif isinstance(val, int) and not isinstance(val, bool):
                        value = Fixed(val)
                    else:
                        continue  # non-numeric facts are out of scope for this dataset
                    accn = r.get("accn", "")
                    filed = r.get("filed")
                    if accn in accepted:
                        available, quality = accepted[accn], ()
                    elif filed:
                        available, quality = end_of_filed_day_ns(filed), ("availability_from_filed_date",)
                    else:
                        continue  # no defensible availability time: do not ingest
                    out.append(Observation(
                        dataset="sec.companyfacts", entity_id=entity_id(cik), field=f"{taxonomy}:{concept}",
                        unit=unit, currency="USD" if unit.split("/")[0] == "USD" else None,
                        period_start=r.get("start"), period_end=r.get("end"),
                        period_kind="INSTANT" if r.get("start") is None else (r.get("fp") or "DURATION"),
                        value=value, receipt_time_ns=receipt_ns, available_time_ns=available,
                        published_time_ns=accepted.get(accn), publication_id=accn or f"filed:{filed}",
                        source_ref=f"raw:{raw_ref}#{taxonomy}/{concept}/{unit}/{accn}",
                        quality=quality + ((f"form:{r.get('form')}",) if r.get("form") else ())))
    return out


def ingest(cik: int, store: ResearchStore, fetcher: Fetcher, now_ns) -> dict:
    registry = load_registry()
    ds_sub, ds_facts = dataset(registry, "sec.submissions"), dataset(registry, "sec.companyfacts")
    if not ds_facts.permits("storage"):
        raise PermissionError("sec.companyfacts storage not permitted by the registry")
    sub_resp = fetcher.get(SUBMISSIONS.format(cik=cik))
    sub_ref = store.put_raw(sub_resp.body, provider="sec-edgar", dataset=ds_sub.dataset_id, request={"url": sub_resp.url},
                            receipt_time_ns=sub_resp.receipt_time_ns, media_type=sub_resp.content_type)
    submissions = parse_json(sub_resp.body)
    store.upsert_entity(entity_id(cik), "issuer", submissions.get("name", ""), {"cik": cik, "sic": submissions.get("sic"),
                                                                                  "submissions_ref": sub_ref})
    for t, ex in zip(submissions.get("tickers", []), submissions.get("exchanges", [])):
        # Current tickers carry no validity dates in this feed: recorded from now, never backdated.
        store.add_alias(f"ticker:{ex or 'UNKNOWN'}", t, entity_id(cik), sub_resp.receipt_time_ns, f"raw:{sub_ref}")
    accepted = acceptance_times(submissions)
    # Older filings live in additional submissions files; their acceptance times replace the
    # conservative filed-date fallback (data.sec.gov/submissions/<name>).
    for extra in submissions.get("filings", {}).get("files", []):
        name = extra.get("name", "")
        if not name.endswith(".json") or "/" in name:
            continue
        resp = fetcher.get(f"https://data.sec.gov/submissions/{name}")
        store.put_raw(resp.body, provider="sec-edgar", dataset=ds_sub.dataset_id, request={"url": resp.url},
                      receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
        accepted.update(acceptance_times(parse_json(resp.body)))
    facts_resp = fetcher.get(COMPANYFACTS.format(cik=cik))
    facts_ref = store.put_raw(facts_resp.body, provider="sec-edgar", dataset=ds_facts.dataset_id, request={"url": facts_resp.url},
                              receipt_time_ns=facts_resp.receipt_time_ns, media_type=facts_resp.content_type)
    counts = {"NEW": 0, "UNCHANGED": 0, "CORRECTED": 0}
    observations = companyfacts_observations(cik, parse_json(facts_resp.body), accepted, facts_ref, facts_resp.receipt_time_ns)
    for o in observations:
        _, outcome = store.record(o, now_ns())
        counts[outcome] += 1
    return {"cik": cik, "entity_id": entity_id(cik), "observations": len(observations), **counts,
            "accessions_with_acceptance_time": len(accepted)}


def main(argv: list[str] | None = None) -> None:
    import time
    from ..service.paths import Paths
    ap = argparse.ArgumentParser(description="Ingest SEC EDGAR companyfacts into the point-in-time store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--cik", type=int, required=True)
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    home = Path(a.home).expanduser() if a.home else None
    paths = Paths(home) if home else Paths.default()
    cfg = local_config("data", home)
    registry = load_registry()
    fetcher = Fetcher(cfg["sec_user_agent"], registry["sec-edgar"].rate_limit)
    research = paths.root / "research"
    research.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(research / "research.sqlite")
    print(json.dumps(ingest(a.cik, store, fetcher, time.time_ns), indent=2))


if __name__ == "__main__":
    main()
