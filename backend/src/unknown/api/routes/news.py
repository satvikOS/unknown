"""News and filings routes (ADR-0019; CAP-NEWS-02, -05, -06, -08, -13). GET only; the server requires the owner
session. Everything reads the news store (``UNKNOWN_HOME/news/news.sqlite``) read-only; nothing here writes, and
the core connection the server passes is not used.

* ``/api/news?source=&form=&issuer=&q=&since=&before=&limit=&duplicates=``: newest first. ``source`` and ``form``
  take comma-separated values (``8-K`` also matches ``8-K/A``); ``issuer`` takes CIKs, tickers or TICKER.MIC keys;
  ``q`` is an FTS5 search ("phrase", prefix*, AND/OR/NOT, NEAR); ``since`` is an ISO date/time or 90m/24h/7d.
* ``/api/news/item?id=``: one item with its entities, the other copies in its story and the fetch it came from.
* ``/api/news/top?hours=``: stories ranked by the number of sources carrying them, then copies, then recency.
* ``/api/news/sources``: every source with its rights verdict, poll state, stored count and ingest lag.
* ``/api/news/stream``: SSE (``unknown.news.stream``), dispatched by the server's stream plumbing.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone

from ...data.fundamentals.readonly import research_paths
from ...domain.clock import ns_to_iso
from ...news import stream as news_stream
from ...news.entities import IssuerIndex
from ...news.sources import catalog
from ...news.store import NewsStore, store_path

NS = 1_000_000_000
MAX_LIMIT = 500


def _unavailable() -> dict:
    return {"status": "UNAVAILABLE", "reason": "No news store yet: the news poller has not run on this machine.",
            "populate": ["python -m unknown.news.poller run --once", "python -m unknown.news.poller run"]}


def _open(ctx) -> NewsStore | None:
    try:
        return NewsStore(store_path(ctx.paths.root), readonly=True)
    except FileNotFoundError:
        return None


def parse_since(v: str | None, now_ns: int) -> int | None:
    if not v:
        return None
    m = re.fullmatch(r"(\d{1,5})([mhd])", v.strip())
    if m:
        return now_ns - int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)] * NS
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()):
            d = date.fromisoformat(v.strip())
            return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) * NS
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("since must be an ISO date (YYYY-MM-DD), an ISO time with a zone, or 90m / 24h / 7d") from None
    if dt.tzinfo is None:
        raise ValueError("since needs a time zone (Z or +HH:MM)")
    return int(dt.timestamp() * 1_000_000) * 1000


def _csv(v: str | None) -> list[str] | None:
    vals = [x.strip() for x in (v or "").split(",") if x.strip()]
    return vals or None


def _int(q: dict, key: str, default: int | None, lo: int, hi: int) -> int | None:
    if q.get(key) in (None, ""):
        return default
    try:
        n = int(q[key])
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None
    if not lo <= n <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return n


def _source_rows(store: NewsStore | None) -> list[dict]:
    cursors = store.cursors() if store else {}
    rows = []
    for c in catalog():
        cur = cursors.get(c["source_id"], {})
        rows.append({k: c[k] for k in ("source_id", "name", "kind", "region", "provider_id", "verdict", "pollable",
                                       "refusal")} | {"last_ok": cur.get("last_ok"), "last_poll": cur.get("last_poll"),
                                                      "last_status": cur.get("last_status"),
                                                      "last_error": cur.get("last_error")})
    return rows


def news_list(ctx, q: dict) -> dict:
    now = time.time_ns()
    store = _open(ctx)
    if store is None:
        return _unavailable() | {"sources": _source_rows(None)}
    try:
        issuers = _csv(q.get("issuer"))
        ciks, unresolved, ident = None, [], None
        if issuers:
            index = IssuerIndex.open(research_paths(ctx.paths.root)[0], now)
            try:
                ciks, unresolved = index.resolve(issuers)
                ident = {"source": index.source, "as_of": ns_to_iso(index.as_of_ns) if index.as_of_ns else None}
            finally:
                index.close()
        limit = _int(q, "limit", 100, 1, MAX_LIMIT)
        items = store.query(source=_csv(q.get("source")), form=_csv(q.get("form")), ciks=ciks,
                            q=(q.get("q") or q.get("query") or "").strip() or None, since_ns=parse_since(q.get("since"), now),
                            before_id=_int(q, "before", None, 1, 2 ** 62), limit=limit,
                            include_duplicates=q.get("duplicates", "1") != "0")
        return {"as_of": ns_to_iso(now), "items": items, "next_before": items[-1]["id"] if len(items) == limit else None,
                "latest_id": store.max_item_id(), "sources": _source_rows(store),
                "issuers": None if issuers is None else {"requested": issuers, "ciks": sorted(ciks or []),
                                                          "unresolved": unresolved, "identifiers": ident}}
    finally:
        store.close()


def news_item(ctx, q: dict) -> dict:
    item_id = _int(q, "id", None, 1, 2 ** 62)
    if item_id is None:
        raise ValueError("id is required")
    store = _open(ctx)
    if store is None:
        return _unavailable()
    try:
        item = store.item(item_id)
    finally:
        store.close()
    if item is None:
        raise LookupError(f"no news item {item_id}")
    return {"as_of": ns_to_iso(time.time_ns()), "item": item}


def news_top(ctx, q: dict) -> dict:
    now = time.time_ns()
    hours = _int(q, "hours", 72, 1, 24 * 30)
    store = _open(ctx)
    if store is None:
        return _unavailable()
    try:
        return {"as_of": ns_to_iso(now), "hours": hours, "ranking": "sources carrying the story, then copies, then recency",
                "stories": store.top(since_ns=now - hours * 3600 * NS, limit=_int(q, "limit", 30, 1, 200))}
    finally:
        store.close()


def news_sources(ctx, q: dict) -> dict:
    now = time.time_ns()
    store = _open(ctx)
    try:
        cursors = store.cursors() if store else {}
        counts = store.counts() if store else {}
        rows = []
        for c in catalog():
            lag = store.ingest_lag(c["source_id"], now - 86400 * NS) if store else None
            rows.append(c | {"poll": cursors.get(c["source_id"]), "stored_items": counts.get(c["source_id"], 0),
                             "ingest_lag_24h": lag})
        return {"as_of": ns_to_iso(now), "store": "AVAILABLE" if store else "UNAVAILABLE", "sources": rows}
    finally:
        if store:
            store.close()


def handle(ctx, parts, q, account_key, conn):
    sub = list(parts[1:])
    if sub == []:
        return news_list(ctx, q)
    if sub == ["item"]:
        return news_item(ctx, q)
    if sub == ["top"]:
        return news_top(ctx, q)
    if sub == ["sources"]:
        return news_sources(ctx, q)
    raise LookupError("unknown news endpoint")


def stream(ctx, q: dict):
    """Validates the request (ValueError = 400 before any header is sent) and returns the stream's runner."""
    root = ctx.paths.root
    issuers = news_stream.parse_issuers(q.get("issuers"))

    def run(last_event_id: int | None, write, flush, stopping) -> None:
        news_stream.serve(store_path(root), research_paths(root)[0], issuers, last_event_id, write, flush, stopping)
    return run


ROUTES = [(("news",), handle)]
STREAM_ROUTES = [(("news", "stream"), stream)]
