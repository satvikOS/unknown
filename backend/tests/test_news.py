"""Live news and filings (ADR-0019; CAP-NEWS-02, -05, -06, -08, -13). SYNTHETIC feeds and issuers only: the
company names, CIKs, tickers and accession numbers below are invented and do not refer to real filings."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from unknown.api.auth import write_owner_record
from unknown.api.routes import news as routes
from unknown.api.server import ApiContext, make_server
from unknown.data.http import FetchError, Response
from unknown.data.pit import OPEN_END, ResearchStore
from unknown.data.sources import sec_tickers
from unknown.news import stream as news_stream
from unknown.news.entities import IssuerIndex
from unknown.news.feeds import FeedError, parse_edgar_atom, parse_feed, parse_time
from unknown.news.poller import Poller, poll_source
from unknown.news.sources import SOURCES, by_id, catalog
from unknown.news.store import NewsStore, fts_query, store_path
from unknown.service.paths import Paths

NS = 1_000_000_000
T0 = 1_792_000_000 * NS          # 2026-10-15T...Z; synthetic clock
PASSPHRASE = "news stream tests passphrase"


def entry(form, name, cik, role, acc, updated, items=()):
    lines = "".join(f"&lt;br&gt;Item {c}: {t}\n" for c, t in items)
    return (f"<entry><title>{form} - {name} ({cik:010d}) ({role})</title>"
            f'<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace("-", "")}/{acc}-index.htm"/>'
            f"<summary type=\"html\"> &lt;b&gt;Filed:&lt;/b&gt; 2026-10-15 &lt;b&gt;AccNo:&lt;/b&gt; {acc} &lt;b&gt;Size:&lt;/b&gt; 12 KB\n{lines}</summary>"
            f"<updated>{updated}</updated><category scheme=\"https://www.sec.gov/\" label=\"form type\" term=\"{form}\"/>"
            f"<id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>")


def atom(*entries) -> bytes:
    return ('<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>Latest Filings</title>" + "".join(entries) + "</feed>").encode("latin-1")


def rss(*items, extra="") -> bytes:
    body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{g}</guid>{extra_i}<pubDate>{d}</pubDate></item>"
                   for t, l, g, d, extra_i in items)
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>x</title>{extra}{body}</channel></rss>'.encode()


EIGHT_K = entry("8-K", "FIXTURE ROBOTICS INC", 1234567, "Filer", "0009999999-26-000001", "2026-10-15T16:05:01-04:00",
                [("2.02", "Results of Operations and Financial Condition"), ("9.01", "Financial Statements and Exhibits")])
FORM4_ISSUER = entry("4", "FIXTURE ROBOTICS INC", 1234567, "Issuer", "0009999999-26-000002", "2026-10-15T16:04:00-04:00")
FORM4_OWNER = entry("4", "Doe Jane Q", 7777777, "Reporting", "0009999999-26-000002", "2026-10-15T16:04:00-04:00")
TEN_Q = entry("10-Q", "Example Harbor Bancorp", 7654321, "Filer", "0009999999-26-000003", "2026-10-15T16:00:00-04:00")


class FakeFetcher:
    def __init__(self, pages: dict[str, bytes | Exception], clock=lambda: T0):
        self.pages, self.calls, self.clock = pages, [], clock

    def get(self, url):
        self.calls.append(url)
        body = self.pages.get(url)
        if body is None:
            raise FetchError(f"404 Not Found: {url}")
        if isinstance(body, Exception):
            raise body
        return Response(url, 200, body, "application/xml", self.clock())


def edgar_url(page):
    return by_id("sec-filings").url.format(start=page * 100, count=100)


def research_home(root: Path, snapshot_ns: int, figi: bool = True) -> None:
    """A research store with one SEC ticker snapshot received at ``snapshot_ns`` and an OpenFIGI outcome."""
    db = root / "research" / "research.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(db)
    doc = {"fields": ["cik", "name", "ticker", "exchange"],
           "data": [[1234567, "FIXTURE ROBOTICS INC", "FXRB", "Nasdaq"], [7654321, "Example Harbor Bancorp", "EXHB", "NYSE"]]}
    sec_tickers.ingest(store, FakeFetcher({sec_tickers.URL: json.dumps(doc).encode()}, clock=lambda: snapshot_ns))
    if figi:
        outcome = {"status": "MAPPED", "candidates": [{"figi": "BBG00FIXTURE1", "compositeFIGI": "BBG00FIXTURE0"}], "message": None}
        store.conn.execute(
            "INSERT INTO observation(dataset, entity_id, field, value_text, unit, receipt_time_ns, available_time_ns, "
            "known_from_ns, known_to_ns, publication_id, source_ref, quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("openfigi.mapping", "OPENFIGI-JOB:TICKER:FXRB:US", "mapping", json.dumps(outcome), "json", snapshot_ns,
             snapshot_ns, snapshot_ns, OPEN_END, "fixture", "FIXTURE", "OK"))
    store.conn.close()


class FeedParserTests(unittest.TestCase):
    def test_edgar_entries_merge_roles_and_type_8k_items(self):
        filings = parse_edgar_atom(atom(EIGHT_K, FORM4_OWNER, FORM4_ISSUER, TEN_Q))
        self.assertEqual([f.accession for f in filings], ["0009999999-26-000001", "0009999999-26-000002", "0009999999-26-000003"])
        k = filings[0]
        self.assertEqual((k.form, k.filed_date, k.size), ("8-K", "2026-10-15", "12 KB"))
        self.assertEqual([(i["code"], i["type"]) for i in k.items], [("2.02", "results"), ("9.01", "financial_statements_exhibits")])
        self.assertEqual(k.published_ns, parse_time("2026-10-15T20:05:01Z")[0])
        self.assertTrue(k.link.endswith("/0009999999-26-000001-index.htm"))
        f4 = filings[1]
        self.assertEqual([(e["cik"], e["role"]) for e in f4.entities], [(1234567, "Issuer"), (7777777, "Reporting")])
        self.assertEqual(f4.title, "4 - FIXTURE ROBOTICS INC")   # the issuer side names the headline

    def test_rss_times_summaries_and_day_precision(self):
        long = "&lt;table&gt;&lt;tr&gt;&lt;td&gt;" + ("word " * 300) + "&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"
        body = rss(("Board announces policy", "https://example.gov/a//b/", "g1", "Fri, 16 Oct 2026 15:00:00 GMT",
                    "<description>Board announces policy</description>"),
                   ("Order in the matter of X", "https://example.gov/o", "g2", "23 Oct, 2026 +0530", ""),
                   ("Repo auction", "https://example.gov/r", "g3", "Thu, 22 Oct 2026 18:30:00 +0530",
                    f"<description>{long}</description>"),
                   ("No time given", "https://example.gov/n", "g4", "sometime", ""))
        a, b, c, d = parse_feed(body)
        self.assertEqual((a.published_precision, a.summary), ("second", None))   # a description equal to the title is no summary
        self.assertEqual((b.published_precision, b.published_ns), ("day", parse_time("2026-10-22T18:30:00Z")[0]))
        self.assertTrue(c.summary_truncated)
        self.assertLessEqual(len(c.summary), 601)
        self.assertNotIn("<", c.summary)
        self.assertEqual((d.published_ns, d.published_precision), (None, None))   # unknown stays unknown
        self.assertEqual(parse_time("2026-10-15T12:00:00"), (None, None))         # no zone: refused, not guessed
        local = rss(("Repo auction", "https://example.gov/r2", "g5", "Fri, 23 Oct 2026 19:25:00", ""))
        self.assertIsNone(parse_feed(local)[0].published_ns)
        ist = parse_feed(local, "Asia/Kolkata")[0]                               # the publisher's declared zone, flagged
        self.assertEqual((ist.published_ns, ist.time_zone_assumed), (parse_time("2026-10-23T13:55:00Z")[0], "Asia/Kolkata"))

    def test_doctype_and_malformed_documents_are_refused(self):
        with self.assertRaises(FeedError):
            parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss version="2.0"><channel/></rss>')
        with self.assertRaises(FeedError):
            parse_feed(b"<rss><channel><item>")
        with self.assertRaises(FeedError):
            parse_edgar_atom(rss(("t", "l", "g", "d", "")))


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = T0
        self.store = NewsStore(store_path(self.root))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def clock(self):
        return self.now

    def test_seen_items_are_never_reinserted_and_the_cursor_survives_restart(self):
        src = by_id("sec-filings")
        f = FakeFetcher({edgar_url(0): atom(EIGHT_K, FORM4_OWNER, FORM4_ISSUER, TEN_Q)}, self.clock)
        r1 = poll_source(self.store, src, f, IssuerIndex.empty(), clock_ns=self.clock)
        self.assertEqual((r1["ok"], r1["entries"], r1["new"]), (True, 3, 3))
        self.store.close()
        self.store = NewsStore(store_path(self.root))          # restart
        self.now += 60 * NS
        r2 = poll_source(self.store, src, f, IssuerIndex.empty(), clock_ns=self.clock)
        self.assertEqual((r2["new"], self.store.counts()["sec-filings"]), (0, 3))
        cur = self.store.cursor("sec-filings")
        self.assertEqual((cur["polls"], cur["items"], cur["last_status"]), (2, 3, "OK"))
        self.assertEqual(cur["high_water_ns"], parse_time("2026-10-15T16:05:01-04:00")[0])
        items = self.store.query()
        self.assertEqual(items[0]["form"], "8-K")              # oldest first in, so the newest has the highest id
        self.assertIn("first_poll_backlog", items[0]["flags"])

    def test_edgar_pages_back_until_it_reaches_the_high_water_mark(self):
        src = by_id("sec-filings")
        f = FakeFetcher({edgar_url(0): atom(TEN_Q)}, self.clock)
        poll_source(self.store, src, f, IssuerIndex.empty(), clock_ns=self.clock)
        newer = [entry("8-K", f"FIXTURE CO {i}", 2000000 + i, "Filer", f"0009999999-26-{100000 + i:06d}",
                       f"2026-10-15T17:{i // 60:02d}:{i % 60:02d}-04:00") for i in range(100)]
        f.pages = {edgar_url(0): atom(*newer[::-1]), edgar_url(1): atom(TEN_Q)}
        f.calls.clear()
        r = poll_source(self.store, src, f, IssuerIndex.empty(), clock_ns=self.clock)
        self.assertEqual((r["new"], r["error"], len(f.calls)), (100, None, 2))
        self.assertNotIn("first_poll_backlog", self.store.query(limit=1)[0]["flags"])
        newest = [entry("8-K", f"FIXTURE CO {i}", 3000000 + i, "Filer", f"0009999999-26-{200000 + i:06d}",
                        f"2026-10-15T18:{i // 60:02d}:{i % 60:02d}-04:00") for i in range(100)]
        f.pages = {edgar_url(p): atom(*newest[::-1]) for p in range(3)}   # never reaches the mark: a gap is recorded
        r = poll_source(self.store, src, f, IssuerIndex.empty(), clock_ns=self.clock, max_pages=3)
        self.assertIn("possible gap", r["error"])

    def test_failed_fetch_is_recorded_and_shows_as_failed(self):
        r = poll_source(self.store, by_id("sec-press"), FakeFetcher({}, self.clock), IssuerIndex.empty(), clock_ns=self.clock)
        self.assertFalse(r["ok"])
        cur = self.store.cursor("sec-press")
        self.assertEqual((cur["last_status"], cur["last_ok_ns"]), ("FAILED", None))
        self.assertIn("404", cur["last_error"])
        self.assertEqual(self.store.conn.execute("SELECT status FROM news_fetch").fetchone()[0], "FAILED")

    def test_syndication_duplicates_and_cross_source_clusters(self):
        fed = rss(("Agencies issue joint statement on liquidity risk management", "https://www.example.gov/press//joint1.htm",
                   "fed-1", "Fri, 16 Oct 2026 15:00:00 GMT", ""))
        sec_copy = rss(("Agencies Issue Joint Statement on Liquidity Risk Management", "https://www.example.gov/press/joint1.htm?utm_source=rss",
                        "sec-9", "Fri, 16 Oct 2026 15:05:00 GMT", ""),
                       ("Joint statement on liquidity risk management issued by agencies today", "https://www.example.gov/other.htm",
                        "sec-10", "Fri, 16 Oct 2026 15:06:00 GMT", ""))
        poll_source(self.store, by_id("frb-press"), FakeFetcher({by_id("frb-press").url: fed}, self.clock), IssuerIndex.empty(), clock_ns=self.clock)
        poll_source(self.store, by_id("sec-press"), FakeFetcher({by_id("sec-press").url: sec_copy}, self.clock), IssuerIndex.empty(), clock_ns=self.clock)
        first, copy, similar = sorted(self.store.query(), key=lambda i: i["id"])
        self.assertEqual(copy["duplicate_of"], first["id"])                    # same canonical link
        self.assertIsNone(similar["duplicate_of"])
        self.assertEqual({first["cluster"]["cluster_id"], copy["cluster"]["cluster_id"], similar["cluster"]["cluster_id"]},
                         {first["cluster"]["cluster_id"]})                     # same story from two feeds
        top = self.store.top(since_ns=0)[0]
        self.assertEqual((top["copies"], top["sources"]), (3, ["frb-press", "sec-press"]))
        self.assertEqual(len(self.store.item(first["id"])["copies"]), 2)
        self.assertEqual(len(self.store.query(include_duplicates=False)), 2)

    def test_full_text_search_boolean_phrase_prefix_and_literal_punctuation(self):
        poll_source(self.store, by_id("sec-filings"), FakeFetcher({edgar_url(0): atom(EIGHT_K, TEN_Q)}, self.clock),
                    IssuerIndex.empty(), clock_ns=self.clock)
        fed = rss(("Board announces approval of bank merger", "https://e.gov/1", "f1", "Fri, 16 Oct 2026 15:00:00 GMT",
                   "<description>The Board approved the application of Example Harbor Bancorp.</description>"))
        poll_source(self.store, by_id("frb-press"), FakeFetcher({by_id("frb-press").url: fed}, self.clock), IssuerIndex.empty(), clock_ns=self.clock)
        titles = lambda q: sorted(i["title"] for i in self.store.query(q=q))
        self.assertEqual(titles("8-K"), ["8-K - FIXTURE ROBOTICS INC"])
        self.assertEqual(titles('"bank merger"'), ["Board announces approval of bank merger"])
        self.assertEqual(titles("harb*"), ["10-Q - Example Harbor Bancorp", "Board announces approval of bank merger"])
        self.assertEqual(titles("harbor NOT merger"), ["10-Q - Example Harbor Bancorp"])
        hit = self.store.query(q="approved")[0]
        self.assertIn("\u0002approved\u0003", hit["snippet"])
        self.assertEqual(fts_query("S&P 2.02"), '"S&P" "2.02"')
        self.assertEqual(titles("8-K", ), titles("8-K"))
        with self.assertRaises(ValueError):
            self.store.query(q='"unclosed')


class EntityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        research_home(self.root, snapshot_ns=T0)
        self.db = self.root / "research" / "research.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_point_in_time_tickers_and_figi(self):
        before = IssuerIndex.open(self.db, T0 - NS)
        try:
            ent = before.tag_filing([{"cik": 1234567, "name": "FIXTURE ROBOTICS INC", "role": "Filer"}], T0 - NS)[0]
            self.assertEqual((ent["value"], ent["tickers"], ent["figi"], ent["id_source"]), (1234567, [], None, None))
        finally:
            before.close()
        after = IssuerIndex.open(self.db, T0 + NS)
        try:
            ent = after.tag_filing([{"cik": 1234567, "name": "FIXTURE ROBOTICS INC", "role": "Filer"}], T0 + NS)[0]
            self.assertEqual([t["ticker"] for t in ent["tickers"]], ["FXRB"])
            self.assertEqual(ent["figi"], "BBG00FIXTURE0")
            self.assertEqual(ent["id_as_of_ns"], T0)
            self.assertIn("snapshot", ent["id_source"])
            self.assertEqual(after.resolve(["FXRB.XNAS", "0007654321", "NOPE"]), ({1234567, 7654321}, ["NOPE"]))
        finally:
            after.close()

    def test_name_mentions_need_two_words_and_keep_evidence(self):
        idx = IssuerIndex.open(self.db, T0 + NS)
        try:
            hits = idx.tag_text("The Board approved the application by Example Harbor Bancorp, and robotics shares rose", T0 + NS)
            self.assertEqual([(h["value"], h["role"], h["method"], h["evidence"]) for h in hits],
                             [(7654321, "mention", "name_match", "EXAMPLE HARBOR BANCORP")])
            self.assertEqual(idx.tag_text("FIXTURE alone is one word", T0 + NS), [])
        finally:
            idx.close()

    def test_poller_tags_filings_with_tickers_as_of_first_seen(self):
        fetchers = {"sec-edgar": FakeFetcher({edgar_url(0): atom(EIGHT_K)}, lambda: T0 + 5 * NS)}
        p = Poller(self.root, sources=(by_id("sec-filings"),), fetcher_for=fetchers.__getitem__, clock_ns=lambda: T0 + 5 * NS)
        try:
            p.poll_due()
            ent = p.store.query()[0]["entities"][0]
            self.assertEqual((ent["scheme"], ent["value"], ent["role"], ent["tickers"][0]["ticker"]), ("SEC_CIK", "1234567", "filer", "FXRB"))
        finally:
            p.store.close()


class RightsTests(unittest.TestCase):
    def test_blocked_and_unrecorded_sources_are_never_fetched(self):
        cat = {c["source_id"]: c for c in catalog()}
        for sid in ("esma", "nse", "bse"):
            self.assertEqual((cat[sid]["verdict"], cat[sid]["pollable"]), ("blocked", False))
            self.assertTrue(cat[sid]["refusal"])
        for sid in ("sec-filings", "sec-press", "sec-litigation", "frb-press", "ecb-press", "rbi-press", "sebi"):
            self.assertTrue(cat[sid]["pollable"], sid)
            self.assertEqual(cat[sid]["terms_verified_on"], "2026-10-23")
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fetcher_for(pid):
                calls.append(pid)
                return FakeFetcher({})
            p = Poller(Path(tmp), fetcher_for=fetcher_for, clock_ns=lambda: T0)
            try:
                self.assertEqual({s.source_id for s in p.sources} & {"esma", "nse", "bse"}, set())
                p.poll_due()
                self.assertNotIn("nse", calls)
                self.assertEqual(set(calls), {"sec-edgar", "federal-reserve", "ecb", "rbi", "sebi"})
                unrecorded = SOURCES[0].__class__("x", "X", "sec-edgar", "no.such.dataset", "https://e.gov", "feed", 60, "regulator", "US")
                self.assertFalse(catalog(sources=(unrecorded,))[0]["pollable"])
            finally:
                p.store.close()


def _seed(root: Path) -> NewsStore:
    store = NewsStore(store_path(root))
    index = IssuerIndex.open(root / "research" / "research.sqlite", T0 + NS)
    try:
        poll_source(store, by_id("sec-filings"), FakeFetcher({edgar_url(0): atom(EIGHT_K, FORM4_OWNER, FORM4_ISSUER, TEN_Q)}),
                    index, clock_ns=lambda: T0 + NS)
    finally:
        index.close()
    return store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ctx = SimpleNamespace(paths=SimpleNamespace(root=self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, *sub, **q):
        return json.loads(json.dumps(routes.handle(self.ctx, ["news", *sub], q, "SIM-1", None), allow_nan=False))

    def test_unavailable_without_a_store_lists_sources(self):
        body = self.get()
        self.assertEqual(body["status"], "UNAVAILABLE")
        self.assertEqual({s["source_id"] for s in body["sources"] if not s["pollable"]}, {"esma", "nse", "bse"})
        self.assertEqual(self.get("sources")["store"], "UNAVAILABLE")

    def test_list_filters_item_top_and_sources(self):
        research_home(self.root, snapshot_ns=T0)
        _seed(self.root).close()
        body = self.get()
        self.assertEqual(len(body["items"]), 3)
        self.assertTrue(all(i["first_seen"] and i["source_id"] == "sec-filings" for i in body["items"]))
        self.assertEqual([i["form"] for i in self.get(form="8-K")["items"]], ["8-K"])
        by_ticker = self.get(issuer="FXRB")
        self.assertEqual(sorted(i["form"] for i in by_ticker["items"]), ["4", "8-K"])
        self.assertEqual(by_ticker["issuers"]["ciks"], [1234567])
        self.assertEqual(self.get(issuer="NOPE")["items"], [])
        self.assertEqual(self.get(source="frb-press")["items"], [])
        self.assertEqual(len(self.get(since="2000-01-01")["items"]), 3)
        self.assertEqual(self.get(q="harbor")["items"][0]["form"], "10-Q")
        one = self.get("item", id=str(body["items"][0]["id"]))["item"]
        self.assertTrue(one["fetch"]["sha256"])
        self.assertEqual(self.get("top", hours="720")["ranking"], "sources carrying the story, then copies, then recency")
        srcs = {s["source_id"]: s for s in self.get("sources")["sources"]}
        self.assertEqual(srcs["sec-filings"]["stored_items"], 3)
        self.assertEqual(srcs["sec-filings"]["poll"]["last_status"], "OK")
        for bad in ({"since": "yesterday"}, {"limit": "0"}, {"q": '"open'}):
            with self.assertRaises(ValueError):
                self.get(**bad)
        with self.assertRaises(LookupError):
            self.get("item", id="999")
        with self.assertRaises(ValueError):
            self.get("item")

    def test_stream_route_is_registered_and_validates_before_headers(self):
        self.assertEqual([p for p, _ in routes.STREAM_ROUTES], [("news", "stream")])
        with self.assertRaises(ValueError):
            routes.stream(self.ctx, {"issuers": ",".join(str(i) for i in range(news_stream.MAX_ISSUERS + 1))})


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.paths = Paths(Path(cls.tmp.name) / "home")
        cls.paths.ensure()
        write_owner_record(cls.paths.secrets / "owner.json", PASSPHRASE)
        from unknown.store.db import connect
        connect(cls.paths.db("SIM-NEWS")).close()
        cls.paths.token("api", create=True)
        research_home(cls.paths.root, snapshot_ns=T0)
        cls.ctx = ApiContext(cls.paths, "SIM-NEWS", None, None, False)
        cls.server = make_server(cls.ctx, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, path, cookie=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})}
        conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        return conn.getresponse(), conn

    def login(self) -> str:
        resp, conn = self.request("/api/auth/login", method="POST", body={"passphrase": PASSPHRASE})
        resp.read()
        conn.close()
        return resp.getheader("Set-Cookie").split(";")[0]

    def test_stream_requires_the_owner_session(self):
        resp, conn = self.request("/api/news/stream")
        self.assertEqual(resp.status, 401)
        conn.close()

    def test_stream_sends_news_and_watchlist_alerts_once_per_story(self):
        cookie = self.login()
        resp, conn = self.request("/api/news/stream?issuers=FXRB.XNAS,NOPE", cookie)
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/event-stream"))
        events, name, ev_id = [], None, None

        def read_until(pred, seconds=6):
            nonlocal name, ev_id
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not pred():
                line = resp.fp.readline().decode().rstrip("\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("id: "):
                    ev_id = int(line[4:])
                elif line.startswith("data: "):
                    events.append((name, json.loads(line[6:]), ev_id))
                    ev_id = None
        read_until(lambda: any(n == "status" for n, _, _ in events))
        status = events[0][1]
        self.assertEqual(status["unresolved"], ["NOPE"])
        self.assertEqual(status["watching"], [{"token": "FXRB.XNAS", "cik": 1234567}])
        store = _seed(self.paths.root)        # store appears after the stream opened: items arrive live
        try:
            read_until(lambda: sum(1 for n, _, _ in events if n == "news") >= 3 and sum(1 for n, _, _ in events if n == "alert") >= 2)
        finally:
            store.close()
        conn.close()
        news = [(e["form"], i) for n, e, i in events if n == "news"]
        self.assertEqual([f for f, _ in news], ["10-Q", "4", "8-K"])
        self.assertTrue(all(i is not None for _, i in news))
        alerts = [e for n, e, _ in events if n == "alert"]
        self.assertEqual(sorted(a["item"]["form"] for a in alerts), ["4", "8-K"])
        self.assertTrue(all(a["issuers"] == [1234567] for a in alerts))

    def test_list_endpoint_through_the_server(self):
        cookie = self.login()
        resp, conn = self.request("/api/news?limit=2", cookie)
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("sources", body)


if __name__ == "__main__":
    unittest.main()
