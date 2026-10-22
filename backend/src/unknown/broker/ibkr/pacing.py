"""Request pacing for the TWS API (research record R8 ``ibkr-request-pacing-and-market-data-lines``).

IBKR: "The maximum number of API requests that can be submitted are equivalent
to your Maximum Market Data Lines divided by 2, per second" (default 100 lines,
so 50/s). R8 asks for a reserved share for cancels and reconciliation, and for
error 100 to halt non-critical classes and back off.

This is a sliding one-second window, not a refilling bucket: a bucket of
capacity R refilled at R/s can emit 2R inside one second, which would breach
"never exceeds R_max in any 1-second window" (R8 acceptance criterion 1).
Every request class counts against the same host budget, because the record
leaves open whether placeOrder/cancelOrder count equally (R8 open question 1).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

WINDOW_NS = 1_000_000_000
PRIORITY_CLASSES = frozenset({"cancel", "reconcile"})
REQUEST_CLASSES = frozenset({"order", "cancel", "reconcile", "query"})


def max_requests_per_second(market_data_lines: int) -> int:
    """R8 analytics: R_max = floor(L / 2)."""
    if type(market_data_lines) is not int or market_data_lines < 2:
        raise ValueError("market_data_lines must be an int >= 2")
    return market_data_lines // 2


class PacingScheduler:
    """Shared per-host budget with capacity held back for cancels and reconciliation."""

    def __init__(self, max_per_second: int, reserved_percent: int, *,
                 monotonic_ns: Callable[[], int] = time.monotonic_ns,
                 sleep: Callable[[float], None] = time.sleep):
        if type(max_per_second) is not int or max_per_second < 1:
            raise ValueError("max_per_second must be a positive int")
        if type(reserved_percent) is not int or not 0 <= reserved_percent < 100:
            raise ValueError("reserved_percent must be in [0, 100)")
        reserved = (max_per_second * reserved_percent) // 100
        if reserved_percent and reserved == 0 and max_per_second > 1:
            reserved = 1
        self.max_per_second = max_per_second
        self.reserved = min(reserved, max_per_second - 1)
        self._mono = monotonic_ns
        self._sleep = sleep
        self._sent: deque[int] = deque()
        self._penalty_until_ns = 0
        self._lock = threading.Lock()
        self.granted: dict[str, int] = {c: 0 for c in REQUEST_CLASSES}
        self.denied: dict[str, int] = {c: 0 for c in REQUEST_CLASSES}

    def _limit(self, cls: str, now: int) -> int:
        if cls in PRIORITY_CLASSES:
            return self.max_per_second
        if now < self._penalty_until_ns:
            return 0   # R8 degraded behaviour: error 100 halts non-critical classes during back-off
        return self.max_per_second - self.reserved

    def try_acquire(self, cls: str) -> bool:
        if cls not in REQUEST_CLASSES:
            raise ValueError(f"unknown request class {cls!r}")
        with self._lock:
            now = self._mono()
            while self._sent and self._sent[0] <= now - WINDOW_NS:
                self._sent.popleft()
            if len(self._sent) < self._limit(cls, now):
                self._sent.append(now)
                self.granted[cls] += 1
                return True
            self.denied[cls] += 1
            return False

    def acquire(self, cls: str, timeout_s: float) -> bool:
        deadline = self._mono() + int(timeout_s * 1e9)
        while True:
            if self.try_acquire(cls):
                return True
            now = self._mono()
            if now >= deadline:
                return False
            with self._lock:
                oldest = self._sent[0] if self._sent else now
            wait_ns = max(1_000_000, min(oldest + WINDOW_NS - now, deadline - now))
            self._sleep(wait_ns / 1e9)

    def penalize(self, backoff_s: float) -> None:
        with self._lock:
            self._penalty_until_ns = max(self._penalty_until_ns, self._mono() + int(backoff_s * 1e9))

    def in_window(self) -> int:
        with self._lock:
            now = self._mono()
            return sum(1 for t in self._sent if t > now - WINDOW_NS)
