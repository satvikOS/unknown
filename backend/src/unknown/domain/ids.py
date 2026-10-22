"""Identifier types and generation.

Identifiers are opaque strings checked at the boundary. ``NewType`` keeps them
distinct for type checkers; ``validate_id`` keeps hostile or malformed values
out of the store. Generated identifiers are UUIDv7 (RFC 9562) so they sort by
creation time; generation is injectable so tests are deterministic.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from typing import Callable, NewType

AccountKey = NewType("AccountKey", str)        # internal key for a broker account; never the broker's account number
SessionId = NewType("SessionId", str)
OrderId = NewType("OrderId", str)              # our client order id, sent to the broker in the order-reference field
CommandId = NewType("CommandId", str)
EventId = NewType("EventId", str)
CorrelationId = NewType("CorrelationId", str)
InstrumentId = NewType("InstrumentId", str)
ExecutionKey = NewType("ExecutionKey", str)    # provider-scoped execution identity
PolicyHash = NewType("PolicyHash", str)
CaseId = NewType("CaseId", str)
DeploymentId = NewType("DeploymentId", str)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,159}$")


class InvalidIdentifier(ValueError):
    pass


def validate_id(value: object, what: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise InvalidIdentifier(f"invalid {what}: {value!r}")
    return value


def _uuid7_from(unix_ms: int, rand: bytes) -> str:
    if len(rand) < 10:
        raise ValueError("uuid7 needs 10 random bytes")
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (int.from_bytes(rand[0:2], "big") & 0x0FFF) << 64
    value |= 0b10 << 62
    value |= int.from_bytes(rand[2:10], "big") & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


class IdGenerator:
    """Monotonic UUIDv7 source. ``random_bytes`` and ``now_ms`` are injectable."""

    def __init__(self, now_ms: Callable[[], int], random_bytes: Callable[[int], bytes] = os.urandom):
        self._now_ms = now_ms
        self._random = random_bytes
        self._lock = threading.Lock()
        self._last = ""

    def new(self, prefix: str = "") -> str:
        with self._lock:
            candidate = _uuid7_from(self._now_ms(), self._random(10))
            if candidate <= self._last:
                # Same millisecond with a smaller random tail: bump past the last id.
                candidate = str(uuid.UUID(int=uuid.UUID(self._last).int + 1))
            self._last = candidate
        return f"{prefix}{candidate}" if prefix else candidate


class SequenceRandom:
    """Deterministic byte source for tests (counter-based, not cryptographic)."""

    def __init__(self, seed: int = 0):
        self._n = seed

    def __call__(self, size: int) -> bytes:
        self._n += 1
        return self._n.to_bytes(size, "big", signed=False) if size >= 8 else (self._n % (1 << (8 * size))).to_bytes(size, "big")
