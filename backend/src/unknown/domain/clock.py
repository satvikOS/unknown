"""Clocks. Wall time is UTC nanoseconds for storage; monotonic time is for
local durations only and is never persisted as if it were wall time."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


class Clock(Protocol):
    def now_ns(self) -> int: ...
    def monotonic_ns(self) -> int: ...


class SystemClock:
    def now_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class ManualClock:
    """Deterministic clock for tests and replay; advances only when told."""

    def __init__(self, start_ns: int):
        if type(start_ns) is not int or start_ns < 0:
            raise ValueError("start_ns must be a non-negative int")
        self._now = start_ns
        self._mono = 0

    def now_ns(self) -> int:
        return self._now

    def monotonic_ns(self) -> int:
        return self._mono

    def advance(self, ns: int) -> None:
        if ns < 0:
            raise ValueError("ManualClock cannot go backwards")
        self._now += ns
        self._mono += ns

    def set(self, now_ns: int) -> None:
        if now_ns < self._now:
            raise ValueError("ManualClock cannot go backwards")
        self._mono += now_ns - self._now
        self._now = now_ns


def utc_ns(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp()) * NS_PER_S


def ns_to_iso(ns: int) -> str:
    seconds, rem = divmod(ns, NS_PER_S)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{rem:09d}Z"
