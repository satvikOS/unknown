"""The news sources. Each one names a provider record and dataset in config/providers; a source is polled only when
that record exists, its verdict is ``allowed`` or ``allowed_with_conditions``, and the dataset permits display and
storage. Blocked records (ESMA, NSE, BSE on 2026-10-23) are listed with their reason and never fetched."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..data.registry import REPO, Provider, load_registry

POLLABLE = ("allowed", "allowed_with_conditions")


@dataclass(frozen=True)
class NewsSource:
    source_id: str
    name: str            # short label shown in the tape
    provider_id: str
    dataset_id: str
    url: str | None
    parser: str          # 'edgar_atom' | 'feed' | 'none'
    poll_interval_s: int
    kind: str            # 'filing' | 'regulator' | 'central_bank' | 'exchange'
    region: str
    feed_tz: str | None = None   # zone of zone-less feed times (flagged on each item), only where the publisher is local


EDGAR_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&dateb=&owner=include"
             "&start={start}&count={count}&output=atom")

SOURCES: tuple[NewsSource, ...] = (
    NewsSource("sec-filings", "SEC EDGAR", "sec-edgar", "sec.latest_filings_atom", EDGAR_URL, "edgar_atom", 60, "filing", "US"),
    NewsSource("sec-press", "SEC press", "sec-edgar", "sec.press_releases_rss", "https://www.sec.gov/news/pressreleases.rss",
               "feed", 900, "regulator", "US"),
    NewsSource("sec-litigation", "SEC litigation", "sec-edgar", "sec.litigation_releases_rss",
               "https://www.sec.gov/enforcement-litigation/litigation-releases/rss", "feed", 900, "regulator", "US"),
    NewsSource("frb-press", "Federal Reserve", "federal-reserve", "frb.press_all_rss",
               "https://www.federalreserve.gov/feeds/press_all.xml", "feed", 600, "central_bank", "US"),
    NewsSource("ecb-press", "ECB", "ecb", "ecb.press_rss", "https://www.ecb.europa.eu/rss/press.html", "feed", 600,
               "central_bank", "EU"),
    NewsSource("rbi-press", "RBI", "rbi", "rbi.press_releases_rss", "https://www.rbi.org.in/pressreleases_rss.xml", "feed",
               900, "central_bank", "IN", "Asia/Kolkata"),
    NewsSource("sebi", "SEBI", "sebi", "sebi.rss", "https://www.sebi.gov.in/sebirss.xml", "feed", 3600, "regulator", "IN"),
    NewsSource("esma", "ESMA", "esma", "esma.news", None, "none", 0, "regulator", "EU"),
    NewsSource("nse", "NSE announcements", "nse", "nse.announcements", None, "none", 0, "exchange", "IN"),
    NewsSource("bse", "BSE announcements", "bse", "bse.announcements", None, "none", 0, "exchange", "IN"),
)


def _verdicts(directory: Path) -> dict[tuple[str, str], tuple[str, str | None]]:
    """(provider_id, dataset_id) -> (verdict, reason). A dataset's own verdict wins over its provider's."""
    out = {}
    for f in sorted(directory.glob("*.json")):
        d = json.loads(f.read_text())
        for ds in d.get("datasets", []):
            out[(d["provider_id"], ds["dataset_id"])] = (ds.get("verdict") or d.get("verdict") or "unrecorded",
                                                         ds.get("verdict_reason") or d.get("verdict_reason"))
    return out


def catalog(directory: Path | None = None, sources: tuple[NewsSource, ...] = SOURCES) -> list[dict]:
    """Every source with its rights record; ``pollable`` is the gate the poller obeys."""
    directory = directory or REPO / "config" / "providers"
    registry: dict[str, Provider] = load_registry(directory)
    verdicts = _verdicts(directory)
    rows = []
    for s in sources:
        p = registry.get(s.provider_id)
        ds = next((d for d in p.datasets if d.dataset_id == s.dataset_id), None) if p else None
        verdict, reason = verdicts.get((s.provider_id, s.dataset_id), ("unrecorded", None))
        refusal = None
        if ds is None:
            refusal = f"no provider record {s.provider_id}/{s.dataset_id} in config/providers"
        elif verdict not in POLLABLE:
            refusal = reason or f"rights verdict is {verdict}"
        elif not (ds.permits("display") and ds.permits("storage")):
            refusal = "the dataset's recorded uses do not include display and storage"
        elif not s.url:
            refusal = "no feed URL"
        rows.append({
            "source_id": s.source_id, "name": s.name, "kind": s.kind, "region": s.region, "provider_id": s.provider_id,
            "provider_name": p.name if p else None, "dataset_id": s.dataset_id, "url": s.url if s.parser != "edgar_atom" else
            s.url.format(start=0, count=100) if s.url else None, "poll_interval_s": s.poll_interval_s,
            "verdict": verdict, "verdict_reason": reason, "pollable": refusal is None, "refusal": refusal,
            "allowed_uses": sorted(ds.allowed_uses) if ds else [], "rights_source": ds.rights_source if ds else None,
            "terms_verified_on": ds.rights_verified_on if ds else None,
            "rate_limit": {"max_requests": p.rate_limit.max_requests, "per_seconds": p.rate_limit.per_seconds} if p else None,
        })
    return rows


def by_id(source_id: str, sources: tuple[NewsSource, ...] = SOURCES) -> NewsSource:
    for s in sources:
        if s.source_id == source_id:
            return s
    raise KeyError(source_id)
