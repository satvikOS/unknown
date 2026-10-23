"""Server-Sent Events writer for GET /api/marketdata/stream (contract: event names quote, trade, depth, status,
heartbeat). The API handler owns authentication and headers; this module only frames and writes."""
from __future__ import annotations

import json
import threading
import time

from .bus import MarketDataBus
from .events import InvalidKey, ns, parse_key, status_event

HEARTBEAT_S = 5.0
RETRY_MS = 3000


def parse_keys(raw: str | None) -> tuple[list[str], list[dict]]:
    """Comma-separated keys, de-duplicated in order; invalid ones become UNAVAILABLE status events."""
    keys, bad, seen = [], [], set()
    for part in (raw or "").split(","):
        k = part.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        try:
            parse_key(k)
            keys.append(k)
        except InvalidKey as exc:
            bad.append(status_event(k[:64], "UNAVAILABLE", str(exc)))
    return keys, bad


def frame(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def serve(bus: MarketDataBus, keys: list[str], refused: list[dict], write, flush, stopping: threading.Event, *,
          heartbeat_s: float = HEARTBEAT_S, time_ns=time.time_ns) -> None:
    """Stream until ``stopping`` is set or the client goes away (write raises). Always releases the subscriptions."""
    client = bus.open_client(keys)
    try:
        head = f"retry: {RETRY_MS}\n\n" + "".join(frame("status", s) for s in refused)
        write(head.encode())
        flush()
        last_beat = time.monotonic()
        while not stopping.is_set():
            items = client.drain(min(heartbeat_s, 0.5))
            if items:
                write("".join(frame(n, ev) for n, ev in items).encode())
                flush()
                t = time_ns()
                bus.latency.record_many("pub_to_write", [t - int(ev["ts_pub_ns"]) for n, ev in items
                                                         if n in ("quote", "trade")])
            if time.monotonic() - last_beat >= heartbeat_s:
                write(frame("heartbeat", {"ts_pub_ns": ns(time_ns())}).encode())
                flush()
                last_beat = time.monotonic()
    finally:
        bus.close_client(client)
