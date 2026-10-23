"""Exact, session-anchored aggregation of base bars into chart buckets.

Formulas (chart.agg@1): open = first constituent open; high = max high; low = min low; close = last close;
volume = sum of volumes (null when any constituent volume is null: unknown is never zero); trades = sum of counts
(same null rule); vwap = sum(vwap_i x volume_i) / sum(volume_i), 6 decimals half-even (null when any input is
missing or total volume is 0). Intraday buckets start at the session open and are cut at the session close; daily
buckets are sessions; weekly buckets are ISO weeks and monthly buckets calendar months of trading dates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from .intervals import NS, Interval
from .sessions import SessionClock

FORMULA = ("chart.agg@1: open=first, high=max, low=min, close=last, volume=sum (null if any null), "
           "vwap=sum(vwap*volume)/sum(volume) to 6 dp half-even; intraday buckets anchored at the session open and "
           "cut at the close; day=session, week=ISO week, month=calendar month of the trading date")
VWAP_Q = Decimal("0.000001")


@dataclass(frozen=True)
class BaseBar:
    start_ns: int
    end_ns: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None = None
    vwap: Decimal | None = None
    trades: int | None = None
    day: date | None = None       # provider-stated trading date (daily bars)


@dataclass
class Bucket:
    start_ns: int
    end_ns: int
    expected: int | None
    parts: list = field(default_factory=list)


@dataclass
class Result:
    bars: list[dict]
    gaps: list[dict]
    excluded_outside_session: int = 0
    misaligned: int = 0


def dstr(d: Decimal | None) -> str | None:
    return None if d is None else format(d, "f")


def _combine(parts: list) -> dict | None:
    parts = [p for p in parts if p.close is not None]
    if not parts:
        return None
    opens = [p.open for p in parts if p.open is not None]
    highs = [p.high for p in parts if p.high is not None]
    lows = [p.low for p in parts if p.low is not None]
    vols = [p.volume for p in parts]
    volume = None if any(v is None for v in vols) else sum(vols, Decimal(0))
    trades = None if any(p.trades is None for p in parts) else sum(p.trades for p in parts)
    vwap = None
    if volume and all(p.vwap is not None for p in parts):
        with localcontext() as ctx:
            ctx.prec = 40
            vwap = (sum((p.vwap * p.volume for p in parts), Decimal(0)) / volume).quantize(VWAP_Q, ROUND_HALF_EVEN)
    return {"open": opens[0] if opens else parts[0].close, "high": max(highs) if highs else None,
            "low": min(lows) if lows else None, "close": parts[-1].close, "volume": volume, "vwap": vwap,
            "trades": trades}


def _period_key(interval: Interval, day: date):
    if interval.period == "week":
        y, w, _ = day.isocalendar()
        return (y, w)
    if interval.period == "month":
        return (day.year, day.month)
    return day


def period_start_ns(interval: Interval, clock: SessionClock, ns: int) -> int:
    """Start of the bucket containing ``ns``, so a requested range never cuts its first bucket."""
    day = clock.trading_date(ns)
    if interval.intraday:
        s = clock.session_of(ns)
        if s is None:
            return ns
        return s.open_ns + ((ns - s.open_ns) // interval.ns) * interval.ns
    if interval.period == "week":
        day = date.fromordinal(day.toordinal() - day.weekday())
    elif interval.period == "month":
        day = day.replace(day=1)
    local = datetime.combine(day, time(0), tzinfo=clock.zone if clock.cal else timezone.utc)
    return int(local.timestamp()) * NS


def aggregate(bars: list[BaseBar], base: int | str, interval: Interval, clock: SessionClock, from_ns: int,
              to_ns: int, now_ns: int) -> Result:
    """Aggregate ``bars`` (base ``base`` seconds, or 'day') into ``interval`` buckets starting in [from_ns, to_ns)."""
    res = Result([], [])
    buckets: dict = {}
    base_ns = None if base == "day" else int(base) * NS
    for b in sorted(bars, key=lambda x: x.start_ns):
        if base_ns is None:
            s = clock.session(b.day or clock.trading_date(b.start_ns))
        else:
            s = clock.session_of(b.start_ns)
            if s is not None and b.end_ns > s.close_ns:
                res.misaligned += 1
                continue
        if s is None:
            res.excluded_outside_session += 1
            continue
        if interval.intraday:
            if base_ns is None:
                raise ValueError("an intraday interval cannot be built from daily bars")
            start = s.open_ns + ((b.start_ns - s.open_ns) // interval.ns) * interval.ns
            end = min(start + interval.ns, s.close_ns)
            if b.end_ns > end:
                res.misaligned += 1
                continue
            key = start
            expected = -(-(end - start) // base_ns)
        else:
            key, start, end = s.day, s.open_ns, s.close_ns
            expected = 1 if base_ns is None else -(-(s.close_ns - s.open_ns) // base_ns)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = Bucket(start, end, expected)
        bucket.parts.append(b)

    units = []          # (bucket, combined)
    for key in sorted(buckets, key=lambda k: buckets[k].start_ns):
        bk = buckets[key]
        c = _combine(bk.parts)
        if c is not None:
            units.append((bk, c, s_day(key)))

    if interval.period in ("week", "month"):
        # whole periods: the week or month holding to_ns counts all its sessions, not only those before to_ns
        known = clock.known_sessions(from_ns, to_ns + 32 * 86_400 * NS)
        expected_days: dict = {}
        last_key = _period_key(interval, clock.trading_date(to_ns - 1))
        for s in known:
            if _period_key(interval, s.day) <= last_key:
                expected_days.setdefault(_period_key(interval, s.day), []).append(s)
        grouped: dict = {}
        for bk, c, day in units:
            grouped.setdefault(_period_key(interval, day), []).append((bk, c))
        out = []
        for pk in sorted(grouped):
            items = grouped[pk]
            exp = expected_days.get(pk)
            parts = [_Agg(c) for _, c in items]
            c = _combine(parts)
            end = max([bk.end_ns for bk, _ in items] + ([s.close_ns for s in exp] if exp else []))
            partial = any(bk.expected and len(bk.parts) < bk.expected for bk, _ in items) or \
                (exp is not None and len(items) < len(exp))
            out.append((pk, items[0][0].start_ns, end, c, len(items), len(exp) if exp else None, partial))
        expected_keys = [(pk, min(s.open_ns for s in ss), max(s.close_ns for s in ss))
                         for pk, ss in sorted(expected_days.items())]
    else:
        out = [(bk.start_ns, bk.start_ns, bk.end_ns, c, len(bk.parts), bk.expected,
                bool(bk.expected and len(bk.parts) < bk.expected)) for bk, c, _ in units]
        expected_keys = []
        for s in clock.known_sessions(from_ns, to_ns):
            if interval.intraday:
                t = s.open_ns
                while t < s.close_ns:
                    expected_keys.append((t, t, min(t + interval.ns, s.close_ns)))
                    t += interval.ns
            else:
                expected_keys.append((s.open_ns, s.open_ns, s.close_ns))

    out = [o for o in out if from_ns <= o[1] < to_ns]
    for _, start, end, c, n, exp, partial in out:
        res.bars.append({"start_ns": str(start), "end_ns": str(end), "open": dstr(c["open"]),
                         "high": dstr(c["high"]), "low": dstr(c["low"]), "close": dstr(c["close"]),
                         "volume": dstr(c["volume"]), "vwap": dstr(c["vwap"]), "trades": c["trades"],
                         "constituents": n, "expected_constituents": exp, "partial": partial,
                         "complete": end <= now_ns})
    if out:
        have = {o[0] for o in out}
        first, last = out[0][0], out[-1][0]
        run = None
        for key, start, end in expected_keys:
            if not (first < key < last) or end > now_ns:
                continue
            if key in have:
                if run:
                    res.gaps.append(run)
                run = None
                continue
            if run is None:
                run = {"start_ns": str(start), "end_ns": str(end), "missing_buckets": 0, "reason": "NO_BARS"}
            run["end_ns"] = str(end)
            run["missing_buckets"] += 1
        if run:
            res.gaps.append(run)
    return res


def s_day(key):
    return key if isinstance(key, date) else None


@dataclass(frozen=True)
class _Agg:
    """A combined daily unit re-used as a constituent of a weekly or monthly bucket."""
    c: dict

    def __getattr__(self, name):
        try:
            return self.c[name]
        except KeyError:
            raise AttributeError(name) from None
