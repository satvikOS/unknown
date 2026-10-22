"""Marks from a supplied quote snapshot (CAP-PORT-01; formula port.mark@1).

A mark records its basis (bid, mid or last), its source, its age at the valuation
instant and whether it is fresh. Delayed data is never a fresh mark. A quote whose
receipt time is after the valuation instant is ignored, so a valuation can never use
information that did not exist yet (point-in-time guard, DAT-04)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..domain.fixed import Fixed
from ..market.quotes import Quote

BASES = ("bid", "mid", "last")


@dataclass(frozen=True)
class Mark:
    instrument_id: str
    price: Fixed | None
    status: str              # fresh | stale | unpriced
    basis: str
    reason: str | None
    source: str | None = None
    age_ms: int | None = None
    event_time_ns: int | None = None
    receipt_time_ns: int | None = None
    delayed: bool = False
    fixture: bool = False
    quote: dict | None = None

    def to_json(self) -> dict:
        return {"instrument_id": self.instrument_id, "price": self.price.to_str() if self.price is not None else None,
                "status": self.status, "basis": self.basis, "reason": self.reason, "source": self.source,
                "age_ms": self.age_ms, "event_time_ns": self.event_time_ns, "receipt_time_ns": self.receipt_time_ns,
                "delayed": self.delayed, "fixture": self.fixture}


def _fx(v) -> Fixed | None:
    if v is None:
        return None
    if isinstance(v, Fixed):
        return v
    return Fixed.parse(str(v))


def quote_from_json(d: Mapping) -> Quote:
    """Rebuild a Quote from the core's ``quotes`` query shape (market.quotes.Quote.to_json).

    Every provenance field survives the round trip, including the feed's ``consolidation`` label
    (consolidated / non_consolidated / unknown, CAP-DATA-10): the pretrade gate's QUOTE_CONSOLIDATED
    check reads it, so a what-if built from a relabelled quote would not be the live decision, and a
    snapshot would record the wrong feed (review fix)."""
    return Quote(instrument_id=d["instrument_id"], bid=_fx(d.get("bid")), ask=_fx(d.get("ask")), last=_fx(d.get("last")),
                 currency=d["currency"], source=d.get("source") or "unknown", event_time_ns=d.get("event_time_ns"),
                 receipt_time_ns=int(d["receipt_time_ns"]), delayed=bool(d.get("delayed")), fixture=bool(d.get("fixture")),
                 consolidation=d.get("consolidation") or "unknown")


def normalize_quotes(quotes: Mapping | None) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for key, q in (quotes or {}).items():
        if isinstance(q, Quote):
            out[q.instrument_id] = q
        elif isinstance(q, Mapping):
            out[q.get("instrument_id", key)] = quote_from_json({"instrument_id": key, **q})
        else:
            raise TypeError(f"quote for {key} must be a Quote or its JSON form")
    return out


def quote_json(q: Quote, now_ns: int) -> dict:
    return q.to_json(now_ns)


def mark_for(instrument_id: str, q: Quote | None, *, basis: str, max_age_ms: int, now_ns: int) -> Mark:
    if basis not in BASES:
        raise ValueError(f"mark basis must be one of {BASES}")
    if q is None:
        return Mark(instrument_id, None, "unpriced", basis, "no quote in the supplied snapshot")
    if q.receipt_time_ns > now_ns:
        return Mark(instrument_id, None, "unpriced", basis,
                    "quote received after the valuation instant (point-in-time guard)", source=q.source,
                    receipt_time_ns=q.receipt_time_ns, fixture=q.fixture)
    price = {"bid": q.bid, "last": q.last, "mid": q.mid()}[basis]
    age_ms = q.age_ns(now_ns) // 1_000_000
    common = dict(source=q.source, age_ms=age_ms, event_time_ns=q.event_time_ns, receipt_time_ns=q.receipt_time_ns,
                  delayed=q.delayed, fixture=q.fixture, quote=q.to_json(now_ns))
    if price is None or not price.is_positive():
        return Mark(instrument_id, None, "unpriced", basis, f"quote has no usable {basis}", **common)
    if q.delayed:
        return Mark(instrument_id, price, "stale", basis, "delayed data is never a fresh mark", **common)
    if age_ms > max_age_ms:
        return Mark(instrument_id, price, "stale", basis, f"age {age_ms} ms exceeds the {max_age_ms} ms budget", **common)
    return Mark(instrument_id, price, "fresh", basis, None, **common)
