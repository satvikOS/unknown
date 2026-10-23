"""The news poller process (ADR-0019).

  python -m unknown.news.poller run [--once] [--home DIR] [--source ID ...]

It polls each pollable source (``sources.catalog``) when due, with the owner's declared User-Agent, one rate-limited
``Fetcher`` per provider (all SEC sources share the SEC's bucket, far below 10 requests/second), and writes only the
news store. One poller per home (an exclusive lock on ``news/poller.lock``).

Durable cursor: every item is keyed by (source, guid) and inserted once, so a restart never re-announces what was
seen. For EDGAR the cursor's high-water mark is the newest acceptance time seen; a poll pages back (``start=``)
until it reaches an entry older than that mark, at most ``EDGAR_MAX_PAGES`` pages, and records a possible gap when
it could not. Filing index pages are linked, never fetched, so nothing already seen is fetched again.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import signal
import threading
import time
from pathlib import Path

from ..data.fundamentals.readonly import research_paths
from ..data.http import FetchError, Fetcher
from ..data.registry import load_registry
from ..data.sources.common import data_config, declared_user_agent
from .entities import IssuerIndex
from .feeds import FeedError, FeedItem, parse_edgar_atom, parse_feed
from .sources import SOURCES, NewsSource, catalog
from .store import NewsStore, store_path

log = logging.getLogger("unknown.news.poller")
EDGAR_PAGE = 100
EDGAR_MAX_PAGES = 10
EDGAR_FIRST_RUN_PAGES = 2


def _kind(src: NewsSource) -> str:
    return "filing" if src.parser == "edgar_atom" else "release"


def _order(items: list[FeedItem]) -> list[FeedItem]:
    """Oldest first, so item ids (the tape and the SSE ids) grow with feed time inside one poll."""
    return sorted(items, key=lambda i: (i.published_ns is None, i.published_ns or 0))


def _fetch(store: NewsStore, src: NewsSource, fetcher, url: str, clock_ns) -> tuple[int, object]:
    fid = store.begin_fetch(src.source_id, url, clock_ns())
    try:
        resp = fetcher.get(url)
    except FetchError as exc:
        store.end_fetch(fid, status="FAILED", error=str(exc)[:500])
        raise
    store.end_fetch(fid, status="OK", receipt_ns=resp.receipt_time_ns, http_status=resp.status, body_len=len(resp.body),
                    sha256=hashlib.sha256(resp.body).hexdigest())
    return fid, resp


def _parse(store: NewsStore, fid: int, resp, parse) -> list[FeedItem]:
    try:
        return parse(resp.body)
    except FeedError as exc:
        store.end_fetch(fid, status="PARSE_FAILED", receipt_ns=resp.receipt_time_ns, http_status=resp.status,
                        body_len=len(resp.body), sha256=hashlib.sha256(resp.body).hexdigest(), error=str(exc)[:500])
        raise


def _store_items(store: NewsStore, src: NewsSource, items: list[FeedItem], index: IssuerIndex, fid: int,
                 seen_ns: int, flags: list[str]) -> int:
    new = 0
    for it in _order(items):
        if src.parser == "edgar_atom":
            ents = index.tag_filing(it.entities, seen_ns)
        else:
            ents = index.tag_text(f"{it.title} {it.summary or ''}", seen_ns)
        extra = [f"time_zone_assumed:{it.time_zone_assumed}"] if it.time_zone_assumed else []
        _, inserted = store.add(src.source_id, _kind(src), it, first_seen_ns=seen_ns, fetch_id=fid, flags=flags + extra,
                                entities=ents)
        new += inserted
    return new


def poll_source(store: NewsStore, src: NewsSource, fetcher, index: IssuerIndex, *, clock_ns=time.time_ns,
                max_pages: int = EDGAR_MAX_PAGES, first_run_pages: int = EDGAR_FIRST_RUN_PAGES) -> dict:
    """One poll of one source. Returns {source_id, ok, entries, new, error}; the cursor records the outcome."""
    cur = store.cursor(src.source_id)
    first = cur is None or cur.get("last_ok_ns") is None
    flags = ["first_poll_backlog"] if first else []
    entries = new = 0
    high = None
    error = None
    try:
        if src.parser == "edgar_atom":
            hw = cur.get("high_water_ns") if cur else None
            pages = first_run_pages if hw is None else max_pages
            reached = hw is None
            for page in range(pages):
                fid, resp = _fetch(store, src, fetcher, src.url.format(start=page * EDGAR_PAGE, count=EDGAR_PAGE), clock_ns)
                filings = _parse(store, fid, resp, parse_edgar_atom)
                entries += len(filings)
                got = _store_items(store, src, filings, index, fid, resp.receipt_time_ns, flags)
                new += got
                store.end_fetch(fid, status="OK", receipt_ns=resp.receipt_time_ns, http_status=resp.status,
                                body_len=len(resp.body), sha256=hashlib.sha256(resp.body).hexdigest(),
                                entries=len(filings), new_items=got)
                times = [f.published_ns for f in filings if f.published_ns is not None]
                if times:
                    high = max(high or 0, max(times))
                if hw is not None and times and min(times) < hw:
                    reached = True
                    break
                if len(filings) < EDGAR_PAGE // 2:   # end of the feed's window
                    reached = True
                    break
            if not reached:
                error = f"possible gap: {pages} pages did not reach the previous high-water mark"
        else:
            fid, resp = _fetch(store, src, fetcher, src.url, clock_ns)
            items = _parse(store, fid, resp, lambda b: parse_feed(b, src.feed_tz))
            entries = len(items)
            new = _store_items(store, src, items, index, fid, resp.receipt_time_ns, flags)
            store.end_fetch(fid, status="OK", receipt_ns=resp.receipt_time_ns, http_status=resp.status,
                            body_len=len(resp.body), sha256=hashlib.sha256(resp.body).hexdigest(), entries=entries,
                            new_items=new)
            times = [i.published_ns for i in items if i.published_ns is not None]
            high = max(times) if times else None
    except (FetchError, FeedError) as exc:
        store.record_poll(src.source_id, now_ns=clock_ns(), ok=False, high_water_ns=high, new_items=new,
                          error=f"{type(exc).__name__}: {exc}"[:500])
        return {"source_id": src.source_id, "ok": False, "entries": entries, "new": new, "error": str(exc)[:300]}
    store.record_poll(src.source_id, now_ns=clock_ns(), ok=True, high_water_ns=high, new_items=new, error=error)
    return {"source_id": src.source_id, "ok": True, "entries": entries, "new": new, "error": error}


class Poller:
    def __init__(self, root: Path, *, sources: tuple[NewsSource, ...] = SOURCES, fetcher_for=None,
                 clock_ns=time.time_ns, providers_dir: Path | None = None):
        self.root = Path(root)
        self.store = NewsStore(store_path(self.root))
        self.clock_ns = clock_ns
        cat = {c["source_id"]: c for c in catalog(providers_dir, sources)}
        self.sources = [s for s in sources if cat[s.source_id]["pollable"]]
        self.refused = {s.source_id: cat[s.source_id]["refusal"] for s in sources if not cat[s.source_id]["pollable"]}
        self._fetchers: dict[str, object] = {}
        self._fetcher_for = fetcher_for or self._default_fetcher

    def _default_fetcher(self, provider_id: str):
        reg = load_registry()
        return Fetcher(declared_user_agent(data_config(self.root)), reg[provider_id].rate_limit, max_retries=2)

    def fetcher(self, provider_id: str):
        if provider_id not in self._fetchers:
            self._fetchers[provider_id] = self._fetcher_for(provider_id)
        return self._fetchers[provider_id]

    def due(self, src: NewsSource, now_ns: int) -> bool:
        cur = self.store.cursor(src.source_id)
        return not cur or cur["last_poll_ns"] is None or now_ns - cur["last_poll_ns"] >= src.poll_interval_s * 1_000_000_000

    def poll_due(self, *, force: bool = False) -> list[dict]:
        results = []
        index = IssuerIndex.open(research_paths(self.root)[0], self.clock_ns())
        try:
            for src in self.sources:
                if force or self.due(src, self.clock_ns()):
                    r = poll_source(self.store, src, self.fetcher(src.provider_id), index, clock_ns=self.clock_ns)
                    log.info("%s: ok=%s entries=%d new=%d%s", r["source_id"], r["ok"], r["entries"], r["new"],
                             f" ({r['error']})" if r["error"] else "")
                    results.append(r)
        finally:
            index.close()
        return results

    def run(self, stopping: threading.Event, tick_s: float = 5.0) -> None:
        while not stopping.is_set():
            self.poll_due()
            stopping.wait(tick_s)


def _lock(root: Path):
    path = store_path(root).parent / "poller.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another news poller holds {path}") from None
    return fh


def main(argv: list[str] | None = None) -> None:
    from ..service.paths import Paths
    ap = argparse.ArgumentParser(description="Poll official news and filing feeds into the news store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true", help="poll every pollable source once and exit")
    r.add_argument("--home", default=None)
    r.add_argument("--source", action="append", default=[], help="limit to these source ids")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    root = Path(a.home).expanduser() if a.home else Paths.default().root
    lock = _lock(root)
    try:
        srcs = tuple(s for s in SOURCES if not a.source or s.source_id in a.source)
        p = Poller(root, sources=srcs)
        for sid, why in p.refused.items():
            log.info("%s: not polled (%s)", sid, why)
        if a.once:
            results = p.poll_due(force=True)
            counts = p.store.counts()
            for res in results:
                print(f"{res['source_id']:16} ok={res['ok']!s:5} entries={res['entries']:4} new={res['new']:4} "
                      f"stored={counts.get(res['source_id'], 0):5} {res['error'] or ''}")
            return
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        p.run(stopping)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
