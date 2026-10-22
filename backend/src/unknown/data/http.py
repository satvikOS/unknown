"""Polite HTTP fetching for data providers: per-provider rate limit, declared
User-Agent, bounded retries with backoff for idempotent GETs, raw capture.
Standard library only; never sends credentials in URLs or logs."""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .registry import RateLimit


class FetchError(RuntimeError):
    pass


class TokenBucket:
    def __init__(self, limit: RateLimit, monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.rate = limit.max_requests / limit.per_seconds
        self.capacity = float(limit.max_requests)
        self.tokens = self.capacity
        self.updated = monotonic()
        self.monotonic = monotonic
        self.sleep = sleep
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            while True:
                now = self.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1 - 1e-9:  # tolerance: float refill can land a hair below 1
                    self.tokens = max(0.0, self.tokens - 1)
                    return
                self.sleep(max((1 - self.tokens) / self.rate, 1e-3))


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    body: bytes
    content_type: str
    receipt_time_ns: int


class Fetcher:
    def __init__(self, user_agent: str, limit: RateLimit, *, timeout_s: float = 30.0, max_retries: int = 4,
                 opener: Callable | None = None, clock_ns: Callable[[], int] = time.time_ns,
                 sleep: Callable[[float], None] = time.sleep):
        if not user_agent or "@" not in user_agent:
            raise FetchError("a declared User-Agent with contact details is required (local config)")
        self.user_agent = user_agent
        self.bucket = TokenBucket(limit, sleep=sleep)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.opener = opener or urllib.request.urlopen
        self.clock_ns = clock_ns
        self.sleep = sleep

    def get(self, url: str) -> Response:
        delay = 1.0
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self.bucket.acquire()
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
            try:
                with self.opener(req, timeout=self.timeout_s) as resp:
                    return Response(url, resp.status, resp.read(), resp.headers.get("Content-Type", ""), self.clock_ns())
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {exc.code}"
                elif exc.code == 404:
                    raise FetchError(f"404 Not Found: {url}") from None
                else:
                    raise FetchError(f"HTTP {exc.code} for {url}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries:
                self.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise FetchError(f"giving up on {url} after {self.max_retries + 1} attempts: {last_error}")
