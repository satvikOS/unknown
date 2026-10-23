"""Issuer tagging with point-in-time identifiers (CAP-NEWS-06).

Identifiers come from the research store (``UNKNOWN_HOME/research/research.sqlite``, opened read-only):
  * SEC company ticker snapshots (``ref_sec_ticker``): the snapshot received at or before the item's first-seen
    time maps CIK <-> tickers and names. A snapshot received later is never used, so a past item never shows a
    ticker the system did not know then. Before the first snapshot an item carries its CIK and the feed's name only.
  * OpenFIGI outcomes (``openfigi.mapping`` observations) when present: a MAPPED ticker job available at that time
    adds its FIGI. MULTIPLE_CANDIDATES, NO_MATCH and ERROR add nothing.

Tagging methods: ``edgar_feed`` (the filing's own entity, confidence 1.0) and ``name_match`` (a company name of at
least two words from the snapshot appears as whole words in a release headline or summary; confidence 0.6, role
``mention``, the matched words kept as evidence). Single-word names are not matched (too many false hits).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..data.sources import sec_tickers

SUFFIXES = frozenset("INC INCORPORATED CORP CORPORATION CO COMPANY LTD LIMITED PLC LLC LP SA NV AG SE".split())
# A name made only of these words ("FIRST BANCORP", "NATIONAL BANK HOLDINGS") is too generic to match in text.
GENERIC = SUFFIXES | frozenset("""THE OF AND A AN BANCORP BANCSHARES BANK BANKS FINANCIAL HOLDINGS HOLDING GROUP TRUST CAPITAL
FIRST NATIONAL AMERICAN AMERICA UNITED FEDERAL RESERVE SECURITIES EXCHANGE COMMISSION BOARD FUND FUNDS PARTNERS
INVESTMENT INVESTMENTS INTERNATIONAL GLOBAL SERVICES SYSTEMS TECHNOLOGIES ENERGY RESOURCES INDUSTRIES CENTRAL
SAVINGS COMMUNITY STATE STATES US USA NEW""".split())
MAX_NGRAM = 8


def _norm(text: str) -> list[str]:
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).split()


class IssuerIndex:
    """CIK, ticker and name lookups as of one snapshot (``empty()`` when the research store has none)."""

    def __init__(self, conn: sqlite3.Connection | None, snapshot: dict | None, rows: list[tuple[int, str, str, str | None]]):
        self.conn, self.snapshot = conn, snapshot
        self.by_cik: dict[int, dict] = {}
        self.by_ticker: dict[str, int] = {}
        self.names: dict[tuple[str, ...], int] = {}
        for cik, name, ticker, exchange in rows:
            e = self.by_cik.setdefault(cik, {"name": name, "tickers": []})
            e["tickers"].append({"ticker": ticker, "exchange": exchange})
            self.by_ticker.setdefault(ticker.upper(), cik)
            toks = _norm(name)
            stripped = list(toks)
            while stripped and stripped[-1] in SUFFIXES:
                stripped.pop()
            for key in {tuple(toks), tuple(stripped)}:
                if len(key) >= 2 and len(" ".join(key)) >= 8 and not set(key) <= GENERIC:
                    self.names.setdefault(key, cik)
        self._figi: dict[str, str | None] = {}

    @classmethod
    def empty(cls) -> "IssuerIndex":
        return cls(None, None, [])

    @classmethod
    def open(cls, research_db: Path, as_of_ns: int) -> "IssuerIndex":
        if not Path(research_db).is_file():
            return cls.empty()
        conn = sqlite3.connect(f"file:{research_db}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        snap = sec_tickers.snapshot_at(conn, as_of_ns)
        if snap is None:
            return cls(conn, None, [])
        rows = [(r["cik"], r["name"], r["ticker"], r["exchange"]) for r in conn.execute(
            "SELECT cik, name, ticker, exchange FROM ref_sec_ticker WHERE snapshot_id=? ORDER BY ticker",
            (snap["rows_snapshot_id"],))]
        return cls(conn, snap, rows)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()

    @property
    def as_of_ns(self) -> int | None:
        return self.snapshot["receipt_time_ns"] if self.snapshot else None

    @property
    def source(self) -> str | None:
        return f"{sec_tickers.DATASET} snapshot {self.snapshot['snapshot_id']}" if self.snapshot else None

    def figi(self, ticker: str, as_of_ns: int) -> str | None:
        """FIGI of a MAPPED OpenFIGI ticker job available at ``as_of_ns``; None when absent or ambiguous."""
        if self.conn is None:
            return None
        if ticker in self._figi:
            return self._figi[ticker]
        found = None
        try:
            r = self.conn.execute(
                "SELECT value_text FROM observation WHERE dataset='openfigi.mapping' AND field='mapping' AND "
                "(entity_id=? OR entity_id LIKE ?) AND available_time_ns<=? ORDER BY available_time_ns DESC LIMIT 1",
                (f"OPENFIGI-JOB:TICKER:{ticker}", f"OPENFIGI-JOB:TICKER:{ticker}:%", as_of_ns)).fetchone()
        except sqlite3.OperationalError:
            r = None
        if r and r["value_text"]:
            outcome = json.loads(r["value_text"])
            cands = outcome.get("candidates") or []
            if outcome.get("status") == "MAPPED" and cands:
                found = cands[0].get("compositeFIGI") or cands[0].get("figi")
        self._figi[ticker] = found
        return found

    def _entity(self, cik: int, name: str | None, role: str, method: str, confidence: float, as_of_ns: int,
                evidence: str | None = None) -> dict:
        known = self.by_cik.get(cik)
        tickers = known["tickers"] if known else []
        figi = next((f for f in (self.figi(t["ticker"], as_of_ns) for t in tickers) if f), None)
        return {"scheme": "SEC_CIK", "value": cik, "name": name or (known["name"] if known else None), "role": role,
                "method": method, "confidence": confidence, "tickers": tickers, "figi": figi,
                "id_as_of_ns": self.as_of_ns, "id_source": self.source, "evidence": evidence}

    def tag_filing(self, entities: list[dict], as_of_ns: int) -> list[dict]:
        return [self._entity(e["cik"], e["name"], e["role"].lower(), "edgar_feed", 1.0, as_of_ns) for e in entities]

    def tag_text(self, text: str, as_of_ns: int) -> list[dict]:
        toks = _norm(text)
        found: dict[int, str] = {}
        i = 0
        while i < len(toks):   # longest name first; the words of a match are not reused for a shorter name
            step = 1
            for n in range(min(MAX_NGRAM, len(toks) - i), 1, -1):
                cik = self.names.get(tuple(toks[i:i + n]))
                if cik is not None:
                    found.setdefault(cik, " ".join(toks[i:i + n]))
                    step = n
                    break
            i += step
        return [self._entity(cik, None, "mention", "name_match", 0.6, as_of_ns, ev) for cik, ev in found.items()]

    def resolve(self, tokens: list[str]) -> tuple[set[int], list[str]]:
        """Issuer tokens from a client (CIK digits, TICKER or TICKER.MIC) -> CIKs; unresolved tokens are returned."""
        ciks, missing = set(), []
        for raw in tokens:
            t = raw.strip().upper()
            if not t:
                continue
            if t.isdigit():
                ciks.add(int(t))
                continue
            sym = t.rsplit(".", 1)[0] if re.fullmatch(r".+\.[A-Z][A-Z0-9]{3}", t) else t
            cik = self.by_ticker.get(sym)
            if cik is None:
                missing.append(raw.strip())
            else:
                ciks.add(cik)
        return ciks, missing
