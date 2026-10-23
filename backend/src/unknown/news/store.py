"""The news store: ``UNKNOWN_HOME/news/news.sqlite`` (ADR-0019). Written only by the poller; the API opens it
read-only. It is separate from the core database and from the research store.

Tables
  news_item     one row per (source, guid) ever seen; ``item_id`` grows in arrival order and is the SSE event id
  news_fts      FTS5 index over title, summary and form (external content = news_item)
  news_entity   issuer tags per item: scheme (SEC_CIK), value, role, method, confidence, tickers and FIGI as of the
                identifier snapshot used, and that snapshot's time
  news_cluster  one story: first item, earliest publication time, copies and the sources that carried it
  news_cursor   per source: high-water mark (newest feed time seen), last poll, last success, last error
  news_fetch    every request: URL, receipt time, HTTP status, bytes, SHA-256, entries, new items, error

Syndication dedup (CAP-NEWS-05): a release whose canonical link, or whose normalised headline within three days,
matches an earlier item is stored as a copy (``dup_of``) in that item's cluster. Copies never alert. Story
clustering: a release from another source whose headline shares at least ``CLUSTER_MIN_SHARED`` significant words
and a Jaccard similarity of ``CLUSTER_JACCARD`` with a story first seen in the last 72 hours joins that story.
Filings cluster by accession only (amendments are not linked yet).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.clock import ns_to_iso
from .feeds import FeedItem

NS = 1_000_000_000
DUP_WINDOW_NS = 3 * 86400 * NS
CLUSTER_WINDOW_NS = 72 * 3600 * NS
CLUSTER_JACCARD = 0.6
CLUSTER_MIN_SHARED = 3
HL_START, HL_END = "\u0002", "\u0003"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_item (
  item_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, guid TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT, summary_truncated INTEGER NOT NULL DEFAULT 0, link TEXT, canonical_link TEXT,
  category TEXT, published_ns INTEGER, published_precision TEXT, first_seen_ns INTEGER NOT NULL,
  form TEXT, accession TEXT, items_json TEXT NOT NULL DEFAULT '[]', filed_date TEXT, size TEXT,
  norm_title TEXT NOT NULL, cluster_id INTEGER, dup_of INTEGER REFERENCES news_item(item_id),
  flags_json TEXT NOT NULL DEFAULT '[]', fetch_id INTEGER, UNIQUE (source_id, guid)
);
CREATE INDEX IF NOT EXISTS news_item_seen ON news_item(first_seen_ns);
CREATE INDEX IF NOT EXISTS news_item_form ON news_item(form);
CREATE INDEX IF NOT EXISTS news_item_cluster ON news_item(cluster_id);
CREATE INDEX IF NOT EXISTS news_item_link ON news_item(canonical_link);
CREATE INDEX IF NOT EXISTS news_item_title ON news_item(norm_title);
CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(title, summary, form, content='news_item', content_rowid='item_id');
CREATE TRIGGER IF NOT EXISTS news_item_ai AFTER INSERT ON news_item BEGIN
  INSERT INTO news_fts(rowid, title, summary, form) VALUES (new.item_id, new.title, new.summary, new.form);
END;
CREATE TABLE IF NOT EXISTS news_entity (
  item_id INTEGER NOT NULL REFERENCES news_item(item_id), scheme TEXT NOT NULL, value TEXT NOT NULL, name TEXT,
  role TEXT NOT NULL, method TEXT NOT NULL, confidence REAL NOT NULL, tickers_json TEXT NOT NULL DEFAULT '[]',
  figi TEXT, id_as_of_ns INTEGER, id_source TEXT, evidence TEXT, PRIMARY KEY (item_id, scheme, value, role)
);
CREATE INDEX IF NOT EXISTS news_entity_value ON news_entity(scheme, value);
CREATE TABLE IF NOT EXISTS news_cluster (
  cluster_id INTEGER PRIMARY KEY, title TEXT NOT NULL, first_item_id INTEGER NOT NULL, first_seen_ns INTEGER NOT NULL,
  last_seen_ns INTEGER NOT NULL, earliest_published_ns INTEGER, copies INTEGER NOT NULL, sources_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_cursor (
  source_id TEXT PRIMARY KEY, high_water_ns INTEGER, last_poll_ns INTEGER, last_ok_ns INTEGER, last_status TEXT,
  last_error TEXT, polls INTEGER NOT NULL DEFAULT 0, items INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news_fetch (
  fetch_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL, started_ns INTEGER NOT NULL,
  receipt_ns INTEGER, http_status INTEGER, bytes INTEGER, sha256 TEXT, entries INTEGER, new_items INTEGER,
  status TEXT NOT NULL, error TEXT
);
"""

STOPWORDS = frozenset("""the and for with from that this its into under over upon about after before their his her
were was are has have had will would than then them they which while also not but all any can new more less amid
announces announce announced statement release press says said update""".split())


def store_path(root: Path) -> Path:
    return Path(root) / "news" / "news.sqlite"


def canonical_link(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if not p.netloc:
        return None
    path = re.sub(r"/{2,}", "/", p.path).rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")))
    return urlunsplit(("https", p.netloc.lower(), path, query, ""))


def norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", title.casefold())).strip()


def title_tokens(title: str) -> set[str]:
    return {t for t in norm_title(title).split() if len(t) >= 3 and t not in STOPWORDS}


def fts_query(q: str) -> str:
    """The owner's query as FTS5 syntax: "phrases", prefix*, AND/OR/NOT and NEAR(...) pass through; any other word is
    quoted so punctuation (8-K, 2.02, S&P) is matched literally instead of failing as syntax."""
    out = []
    for tok in re.findall(r'"[^"]*"|NEAR\([^)]*\)|\S+', q.strip()):
        if tok in ("AND", "OR", "NOT") or tok.startswith('"') or tok.startswith("NEAR("):
            out.append(tok)
        elif re.fullmatch(r"\w+\*", tok):
            out.append(tok)
        else:
            out.append('"' + tok.replace('"', '""') + '"')
    if not out:
        raise ValueError("empty search query")
    return " ".join(out)


def iso(ns: int | None) -> str | None:
    return ns_to_iso(ns) if ns is not None else None


class NewsStore:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(f"news store {self.path} does not exist")
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None,
                                        check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=2000")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ writes (poller only)
    def begin_fetch(self, source_id: str, url: str, started_ns: int) -> int:
        return self.conn.execute("INSERT INTO news_fetch(source_id, url, started_ns, status) VALUES (?,?,?, 'RUNNING')",
                                 (source_id, url, started_ns)).lastrowid

    def end_fetch(self, fetch_id: int, *, status: str, receipt_ns: int | None = None, http_status: int | None = None,
                  body_len: int | None = None, sha256: str | None = None, entries: int | None = None,
                  new_items: int | None = None, error: str | None = None) -> None:
        self.conn.execute("UPDATE news_fetch SET status=?, receipt_ns=?, http_status=?, bytes=?, sha256=?, entries=?, "
                          "new_items=?, error=? WHERE fetch_id=?",
                          (status, receipt_ns, http_status, body_len, sha256, entries, new_items, error, fetch_id))

    def cursor(self, source_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM news_cursor WHERE source_id=?", (source_id,)).fetchone()
        return dict(r) if r else None

    def record_poll(self, source_id: str, *, now_ns: int, ok: bool, high_water_ns: int | None, new_items: int,
                    error: str | None) -> None:
        cur = self.cursor(source_id) or {}
        marks = [t for t in (cur.get("high_water_ns"), high_water_ns) if t is not None]
        self.conn.execute(
            "INSERT INTO news_cursor(source_id, high_water_ns, last_poll_ns, last_ok_ns, last_status, last_error, polls, items) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET high_water_ns=excluded.high_water_ns, "
            "last_poll_ns=excluded.last_poll_ns, last_ok_ns=excluded.last_ok_ns, last_status=excluded.last_status, "
            "last_error=excluded.last_error, polls=excluded.polls, items=excluded.items",
            (source_id, max(marks) if marks else None, now_ns, now_ns if ok else cur.get("last_ok_ns"),
             "OK" if ok else "FAILED", error, cur.get("polls", 0) + 1, cur.get("items", 0) + new_items))

    def add(self, source_id: str, kind: str, item: FeedItem, *, first_seen_ns: int, fetch_id: int | None,
            flags: list[str] | None = None, entities: list[dict] | None = None) -> tuple[int, bool]:
        """Insert once per (source, guid). Returns (item_id, inserted). A known guid inserts nothing; for a filing it
        only adds entity roles not yet recorded (the same accession can be listed once per entity)."""
        known = self.conn.execute("SELECT item_id FROM news_item WHERE source_id=? AND guid=?",
                                  (source_id, item.guid)).fetchone()
        if known:
            if entities:
                self._add_entities(known["item_id"], entities)
            return known["item_id"], False
        canon = canonical_link(item.link)
        nt = norm_title(item.title)
        dup_of, cluster_id = (None, None) if kind == "filing" else self._duplicate_of(canon, nt, first_seen_ns)
        if cluster_id is None and kind != "filing":
            cluster_id = self._similar_cluster(source_id, item.title, first_seen_ns)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            item_id = self.conn.execute(
                "INSERT INTO news_item(source_id, guid, kind, title, summary, summary_truncated, link, canonical_link, "
                "category, published_ns, published_precision, first_seen_ns, form, accession, items_json, filed_date, "
                "size, norm_title, dup_of, flags_json, fetch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, item.guid, kind, item.title, item.summary, int(item.summary_truncated), item.link, canon,
                 item.category, item.published_ns, item.published_precision, first_seen_ns, item.form, item.accession,
                 json.dumps(item.items), item.filed_date, item.size, nt, dup_of, json.dumps(sorted(set(flags or []))),
                 fetch_id)).lastrowid
            if cluster_id is None:
                cluster_id = self.conn.execute(
                    "INSERT INTO news_cluster(title, first_item_id, first_seen_ns, last_seen_ns, earliest_published_ns, "
                    "copies, sources_json) VALUES (?,?,?,?,?,1,?)",
                    (item.title, item_id, first_seen_ns, first_seen_ns, item.published_ns, json.dumps([source_id]))).lastrowid
            else:
                c = self.conn.execute("SELECT * FROM news_cluster WHERE cluster_id=?", (cluster_id,)).fetchone()
                srcs = sorted(set(json.loads(c["sources_json"])) | {source_id})
                earliest = min([t for t in (c["earliest_published_ns"], item.published_ns) if t is not None], default=None)
                self.conn.execute("UPDATE news_cluster SET copies=copies+1, last_seen_ns=?, sources_json=?, "
                                  "earliest_published_ns=? WHERE cluster_id=?",
                                  (max(c["last_seen_ns"], first_seen_ns), json.dumps(srcs), earliest, cluster_id))
            self.conn.execute("UPDATE news_item SET cluster_id=? WHERE item_id=?", (cluster_id, item_id))
            if entities:
                self._add_entities(item_id, entities)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return item_id, True

    def _add_entities(self, item_id: int, entities: list[dict]) -> None:
        for e in entities:
            self.conn.execute(
                "INSERT OR IGNORE INTO news_entity(item_id, scheme, value, name, role, method, confidence, tickers_json, "
                "figi, id_as_of_ns, id_source, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, e["scheme"], str(e["value"]), e.get("name"), e["role"], e["method"], float(e["confidence"]),
                 json.dumps(e.get("tickers", [])), e.get("figi"), e.get("id_as_of_ns"), e.get("id_source"),
                 e.get("evidence")))

    def _duplicate_of(self, canon: str | None, nt: str, now_ns: int) -> tuple[int | None, int | None]:
        r = None
        if canon:
            r = self.conn.execute("SELECT item_id, cluster_id FROM news_item WHERE canonical_link=? AND dup_of IS NULL "
                                  "AND kind!='filing' ORDER BY item_id LIMIT 1", (canon,)).fetchone()
        if r is None and nt:
            r = self.conn.execute("SELECT item_id, cluster_id FROM news_item WHERE norm_title=? AND dup_of IS NULL AND "
                                  "kind!='filing' AND first_seen_ns>=? ORDER BY item_id LIMIT 1",
                                  (nt, now_ns - DUP_WINDOW_NS)).fetchone()
        return (r["item_id"], r["cluster_id"]) if r else (None, None)

    def _similar_cluster(self, source_id: str, title: str, now_ns: int) -> int | None:
        mine = title_tokens(title)
        if len(mine) < CLUSTER_MIN_SHARED:
            return None
        best, best_score = None, 0.0
        for r in self.conn.execute("SELECT cluster_id, title FROM news_item WHERE kind!='filing' AND dup_of IS NULL AND "
                                   "source_id!=? AND first_seen_ns>=? ORDER BY item_id DESC LIMIT 2000",
                                   (source_id, now_ns - CLUSTER_WINDOW_NS)):
            other = title_tokens(r["title"])
            shared = len(mine & other)
            if shared < CLUSTER_MIN_SHARED:
                continue
            score = shared / len(mine | other)
            if score >= CLUSTER_JACCARD and score > best_score:
                best, best_score = r["cluster_id"], score
        return best

    # ------------------------------------------------------------------ reads (API and stream)
    def max_item_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(item_id), 0) FROM news_item").fetchone()[0]

    def entities_for(self, item_ids: list[int]) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {i: [] for i in item_ids}
        if not item_ids:
            return out
        marks = ",".join("?" * len(item_ids))
        for r in self.conn.execute(f"SELECT * FROM news_entity WHERE item_id IN ({marks}) ORDER BY item_id, confidence DESC, "
                                   "role", item_ids):
            out[r["item_id"]].append({
                "scheme": r["scheme"], "value": r["value"], "name": r["name"], "role": r["role"], "method": r["method"],
                "confidence": r["confidence"], "tickers": json.loads(r["tickers_json"]), "figi": r["figi"],
                "identifiers_as_of": iso(r["id_as_of_ns"]), "identifier_source": r["id_source"], "evidence": r["evidence"]})
        return out

    def _rows(self, rows, snippets: dict[int, str] | None = None) -> list[dict]:
        rows = list(rows)
        ents = self.entities_for([r["item_id"] for r in rows])
        clusters = {}
        cids = sorted({r["cluster_id"] for r in rows if r["cluster_id"] is not None})
        if cids:
            marks = ",".join("?" * len(cids))
            for c in self.conn.execute(f"SELECT cluster_id, copies, sources_json, first_item_id FROM news_cluster "
                                       f"WHERE cluster_id IN ({marks})", cids):
                clusters[c["cluster_id"]] = {"cluster_id": c["cluster_id"], "copies": c["copies"],
                                             "sources": json.loads(c["sources_json"]), "first_item_id": c["first_item_id"]}
        return [self.row_dict(r, ents.get(r["item_id"], []), clusters.get(r["cluster_id"]),
                              (snippets or {}).get(r["item_id"])) for r in rows]

    @staticmethod
    def row_dict(r, entities: list[dict], cluster: dict | None, snippet: str | None = None) -> dict:
        return {
            "id": r["item_id"], "source_id": r["source_id"], "kind": r["kind"], "guid": r["guid"], "title": r["title"],
            "summary": r["summary"], "summary_truncated": bool(r["summary_truncated"]), "link": r["link"],
            "category": r["category"], "published": iso(r["published_ns"]), "published_precision": r["published_precision"],
            "first_seen": iso(r["first_seen_ns"]), "first_seen_ns": str(r["first_seen_ns"]), "form": r["form"],
            "accession": r["accession"], "items": json.loads(r["items_json"]), "filed_date": r["filed_date"],
            "size": r["size"], "cluster": cluster, "duplicate_of": r["dup_of"], "flags": json.loads(r["flags_json"]),
            "entities": entities, "snippet": snippet}

    def query(self, *, source: list[str] | None = None, form: list[str] | None = None, ciks: set[int] | None = None,
              q: str | None = None, since_ns: int | None = None, before_id: int | None = None, after_id: int | None = None,
              limit: int = 100, include_duplicates: bool = True) -> list[dict]:
        where, args = [], []
        if source:
            where.append(f"i.source_id IN ({','.join('?' * len(source))})")
            args += source
        if form:
            ors = []
            for f in form:
                ors.append("(i.form = ? OR i.form = ?)")
                args += [f, f + "/A"]
            where.append("(" + " OR ".join(ors) + ")")
        if ciks is not None:
            if not ciks:
                return []
            where.append(f"i.item_id IN (SELECT item_id FROM news_entity WHERE scheme='SEC_CIK' AND value IN "
                         f"({','.join('?' * len(ciks))}))")
            args += [str(c) for c in sorted(ciks)]
        if since_ns is not None:
            where.append("i.first_seen_ns >= ?")
            args.append(since_ns)
        if before_id is not None:
            where.append("i.item_id < ?")
            args.append(before_id)
        if after_id is not None:
            where.append("i.item_id > ?")
            args.append(after_id)
        if not include_duplicates:
            where.append("i.dup_of IS NULL")
        order = "ASC" if after_id is not None else "DESC"   # a replay after an id is oldest first
        snippets = None
        if q:
            match = fts_query(q)
            sql = (f"SELECT i.*, snippet(news_fts, -1, '{HL_START}', '{HL_END}', '…', 14) AS snip FROM news_fts "
                   f"JOIN news_item i ON i.item_id = news_fts.rowid WHERE news_fts MATCH ?"
                   + "".join(f" AND {w}" for w in where) + " ORDER BY i.item_id " + order + " LIMIT ?")
            try:
                rows = self.conn.execute(sql, [match, *args, limit]).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"search query not understood: {exc}") from None
            snippets = {r["item_id"]: r["snip"] for r in rows}
        else:
            sql = "SELECT i.* FROM news_item i" + (" WHERE " + " AND ".join(where) if where else "") + \
                  " ORDER BY i.item_id " + order + " LIMIT ?"
            rows = self.conn.execute(sql, [*args, limit]).fetchall()
        return self._rows(rows, snippets)

    def item(self, item_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM news_item WHERE item_id=?", (item_id,)).fetchone()
        if r is None:
            return None
        out = self._rows([r])[0]
        copies = self.conn.execute("SELECT * FROM news_item WHERE cluster_id=? AND item_id!=? ORDER BY item_id",
                                   (r["cluster_id"], item_id)).fetchall() if r["cluster_id"] is not None else []
        out["copies"] = [{"id": c["item_id"], "source_id": c["source_id"], "title": c["title"], "link": c["link"],
                          "published": iso(c["published_ns"]), "first_seen": iso(c["first_seen_ns"]),
                          "duplicate_of": c["dup_of"]} for c in copies]
        f = self.conn.execute("SELECT * FROM news_fetch WHERE fetch_id=?", (r["fetch_id"],)).fetchone() \
            if r["fetch_id"] is not None else None
        out["fetch"] = {"url": f["url"], "received": iso(f["receipt_ns"]), "http_status": f["http_status"],
                        "sha256": f["sha256"], "bytes": f["bytes"]} if f else None
        return out

    def top(self, *, since_ns: int, limit: int = 30) -> list[dict]:
        """Stories first seen since ``since_ns``, ranked by how many sources carried them, then copies, then recency."""
        cs = self.conn.execute("SELECT * FROM news_cluster WHERE last_seen_ns>=? ORDER BY json_array_length(sources_json) "
                               "DESC, copies DESC, last_seen_ns DESC LIMIT ?", (since_ns, limit)).fetchall()
        firsts = {r["item_id"]: r for r in self.conn.execute(
            f"SELECT * FROM news_item WHERE item_id IN ({','.join('?' * len(cs)) or 'NULL'})", [c["first_item_id"] for c in cs])}
        lead = {d["id"]: d for d in self._rows(list(firsts.values()))}
        return [{"cluster_id": c["cluster_id"], "title": c["title"], "copies": c["copies"],
                 "sources": json.loads(c["sources_json"]), "first_seen": iso(c["first_seen_ns"]),
                 "last_seen": iso(c["last_seen_ns"]), "earliest_published": iso(c["earliest_published_ns"]),
                 "lead": lead.get(c["first_item_id"])} for c in cs]

    def cursors(self) -> dict[str, dict]:
        return {r["source_id"]: {"high_water": iso(r["high_water_ns"]), "last_poll": iso(r["last_poll_ns"]),
                                 "last_ok": iso(r["last_ok_ns"]), "last_status": r["last_status"],
                                 "last_error": r["last_error"], "polls": r["polls"], "items": r["items"]}
                for r in self.conn.execute("SELECT * FROM news_cursor")}

    def counts(self) -> dict[str, int]:
        return {r["source_id"]: r["n"] for r in self.conn.execute(
            "SELECT source_id, COUNT(*) AS n FROM news_item GROUP BY source_id")}

    def ingest_lag(self, source_id: str, since_ns: int) -> dict | None:
        """first_seen - published (seconds) for items seen live (not the first poll's backlog), p50 and p95."""
        lags = sorted((r[0] - r[1]) / NS for r in self.conn.execute(
            "SELECT first_seen_ns, published_ns FROM news_item WHERE source_id=? AND first_seen_ns>=? AND "
            "published_precision='second' AND published_ns IS NOT NULL AND flags_json NOT LIKE '%first_poll_backlog%'",
            (source_id, since_ns)))
        if not lags:
            return None
        pick = lambda q: lags[min(len(lags) - 1, int(q * len(lags)))]
        return {"n": len(lags), "p50_s": round(pick(0.5), 1), "p95_s": round(pick(0.95), 1)}
