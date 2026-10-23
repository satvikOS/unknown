# ADR-0019: Live news and filings: poller process, news store and SSE delivery

Status: proposed (2026-10-23)

## Context

The terminal needs a live tape of SEC filings and official regulator and central-bank releases, full-text search,
story clustering, issuer tagging with point-in-time identifiers and watchlist alerts (CAP-NEWS-02, -05, -06, -08,
-13). Polling remote feeds is slow, failure-prone network work. The API process serves the owner's terminal and must
stay responsive; it must never write the core database (ADR-0003, ADR-0013). Watchlists live in the browser
(monitor workspace), not on the server. Every source needs a rights record (ADR-0015 provider registry).

## Decision

1. **The poller is its own process**: `python -m unknown.news.poller run [--once]`. It is the only writer of the news
   store and holds an exclusive lock (`news/poller.lock`), so two pollers cannot interleave. It never opens the core
   database. Network failures, slow feeds and parsing stay out of the API process; a crashed poller leaves the API
   serving what is stored, and the terminal shows each source's last success and last error.
2. **News store**: `UNKNOWN_HOME/news/news.sqlite` (WAL), separate from the core database and the research store, with
   an FTS5 index over headline, summary and form. Each item is keyed by (source, guid) and inserted once; its
   `item_id` is the arrival order and the SSE event id. Per-source cursors hold the high-water mark (newest feed time
   seen), last poll, last success and last error; every request is logged with URL, receipt time, HTTP status, size and
   SHA-256. Fetched data stays under `UNKNOWN_HOME`, never in git.
3. **EDGAR**: the Latest Filings Atom feed (`browse-edgar?action=getcurrent&output=atom`, 100 per page) is polled every
   60 s with the declared User-Agent through the shared `Fetcher` token bucket (far below the SEC's 10 requests/s). A
   poll pages back until it reaches an entry older than the high-water mark (at most 10 pages) and records a possible
   gap otherwise. Entries of one accession (filer, subject, issuer, reporting owner) merge into one filing. Filing
   index pages are linked, not fetched, so nothing already seen is fetched again. 8-K items are typed from the item
   lines against the Form 8-K item list.
4. **Other feeds**: official RSS only (SEC press and litigation releases, Federal Reserve, ECB, RBI, SEBI), at the
   intervals in `news/sources.py` (SEBI at its declared `ttl` of 60 min). Only headline, link, time and the feed's
   summary are kept, the summary cut to 600 plain-text characters (RBI puts whole releases in the description). A feed
   time without a zone is unknown unless the source declares its publisher's zone (RBI: Asia/Kolkata), in which case
   the item is flagged `time_zone_assumed:<zone>`. A date-only time keeps day precision.
5. **Rights gate**: a source is polled only when its provider record exists, its verdict is `allowed` or
   `allowed_with_conditions`, and the dataset permits display and storage. ESMA (HTTP 403 to automated clients), NSE
   (terms prohibit automated collection) and BSE (HTTP 403; terms unreadable) are recorded as `blocked` with the
   reason and listed in the terminal; they are never fetched.
6. **Dedup and clusters**: a release whose canonical link, or normalised headline within three days, matches an
   earlier item is a copy (`dup_of`) in that item's story; copies never alert. A release from another source sharing
   at least three significant headline words at Jaccard >= 0.6 within 72 h joins that story. Top stories rank by the
   number of sources, then copies, then recency.
7. **Issuer tags**: filings carry their EDGAR entities (CIK, role, confidence 1.0). Tickers come from the SEC ticker
   snapshot received at or before the item's first-seen time, and a FIGI from a MAPPED OpenFIGI outcome available
   then; a later snapshot is never used. Releases get `name_match` mentions (a company name of two or more
   non-generic words, confidence 0.6, the words kept as evidence).
8. **Delivery**: the API opens the news store read-only. `GET /api/news`, `/api/news/item`, `/api/news/top`,
   `/api/news/sources` are plugin routes. `GET /api/news/stream` uses a new plugin `STREAM_ROUTES` contract in
   `api/routes/__init__.py` (the core SSE helper is tied to the event-log sequence): it polls the store for ids above
   `Last-Event-ID` once a second and sends `news` events, and `alert` events for issuers the client lists in
   `issuers=` (CIKs, tickers or TICKER.MIC, at most 500), once per story per connection.

## Consequences

- The API stays fast and read-only for news; the poller can be restarted, upgraded or stopped independently, and
  restarts never re-announce seen items.
- Availability is the first-seen receipt time; the feed's own time is kept as the event time and never used as
  availability, so a backtest cannot see an item before this system did.
- Not done here: confirming filings against the submissions JSON, fetching primary documents and exhibits, the
  daily-index reconciliation (coverage ratio), linking 8-K/A to the 8-K, server-stored alert rules and an alert inbox,
  conditional GET (ETag), licensed newswires (CAP-NEWS-04), India exchange announcements (CAP-NEWS-03).
