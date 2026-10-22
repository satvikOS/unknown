"""SEC company ticker snapshots (CAP-INS-01, CAP-CO-01, CAP-INS-06; registry ``sec-edgar-reference``).

Source: ``https://www.sec.gov/files/company_tickers_exchange.json`` ({"fields": ["cik", "name",
"ticker", "exchange"], "data": [[...], ...]}), the official file named in the capability register
(CAP-INS-01 lawful path). The SEC "does not guarantee accuracy or scope" of these files and they
carry no validity dates, so each fetch is a dated snapshot: ticker history exists only from this
system's first snapshot onward (CAP-INS-06), and earlier as-of searches report that no snapshot was
available. Fair access: declared User-Agent, at most 10 requests/second shared with all SEC jobs
(one request per run here).

Tables (owned by this module, in the research database next to the observation store):
``ref_sec_ticker_snapshot`` (one row per fetch, ``same_as`` pointing at an identical earlier
payload) and ``ref_sec_ticker`` (rows of each distinct payload). ``changes`` diffs two snapshots
(CAP-INS-06 change detection: added, removed, renamed, exchange moved).

  python -m unknown.data.sources.sec_tickers ingest
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ...domain.clock import ns_to_iso
from ..pit import ResearchStore
from ..registry import dataset, load_registry
from .common import data_config, make_fetcher

PROVIDER = "sec-edgar-reference"
DATASET = "sec.company_tickers_exchange"
URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_sec_ticker_snapshot (
  snapshot_id INTEGER PRIMARY KEY, raw_sha256 TEXT NOT NULL, receipt_time_ns INTEGER NOT NULL,
  row_count INTEGER NOT NULL, source_url TEXT NOT NULL, same_as INTEGER REFERENCES ref_sec_ticker_snapshot(snapshot_id)
);
CREATE TABLE IF NOT EXISTS ref_sec_ticker (
  snapshot_id INTEGER NOT NULL REFERENCES ref_sec_ticker_snapshot(snapshot_id), cik INTEGER NOT NULL,
  name TEXT NOT NULL, ticker TEXT NOT NULL, exchange TEXT, PRIMARY KEY (snapshot_id, ticker, cik)
);
CREATE INDEX IF NOT EXISTS ref_sec_ticker_cik ON ref_sec_ticker(cik);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def parse(body: bytes) -> list[tuple[int, str, str, str | None]]:
    doc = json.loads(body)
    fields = doc.get("fields")
    if fields is None or not {"cik", "name", "ticker"} <= set(fields):
        raise ValueError("unexpected company_tickers_exchange.json shape (fields missing)")
    i = {f: fields.index(f) for f in fields}
    out = []
    for row in doc.get("data", []):
        cik, name, ticker = row[i["cik"]], row[i["name"]], row[i["ticker"]]
        if not isinstance(cik, int) or not ticker:
            continue
        out.append((cik, str(name), str(ticker), row[i["exchange"]] if "exchange" in i else None))
    return out


def ingest(store: ResearchStore, fetcher, now_ns=None) -> dict:
    ds = dataset(load_registry(), DATASET)
    if not ds.permits("storage"):
        raise PermissionError(f"{DATASET}: storage not permitted by the registry")
    ensure_schema(store.conn)
    resp = fetcher.get(URL)
    sha = store.put_raw(resp.body, provider=PROVIDER, dataset=DATASET, request={"url": resp.url},
                        receipt_time_ns=resp.receipt_time_ns, media_type=resp.content_type)
    rows = parse(resp.body)
    latest = store.conn.execute("SELECT snapshot_id, raw_sha256, same_as FROM ref_sec_ticker_snapshot "
                                "ORDER BY receipt_time_ns DESC, snapshot_id DESC LIMIT 1").fetchone()
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        if latest is not None and latest["raw_sha256"] == sha:
            same_as = latest["same_as"] or latest["snapshot_id"]
            c = store.conn.execute("INSERT INTO ref_sec_ticker_snapshot(raw_sha256, receipt_time_ns, row_count, source_url, same_as) "
                                   "VALUES (?,?,?,?,?)", (sha, resp.receipt_time_ns, len(rows), resp.url, same_as))
            outcome = "UNCHANGED"
        else:
            c = store.conn.execute("INSERT INTO ref_sec_ticker_snapshot(raw_sha256, receipt_time_ns, row_count, source_url, same_as) "
                                   "VALUES (?,?,?,?,NULL)", (sha, resp.receipt_time_ns, len(rows), resp.url))
            store.conn.executemany("INSERT OR IGNORE INTO ref_sec_ticker VALUES (?,?,?,?,?)",
                                   [(c.lastrowid, cik, name, ticker, exch) for cik, name, ticker, exch in rows])
            outcome = "NEW"
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    return {"provider": PROVIDER, "dataset": DATASET, "url": URL, "raw_sha256": sha, "snapshot_id": c.lastrowid,
            "receipt_time": ns_to_iso(resp.receipt_time_ns), "rows": len(rows), "companies": len({r[0] for r in rows}),
            "outcome": outcome}


def snapshot_at(conn, as_of_ns: int) -> dict | None:
    """The latest snapshot received at or before ``as_of_ns`` (rows resolved through ``same_as``)."""
    try:
        r = conn.execute("SELECT * FROM ref_sec_ticker_snapshot WHERE receipt_time_ns<=? ORDER BY receipt_time_ns DESC, "
                         "snapshot_id DESC LIMIT 1", (as_of_ns,)).fetchone()
    except Exception as exc:          # table absent: never ingested in this store
        if "no such table" in str(exc):
            return None
        raise
    if r is None:
        return None
    return {"snapshot_id": r["snapshot_id"], "rows_snapshot_id": r["same_as"] or r["snapshot_id"],
            "retrieved_at": ns_to_iso(r["receipt_time_ns"]), "receipt_time_ns": r["receipt_time_ns"], "row_count": r["row_count"],
            "raw_sha256": r["raw_sha256"], "source_url": r["source_url"]}


def first_snapshot_time(conn) -> int | None:
    try:
        r = conn.execute("SELECT MIN(receipt_time_ns) AS t FROM ref_sec_ticker_snapshot").fetchone()
    except Exception as exc:
        if "no such table" in str(exc):
            return None
        raise
    return r["t"] if r else None


def changes(conn, older_snapshot_id: int, newer_snapshot_id: int) -> dict:
    def rows(sid):
        s = conn.execute("SELECT COALESCE(same_as, snapshot_id) AS rid FROM ref_sec_ticker_snapshot WHERE snapshot_id=?", (sid,)).fetchone()
        if s is None:
            raise LookupError(f"no snapshot {sid}")
        return {(r["cik"], r["ticker"]): (r["name"], r["exchange"]) for r in
                conn.execute("SELECT * FROM ref_sec_ticker WHERE snapshot_id=?", (s["rid"],))}
    a, b = rows(older_snapshot_id), rows(newer_snapshot_id)
    added = sorted(k for k in b if k not in a)
    removed = sorted(k for k in a if k not in b)
    changed = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
    return {"added": [{"cik": c, "ticker": t, "name": b[(c, t)][0], "exchange": b[(c, t)][1]} for c, t in added],
            "removed": [{"cik": c, "ticker": t, "name": a[(c, t)][0], "exchange": a[(c, t)][1]} for c, t in removed],
            "changed": [{"cik": c, "ticker": t, "before": {"name": a[(c, t)][0], "exchange": a[(c, t)][1]},
                         "after": {"name": b[(c, t)][0], "exchange": b[(c, t)][1]}} for c, t in changed],
            "note": "Effective dates are uncertain within [older retrieved_at, newer retrieved_at] (CAP-INS-06)."}


def main(argv: list[str] | None = None) -> None:
    from ...service.paths import Paths
    ap = argparse.ArgumentParser(description="Snapshot SEC company_tickers_exchange.json into the research store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    home = Path(a.home).expanduser() if a.home else None
    paths = Paths(home) if home else Paths.default()
    research = paths.root / "research"
    research.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(research / "research.sqlite")
    fetcher = make_fetcher(PROVIDER, data_config(home))
    print(json.dumps(ingest(store, fetcher, time.time_ns), indent=2))


if __name__ == "__main__":
    main()
