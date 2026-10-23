"""Chart intervals and the base bar sizes they can be built from."""
from __future__ import annotations

from dataclasses import dataclass

NS = 1_000_000_000


@dataclass(frozen=True)
class Interval:
    code: str                 # API value: 1m 5m 15m 30m 1h 1d 1w 1mo
    seconds: int | None       # intraday length; None for session-based intervals
    period: str               # 'intraday' | 'day' | 'week' | 'month'
    default_span_days: int    # range used when from_ns is not given
    max_span_days: int        # larger ranges are refused (bounded work per request)

    @property
    def intraday(self) -> bool:
        return self.seconds is not None

    @property
    def ns(self) -> int | None:
        return None if self.seconds is None else self.seconds * NS


INTERVALS: dict[str, Interval] = {i.code: i for i in (
    Interval("1m", 60, "intraday", 3, 31),
    Interval("5m", 300, "intraday", 10, 92),
    Interval("15m", 900, "intraday", 30, 183),
    Interval("30m", 1800, "intraday", 60, 366),
    Interval("1h", 3600, "intraday", 120, 731),
    Interval("1d", None, "day", 730, 7320),
    Interval("1w", None, "week", 1830, 7320),
    Interval("1mo", None, "month", 5490, 7320),
)}

# Candidate intraday base sizes, largest first. A base is usable when it divides the target and the session's
# opening offset from local midnight, so clock-aligned provider bars never straddle a session-anchored bucket.
BASE_SECONDS = (3600, 1800, 900, 600, 300, 180, 120, 60)


def parse_interval(code: str | None) -> Interval:
    if not code:
        return INTERVALS["1d"]
    try:
        return INTERVALS[code]
    except KeyError:
        raise ValueError(f"interval must be one of {', '.join(INTERVALS)}") from None


def compatible_bases(target: Interval, open_offsets_s: set[int]) -> list[int]:
    """Intraday base sizes (seconds, largest first) that tile ``target`` inside every session."""
    if not target.intraday:
        return list(BASE_SECONDS)
    return [b for b in BASE_SECONDS
            if target.seconds % b == 0 and all(off % b == 0 for off in open_offsets_s)]
