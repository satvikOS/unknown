"""Per-stage latency samples for GET /api/marketdata/latency (contract "Latency").

Stages (nanoseconds, integers; no floats):

* ``source_to_recv``: ts_recv_ns - ts_source_ns, only for events whose source sends a provider time (clock skew between
  the provider and this host is included; a negative value is counted in ``negative`` and not sampled).
* ``recv_to_pub``: ts_pub_ns - ts_recv_ns (adapter receipt to bus publish, includes quote coalescing).
* ``pub_to_write``: SSE socket write time - ts_pub_ns (bus queue and writer; per client).

The browser adds its receipt and paint stages locally (terminal/src/state/marketdata.ts).
Each stage keeps the most recent 8192 samples, so the percentiles describe recent behaviour, not the whole run.
"""
from __future__ import annotations

import threading
from collections import deque

STAGES = ("source_to_recv", "recv_to_pub", "pub_to_write")
WINDOW = 8192


def percentile(sorted_values: list[int], p: int) -> int | None:
    """Nearest-rank percentile (p in 1..100) of an ascending list."""
    if not sorted_values:
        return None
    rank = -(-p * len(sorted_values) // 100)          # ceil(p/100 * n)
    return sorted_values[max(rank, 1) - 1]


class LatencyStats:
    def __init__(self, window: int = WINDOW):
        self._lock = threading.Lock()
        self._samples = {s: deque(maxlen=window) for s in STAGES}
        self._counts = {s: 0 for s in STAGES}
        self._negative = {s: 0 for s in STAGES}

    def record(self, stage: str, value_ns: int) -> None:
        with self._lock:
            if value_ns < 0:
                self._negative[stage] += 1
                return
            self._samples[stage].append(value_ns)
            self._counts[stage] += 1

    def record_many(self, stage: str, values: list[int]) -> None:
        with self._lock:
            for v in values:
                if v < 0:
                    self._negative[stage] += 1
                else:
                    self._samples[stage].append(v)
                    self._counts[stage] += 1

    def report(self) -> dict:
        with self._lock:
            copies = {s: sorted(self._samples[s]) for s in STAGES}
            counts, negative = dict(self._counts), dict(self._negative)
        out = {}
        for s in STAGES:
            v = copies[s]
            out[s] = {"count": counts[s], "window": len(v), "negative": negative[s], "p50_ns": percentile(v, 50),
                      "p99_ns": percentile(v, 99), "max_ns": v[-1] if v else None}
        return out
