"""Chart bars service: pick a source, aggregate, label (see the package docstring for the source rules)."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from .aggregate import FORMULA, aggregate, period_start_ns
from .intervals import NS, parse_interval
from .sessions import SessionClock
from .sources import AlpacaBarsSource, BarsSource, SeriesRequest, SourceError, StoredBarsSource, store_providers

MAX_BARS = 5000
DAY_NS = 86_400 * NS
RESEARCH_DB = ("research", "research.sqlite")
KIND_ORDER = {"bar-store": 0, "rest": 1}


def parse_key(key: str) -> tuple[str, str | None]:
    """``<SYMBOL>.<MIC>`` -> (symbol, MIC); a bare symbol or core instrument id -> (key, None). Mirrors link.ts."""
    dot = key.rfind(".")
    if 0 < dot < len(key) - 1 and re.fullmatch(r"X[A-Z0-9]{3}", key[dot + 1:]):
        return key[:dot], key[dot + 1:]
    return key, None


def instrument_ids(conn: sqlite3.Connection | None, key: str, symbol: str, mic: str | None) -> list[str]:
    """The key itself plus core instruments listed with this symbol on this venue (read-only core connection)."""
    ids = [key]
    if conn is not None and mic:
        try:
            for r in conn.execute("SELECT instrument_id, body FROM instruments WHERE symbol=?", (symbol,)):
                if json.loads(r[1]).get("venue_mic") == mic and r[0] not in ids:
                    ids.append(r[0])
        except (sqlite3.Error, ValueError, TypeError):
            pass
    return ids


def source_order(root: Path) -> list[str]:
    try:
        d = json.loads((root / "config" / "charts.json").read_text(encoding="utf-8"))
        return [str(s).upper() for s in d.get("source_order", [])]
    except (OSError, ValueError, AttributeError):
        return []


def build_sources(root: Path, secrets: Path, req: SeriesRequest, http_get=None) -> list[BarsSource]:
    db = root.joinpath(*RESEARCH_DB)
    real: list[BarsSource] = [StoredBarsSource(db, p) for p in store_providers(db, req.instrument_ids)]
    real.append(AlpacaBarsSource(secrets / "alpaca.json", http_get))
    order = source_order(root)
    rank = {s: i for i, s in enumerate(order)}
    real.sort(key=lambda s: (rank.get(s.source_id, len(order)), KIND_ORDER.get(s.kind, 9), s.source_id))
    return real


def _int(q: dict, name: str) -> int | None:
    v = q.get(name)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{name} must be an integer (UTC nanoseconds)") from None


def make_request(q: dict, conn, now_ns: int) -> SeriesRequest:
    key = (q.get("key") or "").strip()
    if not key or len(key) > 64:
        raise ValueError("key is required: <SYMBOL>.<MIC> (e.g. AAPL.XNAS) or a core instrument id")
    interval = parse_interval(q.get("interval"))
    to_ns = _int(q, "to_ns") or now_ns + 1
    from_ns = _int(q, "from_ns")
    if from_ns is None:
        from_ns = to_ns - interval.default_span_days * DAY_NS
    if from_ns >= to_ns:
        raise ValueError("from_ns must be before to_ns")
    if to_ns - from_ns > interval.max_span_days * DAY_NS:
        raise ValueError(f"interval {interval.code} allows at most {interval.max_span_days} days per request")
    symbol, mic = parse_key(key)
    clock = SessionClock(mic)
    aligned = period_start_ns(interval, clock, from_ns)
    return SeriesRequest(key, symbol, mic, instrument_ids(conn, key, symbol, mic), interval, min(aligned, from_ns),
                         to_ns, now_ns, clock)


def sources(ctx, q: dict, conn, now_ns: int | None = None, http_get=None) -> dict:
    req = make_request(q, conn, now_ns or time.time_ns())
    root = Path(ctx.paths.root)
    return {"key": req.key, "sources": [s.availability(req).as_json() for s in
                                         build_sources(root, ctx.paths.secrets, req, http_get)]}


def bars(ctx, q: dict, conn, now_ns: int | None = None, http_get=None) -> dict:
    now = now_ns or time.time_ns()
    req = make_request(q, conn, now)
    root = Path(ctx.paths.root)
    srcs = build_sources(root, ctx.paths.secrets, req, http_get)
    avail = {s.source_id: s.availability(req) for s in srcs}
    considered = {sid: a.as_json() for sid, a in avail.items()}
    real = [s for s in srcs if avail[s.source_id].configured and avail[s.source_id].covers]
    wanted = (q.get("source") or "").upper() or None
    if wanted:
        pick = [s for s in srcs if s.source_id == wanted]
        if not pick:
            raise ValueError(f"unknown source {wanted}; known: {', '.join(avail)}")
        if not (avail[wanted].configured and avail[wanted].covers):
            raise ValueError(f"source {wanted} cannot serve {req.key}: {avail[wanted].reason}")
        candidates = pick
    else:
        candidates = real

    chosen = None
    for s in candidates:
        try:
            got = s.fetch(req)
        except (SourceError, OSError) as exc:
            considered[s.source_id]["error"] = str(exc)[:300]
            continue
        considered[s.source_id]["bars_received"] = len(got.bars)
        if chosen is None or (got.bars and not chosen.bars):
            chosen = got
        if got.bars:
            break

    out = {"key": req.key, "symbol": req.symbol, "mic": req.mic, "instrument_ids": req.instrument_ids,
           "interval": req.interval.code, "from_ns": str(req.from_ns), "to_ns": str(req.to_ns),
           "now_ns": str(now), "sources_considered": list(considered.values()), "formula": FORMULA,
           "units": {"price": "quote currency per share", "volume": "as reported by the source", "times": "UTC ns"},
           "session": {"calendar_id": req.clock.calendar_id, "timezone": req.clock.timezone,
                       "rule": "regular session only (bars outside it are excluded)"}}
    if chosen is None:
        # Never a substitute series (ADR-0020): no source covers it (NO_SOURCE) or every source failed (ERROR).
        reason = "ERROR" if candidates else "NO_SOURCE"
        out.update({"available": False, "bars": [], "gaps": [], "count": 0, "source": None, "data_type": None,
                    "reason": reason,
                    "missing_reason": ("every configured source failed; see sources_considered" if candidates else
                                       f"no configured bar source covers {req.key}; see sources_considered")})
        out["session"]["notes"] = req.clock.finish_notes()
        return out
    agg = aggregate(chosen.bars, chosen.base, req.interval, req.clock, req.from_ns, req.to_ns, now)
    rows = agg.bars
    truncated = len(rows) > MAX_BARS
    rows = rows[-MAX_BARS:]
    out.update({
        "available": bool(rows), "source": chosen.source_id, "data_type": chosen.data_type,
        "data_type_reason": chosen.data_type_reason, "label": chosen.label, "currency": chosen.currency,
        "adjustment": chosen.adjustment, "adjustment_note": chosen.adjustment_note,
        "base_interval": chosen.base if chosen.base == "day" else f"{chosen.base}s", "source_detail": chosen.detail,
        "bars": rows, "count": len(rows), "truncated": truncated, "gaps": agg.gaps,
        "recorded_gaps": chosen.recorded_gaps, "notes": chosen.notes,
        "excluded_outside_session": agg.excluded_outside_session, "misaligned_excluded": agg.misaligned,
        "live": live_hint(req, rows, now)})
    if truncated:
        out["truncation_note"] = f"more than {MAX_BARS} bars: these are the latest {MAX_BARS}; narrow the range"
    if not rows:
        out["reason"] = "NO_DATA"
        out["missing_reason"] = (f"{chosen.source_id} returned no bars for {req.key} {req.interval.code} in this "
                                 "range; nothing is substituted")
    out["session"]["notes"] = req.clock.finish_notes()
    return out


def live_hint(req: SeriesRequest, rows: list[dict], now: int) -> dict:
    """What a client needs to roll the last bar forward from stream trades without guessing sessions."""
    hint = {"interval_ns": str(req.interval.ns) if req.interval.intraday else None,
            "last_start_ns": rows[-1]["start_ns"] if rows else None,
            "last_end_ns": rows[-1]["end_ns"] if rows else None, "session_open_ns": None, "session_close_ns": None}
    s = req.clock.session_of(now) or (req.clock.session_of(int(rows[-1]["start_ns"])) if rows else None)
    if s is not None:
        hint["session_open_ns"], hint["session_close_ns"] = str(s.open_ns), str(s.close_ns)
    return hint
