"""Free official data sources ingested into the point-in-time store (ADR-0015; DAT-01..DAT-06).

* ``treasury``    US Treasury daily par yield curve XML feed (CAP-DATA-06, CAP-MAC-07)
* ``ecb``         ECB euro foreign exchange reference rates via the ECB Data API (CAP-DATA-07, CAP-MAC-12)
* ``openfigi``    OpenFIGI identifier mapping (CAP-DATA-04, CAP-INS-02)
* ``sec_tickers`` SEC company_tickers_exchange.json snapshots for search (CAP-INS-01, CAP-CO-01, CAP-INS-06)
* ``identifiers`` ISIN / CUSIP / SEDOL check digits and FIGI format, validated before any network call
* ``common``      declared User-Agent, capture-time availability rule, change-only recording

Each dataset has a registry entry under ``config/providers/`` with its rights, rate limit and
availability rule. Network access happens only in the ingest functions and their CLIs; tests use
fake openers, and live smoke tests are opt-in (``UNKNOWN_LIVE_SOURCES=1``).
"""
