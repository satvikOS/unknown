# Global data sources: EU issuers, global macro, India

Status words follow ADR-0001. Rights were read on 2026-10-23; each source's record in `config/providers/` holds the terms URL, the verdict and the permitted uses, and the registry refuses ingestion from a dataset without `storage`. Fetched data lives under `UNKNOWN_HOME` (default `~/.unknown/research/`), never in git. Every request sends the declared User-Agent from `~/.unknown/config/data.json` (`data_user_agent` or `sec_user_agent`).

## Sources

| Provider record | Dataset | What | Verdict | Module |
|---|---|---|---|---|
| `xbrl-filings` | `xbrl_org.esef_facts` | ESEF annual-report facts (ifrs-full, non-dimensional) from filings.xbrl.org | allowed for single-owner research; no redistribution | `data/sources/esef.py` |
| `esma-firds` | `esma.firds_equity` | FIRDS equity reference data (ISIN, MIC, CFI, name, currency, issuer LEI) for the EU venues | allowed with acknowledgement; condition list only partly read; host returned HTTP 502 all day | `data/sources/firds.py` |
| `world-bank` | `worldbank.wdi` | Annual GDP growth, CPI inflation, unemployment (ILO modelled) | allowed (CC BY 4.0; API non-commercial, reasonable volume) | `data/sources/worldbank.py` |
| `oecd` | `oecd.sdmx_macro` | Monthly CPI inflation (y/y) and unemployment rate (SA) | allowed with citation; paced at 1 request / 180 s | `data/sources/oecd.py` |
| `bis` | `bis.cbpol` | End-of-month policy rates: US, IN (RBI repo rate), euro area (ECB) | allowed with citation | `data/sources/bis.py` |
| `fred` | `fred.series` | Owner-selected FRED series (default DFEDTARU, DFEDTARL, UNRATE, A191RL1Q225SBEA, CPIAUCSL) | personal use with the FRED notice; **inactive without a key** | `data/sources/fred.py` |
| `imf` | `imf.data` | IMF data API | **blocked**: automated bulk download needs explicit IMF permission | none |
| `rbi` | `rbi.website_rates` | rbi.org.in Current Rates | **blocked**: the site terms prohibit caching of its contents | none (repo rate via BIS, INR reference FX via ECB) |
| `fbil` | `fbil.reference_rates` | FBIL INR reference rates | **blocked**: no terms located; benchmark licensing unreviewed | none |
| `nse-india`, `bse-india`, `sebi` | website data | quotes, filings, statistics | **blocked**: storing/aggregating forbidden (NSE), terms unreadable (BSE), no terms located (SEBI) | none |

## FRED key file

FRED stays inactive until the owner writes a free API key (one line) to `~/.unknown/secrets/fred_api_key` (or `<UNKNOWN_HOME>/secrets/fred_api_key`). `python -m unknown.data.sources.fred status` reports ACTIVE or INACTIVE with the path. The key is redacted from raw-request records, returned URLs and error messages.

## Commands

```bash
cd backend && export PYTHONPATH=src
python -m unknown.data.sources.esef ingest --lei 724500Y6DUVHQD6OXN27 --max-filings 3   # repeat --lei per issuer
python -m unknown.data.sources.firds ingest
python -m unknown.data.sources.worldbank ingest --from 2010
python -m unknown.data.sources.oecd ingest --from 2023-01
python -m unknown.data.sources.bis ingest --from 2015-01
python -m unknown.data.sources.fred ingest --from 2015-01-01
```

API (read-only): `GET /api/company/search?q=` (SEC and EU issuers), `GET /api/company/eu/<LEI>[/statements|/compare?sec=<CIK>]`, `GET /api/macro/global`, `GET /api/macro/global/series?column=&countries=`.

## Point-in-time rules

- ESEF: each filing (fxo_id) is a publication available at the index's `published` time read as UTC (never earlier than the real publication for EU time zones), else `date_added`. A later report's comparative for an earlier year is a later publication, flagged `from_later_filing` and `restated` when it differs.
- World Bank, OECD, FRED: no vintages are read, so values are available from first capture (`available_from_first_capture`); a changed value is a new publication flagged `revised_after_first_seen`. History is not usable for backtests before the capture date (ALFRED vintages would fix this).
- BIS policy rates: a backfilled month is available at 23:59:59 UTC on its last day (`availability_estimated_backfill`).

## Mapping notes (esef-ifrs-map@1)

`fundamentals/ifrs.py` maps ifrs-full concepts to the SEC line-item keys: revenue (`Revenue`, `RevenueFromContractsWithCustomers`), operating income (`ProfitLossFromOperatingActivities`), net income (`ProfitLossAttributableToOwnersOfParent`, fallback `ProfitLoss` flagged), total assets, equity (parent, fallback total `Equity` flagged), CFO, shares outstanding, weighted shares, basic EPS. Issuer extension concepts are not mapped (anchoring is not in xBRL-JSON): LVMH and TotalEnergies tag no `ProfitLossFromOperatingActivities`, so operating income shows NOT_REPORTED.

## Owner store ingestion, 2026-10-23

| Source | Result |
|---|---|
| ESEF | ASML 3 filings / 809 facts, LVMH 3 / 661, TotalEnergies 3 FR filings / 815 (its 4 UK FCA copies skipped); SAP and Siemens: NO_FILINGS_INDEXED (Germany is not indexed); Nestle skipped (not EU) |
| SEC (for side by side) | ASML CIK 937966: 11,381 facts; TotalEnergies CIK 879764: 9,531 facts |
| World Bank | 432 observations (3 indicators x 9 countries x 2010-2025) |
| OECD | 701 observations (CPI 349, unemployment 352) |
| BIS | 423 observations (US, IN, XM, 2015-01 onward) |
| FRED | 8,956 observations (owner key present) |
| FIRDS | failed: HTTP 502 from registers.esma.europa.eu after 5 attempts; nothing stored |

Side by side (FY2025): TotalEnergies ESEF and 20-F agree exactly on net income, assets, equity and CFO (USD). ASML's ESEF (IFRS) and 20-F (US GAAP) differ, e.g. operating income EUR 12,054.0m vs 11,301.4m; the difference is shown, never reconciled.
