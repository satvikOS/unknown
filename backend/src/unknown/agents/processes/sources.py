"""Read-only access to the local stores the AI processes read (real data only, ADR-0020).

* the news store ``<UNKNOWN_HOME>/news/news.sqlite`` (written only by the news poller, ADR-0019): filings and
  regulator and central-bank releases with their issuer tags;
* the research store ``<UNKNOWN_HOME>/research/research.sqlite``: the SEC ticker reference (ticker -> CIK) and,
  where a broker history adapter wrote them, daily bars (``mkt_bar``, real rows only: ``fixture = 0``).

Every file is opened read-only and never created. A missing store or table is reported as the reason the data
is unavailable, never replaced by a guess. The agents package does not import the news or chart code
(``test_agents_static``), so the source-to-dataset map below is a copy of ``unknown.news.sources.SOURCES``;
a test requires the two to agree.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

NS = 1_000_000_000
OPEN_END = 2 ** 62          # mkt_bar.known_to_ns of the current version of a bar (charts/sources.py)
STORE_DAILY = "1 day"

# source_id -> (dataset_id, kind, display name); kind is the poller's: filing | regulator | central_bank | exchange
NEWS_SOURCES: dict[str, tuple[str, str, str]] = {
    "sec-filings": ("sec.latest_filings_atom", "filing", "SEC EDGAR"),
    "sec-press": ("sec.press_releases_rss", "regulator", "SEC press"),
    "sec-litigation": ("sec.litigation_releases_rss", "regulator", "SEC litigation"),
    "frb-press": ("frb.press_all_rss", "central_bank", "Federal Reserve"),
    "ecb-press": ("ecb.press_rss", "central_bank", "ECB"),
    "rbi-press": ("rbi.press_releases_rss", "central_bank", "RBI"),
    "sebi": ("sebi.rss", "regulator", "SEBI"),
    "esma": ("esma.news", "regulator", "ESMA"),
    "nse": ("nse.announcements", "exchange", "NSE announcements"),
    "bse": ("bse.announcements", "exchange", "BSE announcements"),
}


def news_path(root: Path) -> Path:
    return Path(root) / "news" / "news.sqlite"


def research_path(root: Path) -> Path:
    return Path(root) / "research" / "research.sqlite"


def iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns // NS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not Path(path).is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


@dataclass(frozen=True)
class NewsItem:
    item_id: int
    source_id: str
    dataset_id: str
    kind: str
    source_name: str
    title: str
    summary: str
    link: str | None
    form: str | None
    items: tuple[str, ...]
    published_ns: int | None
    first_seen_ns: int
    entities: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def citation_id(self) -> str:
        return f"news:{self.item_id}"

    @property
    def ciks(self) -> tuple[str, ...]:
        return tuple(sorted({e["cik"] for e in self.entities}))

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted({t for e in self.entities for t in e.get("tickers", ())}))

    def citation(self) -> dict:
        return {"id": self.citation_id, "kind": self.kind, "source": self.source_name, "source_id": self.source_id,
                "dataset_id": self.dataset_id, "title": self.title, "form": self.form, "link": self.link,
                "published": iso(self.published_ns), "first_seen": iso(self.first_seen_ns),
                "issuers": [{"cik": e["cik"], "name": e.get("name"), "tickers": list(e.get("tickers", ()))} for e in self.entities],
                "open": {"api": f"/api/news/item?id={self.item_id}", "workspace": "news"}}


class SourceUnavailable(RuntimeError):
    """A store or table the process needs is missing; the message is the reason shown to the owner."""


def resolve_tickers(research_db: Path, tickers: Iterable[str]) -> tuple[dict[str, dict], str | None]:
    """ticker -> {cik, name} from the newest SEC ticker snapshot in the research store. Tickers the snapshot does
    not list are absent from the map; the second value is the reason when the reference is unavailable."""
    tickers = sorted(set(tickers))
    conn = _open_ro(research_db)
    if conn is None:
        return {}, f"research store {research_db} is missing (no SEC ticker reference)"
    try:
        if not _has_table(conn, "ref_sec_ticker"):
            return {}, "the research store has no SEC ticker reference (ref_sec_ticker); run the SEC reference ingest"
        snap = conn.execute("SELECT MAX(snapshot_id) FROM ref_sec_ticker").fetchone()[0]
        if snap is None or not tickers:
            return {}, None if tickers == [] else "the SEC ticker reference is empty"
        rows = conn.execute(f"SELECT ticker, cik, name FROM ref_sec_ticker WHERE snapshot_id = ? AND ticker IN "
                            f"({','.join('?' * len(tickers))}) ORDER BY ticker, cik", (snap, *tickers)).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["ticker"], {"cik": str(r["cik"]), "name": r["name"]})
    return out, None


def _entities(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    for e in conn.execute(f"SELECT item_id, value, name, role, tickers_json FROM news_entity WHERE scheme='SEC_CIK' AND item_id IN "
                          f"({','.join('?' * len(ids))}) ORDER BY item_id, value", ids):
        try:
            tickers = tuple(t for t in json.loads(e["tickers_json"] or "[]") if isinstance(t, str))
        except ValueError:
            tickers = ()
        lst = out.setdefault(e["item_id"], [])
        if not any(x["cik"] == e["value"] for x in lst):
            lst.append({"cik": e["value"], "name": e["name"], "role": e["role"], "tickers": tickers})
    return out


def _item(r: sqlite3.Row, ents: list[dict]) -> NewsItem:
    dataset, kind, name = NEWS_SOURCES.get(r["source_id"], (None, r["kind"], r["source_id"]))
    try:
        items = tuple(str(i) for i in json.loads(r["items_json"] or "[]"))
    except ValueError:
        items = ()
    return NewsItem(r["item_id"], r["source_id"], dataset or "", kind, name, r["title"], r["summary"] or "", r["link"],
                    r["form"], items, r["published_ns"], r["first_seen_ns"], tuple(ents))


def news_items(news_db: Path, *, since_ns: int | None = None, until_ns: int | None = None, after_id: int | None = None,
               kinds: Iterable[str] | None = None, forms: Iterable[str] | None = None, ciks: Iterable[str] | None = None,
               item_ids: Iterable[int] | None = None, limit: int = 200) -> list[NewsItem]:
    """Stored items (syndicated copies excluded), oldest first: published at or after ``since_ns`` (the received time
    when the feed states no publication time) and received by ``until_ns``. ``forms`` matches amendments too (8-K/A).
    Raises SourceUnavailable when the news store is missing."""
    conn = _open_ro(news_db)
    if conn is None:
        raise SourceUnavailable(f"the news store {news_db} is missing (start the news poller)")
    where, args = ["dup_of IS NULL"], []
    if since_ns is not None:   # published in the window (received time when the feed states none): a backfilled
        where.append("COALESCE(published_ns, first_seen_ns) >= ?")   # old release is not an overnight item
        args.append(since_ns)
    if until_ns is not None:
        where.append("first_seen_ns <= ?")
        args.append(until_ns)
    if after_id is not None:
        where.append("item_id > ?")
        args.append(after_id)
    if item_ids is not None:
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        where.append(f"item_id IN ({','.join('?' * len(ids))})")
        args += ids
    if kinds is not None:
        sources = sorted(s for s, (_, k, _) in NEWS_SOURCES.items() if k in set(kinds))
        if not sources:
            return []
        where.append(f"source_id IN ({','.join('?' * len(sources))})")
        args += sources
    if forms is not None:
        fl = sorted(set(forms))
        where.append("(" + " OR ".join(["form = ? OR form = ?"] * len(fl)) + ")")
        for f in fl:
            args += [f, f + "/A"]
    if ciks is not None:
        cl = sorted(set(ciks))
        if not cl:
            return []
        where.append(f"item_id IN (SELECT item_id FROM news_entity WHERE scheme='SEC_CIK' AND value IN ({','.join('?' * len(cl))}))")
        args += cl
    try:
        rows = conn.execute("SELECT * FROM news_item WHERE " + " AND ".join(where) + " ORDER BY item_id LIMIT ?",
                            (*args, int(limit))).fetchall()
        ents = _entities(conn, [r["item_id"] for r in rows])
    except sqlite3.OperationalError as exc:
        raise SourceUnavailable(f"the news store is unreadable: {exc}") from None
    finally:
        conn.close()
    return [_item(r, ents.get(r["item_id"], [])) for r in rows]


def max_item_id(news_db: Path) -> int | None:
    conn = _open_ro(news_db)
    if conn is None:
        return None
    try:
        return conn.execute("SELECT MAX(item_id) FROM news_item").fetchone()[0] or 0
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


@dataclass(frozen=True)
class DailyMove:
    symbol: str
    instrument_id: str
    provider: str
    date: str
    close: str
    prev_date: str
    prev_close: str
    change: str
    change_pct: str
    currency: str | None
    adjustment: str
    available_ns: int

    @property
    def citation_id(self) -> str:
        return f"bar:{self.provider}:{self.instrument_id}:{self.date}"

    def citation(self) -> dict:
        return {"id": self.citation_id, "kind": "daily_bar", "source": f"stored {self.provider} daily bars",
                "title": f"{self.symbol} daily bars {self.prev_date} and {self.date}", "symbol": self.symbol,
                "instrument_id": self.instrument_id, "adjustment": self.adjustment, "available": iso(self.available_ns),
                "open": {"workspace": "chart", "security": self.symbol}}

    def line(self) -> str:
        cur = f" {self.currency}" if self.currency else ""
        return (f"{self.symbol}: close {self.close}{cur} on {self.date} vs {self.prev_close}{cur} on {self.prev_date}, "
                f"change {self.change}{cur} ({self.change_pct}%), {self.adjustment} bars from {self.provider}")


def _dec(text) -> Decimal | None:
    try:
        return Decimal(str(text)) if text is not None and str(text).strip() else None
    except InvalidOperation:
        return None


def daily_moves(research_db: Path, instruments: dict[str, str], *, until_ns: int,
                since_ns: int | None = None) -> tuple[list[DailyMove], dict[str, str]]:
    """The latest real daily move per symbol (``instruments``: symbol -> internal instrument id): the last bar
    available by ``until_ns`` against the one before it, or against the last bar before ``since_ns`` when given
    (the move over a window). Returns the moves and, per symbol without one, the reason."""
    reasons: dict[str, str] = {}
    moves: list[DailyMove] = []
    if not instruments:
        return moves, reasons
    conn = _open_ro(research_db)
    if conn is None:
        return moves, {s: f"research store {research_db} is missing (no stored bars)" for s in instruments}
    try:
        if not _has_table(conn, "mkt_bar"):
            return moves, {s: "no stored daily bars: the research store has no mkt_bar table (no broker history has been "
                              "stored)" for s in instruments}
        for symbol, iid in sorted(instruments.items()):
            if not iid:
                reasons[symbol] = "no internal instrument id for this symbol in the core's instrument master"
                continue
            base = ("FROM mkt_bar WHERE instrument_id = ? AND bar_size = ? AND fixture = 0 AND known_to_ns = ? "
                    "AND available_time_ns <= ?")
            last = conn.execute(f"SELECT * {base} ORDER BY bar_start_ns DESC LIMIT 1", (iid, STORE_DAILY, OPEN_END, until_ns)).fetchone()
            if last is None:
                reasons[symbol] = "no stored real daily bar for this instrument"
                continue
            prev_sql = f"SELECT * {base} AND provider = ? AND bar_start_ns < ? ORDER BY bar_start_ns DESC LIMIT 1"
            bound = last["bar_start_ns"] if since_ns is None else min(last["bar_start_ns"], since_ns)
            prev = conn.execute(prev_sql, (iid, STORE_DAILY, OPEN_END, until_ns, last["provider"], bound)).fetchone()
            c1, c0 = _dec(last["close"]), _dec(prev["close"]) if prev is not None else None
            if prev is None or c1 is None or c0 is None or c0 == 0:
                reasons[symbol] = "fewer than two stored real daily closes for this instrument"
                continue
            change = c1 - c0
            pct = (change * 100 / c0).quantize(Decimal("0.01"))
            moves.append(DailyMove(symbol, iid, last["provider"], str(last["bar_date"])[:10], str(c1), str(prev["bar_date"])[:10],
                                   str(c0), str(change), str(pct), last["currency"], last["adjustment"] or "unknown",
                                   last["available_time_ns"]))
    except sqlite3.OperationalError as exc:
        return [], {s: f"the bar store is unreadable: {exc}" for s in instruments}
    finally:
        conn.close()
    return moves, reasons
