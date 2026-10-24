"""Reconnect pacing and health for the IBKR connections that are not the order adapter (CAP-IBKR-07, OPS-03).

The core's market-data maintainer (``serve.MarketDataMaintainer``) and the market-data bus source
(``mdsource.IbkrSource``) each own a read-only TWS API connection. When IB Gateway drops (IBKR 1100, a closed socket,
the nightly restart or weekend maintenance) they retry with capped exponential backoff until the Gateway is back, and
report the outage in their health views; nobody restarts a process by hand. Nothing here logs in: a Gateway that is
not logged in stays AUTH_REQUIRED until the owner logs in (ADR-0012), and an identity latch (DISCONNECTED_UNSAFE) is
never retried.

The core service has its own copy of this policy for the order adapter (``service.core``), because the core never
imports the adapter package."""
from __future__ import annotations

import time
from typing import Callable

RECONNECT_MIN_S = 5.0
RECONNECT_MAX_S = 300.0
UNSAFE_PREFIX = "DISCONNECTED_UNSAFE"


class ReconnectBackoff:
    """Capped exponential backoff (5 s doubling to 300 s) with an owner-readable state. Thread-compatible: one owner
    thread calls ``due``/``failed``/``connected``; ``to_json`` may be read from any thread (plain attribute reads)."""

    def __init__(self, *, min_s: float = RECONNECT_MIN_S, max_s: float = RECONNECT_MAX_S,
                 monotonic: Callable[[], float] = time.monotonic, now_ns: Callable[[], int] = time.time_ns):
        if not 0 < min_s <= max_s:
            raise ValueError("backoff needs 0 < min_s <= max_s")
        self.min_s, self.max_s = float(min_s), float(max_s)
        self._mono, self._now_ns = monotonic, now_ns
        self.backoff_s = self.min_s
        self.next_attempt_at: float = 0.0
        self.attempts = 0                      # failed attempts in the current outage
        self.reconnects = 0                    # outages recovered since start
        self.up = False
        self.ever_up = False
        self.reason: str | None = "NOT_CONNECTED"
        self.down_since_ns: int | None = None
        self.last_attempt_ns: int | None = None
        self.last_up_ns: int | None = None

    def due(self) -> bool:
        return self._mono() >= self.next_attempt_at

    def wait_s(self) -> float:
        return max(0.0, self.next_attempt_at - self._mono())

    def lost(self, reason: str | None) -> None:
        """The connection is down (seen without an attempt). A fresh outage is retried at once."""
        if self.up or self.down_since_ns is None:
            self.down_since_ns = self._now_ns()
            self.next_attempt_at, self.backoff_s, self.attempts = 0.0, self.min_s, 0
        self.up = False
        self.reason = reason or "NOT_CONNECTED"

    def failed(self, reason: str | None) -> float:
        """An attempt failed: schedule the next one and return the wait in seconds."""
        if self.up or self.down_since_ns is None:
            self.down_since_ns = self._now_ns()
        self.up = False
        self.reason = reason or "NOT_CONNECTED"
        self.attempts += 1
        self.last_attempt_ns = self._now_ns()
        wait = self.backoff_s
        self.next_attempt_at = self._mono() + wait
        self.backoff_s = min(self.backoff_s * 2, self.max_s)
        return wait

    def connected(self) -> None:
        if not self.up:
            if self.ever_up:
                self.reconnects += 1
            self.last_up_ns = self._now_ns()
        self.up, self.ever_up = True, True
        self.reason, self.down_since_ns, self.attempts = None, None, 0
        self.backoff_s, self.next_attempt_at = self.min_s, 0.0

    @property
    def latched(self) -> bool:
        return (self.reason or "").startswith(UNSAFE_PREFIX)

    def to_json(self) -> dict:
        state = "connected" if self.up else ("latched" if self.latched else "reconnecting")
        return {"state": state, "reason": None if self.up else self.reason,
                "down_since_ns": self.down_since_ns, "failed_attempts": self.attempts,
                "next_attempt_in_s": None if self.up or self.latched else round(self.wait_s(), 3),
                "backoff_s": self.backoff_s, "reconnects": self.reconnects, "last_attempt_ns": self.last_attempt_ns,
                "last_connected_ns": self.last_up_ns, "policy": f"{self.min_s:g} s doubling to {self.max_s:g} s; "
                                                              "never logs in (the owner logs in to IB Gateway)"}
