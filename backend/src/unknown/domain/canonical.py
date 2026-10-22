"""Canonical JSON and hashing. Used for payload hashes, policy hashes,
mandate hashes and the event chain; any change here changes every hash, so
the encoding is fixed and tested."""
from __future__ import annotations

import enum
import hashlib
import json
from typing import Any

from .fixed import Fixed
from .money import Money, Price, Quantity


def _default(value: Any) -> Any:
    if isinstance(value, Fixed):
        return value.to_str()
    if isinstance(value, (Money, Price)):
        return value.to_json()
    if isinstance(value, Quantity):
        return value.to_str()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not canonically encodable: {type(value).__name__}")


def _check_keys(value: Any) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"canonical payload keys must be str, got {type(k).__name__}")
            _check_keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _check_keys(v)


def canonical_json(value: Any) -> str:
    if isinstance(value, float):
        raise TypeError("floats are not canonical; use Fixed")
    _check_keys(value)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=_default)
    _reject_floats(json.loads(text))
    return text


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("floats are not permitted in canonical payloads; use decimal strings")
    if isinstance(value, dict):
        for v in value.values():
            _reject_floats(v)
    elif isinstance(value, list):
        for v in value:
            _reject_floats(v)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))
