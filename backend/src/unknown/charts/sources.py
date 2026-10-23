"""Bar source adapters behind one interface (no adapter is privileged; see the package docstring).

* :class:`StoredBarsSource`: the research point-in-time bar store (``research/research.sqlite``, table
  ``mkt_bar``). One source per provider found there, labelled with that provider id: broker history adapters (for
  example ``unknown.broker.ibkr.history``) write this table, so their bars arrive here without importing them.
* :class:`AlpacaBarsSource`: Alpaca Market Data API v2 historical stock bars over REST
  (``GET https://data.alpaca.markets/v2/stocks/{symbol}/bars``; docs.alpaca.markets, checked 2026-10-23), enabled
  only when ``<UNKNOWN_HOME>/secrets/alpaca.json`` holds ``key_id`` and ``secret_key``.
* :class:`SyntheticBarsSource`: deterministic generated bars for demos, always labelled SYNTHETIC.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import random
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol

from .aggregate import BaseBar
from .intervals import NS, Interval, compatible_bases
from .sessions import SessionClock

DATA_TYPES = ("REALTIME", "DELAYED", "FROZEN", "SYNTHETIC")
OPEN_END = 2 ** 62
SYNTHETIC_LABEL = "SYNTHETIC: generated demo data, not market data"
UNADJUSTED_NOTE = ("unadjusted: no corporate-action data is applied, so splits and dividends show as price jumps")
# The bar store's bar_size vocabulary (written by broker history adapters) in seconds.
STORE_BAR_SECONDS = {"1 min": 60, "2 mins": 120, "3 mins": 180, "5 mins": 300, "10 mins": 600, "15 mins": 900,
                     "20 mins": 1200, "30 mins": 1800, "1 hour": 3600}
STORE_DAILY = "1 day"


class SourceError(RuntimeError):
    """The source is configured but could not answer (network, auth, malformed reply)."""


@dataclass
class SeriesRequest:
    key: str
    symbol: str
    mic: str | None
    instrument_ids: list[str]
    interval: Interval
    from_ns: int
    to_ns: int
    now_ns: int
    clock: SessionClock


@dataclass
class Availability:
    source_id: str
    kind: str
    real: bool
    configured: bool
    covers: bool
    reason: str

    def as_json(self) -> dict:
        return {"source": self.source_id, "kind": self.kind, "real": self.real, "configured": self.configured,
                "covers": self.covers, "reason": self.reason}


@dataclass
class SourceBars:
    source_id: str
    data_type: str
    data_type_reason: str
    base: int | str                  # seconds, or 'day'
    bars: list[BaseBar]
    currency: str | None
    adjustment: str
    adjustment_note: str
    notes: list[str] = field(default_factory=list)
    recorded_gaps: list[dict] = field(default_factory=list)
    label: str | None = None
    detail: dict = field(default_factory=dict)


class BarsSource(Protocol):
    source_id: str
    kind: str
    real: bool

    def availability(self, req: SeriesRequest) -> Availability: ...

    def fetch(self, req: SeriesRequest) -> SourceBars: ...


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v)) if not isinstance(v, Decimal) else v
    except InvalidOperation:
        return None


def _pick_base(req: SeriesRequest, available: list[int]) -> int | None:
    ok = compatible_bases(req.interval, req.clock.open_offsets())
    return next((b for b in ok if b in available), None)


# ============================================================================ research bar store
def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ids_clause(ids: list[str]) -> str:
    return ",".join("?" * len(ids))


def store_providers(db: Path, instrument_ids: list[str]) -> list[str]:
    """Providers with current, non-fixture bars for any of ``instrument_ids``."""
    if not db.exists() or not instrument_ids:
        return []
    try:
        conn = _open_ro(db)
    except sqlite3.Error:
        return []
    try:
        return [r[0] for r in conn.execute(
            f"SELECT DISTINCT provider FROM mkt_bar WHERE instrument_id IN ({_ids_clause(instrument_ids)}) "
            "AND fixture=0 AND known_to_ns=? ORDER BY provider", (*instrument_ids, OPEN_END))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _store_day(text) -> date | None:
    if not text:
        return None
    s = str(text).strip()[:10]
    try:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8])) if s.isdigit() else date.fromisoformat(s)
    except ValueError:
        return None


class StoredBarsSource:
    kind = "bar-store"
    real = True

    def __init__(self, db: Path, provider: str):
        self.db, self.provider = db, provider
        self.source_id = provider.upper()

    def availability(self, req: SeriesRequest) -> Availability:
        has = self.provider in store_providers(self.db, req.instrument_ids)
        return Availability(self.source_id, self.kind, True, has, has,
                            f"{'stored' if has else 'no stored'} {self.provider} bars for {', '.join(req.instrument_ids)} "
                            f"in {self.db.name}")

    def fetch(self, req: SeriesRequest) -> SourceBars:
        ids = req.instrument_ids
        conn = _open_ro(self.db)
        try:
            base_sql = (f"FROM mkt_bar WHERE instrument_id IN ({_ids_clause(ids)}) AND provider=? AND fixture=0 "
                        "AND known_to_ns=? AND available_time_ns<=? AND bar_start_ns>=? AND bar_start_ns<?")
            args = (*ids, self.provider, OPEN_END, req.now_ns, req.from_ns, req.to_ns)
            series = [dict(r) for r in conn.execute(
                f"SELECT bar_size, what_to_show, use_rth, COUNT(*) AS n {base_sql} GROUP BY 1, 2, 3", args)]
            pref = sorted(series, key=lambda s: (s["what_to_show"] != "TRADES", s["use_rth"] != 1))
            chosen, base = None, None
            if not req.interval.intraday:
                chosen = next((s for s in pref if s["bar_size"] == STORE_DAILY), None)
                base = "day" if chosen else None
            if chosen is None:
                sizes = [STORE_BAR_SECONDS[s["bar_size"]] for s in pref if s["bar_size"] in STORE_BAR_SECONDS]
                b = _pick_base(req, sizes)
                if b is not None:
                    chosen = next(s for s in pref if STORE_BAR_SECONDS.get(s["bar_size"]) == b)
                    base = b
            if chosen is None:
                return SourceBars(self.source_id, "DELAYED", "stored history", req.interval.seconds or "day", [],
                                  None, "unknown", "", notes=[
                                      "no stored series can build this interval (stored bar sizes: "
                                      f"{sorted({s['bar_size'] for s in series}) or 'none in range'})"])
            rows = conn.execute(f"SELECT * {base_sql} AND bar_size=? AND what_to_show=? AND use_rth=? "
                                "ORDER BY bar_start_ns", (*args, chosen["bar_size"], chosen["what_to_show"],
                                                          chosen["use_rth"])).fetchall()
            gaps = [dict(g) for g in conn.execute(
                f"SELECT start_ns, end_ns, reason, detail FROM mkt_bar_gap WHERE instrument_id IN ({_ids_clause(ids)}) "
                "AND provider=? AND bar_size=? AND fixture=0 AND (end_ns IS NULL OR end_ns>?) AND "
                "(start_ns IS NULL OR start_ns<?)", (*ids, self.provider, chosen["bar_size"], req.from_ns,
                                                    req.to_ns))]
        except sqlite3.OperationalError as exc:
            raise SourceError(f"bar store unreadable: {exc}") from None
        finally:
            conn.close()
        bars = [BaseBar(r["bar_start_ns"], r["bar_end_ns"], _dec(r["open"]), _dec(r["high"]), _dec(r["low"]),
                        _dec(r["close"]), _dec(r["volume"]), _dec(r["wap"]), r["bar_count"],
                        _store_day(r["bar_date"]) if base == "day" else None) for r in rows]
        adjustments = sorted({r["adjustment"] for r in rows}) or ["unknown"]
        currencies = sorted({r["currency"] for r in rows})
        adjustment = adjustments[0] if len(adjustments) == 1 else "mixed: " + ", ".join(adjustments)
        notes = [] if chosen["what_to_show"] == "TRADES" else [f"bars are {chosen['what_to_show']}, not TRADES"]
        if chosen["use_rth"] != 1:
            notes.append("stored bars include extended hours; only regular-session bars are charted")
        return SourceBars(self.source_id, "DELAYED",
                          "stored history: each bar becomes available only after it closes (point-in-time store)",
                          base, bars, currencies[0] if len(currencies) == 1 else None, adjustment,
                          UNADJUSTED_NOTE if adjustment == "unadjusted" else (rows[0]["adjustment_note"] if rows else ""),
                          notes=notes, recorded_gaps=gaps,
                          detail={"bar_size": chosen["bar_size"], "what_to_show": chosen["what_to_show"],
                                  "use_rth": bool(chosen["use_rth"]), "bar_spec_version":
                                      rows[0]["bar_spec_version"] if rows else None})


# ============================================================================ Alpaca REST
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
ALPACA_MICS = frozenset({"XNYS", "XNAS", "ARCX", "BATS", "XASE", "IEXG"})
ALPACA_MAX_PAGES = 20
HttpGet = Callable[[str, dict], "tuple[int, bytes]"]


def _urllib_get(url: str, headers: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:      # noqa: S310 (fixed https host)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _rfc3339(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text: str) -> int:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return calendar.timegm(dt.utctimetuple()) * NS + dt.microsecond * 1000


class AlpacaBarsSource:
    source_id = "ALPACA"
    kind = "rest"
    real = True

    def __init__(self, secrets_file: Path, http_get: HttpGet | None = None):
        self.secrets_file = secrets_file
        self.http_get = http_get or _urllib_get

    def _config(self) -> dict | None:
        try:
            d = json.loads(self.secrets_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("key_id") and d.get("secret_key") else None

    def availability(self, req: SeriesRequest) -> Availability:
        cfg = self._config()
        if cfg is None:
            return Availability(self.source_id, self.kind, True, False, False,
                                f"no Alpaca keys ({self.secrets_file.name} with key_id and secret_key)")
        covers = req.mic in ALPACA_MICS
        return Availability(self.source_id, self.kind, True, True, covers,
                            "Alpaca covers US equity venues" if covers else
                            f"Alpaca stock bars cover US venues only, not {req.mic or 'a key without a MIC'}")

    def fetch(self, req: SeriesRequest) -> SourceBars:
        cfg = self._config()
        if cfg is None:
            raise SourceError("Alpaca keys are not configured")
        feed = str(cfg.get("feed") or "iex")
        data_type = str(cfg.get("data_type") or ("REALTIME" if feed == "iex" else "DELAYED")).upper()
        if data_type not in DATA_TYPES or data_type == "SYNTHETIC":
            raise SourceError(f"alpaca.json data_type must be REALTIME, DELAYED or FROZEN, not {data_type}")
        if req.interval.intraday:
            base = _pick_base(req, [3600, 1800, 900, 600, 300, 180, 120, 60])
            timeframe = f"{base // 3600}Hour" if base % 3600 == 0 else f"{base // 60}Min"
        else:
            base, timeframe = "day", "1Day"
        headers = {"APCA-API-KEY-ID": cfg["key_id"], "APCA-API-SECRET-KEY": cfg["secret_key"],
                   "Accept": "application/json"}
        params = {"timeframe": timeframe, "start": _rfc3339(req.from_ns), "end": _rfc3339(req.to_ns - NS),
                  "limit": "10000", "adjustment": "raw", "feed": feed, "sort": "asc"}
        bars: list[BaseBar] = []
        currency, token, pages = None, None, 0
        while True:
            q = dict(params, **({"page_token": token} if token else {}))
            url = ALPACA_BARS_URL.format(symbol=urllib.parse.quote(req.symbol, safe="")) + "?" + \
                urllib.parse.urlencode(q)
            status, body = self.http_get(url, headers)
            if status != 200:
                msg = body[:200].decode("utf-8", "replace") if body else ""
                raise SourceError(f"Alpaca bars HTTP {status}: {msg}")
            try:
                d = json.loads(body, parse_float=Decimal)
            except ValueError:
                raise SourceError("Alpaca bars reply is not JSON") from None
            currency = d.get("currency") or currency
            for b in d.get("bars") or []:
                start = _parse_ts(b["t"])
                end = start + (86_400 * NS if base == "day" else base * NS)
                bars.append(BaseBar(start, end, _dec(b.get("o")), _dec(b.get("h")), _dec(b.get("l")), _dec(b.get("c")),
                                    _dec(b.get("v")), _dec(b.get("vw")), b.get("n"),
                                    req.clock.trading_date(start) if base == "day" else None))
            token, pages = d.get("next_page_token"), pages + 1
            if not token or pages >= ALPACA_MAX_PAGES:
                break
        notes = [f"Alpaca feed '{feed}'" + (": IEX only (one venue, not consolidated)" if feed == "iex" else "")]
        if token:
            notes.append(f"stopped after {ALPACA_MAX_PAGES} pages; narrow the range for the rest")
        return SourceBars(self.source_id, data_type, f"Alpaca {feed} feed (alpaca.json 'data_type' overrides)",
                          base, bars, currency or "USD", "unadjusted", UNADJUSTED_NOTE + " (Alpaca adjustment=raw)",
                          notes=notes, detail={"timeframe": timeframe, "feed": feed, "pages": pages})


# ============================================================================ synthetic (demo only)
SYNTHETIC_ANCHOR = date(2010, 1, 4)


class SyntheticBarsSource:
    source_id = "SYNTHETIC"
    kind = "synthetic"
    real = False

    def availability(self, req: SeriesRequest) -> Availability:
        return Availability(self.source_id, self.kind, False, False, True,
                            "generated demo bars; used only when no real source is configured for this security")

    def fetch(self, req: SeriesRequest) -> SourceBars:
        clock = req.clock
        seed = int.from_bytes(hashlib.sha256(req.key.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        price = 2000 + seed % 48000                        # integer cents
        first, last = clock.trading_date(req.from_ns), clock.trading_date(min(req.to_ns, req.now_ns))
        base = "day" if not req.interval.intraday else _pick_base(req, [3600, 1800, 900, 600, 300, 180, 120, 60])
        cents = Decimal("0.01")
        bars: list[BaseBar] = []
        d = SYNTHETIC_ANCHOR
        while d <= last:
            step, gap = rng.randint(-190, 200), rng.randint(-50, 50)
            up, dn, vol = rng.randint(0, 120), rng.randint(0, 120), rng.randint(200_000, 4_000_000)
            o = max(100, price * (10_000 + gap) // 10_000)
            c = max(100, price * (10_000 + step) // 10_000)
            h, lo = max(o, c) * (10_000 + up) // 10_000, min(o, c) * (10_000 - dn) // 10_000
            price = c
            s = clock.session(d) if d >= first else None
            if s is not None and s.open_ns <= req.now_ns:
                if base == "day":
                    if s.close_ns <= req.now_ns:
                        bars.append(BaseBar(s.open_ns, s.close_ns, Decimal(o) * cents, Decimal(h) * cents,
                                            Decimal(lo) * cents, Decimal(c) * cents, Decimal(vol), None, None, d))
                else:
                    bars.extend(self._intraday(seed, d, s, base, o, h, lo, c, vol, req.now_ns))
            d += timedelta(days=1)
        try:
            from .sessions import _registry
            currency = _registry().venue(req.mic).currency if req.mic else None
        except (KeyError, LookupError, ValueError, OSError):
            currency = None
        return SourceBars(self.source_id, "SYNTHETIC", SYNTHETIC_LABEL, base, bars, currency,
                          "not applicable (synthetic)", "synthetic bars have no corporate actions",
                          notes=[SYNTHETIC_LABEL], label=SYNTHETIC_LABEL, detail={"seed_basis": "sha256(key)"})

    @staticmethod
    def _intraday(seed, d, s, base, o, h, lo, c, vol, now_ns):
        rng = random.Random(seed ^ d.toordinal())
        step_ns = base * NS
        n = max(1, -(-(s.close_ns - s.open_ns) // step_ns))
        k = max(1, o * 12 // 10_000)
        walk = [0]
        for _ in range(n):
            walk.append(walk[-1] + rng.randint(-k, k))
        path = [min(h, max(lo, o + (c - o) * i // n + walk[i] - walk[n] * i // n)) for i in range(n + 1)]
        cents = Decimal("0.01")
        out = []
        for i in range(n):
            start = s.open_ns + i * step_ns
            end = min(start + step_ns, s.close_ns)
            if end > now_ns:
                break
            po, pc = path[i], path[i + 1]
            ph = min(h, max(po, pc) + rng.randint(0, k))
            pl = max(lo, min(po, pc) - rng.randint(0, k))
            out.append(BaseBar(start, end, Decimal(po) * cents, Decimal(ph) * cents, Decimal(pl) * cents,
                               Decimal(pc) * cents, Decimal(vol // n + rng.randint(0, max(1, vol // (4 * n)))),
                               None, None))
        return out
