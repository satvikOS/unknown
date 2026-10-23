# News & filings workspace (`g n`, functions N, TOP, CN)

Reads the news store written by the news poller (`python -m unknown.news.poller run`, ADR-0019):

- `GET /api/news` — the tape (filters: source, form, issuer, search, since); new items arrive on
  `GET /api/news/stream` (`news` events) and flash once.
- `GET /api/news/top` — stories ranked by the number of sources carrying them.
- `GET /api/news/item?id=` — the reader pane: feed summary (cut, never the whole release), typed 8-K items,
  tagged issuers with their identifier snapshot time, other copies of the story, the original link, fetch provenance.
- Company news (`CN`, route `#/news/cn`) follows the panel's linked security.
- Alerts: the issuers of the Monitor workspace's watchlists (browser storage) are sent on the stream URL;
  `alert` events list matches, one per story.

Sources the rights records block (ESMA, NSE, BSE) are listed with their reason and never fetched.
