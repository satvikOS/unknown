"""Server-Sent Events for GET /api/news/stream (ADR-0019).

Events
  ``status``  first: {store: AVAILABLE|UNAVAILABLE, reason?, watching: [{token, cik}], unresolved: [token]}
  ``news``    one per new item, ``id:`` = item id; data = the item as GET /api/news returns it
  ``alert``   a new item tagged with a watched issuer: {item, issuers: [cik]}; copies of a story (syndication
              duplicates, or a second item of an already-alerted cluster on this connection) never alert
A comment line every ``KEEPALIVE_S`` keeps proxies open. Reconnects send ``Last-Event-ID`` (or ``?last_event_id=``):
items after it are replayed, at most ``REPLAY_MAX``. Without one the stream starts at the newest item.

Watchlists live in the browser (monitor workspace), so the client passes ``issuers=`` (CIK digits, TICKER or
TICKER.MIC, comma-separated, at most ``MAX_ISSUERS``); tickers resolve through the newest SEC ticker snapshot.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .entities import IssuerIndex
from .store import NewsStore

RETRY_MS = 3000
KEEPALIVE_S = 15.0
POLL_S = 1.0
REPLAY_MAX = 200
MAX_ISSUERS = 500


def frame(name: str, payload: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def parse_issuers(raw: str | None) -> list[str]:
    toks = [t.strip() for t in (raw or "").split(",") if t.strip()]
    if len(toks) > MAX_ISSUERS:
        raise ValueError(f"at most {MAX_ISSUERS} issuers per stream")
    return toks


def serve(store_file: Path, research_db: Path, issuers: list[str], last_event_id: int | None, write, flush,
          stopping: threading.Event, *, poll_s: float = POLL_S, keepalive_s: float = KEEPALIVE_S,
          clock_ns=time.time_ns) -> None:
    index = IssuerIndex.open(research_db, clock_ns())
    try:
        ciks, missing = index.resolve(issuers)
        watching = [{"token": t, "cik": c} for t in issuers for c in index.resolve([t])[0]]
    finally:
        index.close()
    store = None
    last = last_event_id
    alerted_clusters: set[int] = set()
    write(f"retry: {RETRY_MS}\n\n".encode())
    status_sent = None
    last_beat = time.monotonic()
    try:
        while not stopping.is_set():
            if store is None:
                try:
                    store = NewsStore(store_file, readonly=True)
                except FileNotFoundError:
                    if status_sent != "UNAVAILABLE":
                        write(frame("status", {"store": "UNAVAILABLE", "reason": "no news store yet: start the poller",
                                               "watching": watching, "unresolved": missing}).encode())
                        flush()
                        status_sent = "UNAVAILABLE"
                    stopping.wait(poll_s)
                    continue
                if last is None:   # a store born after this stream opened is all new
                    last = 0 if status_sent == "UNAVAILABLE" else store.max_item_id()
                write(frame("status", {"store": "AVAILABLE", "watching": watching, "unresolved": missing}).encode())
                flush()
                status_sent = "AVAILABLE"
            rows = store.query(after_id=last, limit=REPLAY_MAX)
            if rows:
                out = []
                for item in rows:   # oldest first (after_id queries ascend)
                    out.append(frame("news", item, item["id"]))
                    hit = sorted({int(e["value"]) for e in item["entities"] if e["scheme"] == "SEC_CIK"} & ciks)
                    cl = (item.get("cluster") or {}).get("cluster_id")
                    if hit and item["duplicate_of"] is None and cl not in alerted_clusters:
                        if cl is not None:
                            alerted_clusters.add(cl)
                        out.append(frame("alert", {"item": item, "issuers": hit}))
                    last = item["id"]
                write("".join(out).encode())
                flush()
                last_beat = time.monotonic()
            elif time.monotonic() - last_beat >= keepalive_s:
                write(b": keepalive\n\n")
                flush()
                last_beat = time.monotonic()
            stopping.wait(poll_s)
    finally:
        if store is not None:
            store.close()
