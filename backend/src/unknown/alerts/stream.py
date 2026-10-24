"""Server-Sent Events for GET /api/alerts/stream.

Events
  ``status``  first, and whenever it changes: {store, engine (the engine's last heartbeat record), unread}
  ``alert``   one per fired alert, ``id:`` = the inbox event id; data = the inbox entry
  ``rules``   the rules version changed (a rule was created, paused, retired, ...): {version}
Reconnects send ``Last-Event-ID``: alerts after it are replayed (at most ``REPLAY_MAX``); without one the stream starts
at the newest alert. The stream reads the alert store only; it never opens the core database.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .store import AlertStore

RETRY_MS = 3000
KEEPALIVE_S = 15.0
POLL_S = 0.5
REPLAY_MAX = 200


def frame(name: str, payload: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n"


def serve(path: Path, last_event_id: int | None, write, flush, stopping: threading.Event, *, poll_s: float = POLL_S,
          keepalive_s: float = KEEPALIVE_S) -> None:
    write(f"retry: {RETRY_MS}\n\n".encode())
    store, last, version, status_sent = None, last_event_id, None, None
    beat = time.monotonic()
    try:
        while not stopping.is_set():
            if store is None:
                try:
                    store = AlertStore(path, readonly=True)
                except FileNotFoundError:
                    if status_sent != "UNAVAILABLE":
                        write(frame("status", {"store": "UNAVAILABLE", "reason": "NO_DATA",
                                               "detail": "no alert rules yet"}).encode())
                        flush()
                        status_sent = "UNAVAILABLE"
                    stopping.wait(poll_s)
                    continue
                if last is None:
                    last = 0 if status_sent == "UNAVAILABLE" else store.max_event_id()
            out = []
            engine = store.meta("engine")
            status = {"store": "AVAILABLE", "engine": json.loads(engine) if engine else None, "unread": store.unread()}
            key = json.dumps({**status, "engine": (status["engine"] or {}).get("started_ns")}, sort_keys=True)
            if key != status_sent:
                out.append(frame("status", status))
                status_sent = key
            v = store.rules_version()
            if v != version:
                if version is not None:
                    out.append(frame("rules", {"version": v}))
                version = v
            for ev in store.events(after_id=last, limit=REPLAY_MAX):
                out.append(frame("alert", ev, ev["event_id"]))
                last = ev["event_id"]
            if out:
                write("".join(out).encode())
                flush()
                beat = time.monotonic()
            elif time.monotonic() - beat >= keepalive_s:
                write(b": keepalive\n\n")
                flush()
                beat = time.monotonic()
            stopping.wait(poll_s)
    finally:
        if store is not None:
            store.close()
