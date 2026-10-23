"""Live news and filings (ADR-0019; CAP-NEWS-02, -05, -06, -08, -13).

  sources.py   the configured feeds, each bound to a provider record in config/providers with its rights verdict
  feeds.py     parsers: EDGAR Latest Filings Atom (form, CIK, role, accession, acceptance time, 8-K items) and
               generic RSS 2.0 / Atom (headline, link, time, summary as the feed gives it, truncated)
  store.py     the news store (SQLite under UNKNOWN_HOME/news/news.sqlite, FTS5 search, dedup, clusters, cursors)
  entities.py  issuer tagging with point-in-time identifiers from the research store (SEC CIK, ticker snapshots,
               OpenFIGI outcomes when present)
  poller.py    the polling process: ``python -m unknown.news.poller run [--once]``
  stream.py    Server-Sent Events for GET /api/news/stream ('news' and 'alert' events)

The poller is its own process; the API only reads the news store and never touches the core database for news.
"""
