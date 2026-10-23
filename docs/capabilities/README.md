# Capability register

Machine-readable source: `register.json` (import tool and checker: `scripts/capabilities/build_register.py`).
Reference products: Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro and IBKR, scoped only by what their
public documentation and product pages show (URLs and quotes per capability). Status vocabulary follows ADR-0001;
nothing is `paper-verified` or `live-verified` yet.

Parity is tracked in six separate dimensions per capability (functional, data coverage, data quality, latency,
usability, operational reliability). Proprietary datasets, interfaces and closed-network functions are not copied;
each capability records lawful alternatives and what cannot be replicated.

## How statuses are assigned

- `proposed`: researched and scoped; nothing built.
- `specified`: an accepted ADR or specification fixes the behaviour; no code.
- `implemented`: code for part of the capability exists, but either its main behaviour is not exercised by tests or the part that exists is a small fraction of the capability. The cited tests cover only what exists.
- `tested`: the capability's main behaviour exists and passing offline deterministic tests exercise it. `tested` does not mean complete: the gaps list the acceptance criteria and parts still missing.
- `paper-verified` / `live-verified`: verified against an IBKR paper or live account; needs recorded external evidence. Offline tests can never produce these.

Every status above `proposed` cites the implementing files, the tests that prove it and the gaps that remain
(`implementation.files`, `.tests`, `.gaps` in `register.json`). `build_register.py --check` verifies that every cited
file exists, every cited Python test class and method exists, and every cited terminal test is an enabled `it`/`test`
call under the cited `describe` titles; `--run-tests` runs the cited backend tests. Terminal unit tests run in CI; the
Playwright end-to-end tests (`terminal/e2e`) and the tests marked IBKR venv run only locally. Passing offline tests is
evidence of offline behaviour only.

Checked against `feat/live-market-data@77c16d1` on 2026-10-23; `feat/workstation-shell@c052431` on 2026-10-23; `impl/foundation@73f7b03` on 2026-10-22; `impl/foundation@8b98d00` on 2026-10-22.

## Summary by area

| Area | Capabilities | proposed | specified | implemented | tested | paper-verified | live-verified |
|---|---|---|---|---|---|---|---|
| instruments | 14 | 6 | 0 | 2 | 6 | 0 | 0 |
| market-data | 13 | 5 | 0 | 5 | 3 | 0 | 0 |
| company-research | 14 | 4 | 0 | 2 | 8 | 0 | 0 |
| news-documents | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| macro-rates-fx | 14 | 12 | 0 | 1 | 1 | 0 | 0 |
| portfolio-risk | 13 | 4 | 0 | 0 | 9 | 0 | 0 |
| multiasset | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| strategy-research | 14 | 2 | 0 | 3 | 9 | 0 | 0 |
| execution-oms | 14 | 5 | 0 | 1 | 8 | 0 | 0 |
| workspace-ux | 12 | 3 | 0 | 3 | 6 | 0 | 0 |
| agent-operations | 14 | 4 | 0 | 3 | 7 | 0 | 0 |
| ibkr-integration | 14 | 2 | 0 | 2 | 10 | 0 | 0 |
| data-providers | 14 | 8 | 0 | 2 | 4 | 0 | 0 |
| model-providers | 12 | 1 | 0 | 1 | 10 | 0 | 0 |
| regulatory | 13 | 7 | 0 | 2 | 4 | 0 | 0 |
| ops-aws | 14 | 7 | 0 | 4 | 3 | 0 | 0 |
| **all** | 217 | 98 | 0 | 31 | 88 | 0 | 0 |

## Capabilities


### instruments

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-INS-01 | P1 | Security search and instrument discovery | Bloomberg Terminal, IBKR TWS API, LSEG Workspace / LSEG Data Library, OpenFIGI ( | tested | 9 | no labelled 200-query fixture and no latency benchmark (AC1, AC2, AC5) |
| CAP-INS-02 | P0 | Identifier cross-reference and FIGI mapping | IBKR TWS API, LSEG Data Platform / Workspace, LSEG DataScope Select, OpenFIGI (B | tested | 6 | no 300-row hand-verified fixture (AC1) |
| CAP-INS-03 | P0 | Licensed identifier rights enforcement (CUSIP, SEDOL, RIC) | Bloomberg / S&P Capital IQ (library comparison), CUSIP Global Services (CGS) ter | implemented | 3 | no rights classes per column, no write-path interceptor, no licence records, no  |
| CAP-INS-04 | P0 | Broker contract resolution and verification (IBKR conid, Indian broker instrument tokens) | IBKR TWS API, Zerodha Kite Connect (India broker reference) | tested | 8 | Kite instrument tokens not implemented (AC5) |
| CAP-INS-05 | P1 | India listing master (NSE symbol/series, BSE code, ISIN, symbol changes) | NSE (exchange), NSE Data & Analytics, Zerodha Kite Connect | proposed |  | not yet implemented |
| CAP-INS-06 | P0 | Point-in-time instrument master and symbology history (ticker changes, reuse, name changes) | Bloomberg Terminal, Bloomberg enterprise data (reference data catalog page), LSE | tested | 5 | no symbology source with history before this system's first snapshot (SEC ticker |
| CAP-INS-07 | P0 | Exchange trading calendars, sessions, half days and ad hoc closures (US, India, Europe) | Deutsche Börse Xetra / Börse Frankfurt (exchange), Euronext (exchange), IBKR TWS | tested | 7 | no versioned ad hoc closures with replay pinning (AC2) |
| CAP-INS-08 | P0 | Settlement and clearing business-day calendars (T+n by market and currency) | Deutsche Börse Xetra / Börse Frankfurt (exchange), ECB T2 (central bank), Federa | tested | 7 | the core uses the instrument's settlement days and the policy calendar, not the  |
| CAP-INS-09 | P0 | Splits, reverse splits, bonus issues, face-value splits and price/volume adjustment factors | Bloomberg Terminal, IBKR TWS API, LSEG Corporate Actions (data product), LSEG Wo | proposed |  | not yet implemented |
| CAP-INS-10 | P0 | Cash dividends: declaration, ex, record and pay dates, amounts, receivables and total-return series | Bloomberg Terminal, IBKR TWS API, LSEG Corporate Actions (data product), LSEG Wo | proposed |  | not yet implemented |
| CAP-INS-11 | P1 | Mergers, acquisitions, spin-offs/demergers, rights issues, tender offers and buybacks | Bloomberg Terminal, Bloomberg enterprise data (reference data catalog page), LSE | proposed |  | not yet implemented |
| CAP-INS-12 | P1 | Delistings, suspensions, deficiency flags and survivorship-free universes | Nasdaq (exchange), SEC (regulator) | proposed |  | not yet implemented |
| CAP-INS-13 | P2 | Forward corporate-event calendar and alerts (ex-dates, splits, reorganizations, earnings dates) | Bloomberg Terminal, IBKR TWS API, LSEG Workspace / Data Library, S&P Capital IQ  | proposed |  | not yet implemented |
| CAP-INS-14 | P2 | Venue (ISO 10383 MIC) and issuer (LEI, PermID) reference linkage | Bloomberg enterprise data (reference data catalog page), ESMA FIRDS (regulator), | implemented | 2 | no MIC change loading or versioning (AC1, AC2) |

### market-data

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-MKT-01 | P0 | Level 1 quotes with explicit real-time / delayed / frozen / snapshot status, source and age | Bloomberg B-PIPE, IBKR (all APIs), IBKR TWS API, IBKR Web API, LSEG Workspace, L | tested | 11 | Web API status codes (R, D, Z, Y, N, O, P, p) not mapped (TWS only) |
| CAP-MKT-02 | P1 | Consolidated (NBBO / tape) versus single-venue and derived best quotes across US, Europe and India | Bloomberg B-PIPE, IBKR Web API, LSEG Workspace, US SIP (CTA Plan) | tested | 5 | no derived best across venues and no venue panel (AC2, AC3) |
| CAP-MKT-03 | P1 | Trades and time & sales (live tick-by-tick and historical) with conditions, corrections and halts | Bloomberg B-PIPE, IBKR TWS, IBKR TWS API, LSEG Workspace | implemented | 6 | no historical backfill (reqHistoricalTicks or vendor history) |
| CAP-MKT-04 | P0 | Intraday bars (seconds to hours), live-updating and historical, with explicit completeness | IBKR TWS API, IBKR Web API, LSEG Data Library (Workspace) | tested | 5 | no live-updating bars and no platform bars built from trades (AC2) |
| CAP-MKT-05 | P0 | Daily/weekly/monthly bars with raw and versioned split/dividend-adjusted series | IBKR TWS API, LSEG Data Library (Workspace) | implemented | 2 | no split/dividend adjustment factors, no total-return series, no factor versions |
| CAP-MKT-06 | P2 | Market depth (Level 2) by price and by order, where entitled | Bloomberg B-PIPE, IBKR TWS, IBKR TWS API, LSEG Workspace / Data Library | implemented | 4 | no depth by order (only by price, with market maker where IBKR sends it) |
| CAP-MKT-07 | P1 | Price charts with technical studies, comparisons and annotations | Bloomberg Terminal, IBKR TWS API, S&P Capital IQ Pro | proposed |  | not yet implemented |
| CAP-MKT-08 | P0 | Watchlists / monitors with per-cell status, as-of dates and market-data line budgeting | Bloomberg Terminal, IBKR (all platforms), IBKR Web API, S&P Capital IQ Pro | implemented | 7 | rows beyond the budget are refused, not polled or served delayed (AC1) |
| CAP-MKT-09 | P1 | Alerts on price, volume, time, study values, news and filings | Bloomberg Terminal, IBKR Web API, LSEG Workspace, S&P Capital IQ Pro | proposed |  | not yet implemented |
| CAP-MKT-10 | P1 | Equity screening with point-in-time criteria, reproducible as-of runs and real-time scans | Bloomberg Terminal, Bloomberg Terminal (via MATLAB Datafeed Toolbox), IBKR TWS A | proposed |  | not yet implemented |
| CAP-MKT-11 | P0 | Market-data entitlement, professional status, display vs non-display use and fee registry | Bloomberg B-PIPE, IBKR (all APIs), IBKR (all platforms) | implemented | 2 | no entitlement and fee registry with display vs non-display use, no cost report, |
| CAP-MKT-12 | P1 | India equity market data (NSE and BSE): quotes, depth, candles, instruments and licensing | BSE India (via Deutsche Börse licensing), Zerodha Kite Connect, Zerodha Kite Con | proposed |  | not yet implemented |
| CAP-MKT-13 | P2 | European equity market data: per-venue quotes, delayed public data and the EU consolidated tape | Bloomberg B-PIPE, EU consolidated tape (EuroCTP), LSEG Workspace | proposed |  | not yet implemented |

### company-research

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-CO-01 | P0 | Issuer, filer and listing identity resolution for company research | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, S&P Capital IQ Pro | tested | 7 | no 200-ticker resolution fixture and no latency target run (AC1, AC8) |
| CAP-CO-02 | P0 | US SEC filings feed, archive and full-text search | Bloomberg Terminal, LSEG Workspace / LSEG fundamentals, S&P Capital IQ Pro | implemented | 3 | no filing feed or archive: filings are listed only for issuers whose submissions |
| CAP-CO-03 | P1 | India and Europe company filings ingestion (ESEF, UKSEF, NSE/BSE, MCA) under explicit rights gates | IBKR TWS Fundamentals Explorer, LSEG Workspace / LSEG fundamentals | proposed |  | not yet implemented |
| CAP-CO-04 | P1 | As-reported financial statements from XBRL (income, balance sheet, cash flow, segments) | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace / LSEG fundam | tested | 7 | statements follow the project's standardized mapping, not each filing's presenta |
| CAP-CO-05 | P1 | Standardized line items, currency/unit normalization, calendarization and TTM | Bloomberg Terminal, LSEG Workspace / LSEG fundamentals, S&P Capital IQ Pro | tested | 5 | no 100-issuer coverage report (AC2) |
| CAP-CO-06 | P1 | Point-in-time fundamentals, amendment and restatement history | Bloomberg (Enterprise Data), LSEG Workspace / LSEG fundamentals, S&P Capital IQ  | tested | 6 | no 8-K 4.02 NON_RELIANCE marking (AC6) |
| CAP-CO-07 | P1 | Valuation multiples, financial ratios and documented enterprise-value bridge | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace / LSEG fundam | tested | 8 | market multiples need an explicit price input: no licensed price source is wired |
| CAP-CO-08 | P1 | Analyst consensus estimates, revisions, actuals and earnings surprise (licensed) | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace (I/B/E/S), S& | proposed |  | not yet implemented |
| CAP-CO-09 | P1 | Ownership: institutional (13F), insider (Forms 3/4/5), major holders (13D/13G) and non-US shareholding disclosures | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace, S&P Capital  | proposed |  | not yet implemented |
| CAP-CO-10 | P1 | Peer group construction and trading comparables (current and point-in-time) | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, S&P Capital IQ Pro | tested | 5 | no saved named peer sets (AC2) |
| CAP-CO-11 | P1 | DCF and WACC valuation with sensitivity surface and valuation range summary | Bloomberg Terminal, S&P Capital IQ Pro | tested | 8 | no EUR risk-free curve (AC2) |
| CAP-CO-12 | P1 | Company tearsheet / overview page with sourced key facts | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, S&P Capital IQ Pro | tested | 4 | no quote block with live status (AC3) |
| CAP-CO-13 | P2 | Spreadsheet export with live formulas and a programmatic data function (Excel plug-in parity) | Bloomberg Terminal, LSEG Data Library for Python, LSEG Workspace, S&P Capital IQ | proposed |  | not yet implemented |
| CAP-CO-14 | P2 | Filing and document question answering with verifiable citations | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | implemented | 5 | filing and document text is not ingested, so questions are answered over XBRL fa |

### news-documents

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-NEWS-01 | P0 | Rights registry and cloud-model processing gate for news, transcripts and documents | IBKR (TWS API), LSEG Workspace | proposed |  | not yet implemented |
| CAP-NEWS-02 | P0 | Real-time SEC EDGAR filing stream with 8-K item typing | LSEG (Intelligent Tagging NLP service; the fact sheet names Eikon and Reuters Ne | proposed |  | not yet implemented |
| CAP-NEWS-03 | P1 | India, UK and EU regulatory disclosure ingestion | LSEG Workspace, S&P Capital IQ (Key Developments dataset; the page names 'S&P Ca | proposed |  | not yet implemented |
| CAP-NEWS-04 | P1 | Licensed real-time newswire ingestion with history backfill | Bloomberg Terminal, IBKR (TWS API), LSEG (Machine Readable News feed; Workspace  | proposed |  | not yet implemented |
| CAP-NEWS-05 | P1 | Syndication deduplication, story clustering and revision tracking | LSEG Workspace, S&P Capital IQ (Key Developments dataset; Capital IQ Pro not nam | proposed |  | not yet implemented |
| CAP-NEWS-06 | P0 | Issuer and instrument entity tagging with point-in-time identifier resolution | LSEG (Intelligent Tagging NLP service; the fact sheet names Eikon and Reuters Ne | proposed |  | not yet implemented |
| CAP-NEWS-07 | P1 | Earnings and investor call transcripts with speaker turns, versions and live streaming | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed |  | not yet implemented |
| CAP-NEWS-08 | P1 | Full-text search across filings, announcements, transcripts and news | Bloomberg Terminal, LSEG (news products page; Workspace not named on page), S&P  | proposed |  | not yet implemented |
| CAP-NEWS-09 | P1 | Typed corporate event detection from filings, announcements and news | LSEG (Intelligent Tagging NLP service; the fact sheet names Eikon and Reuters Ne | proposed |  | not yet implemented |
| CAP-NEWS-10 | P1 | Scheduled corporate event calendar (earnings dates, calls, board meetings) | IBKR (TWS API), LSEG Workspace | proposed |  | not yet implemented |
| CAP-NEWS-11 | P2 | Sentiment scoring for news, filings and transcripts | LSEG (MarketPsych Analytics data feeds; Workspace not named on page), LSEG (Star | proposed |  | not yet implemented |
| CAP-NEWS-12 | P1 | Grounded summaries and multi-document Q&A with span-level citations | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed |  | not yet implemented |
| CAP-NEWS-13 | P1 | Alerts on filings, announcements, news and events for watchlists | IBKR (TWS API), LSEG Workspace | proposed |  | not yet implemented |
| CAP-NEWS-14 | P2 | Event studies and news-impact analytics with point-in-time availability | LSEG (StarMine model fact sheet; Workspace not named in document), S&P Capital I | proposed |  | not yet implemented |

### macro-rates-fx

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-MAC-01 | P1 | Economic release calendar (scheduled releases for the US, euro area/EU and India) | Bloomberg (enterprise data catalogue), Bloomberg Terminal, Bloomberg point-in-ti | proposed |  | not yet implemented |
| CAP-MAC-02 | P1 | Release-time capture of actuals, revisions to prior and first prints | Bloomberg (EcoNext delivery for ECO data), IBKR (Econoday on Interactive Brokers | proposed |  | not yet implemented |
| CAP-MAC-03 | P1 | Consensus forecasts, point-in-time consensus snapshots and surprise metrics | Bloomberg Terminal, Bloomberg point-in-time economic dataset (trade press), IBKR | proposed |  | not yet implemented |
| CAP-MAC-04 | P1 | Macro series vintages and point-in-time (as-of) queries | Bloomberg point-in-time economic dataset (trade press), LSEG economic data, LSEG | proposed |  | not yet implemented |
| CAP-MAC-05 | P1 | Macro time-series catalog, retrieval, transformations and country-indicator matrix | Bloomberg Terminal, Bloomberg Terminal (economics), LSEG economic data, S&P Capi | proposed |  | not yet implemented |
| CAP-MAC-06 | P1 | Central bank meeting calendar, decisions and policy-rate history (Fed, ECB, RBI) | Bloomberg (enterprise data catalogue, ECO), IBKR (Econoday) | proposed |  | not yet implemented |
| CAP-MAC-07 | P1 | US Treasury par/real yield curves, H.15, fitted zero curve and overnight reference rates | IBKR TWS API, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Universit | implemented | 4 | H.15 reconciliation, GSW fitted zero curve and NY Fed overnight rates not built  |
| CAP-MAC-08 | P1 | Euro area government yield curves (ECB), EUR short-term rate and UK gilt/OIS curves (BoE) | Bloomberg MAC3 (risk model), LSEG/Refinitiv Datastream (2022 fact sheet hosted b | proposed |  | not yet implemented |
| CAP-MAC-09 | P1 | India policy rates, money-market benchmarks and government securities curve | LSEG Reuters Polls, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Uni | proposed |  | not yet implemented |
| CAP-MAC-10 | P1 | Yield-curve analytics: interpolation, bootstrapping, forwards, spreads, curve history and factor decomposition | Bloomberg MAC3, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Univers | proposed |  | not yet implemented |
| CAP-MAC-11 | P2 | Market-implied policy-rate path and meeting probabilities, with published-proxy fallback | Bloomberg Terminal, LSEG Reuters Polls | proposed |  | not yet implemented |
| CAP-MAC-12 | P0 | Official FX reference rates (ECB, Fed H.10, FBIL/RBI) for valuation and reporting | Bloomberg MAC3, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Univers | tested | 4 | Fed H.10 and FBIL/RBI not ingested (AC2, AC3) |
| CAP-MAC-13 | P1 | Tradable FX quotes, cross-rate matrix and currency attribution of foreign equity returns | IBKR TWS API, IBKR Trader Workstation, LSEG/Refinitiv Datastream (2022 fact shee | proposed |  | not yet implemented |
| CAP-MAC-14 | P1 | Cross-asset and cross-market correlation, rolling and shrinkage estimates, and macro-factor betas | Bloomberg MAC3, IBKR Trader Workstation, LSEG/Refinitiv Datastream (2022 fact sh | proposed |  | not yet implemented |

### portfolio-risk

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-PORT-01 | P0 | Consolidated holdings, per-currency cash ladder and NAV across brokers and accounts | Bloomberg PORT, IBKR PortfolioAnalyst, IBKR TWS Risk Navigator, S&P Capital IQ P | tested | 9 | single broker account only: no multi-broker consolidation (AC4) |
| CAP-PORT-02 | P0 | Daily, realized and unrealized P&L with tax-lot cost basis, fee finalization and FX translation | IBKR TWS API, IBKR TWS Risk Navigator, Zerodha Kite Connect | tested | 4 | no FX translation P&L (AC2) |
| CAP-PORT-03 | P0 | Exposure, concentration and fund look-through by sector, country, currency and beta | Bloomberg PORT, IBKR PortfolioAnalyst, IBKR TWS Risk Navigator, S&P Capital IQ P | tested | 5 | no fund look-through (AC3, AC4) |
| CAP-PORT-04 | P0 | Time-weighted and money-weighted return measurement with GIPS-consistent cash-flow handling | GIPS 2020 (CFA Institute), IBKR PortfolioAnalyst | tested | 7 | no component (sector) returns by daily aggregation (AC5) |
| CAP-PORT-05 | P1 | Ex-post risk and risk-adjusted performance statistics (Sharpe, Sortino, IR, TE, beta, alpha, drawdown, Calmar) | Bloomberg PORT, GIPS 2020 (CFA Institute), IBKR PortfolioAnalyst | tested | 5 | benchmark-relative statistics have no benchmark series to consume (CAP-PORT-06) |
| CAP-PORT-06 | P1 | Benchmark registry: index levels, total-return proxies, blended benchmarks and constituent data rights | Bloomberg PORT, IBKR PortfolioAnalyst, S&P Capital IQ Portfolio Analytics, S&P C | proposed |  | not yet implemented |
| CAP-PORT-07 | P1 | Holdings-based Brinson performance attribution (allocation, selection, interaction) with multi-period linking and contribution to return | Bloomberg PORT, Bloomberg PORT Enterprise, IBKR PortfolioAnalyst, LSEG Workspace | proposed |  | not yet implemented |
| CAP-PORT-08 | P1 | Equity factor risk model and ex-ante risk decomposition (total, active, factor vs specific, marginal contributions) | Bloomberg PORT, LSEG Workspace, S&P Capital IQ Pro Portfolio Analytics | proposed |  | not yet implemented |
| CAP-PORT-09 | P2 | Returns-based factor and style attribution using public factor libraries | Bloomberg PORT, LSEG Workspace Portfolio Analytics, S&P Capital IQ Pro Portfolio | proposed |  | not yet implemented |
| CAP-PORT-10 | P1 | Value-at-Risk and Expected Shortfall (historical, parametric, Monte Carlo) with backtesting | Basel Committee (BIS) market risk standard, Bloomberg PORT, IBKR TWS Risk Naviga | tested | 8 | historical method only: no parametric or Monte Carlo VaR, no component VaR (AC3- |
| CAP-PORT-11 | P1 | Stress testing and scenario analysis: historical replays, custom shocks, factor-propagated and regulator-published scenarios | Bloomberg PORT, EBA 2025 EU-wide stress test, Federal Reserve supervisory stress | tested | 7 | no historical replays and no regulator scenarios (Fed, EBA) (AC2, AC3) |
| CAP-PORT-12 | P0 | What-if portfolio sandbox and pre-trade portfolio impact (exposure, cash, VaR/ES, limits, broker margin/commission preview) | IBKR TWS API, IBKR TWS Risk Navigator, S&P Capital IQ Portfolio Analytics | tested | 7 | no broker what-if commission and margin preview (CAP-IBKR-10) |
| CAP-PORT-13 | P1 | Constrained portfolio optimisation, target allocations and rebalancing trade lists | Bloomberg PORT, IBKR PortfolioAnalyst, LSEG Workspace, S&P Capital IQ Portfolio  | tested | 8 | no sector or group caps, no tracking-error minimisation, no allocation-goal drif |

### multiasset

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-MA-01 | P0 | Research-only eligibility gate for options, bonds, futures, FX, crypto and other non-equity classes | IBKR Options Strategy Builder, IBKR Portfolio Performance Profile, IBKR TWS API, | proposed |  | not yet implemented |
| CAP-MA-02 | P1 | Option chains and derivative contract master (US, India, Europe) | Bloomberg Terminal, IBKR TWS API, IBKR TWS OptionTrader, LSEG Workspace for equi | proposed |  | not yet implemented |
| CAP-MA-03 | P1 | Option fair-value models and implied volatility solver with arbitrage bounds | Bloomberg Terminal, IBKR TWS API, IBKR TWS OptionTrader, LSEG Workspace for equi | proposed |  | not yet implemented |
| CAP-MA-04 | P1 | Option Greeks with explicit unit conventions and position-level aggregation | Bloomberg Terminal, IBKR Options Trading Tools, IBKR TWS API, LSEG Workspace for | proposed |  | not yet implemented |
| CAP-MA-05 | P1 | Implied volatility smiles, surfaces, term structure and IV-vs-realised history | Academic method (Gatheral and Jacquier, 'Arbitrage-free SVI volatility surfaces' | proposed |  | not yet implemented |
| CAP-MA-06 | P2 | Multi-leg option strategy builder, payoff and scenario P&L (research-only) | Bloomberg Terminal, IBKR Option Strategy Lab, IBKR Options Strategy Builder, IBK | proposed |  | not yet implemented |
| CAP-MA-07 | P2 | Market-implied probability distribution and user-view trade evaluation (Probability Lab analog) | IBKR Options Trading Tools, IBKR Probability Lab | proposed |  | not yet implemented |
| CAP-MA-08 | P1 | Portfolio risk slices, what-if portfolios and custom price/date/volatility scenarios | Bloomberg PORT, IBKR Portfolio Performance Profile, IBKR Risk Navigator, IBKR TW | proposed |  | not yet implemented |
| CAP-MA-09 | P1 | Government yield curves (par, zero, forward) for US, euro area and India with vintages | ECB euro area yield curves, LSEG Workspace for fixed income, S&P Capital IQ Pro, | proposed |  | not yet implemented |
| CAP-MA-10 | P2 | Bond calculator: price/yield, accrued interest, duration, convexity, DV01 and cash flows | Bloomberg Terminal, IBKR Bond Search, LSEG Workspace for fixed income, LSEG Yiel | proposed |  | not yet implemented |
| CAP-MA-11 | P2 | Corporate bond trade prints (TRACE) and credit spread analytics (G-spread, Z-spread, relative value) | Bloomberg FIW, Bloomberg PORT, FINRA Fixed Income Data, LSEG Workspace for fixed | proposed |  | not yet implemented |
| CAP-MA-12 | P2 | Futures curves, calendar spreads, carry/roll analytics and continuous series (research construction) | Eurex (exchange), IBKR TWS API, LSEG Workspace for fixed income, LSEG option vol | proposed |  | not yet implemented |
| CAP-MA-13 | P1 | FX spot, cross rates, reference fixings and covered-interest-parity forward analytics | Bloomberg Terminal, IBKR TWS API, LSEG Workspace FX trading | proposed |  | not yet implemented |
| CAP-MA-14 | P3 | Crypto spot market data and analytics (research-only, no custody or keys) | Coinbase Exchange API, IBKR TWS API, IBKR cryptocurrency trading | proposed |  | not yet implemented |

### strategy-research

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-RES-01 | P0 | Point-in-time fundamentals, filings and macro vintages for research (bitemporal as-of layer) | Bloomberg Company Financials, Estimates and Pricing Point-in-Time, Bloomberg Com | tested | 6 | macro vintages (ALFRED) not ingested: FRED needs an owner key |
| CAP-RES-02 | P0 | Survivorship-free universe membership, security master and adjusted price history for backtests | Bloomberg investment research data (PiT), CRSP US Stock Databases (Morningstar), | proposed |  | not yet implemented |
| CAP-RES-03 | P1 | Research workspace with programmatic data query API, notebooks and scheduled jobs | Bloomberg API (BLPAPI) library, Bloomberg BQuant Desktop / BQuant Enterprise, Ke | implemented | 5 | no notebooks, no programmatic client library and no job scheduler |
| CAP-RES-04 | P0 | Causal event-driven backtest engine with explicit fill model and deterministic replay | Academic method: Lopez de Prado, Advances in Financial Machine Learning (Wiley,  | tested | 7 | daily bars only: no intraday execution window, auction or queue model (book find |
| CAP-RES-05 | P0 | Transaction cost, market impact and capacity model for backtests and promotion | Academic: Almgren and Chriss, Optimal execution of portfolio transactions, Journ | tested | 6 | third-party sell fees are an UNVERIFIED 1 bp assumption |
| CAP-RES-06 | P1 | Cross-sectional factor and signal research (IC, quantile portfolios, neutralization, decay, turnover) | Academic: Harvey, Liu and Zhu, ...and the Cross-Section of Expected Returns (NBE | proposed |  | not yet implemented |
| CAP-RES-07 | P0 | Walk-forward evaluation with purging, embargo and combinatorial purged cross-validation | Academic: Bailey, Borwein, Lopez de Prado and Zhu, The Probability of Backtest O | tested | 5 | no market-data evaluation yet |
| CAP-RES-08 | P0 | Multiple-testing correction and backtest-overfitting diagnostics (Deflated Sharpe, PBO/CSCV, SPA/StepM, haircuts) | Academic: Bailey and Lopez de Prado, The Deflated Sharpe Ratio, Academic: Bailey | tested | 6 | no SPA/StepM test |
| CAP-RES-09 | P0 | Dependence-aware performance statistics and paired candidate-versus-incumbent comparison | Academic: Ledoit and Wolf (2008), Robust performance hypothesis testing with the | tested | 5 | coverage under strong dependence is 92.7% at phi = 0.6 (below the 93-97% band);  |
| CAP-RES-10 | P0 | Pre-registered experiment and complete trial registry (including failures) | Academic: Bailey and Lopez de Prado, The Deflated Sharpe Ratio, Bloomberg BQuant | tested | 9 | research registry uses WAL with default synchronous settings |
| CAP-RES-11 | P0 | Reproducibility manifests and deterministic replay of experiments | Academic: Lyle, Siano and Yohn (2024), on restandardized Compustat data, DVC (op | tested | 5 | no external artifact store (DVC or similar); manifests live in the local registr |
| CAP-RES-12 | P0 | Strategy artifact registry, status lifecycle and predeclared promotion gates | FCA, Algorithmic Trading Compliance in Wholesale Markets (summary of RTS 6 Artic | tested | 11 | ADR-0017 is proposed, not accepted |
| CAP-RES-13 | P0 | Shadow mode and IBKR paper-trading stage with divergence measurement | FCA review (summary of RTS 6), IBKR Paper Trading Account (Client Portal guide), | implemented | 5 | the paper stage (live mode on an IBKR paper account) has never run |
| CAP-RES-14 | P1 | Ongoing performance and drift monitoring, quarantine and rollback of active artifacts | Academic: Bailey et al., The Probability of Backtest Overfitting, FCA review (su | implemented | 4 | no performance or drift monitoring produces MONITORING_INCIDENT evidence |

### execution-oms

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-OMS-01 | P0 | Order ticket: order-type, time-in-force and session capability catalog | Bloomberg EMSX, IBKR TWS API, IBKR Trader Workstation, LSEG REDI on Workspace, Z | implemented | 5 | no capability matrix keyed by (broker, account mode, product, venue, session, or |
| CAP-OMS-02 | P0 | Order lifecycle state machine, parent/child model and order blotter | Bloomberg EMSX, IBKR TWS API, LSEG REDI on Workspace, Zerodha Kite Connect v3 | tested | 15 | parent/child orders not implemented |
| CAP-OMS-03 | P0 | Pre-trade controls: order presets, precautionary limits and rule-based compliance | Bloomberg AIM, IBKR Org Portal, IBKR Pre-Trade Compliance, IBKR TWS API, IBKR Tr | tested | 11 | order presets (instrument-type and ticker level) not implemented |
| CAP-OMS-04 | P0 | IBKR connectivity: TWS/IB Gateway session, authentication, pacing and transport options (US and Europe) | Bloomberg EMSX, IBKR FIX, IBKR TWS API, IBKR Web API, LSEG Autex Trade Route | tested | 8 | Web API / FIX transports not implemented (TWS socket only) |
| CAP-OMS-05 | P2 | India broker adapter (Kite Connect) with SEBI retail-algo controls | SEBI, Zerodha Kite Connect v3 | proposed |  | not yet implemented |
| CAP-OMS-06 | P0 | Execution (fill) and commission event capture with corrections and history backfill | Bloomberg EMSX, IBKR TWS API, IBKR Web API, Zerodha Kite Connect v3 | tested | 14 | history backfill beyond the broker's window needs statements (CAP-OMS-07, not bu |
| CAP-OMS-07 | P0 | Broker statements and trade confirmations ingestion (IBKR Flex, activity statements) | IBKR Client Portal, IBKR Flex Web Service, IBKR Web API, IBKR Web API (Account M | proposed |  | not yet implemented |
| CAP-OMS-08 | P0 | Three-way reconciliation: real-time events, intraday broker snapshot and end-of-day statement vs ledger | Bloomberg AIM, IBKR Flex Queries, IBKR TWS API, LSEG REDI on Workspace, Zerodha  | tested | 11 | tier 3, the end-of-day statement leg (IBKR Flex), is not implemented: two of thr |
| CAP-OMS-09 | P0 | Settlement-date calculation (US T+1, India T+1/T+0, EU T+2 to T+1 on 11 Oct 2027) and settled-cash availability | European Commission, Federal Reserve Board, IBKR TWS API, NSE Clearing, NYSE, Na | tested | 8 | the core does not read the venue registry: its settlement days come from the ins |
| CAP-OMS-10 | P1 | Corporate-action processing: mandatory events, voluntary elections and lot/basis adjustments | IBKR Client Portal, IBKR Flex Queries, IBKR Web API, NSE, SEC EDGAR | proposed |  | not yet implemented |
| CAP-OMS-11 | P0 | Commission, fee and tax-charge estimation with estimated-vs-final tracking | Bloomberg EMSX, IBKR, IBKR TWS API, Zerodha Kite Connect v3 | tested | 6 | fee model values UNVERIFIED pending IBKR schedule confirmation; core and researc |
| CAP-OMS-12 | P1 | Execution algorithms: broker algos (IBKR) and in-house parent/child scheduling (TWAP, participation) | Bloomberg AIM, Bloomberg EMSX, IBKR Paper Trading, IBKR TWS API, IBKR Web API, L | proposed |  | not yet implemented |
| CAP-OMS-13 | P1 | Transaction cost analysis (arrival, price improvement, VWAP, markouts, daily range) | Bloomberg BTCA, IBKR Client Portal, IBKR Org Portal | proposed |  | not yet implemented |
| CAP-OMS-14 | P0 | Simulation, broker-paper and live evidence levels with paper-environment limitations | IBKR Flex Web Service, IBKR Paper Trading, IBKR TWS API | tested | 7 | no paper-limitations register and no UNTESTED labelling of paper-unsupported ord |

### workspace-ux

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-UX-01 | P0 | Command line / palette for function, instrument, document and help search | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro, W3C WAI-ARIA Authoring P | tested | 3 | no instrument, document or help search index in the palette (AC1-AC4) |
| CAP-UX-02 | P0 | Keyboard-first operation: scoped, rebindable shortcuts, hint mode, history and safe order hotkeys | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace, LSEG Workspace (acc | tested | 4 | shortcuts are not rebindable (AC2, AC7) |
| CAP-UX-03 | P1 | Linked panels: numbered/coloured instrument-context channels with source/destination roles | Bloomberg Terminal, Bloomberg Terminal (via HERE Core 'Bloomberg integration v1. | tested | 6 | No source/destination roles: every panel both sends and receives. |
| CAP-UX-04 | P1 | Persistent, versioned multi-panel workspace layouts with templates, lock mode and recovery | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace, S&P Capital IQ (pla | tested | 10 | Browser localStorage only: no backend layout service, no cross-device sync and n |
| CAP-UX-05 | P2 | Bookmarks, favourites bar and deep links that restore a panel with instrument and parameters (live or as-of) | Bloomberg Terminal, LSEG Workspace | implemented | 2 | no bookmarks or favourites bar (AC1, AC5) |
| CAP-UX-06 | P1 | Accessibility: WCAG 2.2 AA conformance for dense financial screens, with a published conformance report | LSEG Workspace, W3C WAI-ARIA APG, W3C WCAG 2.2 | tested | 4 | no manual WCAG 2.2 audit and no published conformance report (AC2, AC7) |
| CAP-UX-07 | P1 | Export any grid to CSV/XLSX with provenance, exact decimals, injection protection and licence gating; validated re-import | Bloomberg Terminal, IBKR Client Portal, IBKR Trader Workstation, LSEG Workspace, | proposed |  | not yet implemented |
| CAP-UX-08 | P2 | Optional spreadsheet bridge: custom functions, formula builder and audit from Excel to the private API | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace, S&P Capital IQ Pro | proposed |  | not yet implemented |
| CAP-UX-09 | P0 | 'Show calculation' inspector: formula, inputs, units, adjustments and level-by-level lineage to source records | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | implemented | 4 | the terminal inspector covers company metrics only (end-to-end test); desk and p |
| CAP-UX-10 | P1 | Data-item dictionary and contextual function help (definitions, formulas, units, sources, coverage, rights) | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed |  | not yet implemented |
| CAP-UX-11 | P0 | Value display semantics: currency, scale, locale grouping, time zones, sign glyphs, staleness and explicit missingness | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace (anti-pattern from i | tested | 5 | no locale switching (en-IN, de-DE) (AC2) |
| CAP-UX-12 | P1 | Live panels: pause/freeze, manual refresh scopes, snapshot-on-gap and point-in-time (as-of) viewing | LSEG Workspace, LSEG Workspace (ACR baseline, March 2025), S&P Capital IQ Pro, W | implemented | 3 | no pause/freeze per panel (AC1, AC2) |

### agent-operations

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-AGT-01 | P0 | Bounded agent jobs: envelope, deadlines, turn/tool/retry caps, no unbounded spawning | Bloomberg Terminal, Claude Agent SDK, LSEG Workspace, OWASP Top 10 for LLM Appli | tested | 8 | kill_grace_s is not in the envelope; a provider call in flight at the deadline i |
| CAP-AGT-02 | P0 | Agent cost budgets: pre-call reservation, hierarchical caps, provider hard limits and billing reconciliation | Claude API (Anthropic), Claude Agent SDK, LSEG Workspace, OWASP Top 10 for LLM A | tested | 6 | provider cost-report reconciliation is manual: reconcile() takes an owner eviden |
| CAP-AGT-03 | P0 | Per-role tool allowlists and capability manifests enforced outside the model | Bloomberg Terminal, FINRA 2026 Annual Regulatory Oversight Report, Model Context | tested | 6 | no owner-signed capability manifests (AC3) |
| CAP-AGT-04 | P0 | Human-in-the-loop approval queue binding owner decisions to exact proposals | FINRA 2026 Annual Regulatory Oversight Report, IBKR, Model Context Protocol spec | proposed |  | not yet implemented |
| CAP-AGT-05 | P0 | Owner intervention controls: pause roles, cancel jobs, disable model-dependent decisions, red stop with order cancel | EU MiFID II RTS 6 (Delegated Regulation 2017/589), UK onshored copy, IBKR TWS AP | tested | 11 | no 'disable model-dependent decisions' switch: no model output feeds orders toda |
| CAP-AGT-06 | P0 | Prompt-injection defence for filings, news, web pages, owner uploads and tool outputs | Bloomberg Terminal, Claude Agent SDK, NIST AI 600-1 Generative AI Profile, OWASP | tested | 4 | no injection corpus or canary scanner (AC1, AC2) |
| CAP-AGT-07 | P0 | Secret and sensitive-data isolation for agents (credentials, account ids, owner data) | IBKR, IBKR TWS API, OWASP Top 10 for LLM Applications 2025, SEBI | implemented | 3 | the worker runs as the same OS user as the core, so a same-user process can read |
| CAP-AGT-08 | P0 | Data-rights gating before content enters a model context (licence, entitlement, retention, provider) | LSEG Workspace, NIST AI RMF 1.0 (AI 100-1), S&P Global / Kensho | tested | 5 | no label propagation to derived summaries and exports (AC2) |
| CAP-AGT-09 | P0 | Agent audit trail: tamper-evident lineage from evidence to job to proposal to order | FINRA 2026 Annual Regulatory Oversight Report, Model Context Protocol specificat | tested | 7 | proposal-to-order lineage: no order intent consumes research proposals yet |
| CAP-AGT-10 | P1 | Grounded research Q&A over filings and permitted documents with span-level citations and generated query code | Bloomberg Terminal, LSEG Workspace, OWASP Top 10 for LLM Applications 2025, S&P  | implemented | 6 | filing and document text is not ingested: spans are XBRL facts only |
| CAP-AGT-11 | P1 | AI summaries of news and earnings events filtered to holdings and watchlists, with provenance | IBKR, LSEG Workspace, NIST AI 600-1 Generative AI Profile, OWASP Top 10 for LLM  | proposed |  | not yet implemented |
| CAP-AGT-12 | P1 | Model/provider registry with version pinning, measured routing, fallback and change control | Bloomberg Terminal, LSEG Workspace, NIST AI 600-1 Generative AI Profile, NIST AI | implemented | 4 | no evaluation records, so routing is by owner order, not measured quality (AC4) |
| CAP-AGT-13 | P1 | Agent observability: traces, metrics, cost and quality dashboards with service objectives | NIST AI 600-1 Generative AI Profile, NIST AI RMF 1.0 (AI 100-1), OpenAI Agents S | proposed |  | not yet implemented |
| CAP-AGT-14 | P1 | Agent evaluation suites, red-team tests and release gate for model, prompt, tool and manifest changes | LSEG Workspace, NIST AI 600-1 Generative AI Profile, NIST AI RMF 1.0 (AI 100-1), | proposed |  | not yet implemented |

### ibkr-integration

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-IBKR-01 | P0 | TWS API SDK distribution, licensing and TWS/IB Gateway version pinning | IBKR IB Gateway, IBKR TWS, IBKR TWS / IB Gateway, IBKR TWS API, PyPI (third-part | tested | 5 | no per-session version attestation event with host build and installer hash (AC2 |
| CAP-IBKR-02 | P0 | TWS/IB Gateway socket connection, paper vs live ports and account-identity isolation | IBKR Campus, IBKR IB Gateway, IBKR Paper Trading Account, IBKR TWS API, IBKR TWS | tested | 7 | no persisted session record with server_version and connect time (AC5) |
| CAP-IBKR-03 | P0 | Order identifiers and ownership: nextValidId, orderId, permId, clientId, binding and cancel scope | IBKR TWS API | tested | 7 | whether orderRef is echoed for individual IBKR Pro accounts is unverified (paper |
| CAP-IBKR-04 | P0 | Normalizing orderStatus, openOrder and order errors into intent, transmission and economic states | IBKR TWS API | tested | 9 | the real IBKR sequences that produce Inactive are not observed (docs/integration |
| CAP-IBKR-05 | P0 | Execution and commission/fee capture with execId correction handling and bounded history | IBKR Client Portal, IBKR TWS API, IBKR Web API | tested | 7 | whether a corrected execution gets its own commission report is unverified; last |
| CAP-IBKR-06 | P0 | Account values, positions and portfolio snapshots for reconciliation | IBKR TWS API | tested | 5 | the real AccountType string and paper SettledCash behaviour are unverified |
| CAP-IBKR-07 | P0 | Connectivity loss and restore handling (1100/1101/1102/1300/2110, farm codes, socket EOF) and host-side order resubmission | IBKR TWS API, IBKR platform | tested | 6 | no MAINTENANCE label for published reset windows (AC5) |
| CAP-IBKR-08 | P0 | Broker-aware request pacing, market-data line budget and historical-data limits | IBKR Market Data, IBKR Orders, IBKR TWS API, IBKR Web API | tested | 7 | whether placeOrder, cancelOrder and what-if count equally toward lines/2 is unve |
| CAP-IBKR-09 | P0 | Daily auto-restart, weekly re-authentication and 2FA constraints for unattended paper operation | IBKR Excel/DDE docs, IBKR IB Gateway, IBKR TWS API, IBKR Web API | implemented | 4 | no owner notification or reminders for AUTH_REQUIRED (alerting not built, CAP-OP |
| CAP-IBKR-10 | P1 | Pre-trade what-if preview for commission and margin impact | IBKR TWS API, IBKR Web API | proposed |  | not yet implemented |
| CAP-IBKR-11 | P0 | Cash vs margin account semantics and enforcement of long-only, no-short, no-margin behaviour | IBKR Campus glossary, IBKR Market Data / Users, IBKR Paper Trading Account, IBKR | tested | 5 | no explicit refusal test for secType SBL or exchange PREBORROW (AC2): only STK c |
| CAP-IBKR-12 | P0 | Market data on paper accounts: sharing from live, API acknowledgements, delayed-data labelling and rights | IBKR Market Data, IBKR TWS API | tested | 5 | no registry entry exists for IBKR market data, so bars are not stored and no mar |
| CAP-IBKR-13 | P0 | Paper-trading simulation fidelity limits and evidence-level labelling | IBKR Campus, IBKR Paper Trading Account, IBKR TWS API | implemented | 2 | no local rejection of paper-unsupported order types (AC2); no CA_NOT_PROCESSED f |
| CAP-IBKR-14 | P2 | IBKR Web API (REST + WebSocket) via Client Portal Gateway as a secondary adapter path for individual accounts | IBKR Web API | proposed |  | not yet implemented |

### data-providers

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-DATA-01 | P0 | Provider and entitlement registry with enforced usage rights | FRED (Federal Reserve Bank of St. Louis), Interactive Brokers, LSEG Workspace, M | tested | 6 | no registry entry for IBKR market data, so historical bars are returned but not  |
| CAP-DATA-02 | P0 | SEC EDGAR filings, XBRL company facts and bulk files under fair-access rules | SEC DERA, SEC EDGAR | tested | 5 | bulk files (FSDS, daily index) and filing documents not ingested (AC3, AC6) |
| CAP-DATA-03 | P1 | European issuer filings (ESEF iXBRL) via filings.xbrl.org | filings.xbrl.org (XBRL International) | proposed |  | not yet implemented |
| CAP-DATA-04 | P0 | Instrument identifier mapping with OpenFIGI (ISIN/CUSIP/SEDOL/ticker to FIGI) | OpenFIGI (Bloomberg) | tested | 4 | no 400-ISIN coverage report per market (AC3) |
| CAP-DATA-05 | P1 | Macro time series with point-in-time vintages from FRED/ALFRED | FRED API, FRED/ALFRED | proposed |  | not yet implemented |
| CAP-DATA-06 | P1 | US Treasury yield curves and Fiscal Data datasets | US Treasury, US Treasury Fiscal Data | implemented | 4 | real yields, bill rates and Fiscal Data datasets not ingested (AC4) |
| CAP-DATA-07 | P1 | Euro-area and EU statistics: ECB Data Portal (with vintages) and Eurostat | ECB, ECB Data Portal, Eurostat | implemented | 3 | only the EXR dataflow: no other ECB series, no updatedAfter delta loads or vinta |
| CAP-DATA-08 | P2 | India macro and monetary statistics from RBI DBIE (manual, rights-limited) | Reserve Bank of India | proposed |  | not yet implemented |
| CAP-DATA-09 | P0 | India equity market data: NSE/BSE restrictions and licensed paths (NSE Data, Zerodha Kite, IBKR) | Interactive Brokers, NSE Data & Analytics, NSE India, Zerodha Kite Connect | proposed |  | not yet implemented |
| CAP-DATA-10 | P0 | IBKR market data entitlements, lines, costs and API compliance | Interactive Brokers | tested | 4 | no preflight check of PRO status, API acknowledgement or subscriptions (AC1) |
| CAP-DATA-11 | P0 | Commercial US/global equity price data vendors (Massive, Tiingo, EODHD, Intrinio, Alpha Vantage) | Alpha Vantage, EOD Historical Data (EODHD), Intrinio, Massive (formerly Polygon. | proposed |  | not yet implemented |
| CAP-DATA-12 | P1 | Standardized and as-reported fundamentals from commercial vendors (FMP, EODHD, Intrinio, Massive, Tiingo) | EOD Historical Data (EODHD), Financial Modeling Prep (FMP), Intrinio | proposed |  | not yet implemented |
| CAP-DATA-13 | P1 | Analyst estimates, revisions, ratings and price targets (licensed only) | Benzinga, Financial Modeling Prep (FMP), Intrinio, LSEG Workspace | proposed |  | not yet implemented |
| CAP-DATA-14 | P1 | Licensed news, press releases and earnings-call transcripts | Benzinga, Intrinio, LSEG Workspace, Tiingo | proposed |  | not yet implemented |

### model-providers

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-MODEL-01 | P1 | Runtime model catalog discovery and capability registry | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | tested | 7 | Mistral not implemented |
| CAP-MODEL-02 | P1 | Schema-constrained structured output with local validation | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | tested | 6 | no conformance corpus or measured schema_conformance_rate threshold (AC3) |
| CAP-MODEL-03 | P1 | Read-only tool calling with allowlists and per-provider tool_choice mapping | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | tested | 4 | provider tool_choice mappings and tool-count limits for OpenAI, xAI and Mistral  |
| CAP-MODEL-04 | P1 | Token counting preflight and context-fit check | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | tested | 4 | no est_error calibration report per provider/model (AC4, AC5) |
| CAP-MODEL-05 | P1 | Effective-dated tariff registry, cost accounting and budget enforcement | Anthropic Claude API, Google Gemini API, Mistral, OpenAI API, xAI API | tested | 6 | no pricing-page hash monitor that sets rows to needs_review (AC7) |
| CAP-MODEL-06 | P1 | Rate-limit headers, error taxonomy, retry and circuit-breaker policy | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | tested | 6 | streaming (SSE) errors not handled: calls are not streamed (AC5) |
| CAP-MODEL-07 | P2 | Asynchronous batch processing for research backfills | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed |  | not yet implemented |
| CAP-MODEL-08 | P2 | Prompt caching with stable prefixes and normalized cache accounting | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | implemented | 2 | prompt caching itself is not used: no stable-prefix templates, breakpoints or ca |
| CAP-MODEL-09 | P1 | Provider data-usage, retention and training-policy gate | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | tested | 5 | Gemini unpaid-project and batch-endpoint rules are data only: those adapters are |
| CAP-MODEL-10 | P1 | Inference region and data-residency routing for US, Europe and India | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | tested | 4 | no EU-only routing to an OpenAI project (AC1): other providers disabled |
| CAP-MODEL-11 | P1 | Model version pinning, lifecycle tracking and per-model parameter compatibility | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | tested | 4 | no regression-evaluation artifact required to replace a pin (AC4) |
| CAP-MODEL-12 | P1 | Evidence-cited document Q&A over permitted filings (terminal AI-assistant functional parity) | LSEG Workspace, S&P Capital IQ Pro | tested | 7 | no frozen evaluation set or per-provider citation metrics (AC6) |

### regulatory

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-LAW-01 | P0 | Dated regulatory source register and applicability record (S11-S21, S41, S42) with change detection | IRS, Interactive Brokers LLC Client Agreement (Form 3203, 10 January 2025), NSE  | proposed |  | not yet implemented |
| CAP-LAW-02 | P0 | US day-trading / intraday-margin regime gate (FINRA Regulatory Notice 26-10, new Rule 4210(d)(2)) | FINRA Regulatory Notice 26-10, FINRA Regulatory Notice 26-10 (published April 20 | proposed |  | not yet implemented |
| CAP-LAW-03 | P0 | US cash-account settled-funds and freeriding gate (Regulation T 220.8; Investor.gov cash-account bulletin) | Federal Reserve Regulation T, 12 CFR 220.8 (eCFR, Title 12 up to date as of 10/2 | tested | 5 | no explicit freeriding rule or reason code, and no freeze-mode handling (the str |
| CAP-LAW-04 | P0 | Long-only enforcement and Regulation SHO short-sale gate | SEC Regulation SHO key points (S42), SEC Regulation SHO key points (S42), modifi | tested | 6 | no 'shorting disabled' reason string; the rejection code is OWNED_QUANTITY |
| CAP-LAW-05 | P0 | Owner-side pre-trade risk controls benchmarked to SEC Rule 15c3-5, NSE algo risk-control table and MiFID II Art. 17 | Directive 2014/65/EU (MiFID II) Article 17 (original OJ text via Publications Of | tested | 7 | no kill switch on unacknowledged order bursts (AC3); the order-rate limit is per |
| CAP-LAW-06 | P1 | India retail algorithmic/API trading eligibility gate (SEBI Feb 4, 2025 circular; SEBI Sep 30, 2025 glide path; NSE implementation standards and consolidated circular 73992) | NSE Consolidated NNF Circular NSE/INVG/73992, NSE Consolidated NNF Circular NSE/ | proposed |  | not yet implemented |
| CAP-LAW-07 | P1 | India API order runtime constraints: OPS throttle, no algorithmic market orders, session expiry, broker daily caps | NSE Consolidated NNF Circular NSE/INVG/73992, s.8.6 risk-control table, NSE NSE/ | proposed |  | not yet implemented |
| CAP-LAW-08 | P1 | India exchange-control gate for overseas accounts after relocation (RBI Forex FAQ, LRS FAQ) | Interactive Brokers market data minimum equity requirements, RBI Forex Transacti | proposed |  | not yet implemented |
| CAP-LAW-09 | P1 | EU MiFID II own-account dealing exemption gate (Art. 2(1)(d), Art. 4(1)(39)-(41), Art. 17) | Directive 2014/65/EU (MiFID II), Art. 2(1)(d) (original OJ text via Publications | proposed |  | not yet implemented |
| CAP-LAW-10 | P1 | Market-abuse conduct controls: self-cross prevention, order-intent logging, layering/spoofing pattern review (EU MAR; NSE 73992; book chapter 25) | MAR Art. 15, MAR Art. 16(2), MAR Art. 2(4), MAR Art. 39(2), NSE Consolidated NNF | implemented | 4 | no order-to-trade or fast-cancel ratios, no layering/spoofing review, no review  |
| CAP-LAW-11 | P0 | IBKR API market-data entitlement, subscriber status and usage-rights gate | Interactive Brokers API documentation, Interactive Brokers API documentation (ci | tested | 5 | no dated subscriber-status attestation record (AC4); no non-display use flag per |
| CAP-LAW-12 | P0 | IBKR account-terms gate for unattended automated trading (Client Agreement Form 3203; Relationship Summary Form 4024; API user certification) | IBKR Client Agreement (Form 3203), section 39, IBKR Client Agreement (Form 3203) | implemented | 2 | no broker-terms record, no Form 3203 change monitor, no withdrawal-permission ch |
| CAP-LAW-13 | P1 | US tax records for an automated trader: trader-status evidence, section 475(f) election calendar, wash-sale tracking (IRS Topic 429) | IRS Topic 429, IRS Topic 429 Traders in securities (S21), last reviewed 24-Sep-2 | proposed |  | not yet implemented |

### ops-aws

| ID | Priority | Capability | Reference scope | Status | Tests | Main gap |
|---|---|---|---|---|---|---|
| CAP-OPS-01 | P0 | Mac process supervision with launchd (restart drill, crash-loop handling, sleep and power settings) | Apple macOS, Apple macOS launchd (platform supervisor; not a reference terminal) | implemented | 2 | the renderer has no test and no plist has been loaded on this Mac; no SIGKILL/cr |
| CAP-OPS-02 | P0 | SQLite ledger durability settings, version advisory gate and crash/power-loss tests (macOS F_FULLFSYNC, Linux EBS) | IBKR TWS API, SQLite, SQLite (storage engine; not a reference terminal) | implemented | 6 | no test reads back the PRAGMA values at open (AC1) |
| CAP-OPS-03 | P0 | Consistent encrypted ledger backups and an isolated restore drill with post-backup broker reconciliation | IBKR Flex Web Service, IBKR TWS API, SQLite | tested | 7 | backups are not encrypted and not copied off-host (AC3) |
| CAP-OPS-04 | P0 | IB Gateway supervision: pinned install, daily auto-restart, weekly manual reauthentication and connectivity-state handling | IBC (third-party login automation; not IBKR), IBKR IB Gateway, IBKR TWS API, IBK | implemented | 9 | IB Gateway itself is not supervised: no pinned Gateway install, no launchd job f |
| CAP-OPS-05 | P0 | Layered health and readiness model (alive, writable, fresh, authenticated, eligible) with explicit inactivity reasons | Amazon EC2 (deployment platform), IBKR, IBKR APIs, IBKR TWS API | tested | 8 | no explicit layered states (alive, writable, fresh, authenticated, eligible) wit |
| CAP-OPS-06 | P0 | Operational alerting: severity, deduplication, runbook links, dead-man detection (local and CloudWatch) | Amazon CloudWatch, Amazon CloudWatch (deployment platform), IBKR TWS API, LSEG W | proposed |  | not yet implemented |
| CAP-OPS-07 | P1 | Spend cap enforced before any AWS provisioning (AWS Budgets, budget actions, IaC gate) and all-in operating cost | AWS Budgets, AWS Budgets (self-hosting cost control; not a reference-terminal fe | proposed |  | not yet implemented |
| CAP-OPS-08 | P1 | AWS Region and instance selection (us-east-1 vs ap-south-1) by measured broker latency, eligibility and availability, with pricing linked not assumed | Amazon EC2, IBKR, IBKR IB Gateway, IBKR TWS / IB Gateway | proposed |  | not yet implemented |
| CAP-OPS-09 | P1 | Private administrative access via Systems Manager Session Manager (no inbound ports), egress allowlist and gateway API exposure control | AWS Systems Manager, AWS Systems Manager Session Manager, IBKR, IBKR TWS API | proposed |  | not yet implemented |
| CAP-OPS-10 | P0 | Secrets management: Mac Keychain and AWS Secrets Manager, broker credentials never stored, paper/live separation, rotation | AWS Secrets Manager, Apple macOS Keychain, Bloomberg Terminal (Bloomberg Anywher | implemented | 4 | no Keychain or AWS Secrets Manager integration; every process runs as the same O |
| CAP-OPS-11 | P1 | IAM least privilege for execution and research roles, MFA for humans, and IMDSv2-only instance metadata | AWS IAM, Amazon EC2, IBKR | proposed |  | not yet implemented |
| CAP-OPS-12 | P1 | Encryption at rest on Mac and AWS (FileVault, EBS encryption by default, KMS keys, encrypted backups) | AWS Backup, Amazon EBS, Apple macOS FileVault | proposed |  | not yet implemented |
| CAP-OPS-13 | P1 | AWS Backup plans, vault lock and scheduled restore testing for the execution host (secondary to application-consistent SQLite backups) | AWS Backup, IBKR Flex Web Service | proposed |  | not yet implemented |
| CAP-OPS-14 | P1 | Instance recovery and single-writer fencing: automatic EC2 recovery, manual ownership transfer, competing-session detection | Amazon EC2, IBKR, IBKR TWS API | tested | 6 | automatic EC2 recovery not configured (no AWS resources provisioned) |

## Evidence for capabilities above `proposed`

### CAP-INS-01: Security search and instrument discovery (tested)

- Files: `backend/src/unknown/data/fundamentals/search.py`, `backend/src/unknown/data/sources/sec_tickers.py`, `backend/src/unknown/data/sources/identifiers.py`, `backend/src/unknown/api/routes/company.py`, `terminal/src/workspaces/company/CompanyWorkspace.tsx`, `terminal/src/workspaces/instruments/InstrumentsWorkspace.tsx`
- Tests: `test_company_peers_search::SearchAndFilingsTests.test_exact_ticker_ranks_first_and_never_selects`; `test_company_peers_search::SearchAndFilingsTests.test_cik_name_and_former_name_matches`; `test_company_peers_search::SearchAndFilingsTests.test_point_in_time_snapshot_and_licensed_ids`; `test_company_peers_search::SearchAndFilingsTests.test_openfigi_outcomes_are_searchable_as_of_receipt`; `test_sources_openfigi::IdentifierTests.test_check_digits`; `test_company_api::CompanyApiTests.test_filings_peers_dcf_and_search`; `src/workspaces/company/model.test.ts::company route > parses the search view, an issuer, its tab and options`; `src/workspaces/company/model.test.ts::search sources > tells when no SEC issuer source was searched (no research store: only the core instrument master)`; `src/workspaces/instruments/model.test.ts::mappings and search > matches every token against symbol, name, ids, venue and provider ids`
- Evidence: GET /api/company/search ranks EXACT_ID < EXACT_TICKER < NAME_PREFIX < NAME_CONTAINS < FORMER_NAME over local, as-of sources (SEC ticker snapshots, ingested issuers, OpenFIGI outcomes, the core instrument master) and never selects.
- Evidence: Live smoke of the SEC company tickers file ran once (test_sources_live, opt-in).
- Evidence: Terminal: the Company workspace searches issuers through this API; candidates are ranked, never selected automatically, and each source states its status. The Instruments workspace searches the core instrument master.
- Gap: no labelled 200-query fixture and no latency benchmark (AC1, AC2, AC5)
- Gap: US only: no India or EU listings
- Gap: no network fallback search (OpenFIGI /v3/search, reqMatchingSymbols)
- Requirements: DOM-03, DAT-02

### CAP-INS-02: Identifier cross-reference and FIGI mapping (tested)

- Files: `backend/src/unknown/data/sources/openfigi.py`, `backend/src/unknown/data/sources/identifiers.py`, `backend/src/unknown/domain/instruments.py`, `backend/src/unknown/broker/ibkr/contracts.py`
- Tests: `test_sources_openfigi::OpenFigiTests.test_batching_and_sliding_window_without_key`; `test_sources_openfigi::OpenFigiTests.test_429_waits_retry_after_and_413_splits`; `test_sources_openfigi::OpenFigiTests.test_invalid_and_licensed_inputs_rejected_before_any_call`; `test_sources_openfigi::OpenFigiTests.test_ingest_stores_every_outcome_and_never_auto_selects`; `test_ibkr_marketdata::ContractEvaluationTests.test_conflicts_block_routing`; `test_ibkr_marketdata::ContractEvaluationTests.test_changed_mapping_blocks_until_owner_remap_creates_a_new_version`
- Evidence: OpenFIGI mapping with documented batch limits, 429/413 handling and check-digit validation before any call; every outcome is stored point in time and never auto-selected. Provider mappings are versioned children of the instrument.
- Evidence: Live smoke of one ISIN against OpenFIGI ran once (test_sources_live, opt-in).
- Gap: no 300-row hand-verified fixture (AC1)
- Gap: OpenFIGI-versus-IBKR ISIN disagreement is detected in IBKR contract evaluation, not across the two sources (AC3)
- Gap: no database constraint on mapping provenance (AC5)
- Requirements: DOM-03, DAT-01

### CAP-INS-03: Licensed identifier rights enforcement (CUSIP, SEDOL, RIC) (implemented)

- Files: `backend/src/unknown/data/sources/identifiers.py`, `backend/src/unknown/data/sources/openfigi.py`, `backend/src/unknown/data/fundamentals/search.py`
- Tests: `test_sources_openfigi::OpenFigiTests.test_invalid_and_licensed_inputs_rejected_before_any_call`; `test_company_peers_search::SearchAndFilingsTests.test_point_in_time_snapshot_and_licensed_ids`; `test_sources_openfigi::IdentifierTests.test_classify`
- Evidence: CUSIP and SEDOL inputs are validated locally but refused for storage or sending (no licence is recorded).
- Gap: no rights classes per column, no write-path interceptor, no licence records, no scan or purge job, no rights events (AC1-AC5)
- Requirements: DAT-01

### CAP-INS-04: Broker contract resolution and verification (IBKR conid, Indian broker instrument tokens) (tested)

- Files: `backend/src/unknown/broker/ibkr/contracts.py`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/broker/ibkr/serve.py`, `backend/src/unknown/domain/instruments.py`, `backend/src/unknown/broker/ibkr/bootstrap_paper.py`
- Tests: `test_ibkr_marketdata::ContractEvaluationTests.test_verified_listing_produces_a_valid_instrument_proposal_and_mapping`; `test_ibkr_marketdata::ContractEvaluationTests.test_more_than_one_match_is_ambiguous_with_no_mapping`; `test_ibkr_marketdata::ContractEvaluationTests.test_conflicts_block_routing`; `test_ibkr_marketdata::ContractEvaluationTests.test_rules_that_differ_by_exchange_use_the_primary_exchanges_rule`; `test_ibkr_marketdata::ContractResolverTests.test_resolution_requests_details_then_the_market_rule`; `test_ibkr_serve::InstrumentDetailsTests.test_service_copies_ibkr_mappings_with_currency`; `test_ibkr_bootstrap_paper::BootstrapPaperTests.test_registers_only_verified_listings_and_never_starts`; `test_ibkr_bootstrap_paper::BootstrapPaperTests.test_universe_must_name_primary_exchanges`
- Evidence: reqContractDetails by symbol, currency and primary exchange (never by name); exactly one match required; ISIN, MIC, currency and name checks; MarketRuleIds fetched for tick bands; a changed mapping blocks until an owner remap.
- Evidence: The owner's paper bootstrap (python -m unknown.broker.ibkr.bootstrap_paper) resolves each listing by symbol and US primary exchange on the read-only market-data connection and registers only VERIFIED contracts, each with its ibkr conId mapping; AMBIGUOUS, UNRESOLVED and CONFLICT listings are reported and skipped. Tested against a real core desk with a fake resolver; it has never run against IB Gateway.
- Gap: Kite instrument tokens not implemented (AC5)
- Gap: no 500-listing paper preflight run (AC6)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Gap: the paper bootstrap accepts US primary exchanges only (NASDAQ, NYSE, ARCA, AMEX, BATS)
- Requirements: DOM-03, BRK-02

### CAP-INS-06: Point-in-time instrument master and symbology history (ticker changes, reuse, name changes) (tested)

- Files: `backend/src/unknown/data/pit.py`, `backend/src/unknown/domain/instruments.py`, `backend/src/unknown/data/sources/sec_tickers.py`, `backend/src/unknown/data/fundamentals/filings.py`
- Tests: `test_pit_store::PointInTimeTests.test_symbology_history_and_ambiguity`; `test_company_peers_search::SearchAndFilingsTests.test_cik_name_and_former_name_matches`; `test_company_peers_search::SearchAndFilingsTests.test_snapshot_dedup_and_changes`; `test_analytics_exposure::SicTest.test_alias_recorded_after_the_decision_time_cannot_classify`; `test_analytics_review::ResearchHistoryTest.test_point_in_time_and_ambiguity`
- Evidence: The core instrument id is immutable with versioned provider mappings; research entity aliases carry validity intervals; SEC ticker snapshots give ticker history from the first snapshot onward; former names come from SEC submissions.
- Gap: no symbology source with history before this system's first snapshot (SEC tickers and OpenFIGI carry no validity dates)
- Gap: no database constraint against overlapping intervals (AC5)
- Gap: no survivorship-free universe (CAP-RES-02)
- Requirements: DOM-03, DAT-04

### CAP-INS-07: Exchange trading calendars, sessions, half days and ad hoc closures (US, India, Europe) (tested)

- Files: `backend/src/unknown/domain/calendar.py`, `config/calendars/`, `docs/markets/README.md`
- Tests: `test_markets::CalendarFilesTests.test_every_calendar_cites_a_source_url_and_retrieval_date`; `test_markets::BusinessDayArithmeticTests.test_india_trading_holidays`; `test_markets::BusinessDayArithmeticTests.test_euronext_holidays_and_half_days`; `test_markets::BusinessDayArithmeticTests.test_xetra_holidays`; `test_markets::HorizonTests.test_calendars_refuse_dates_beyond_their_source`; `test_markets::MuhuratTests.test_nse_muhurat_is_an_explicit_special_session`; `test_markets::SessionTests.test_regular_sessions`
- Evidence: Sourced, dated calendar files for XNYS, XNSE, XBOM, XETR, XPAR, XAMS, XBRU, XLIS, XMIL and XMAD with sessions, early closes and explicit special sessions; dates beyond the source horizon are refused. Every date was independently re-verified (3 corrections applied).
- Gap: no versioned ad hoc closures with replay pinning (AC2)
- Gap: Oslo and Dublin not loaded (AC3)
- Gap: no DST transition test per venue (AC7)
- Gap: IBKR TradingHours cross-check not implemented
- Requirements: DOM-06

### CAP-INS-08: Settlement and clearing business-day calendars (T+n by market and currency) (tested)

- Files: `backend/src/unknown/domain/calendar.py`, `backend/src/unknown/domain/markets.py`, `config/calendars/`, `config/markets/venues.json`, `backend/src/unknown/service/core.py`
- Tests: `test_markets::BusinessDayArithmeticTests.test_india_settlement_holidays_differ_from_trading`; `test_markets::BusinessDayArithmeticTests.test_t2s_settlement_days`; `test_markets::SettlementTests.test_eu_moves_to_t_plus_1_on_trade_date_2027_10_11`; `test_markets::SettlementTests.test_sample_trade_per_venue`; `test_markets::HorizonTests.test_settlement_refuses_to_cross_the_horizon`; `test_reconciliation::SettlementEvidenceTests.test_due_settlement_is_not_confirmed_by_the_calendar_and_late_case_opens`; `test_service_calendars::ServiceCalendarTests.test_service_loads_every_sourced_calendar`
- Evidence: Settlement calendars (NCL-CM, ICCL-EQ, T2S-EUR) are separate from trading calendars; the venue registry applies settlement cycles by trade date; the core never releases cash on the expected date alone.
- Evidence: The core service passes load_calendars(config/calendars) to the desk at start (service/core.py), and every shipped policy names a calendar in that set; until b87451b it loaded WEEKDAYS only, so the paper policy's XNYS settlement calendar silently fell back to weekdays (a 2026-11-25 trade would have been expected to settle on Thanksgiving). The cited test_service_calendars tests check the loader, the shipped policies and XNYS date arithmetic, not the running service.
- Gap: the core uses the instrument's settlement days and the policy calendar, not the venue registry
- Gap: US securities settlement calendar (DTC) unverified; currency payment calendars not modelled
- Gap: no exception when a broker settle date differs from the estimate (AC6)
- Gap: a policy naming a calendar that is not loaded still falls back to WEEKDAYS silently in the core (handlers/broker.py and handlers/recon.py) instead of being refused at publish or activation
- Gap: no test starts the core service and checks which calendar its desk settles with, so the b87451b fix has no regression test: with service/core.py reverted to WEEKDAYS only, all 1,205 backend tests still pass (checked 2026-10-22)
- Requirements: DOM-06, LED-03, REC-04

### CAP-INS-14: Venue (ISO 10383 MIC) and issuer (LEI, PermID) reference linkage (implemented)

- Files: `backend/src/unknown/domain/markets.py`, `config/markets/venues.json`
- Tests: `test_markets::RegistryTests.test_venues_and_identity`; `test_markets::RegistryTests.test_every_rule_and_convention_is_sourced`
- Evidence: The venue registry records ISO 10383 MICs with country, currency, time zone and calendars.
- Gap: no MIC change loading or versioning (AC1, AC2)
- Gap: no LEI or PermID issuer linkage (AC3-AC5)
- Requirements: DOM-06

### CAP-MKT-01: Level 1 quotes with explicit real-time / delayed / frozen / snapshot status, source and age (tested)

- Files: `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/market/quotes.py`, `terminal/src/workspaces/marketdata/MarketDataWorkspace.tsx`
- Tests: `test_ibkr_marketdata::QuoteTests.test_every_market_data_type_maps_to_one_status_and_only_live_is_live`; `test_ibkr_marketdata::QuoteTests.test_minus_one_or_zero_then_size_zero_is_null_no_data`; `test_ibkr_marketdata::QuoteTests.test_crossed_and_locked_quotes_are_kept_labelled_and_counted`; `test_ibkr_marketdata::QuoteTests.test_delayed_to_live_updates_without_restart_with_one_audit_entry_and_no_merging`; `test_ibkr_marketdata::QuoteTests.test_non_live_quotes_fail_the_real_pretrade_check`; `test_ibkr_marketdata::RouteTests.test_stale_flag_flips_exactly_at_each_budget`; `test_ibkr_marketdata::QuoteTests.test_streaming_request_is_never_a_snapshot_or_paid_regulatory_snapshot`; `src/workspaces/marketdata/model.test.ts::presentation > never presents a non-consolidated feed as the national best bid/offer`; `src/workspaces/marketdata/model.test.ts::presentation > shows the delivered type and flags a difference from the requested type`; `src/workspaces/marketdata/render.test.ts::subscription detail: quote analytics > shows mid and spread with the quote currency`; `e2e/03-portfolio-agents.spec.ts::market data: no session, then FIXTURE status with delivered types, consolidation and entitlement errors`
- Evidence: A read-only L1 session on its own client id feeds the core's quote book with delivered type, source, receipt time and null reasons; GET /api/marketdata/status adds age and staleness with formula ids.
- Evidence: Terminal: the Market data workspace shows each subscription's requested and delivered type, consolidation, entitlement errors and quote analytics.
- Gap: Web API status codes (R, D, Z, Y, N, O, P, p) not mapped (TWS only)
- Gap: regulatory snapshots are never requested, so the confirmation and local cap of AC6 are not built
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: DAT-02, DAT-06, ORD-08

### CAP-MKT-02: Consolidated (NBBO / tape) versus single-venue and derived best quotes across US, Europe and India (tested)

- Files: `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/market/quotes.py`, `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/analytics/marks.py`
- Tests: `test_quote_consolidation::QuoteConsolidationTests.test_unknown_is_the_default_and_values_are_closed`; `test_quote_consolidation::QuoteConsolidationTests.test_non_consolidated_quote_validates_paper_but_not_live`; `test_ibkr_marketdata::QuoteTests.test_live_quote_is_labelled_non_consolidated_and_feeds_the_book`; `test_ibkr_marketdata::ConfigTests.test_consolidation_claims_need_evidence_and_known_values`; `test_analytics_review::ConsolidationLabelTest.test_label_survives_into_marks_snapshots_and_the_what_if_decision`
- Evidence: Every quote carries consolidation (consolidated / non_consolidated / unknown); a claim needs evidence in the owner's market-data config; LIVE orders require consolidated quotes (QUOTE_CONSOLIDATED).
- Gap: no derived best across venues and no venue panel (AC2, AC3)
- Gap: India and Europe quotes not built
- Gap: no NBBO comparison run (AC5)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: DAT-06

### CAP-MKT-03: Trades and time & sales (live tick-by-tick and historical) with conditions, corrections and halts (implemented)

- Files: `backend/src/unknown/marketdata/`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/api/server.py`, `backend/src/unknown/broker/ibkr/mdsource.py`, `backend/src/unknown/broker/alpaca/stream.py`, `terminal/src/state/marketdata.ts`, `terminal/src/workspaces/monitor/panels.tsx`
- Tests: `test_marketdata_bus::BusTests.test_trades_are_never_coalesced_and_overflow_is_reported`; `test_marketdata_bus::TickStoreTests.test_paging_and_bounded_retention`; `test_marketdata_ibkr_source::IbkrSourceTests.test_tick_by_tick_within_five_percent_of_lines`; `test_marketdata_alpaca::ParsingTests.test_documented_messages_map_to_contract_fields_without_floats`; `test_marketdata_perf::StreamPerformanceTest.test_500_keys_at_4hz_bus_to_client_p99`; `src/state/marketdata.test.ts::market-data client > keeps trades newest first, re-reads time & sales on a trade gap, and counts overflow notices`
- Evidence: Live prints stream per key with price, size, venue, conditions, source and data type; trades are never coalesced and a full client buffer produces a status event and a counter, never a silent drop.
- Evidence: Time & sales history is kept outside the core database in a tick store with bounded retention (age and rows per key) and paged newest first (GET /api/marketdata/trades).
- Evidence: IBKR trades come only from tick-by-tick AllLast when the owner asserts the entitlement, within 5% of lines; Alpaca trades from the documented T=t messages (fixtures from docs.alpaca.markets).
- Gap: no historical backfill (reqHistoricalTicks or vendor history)
- Gap: no corrections/cancels linked to the original print
- Gap: no halt/resume events inline
- Gap: no size/condition filters or date search in the panel
- Gap: no live run recorded

### CAP-MKT-04: Intraday bars (seconds to hours), live-updating and historical, with explicit completeness (tested)

- Files: `backend/src/unknown/broker/ibkr/history.py`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/research/bars.py`
- Tests: `test_ibkr_marketdata::BarParsingTests.test_intraday_epoch_bar_end_availability_and_completeness`; `test_ibkr_marketdata::HistoryClientTests.test_a_bar_open_when_requested_is_never_complete_even_if_the_answer_arrives_after_its_end`; `test_ibkr_marketdata::HistoryClientTests.test_small_bars_older_than_six_months_are_a_coverage_gap_not_an_empty_success`; `test_ibkr_marketdata::HistoricalPacerTests.test_ten_minute_soak_against_a_mock_enforcing_ibkr_rules`; `test_ibkr_marketdata::BarStoreTests.test_no_bar_is_visible_before_its_availability_time`
- Evidence: Historical bars are half-open [start, end) with availability time and a completeness rule bounded by the send time; stored point in time in a research bars table.
- Gap: no live-updating bars and no platform bars built from trades (AC2)
- Gap: RTH and all-hours series labelling not asserted (AC5)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: DAT-02, DAT-04

### CAP-MKT-05: Daily/weekly/monthly bars with raw and versioned split/dividend-adjusted series (implemented)

- Files: `backend/src/unknown/broker/ibkr/history.py`, `backend/src/unknown/research/bars.py`
- Tests: `test_ibkr_marketdata::BarParsingTests.test_daily_rth_bar_ends_at_the_calendar_close_including_early_closes`; `test_ibkr_marketdata::HistoryClientTests.test_no_security_definition_is_a_recorded_survivorship_gap`
- Evidence: Raw daily bars from IBKR with availability times and coverage gaps.
- Gap: no split/dividend adjustment factors, no total-return series, no factor versions (AC1-AC4): corporate actions are not ingested
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: DAT-02, DAT-03

### CAP-MKT-06: Market depth (Level 2) by price and by order, where entitled (implemented)

- Files: `backend/src/unknown/marketdata/`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/api/server.py`, `backend/src/unknown/broker/ibkr/mdsource.py`, `backend/src/unknown/broker/ibkr/marketdata.py`, `terminal/src/workspaces/monitor/panels.tsx`
- Tests: `test_marketdata_ibkr_source::IbkrSourceTests.test_depth_book_operations_and_reset`; `test_marketdata_ibkr_source::SessionRoutingTests.test_wrapper_routes_tick_by_tick_and_depth_to_registered_handlers`; `test_marketdata_bus::SyntheticTests.test_events_through_the_bus_carry_the_synthetic_label`; `src/workspaces/monitor/render.test.ts::monitor rendering > renders time & sales, depth with cumulative size, and the ticker tape`
- Evidence: IBKR depth (reqMktDepth, smart or venue, updateMktDepth and updateMktDepthL2 with market maker) is requested only with the owner's depth allowance; insert/update/delete maintain the book and each update publishes the full side; error 317 resets it.
- Evidence: The depth panel shows price levels per side with size and cumulative size, labelled with source and data type, and says when the serving source has no depth.
- Gap: no depth by order (only by price, with market maker where IBKR sends it)
- Gap: no imbalance figure
- Gap: no explicit stale marker beyond the key status
- Gap: Alpaca stock stream has no depth
- Gap: no live run recorded

### CAP-MKT-08: Watchlists / monitors with per-cell status, as-of dates and market-data line budgeting (implemented)

- Files: `backend/src/unknown/marketdata/`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/api/server.py`, `terminal/src/state/marketdata.ts`, `terminal/src/workspaces/monitor/`
- Tests: `test_marketdata_bus::BusTests.test_line_budget_refuses_with_a_status_per_key_and_falls_back`; `test_marketdata_bus::BusTests.test_quotes_coalesce_per_key_to_the_latest_state`; `test_marketdata_bus::BusTests.test_refusal_falls_back_and_every_event_names_its_source`; `test_marketdata_bus::ApiTests.test_stream_snapshot_latency_sources_and_trades`; `src/workspaces/monitor/model.test.ts::monitor model > edits watchlists and refuses duplicates and invalid keys`; `src/workspaces/monitor/render.test.ts::monitor rendering > shows quotes with change, sizes and the SYNTHETIC label, and line-budget refusals per row`; `src/state/marketdata.test.ts::market-data client > resyncs from the snapshot on a quote seq gap and ignores older quotes`
- Evidence: Watchlists hold security keys (SYMBOL.MIC), not broker ids, and are edited and persisted in the browser; each row shows last, change, %change (exact decimals), bid/ask with sizes, volume, time and a source/data-type badge; SYNTHETIC is always labelled.
- Evidence: The bus enforces each source's line budget before subscribing; a key no source can take shows LINE_BUDGET, NOT_ENTITLED or UNAVAILABLE with the reason in its row.
- Gap: rows beyond the budget are refused, not polled or served delayed (AC1)
- Gap: no CSV/XLSX export (AC3)
- Gap: no fundamental columns with as-of dates
- Gap: no IBKR watchlist sync (AC4)
- Gap: no column chooser or grouping
- Gap: watchlists are per browser, not stored server-side
- Gap: no paper run against the 100-line budget recorded

### CAP-MKT-11: Market-data entitlement, professional status, display vs non-display use and fee registry (implemented)

- Files: `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/data/registry.py`
- Tests: `test_ibkr_marketdata::ConfigTests.test_consolidation_claims_need_evidence_and_known_values`; `test_ibkr_marketdata::ConfigTests.test_line_budget_headroom_and_refusal`
- Evidence: Market-data line budget and consolidation claims with evidence; owner facts (Non-Commercial Professional status, fee-waived non-consolidated feed) recorded in STATUS.md.
- Gap: no entitlement and fee registry with display vs non-display use, no cost report, no effective-dated policy warnings (AC1-AC5)
- Requirements: DAT-01, LAW-01

### CAP-CO-01: Issuer, filer and listing identity resolution for company research (tested)

- Files: `backend/src/unknown/data/fundamentals/search.py`, `backend/src/unknown/data/fundamentals/filings.py`, `backend/src/unknown/data/sources/sec_tickers.py`, `backend/src/unknown/api/routes/company.py`, `terminal/src/workspaces/company/CompanyWorkspace.tsx`
- Tests: `test_company_peers_search::SearchAndFilingsTests.test_cik_name_and_former_name_matches`; `test_company_peers_search::SearchAndFilingsTests.test_exact_ticker_ranks_first_and_never_selects`; `test_company_peers_search::SearchAndFilingsTests.test_point_in_time_snapshot_and_licensed_ids`; `test_company_api::CompanyApiTests.test_filings_peers_dcf_and_search`; `src/workspaces/company/model.test.ts::company route > parses the search view, an issuer, its tab and options`; `src/workspaces/company/model.test.ts::search sources > tells when no SEC issuer source was searched (no research store: only the core instrument master)`; `e2e/03-research.spec.ts::company: search, tearsheet, point-in-time statements, metrics, DCF and peers`
- Evidence: Issuer identity (CIK, names in force as of a time, SIC, tickers labelled current-as-of-retrieval) from stored SEC submissions; candidates are listed, never auto-resolved.
- Evidence: Terminal: the Company workspace resolves issuers through this API at a chosen point in time.
- Gap: no 200-ticker resolution fixture and no latency target run (AC1, AC8)
- Gap: no ESEF/LEI resolution (AC5)
- Gap: ticker validity intervals exist only from this system's first SEC snapshot
- Requirements: DOM-03, DAT-02

### CAP-CO-02: US SEC filings feed, archive and full-text search (implemented)

- Files: `backend/src/unknown/data/fundamentals/filings.py`, `backend/src/unknown/data/sec.py`, `backend/src/unknown/api/routes/company.py`, `terminal/src/workspaces/company/Peers.tsx`
- Tests: `test_company_peers_search::SearchAndFilingsTests.test_filings_point_in_time_with_deep_links`; `test_company_api::CompanyApiTests.test_description_link_found_beyond_the_displayed_filings_page`; `src/workspaces/company/views.test.ts::filings table > shows acceptanceDateTime as published, never converted as if it were true UTC`
- Evidence: Filings accepted at or before an as-of time are listed from the stored, hash-verified SEC submissions payload with deep links and 8-K item flags.
- Evidence: Terminal: the Company workspace's Filings tab lists the ingested filings with acceptanceDateTime as published.
- Gap: no filing feed or archive: filings are listed only for issuers whose submissions were ingested
- Gap: no full-text search, no amendment linking, no 8-K 2.02/4.02 alerts (AC5-AC7)
- Gap: filing documents are not stored
- Requirements: DAT-03

### CAP-CO-04: As-reported financial statements from XBRL (income, balance sheet, cash flow, segments) (tested)

- Files: `backend/src/unknown/data/fundamentals/statements.py`, `backend/src/unknown/data/fundamentals/periods.py`, `backend/src/unknown/data/fundamentals/mapping.py`, `terminal/src/workspaces/company/Statements.tsx`
- Tests: `test_company_statements::StatementTests.test_53_week_year_and_derived_fourth_quarter`; `test_company_statements::StatementTests.test_cash_flow_quarters_derived_from_ytd`; `test_company_statements::StatementTests.test_statement_output_carries_lineage`; `test_company_statements::PeriodTests.test_duration_classes_cover_52_53_week_periods`; `test_company_fixtures::FixtureAndMappingTests.test_fixture_ingests_through_real_sec_path`; `src/workspaces/company/model.test.ts::statements > counts cells by method and missing reason`; `e2e/03-research.spec.ts::company: search, tearsheet, point-in-time statements, metrics, DCF and peers`
- Evidence: Income, balance sheet and cash flow from companyfacts as of a decision time; derived quarters are flagged with lineage (observation ids, accessions, availability times).
- Evidence: Terminal: the Statements tab shows point-in-time statements, with each missing cell's reason.
- Gap: statements follow the project's standardized mapping, not each filing's presentation linkbase (AC1)
- Gap: no tie-out residual report (AC2); extension concepts and segments not rendered (AC4, AC5)
- Requirements: DAT-03, DAT-05

### CAP-CO-05: Standardized line items, currency/unit normalization, calendarization and TTM (tested)

- Files: `backend/src/unknown/data/fundamentals/mapping.py`, `backend/src/unknown/data/fundamentals/statements.py`, `backend/src/unknown/data/fundamentals/periods.py`, `docs/research/company.md`
- Tests: `test_company_statements::StatementTests.test_concept_precedence_across_concept_switch`; `test_company_statements::StatementTests.test_ttm_sum_uses_four_contiguous_quarters`; `test_company_statements::StatementTests.test_late_filer_ttm_is_null_with_reason`; `test_company_statements::TransitionPeriodTests.test_transition_quarters_excluded_from_ttm`; `test_company_fixtures::FixtureAndMappingTests.test_mapping_table_is_consistent`
- Evidence: Versioned mapping std-map@<version> with concept precedence; every value names its rule and contributing facts; TTM is null with a reason when a quarter is unavailable; transition periods are excluded.
- Gap: no 100-issuer coverage report (AC2)
- Gap: no currency conversion: values stay in the reporting currency and mixed units are flagged (AC5)
- Gap: ifrs-full alternates are mapped but no IFRS or ESEF filer has been exercised
- Requirements: DAT-05

### CAP-CO-06: Point-in-time fundamentals, amendment and restatement history (tested)

- Files: `backend/src/unknown/data/pit.py`, `backend/src/unknown/data/sec.py`, `backend/src/unknown/data/fundamentals/statements.py`
- Tests: `test_pit_store::PointInTimeTests.test_restatement_never_leaks_backwards`; `test_pit_store::PointInTimeTests.test_silent_correction_of_same_publication`; `test_data_sec::SecIngestTests.test_ingest_point_in_time`; `test_data_sec::SecIngestTests.test_acceptance_time_parsing`; `test_company_statements::StatementTests.test_restatement_never_leaks_backwards`; `test_company_statements::StatementTests.test_audit_mode_knowledge_cutoff`
- Evidence: SEC companyfacts as separate publications keyed by acceptance time
- Evidence: live run 2026-10-22 against data.sec.gov (owner-declared User-Agent): Apple 25,135 and Microsoft 32,711 observations; 0 false corrections, 0 same-instant conflicts, 100% acceptance-time coverage via additional submissions files
- Gap: no 8-K 4.02 NON_RELIANCE marking (AC6)
- Gap: the 20-accession EST/EDT sample check of AC2 has not been run
- Requirements: DAT-02, DAT-04

### CAP-CO-07: Valuation multiples, financial ratios and documented enterprise-value bridge (tested)

- Files: `backend/src/unknown/data/fundamentals/metrics.py`, `backend/src/unknown/data/fundamentals/statements.py`, `backend/src/unknown/data/pit.py`, `terminal/src/workspaces/company/Metrics.tsx`
- Tests: `test_company_metrics::MetricTests.test_library_has_at_least_fifteen_documented_ratios`; `test_company_metrics::MetricTests.test_ratios_recompute_from_displayed_inputs`; `test_company_metrics::MetricTests.test_negative_earnings_pe_is_not_meaningful`; `test_company_metrics::MetricTests.test_ev_bridge_lists_missing_components_and_is_incomplete`; `test_company_metrics::MetricTests.test_financial_sector_suppresses_ev_metrics`; `test_company_metrics::MetricTests.test_show_calculation`; `src/workspaces/company/model.test.ts::price input > requires currency, source and a zoned time with a price`; `e2e/03-research.spec.ts::company: search, tearsheet, point-in-time statements, metrics, DCF and peers`
- Evidence: Ratios and multiples with formula id@version, inputs, dates and accessions; EV bridge INCOMPLETE when a component is missing; NM for negative earnings; financial-sector suppression.
- Evidence: Terminal: the Metrics tab shows every formula with its status and the API's reason, and its calculation on request.
- Gap: market multiples need an explicit price input: no licensed price source is wired
- Gap: EV is INCOMPLETE for most filers because preferred stock and minority interest are usually omitted (owner decision pending)
- Requirements: DAT-05

### CAP-CO-10: Peer group construction and trading comparables (current and point-in-time) (tested)

- Files: `backend/src/unknown/data/fundamentals/peers.py`, `backend/src/unknown/data/fundamentals/metrics.py`, `backend/src/unknown/api/routes/company.py`, `terminal/src/workspaces/company/Peers.tsx`
- Tests: `test_company_peers_search::PeersTests.test_default_peers_are_deterministic_by_sic`; `test_company_peers_search::PeersTests.test_owner_list_and_comps_statistics`; `test_company_peers_search::PeersTests.test_comps_values_carry_lineage_and_flag_members_without_fundamentals`; `test_company_peers_search::PeersTests.test_size_band_with_market_caps`; `src/workspaces/company/views.test.ts::peer group > opens a member at the same point in time, without this issuer price input`
- Evidence: Deterministic SIC rule peers_sic4@1 shown with the result; an owner list replaces it; comps carry lineage; NM values are excluded from statistics.
- Evidence: Terminal: the Peers tab shows the rule, members and statistics; a member opens at the same point in time.
- Gap: no saved named peer sets (AC2)
- Gap: SIC is the current classification, not point in time
- Gap: no XLSX export (AC5)
- Requirements: DAT-05

### CAP-CO-11: DCF and WACC valuation with sensitivity surface and valuation range summary (tested)

- Files: `backend/src/unknown/data/fundamentals/dcf.py`, `backend/src/unknown/data/fundamentals/valuation.py`, `backend/src/unknown/api/routes/company.py`, `terminal/src/workspaces/company/Dcf.tsx`, `terminal/src/workspaces/research-common/charts.tsx`
- Tests: `test_company_dcf::DcfMathTests.test_equation_17_1_hand_computed`; `test_company_dcf::DcfMathTests.test_wacc_must_exceed_g`; `test_company_dcf::DcfMathTests.test_sensitivity_marks_invalid_cells`; `test_company_dcf::DcfModelTests.test_model_with_treasury_rf_and_market_weights`; `test_company_dcf::DcfModelTests.test_guards`; `src/workspaces/company/model.test.ts::DCF parameters > builds the explicit-forecast CAPM request`; `src/workspaces/company/model.test.ts::DCF equity bridge caveat > names the missing components every equity-derived figure inherits`; `src/workspaces/research-common/views.test.ts::range chart > draws the reference marker (the owner price input) on the shared axis, with its exact text`
- Evidence: Book eq. 17.1 DCF in exact decimal context, WACC build-up with the Treasury curve as risk-free source, sensitivity surface with invalid cells, scenario range and equity bridge; financial-sector block.
- Evidence: Terminal: the DCF tab runs the recipe and shows the sensitivity grid, the equity bridge and a football field of the per-share ranges, with the owner's price input as a marker.
- Gap: no EUR risk-free curve (AC2)
- Gap: no XLSX export (AC5)
- Gap: the football field shows the DCF scenario and sensitivity ranges only: the trading-comps interquartile range is on the Peers tab, and there is no 52-week range without a price history (AC7)
- Requirements: DAT-05

### CAP-CO-12: Company tearsheet / overview page with sourced key facts (tested)

- Files: `backend/src/unknown/data/fundamentals/tearsheet.py`, `backend/src/unknown/api/routes/company.py`, `terminal/src/workspaces/company/Tearsheet.tsx`
- Tests: `test_company_api::CompanyApiTests.test_tearsheet_blocks`; `test_company_api::CompanyApiTests.test_unavailable_without_research_store`; `src/workspaces/company/model.test.ts::company route > builds routes that parse back to the same view`; `e2e/03-research.spec.ts::company: search, tearsheet, point-in-time statements, metrics, DCF and peers`
- Evidence: A tearsheet read model of typed blocks (OK, STALE, NOT_LICENSED, NOT_REPORTED, NOT_AVAILABLE, NOT_CONNECTED, PENDING, ERROR) that fail independently.
- Evidence: Terminal: the Company tearsheet shows each block with its source; a block fails on its own and says why.
- Gap: no quote block with live status (AC3)
- Gap: no snapshot export (AC5)
- Gap: no latency run (AC1)
- Requirements: DAT-05, DAT-06

### CAP-CO-14: Filing and document question answering with verifiable citations (implemented)

- Files: `backend/src/unknown/agents/evidence.py`, `backend/src/unknown/agents/pit_evidence.py`, `backend/src/unknown/agents/runner.py`, `terminal/src/workspaces/research-jobs/JobDetailView.tsx`
- Tests: `test_agents_evidence::ClaimVerificationTest.test_fabricated_span_and_misquote_are_unsupported`; `test_agents_evidence::ClaimVerificationTest.test_numeric_claims_against_typed_facts`; `test_agents_runner::RunnerTest.test_prompt_injection_document_has_zero_effect`; `src/workspaces/research-jobs/render.test.ts::proposal panel > fences model text off as not advice and labels the recorded proposal advisory only`; `src/workspaces/research-jobs/render.test.ts::proposal panel, schema version 2 > shows the verified claims and the typed evidence references instead of "None cited."`
- Evidence: The agent layer answers over point-in-time XBRL fact spans and verifies every cited span, quote and number before a claim becomes evidence.
- Evidence: Terminal: the Research jobs workspace submits a question and shows the proposal, fenced off as model output, with the verified claims and their typed evidence references.
- Gap: filing and document text is not ingested, so questions are answered over XBRL facts only
- Gap: no 100-question benchmark or citation-precision measurement (AC1, AC2)
- Requirements: AI-04, DAT-05

### CAP-MAC-07: US Treasury par/real yield curves, H.15, fitted zero curve and overnight reference rates (implemented)

- Files: `backend/src/unknown/data/sources/treasury.py`, `backend/src/unknown/api/routes/company.py`
- Tests: `test_sources_treasury::TreasuryTests.test_parse_exact_values_missing_tenor_and_display_field`; `test_sources_treasury::TreasuryTests.test_live_capture_and_availability`; `test_sources_treasury::TreasuryTests.test_curve_as_of_fallback_and_point`; `test_company_api::CompanyApiTests.test_macro_routes`
- Evidence: Treasury par curve with BC_30YEARDISPLAY kept raw only and capture-time availability; GET /api/macro serves it as of a time.
- Gap: H.15 reconciliation, GSW fitted zero curve and NY Fed overnight rates not built (AC2-AC4)
- Gap: real yields not ingested
- Requirements: DAT-02, DAT-04

### CAP-MAC-12: Official FX reference rates (ECB, Fed H.10, FBIL/RBI) for valuation and reporting (tested)

- Files: `backend/src/unknown/data/sources/ecb.py`, `backend/src/unknown/api/routes/company.py`
- Tests: `test_sources_ecb::EcbTests.test_exact_values_and_availability`; `test_sources_ecb::EcbTests.test_rates_as_of_previous_fixing_and_normalize`; `test_sources_ecb::EcbTests.test_cross_rates_carry_input_dates_and_flags`; `test_sources_ecb::EcbTests.test_reference_rates_never_reach_the_money_path`
- Evidence: ECB reference rates stored exactly with orientation and capture-time availability; normalization to x_{c->B}; a static test keeps fixings out of the money path.
- Gap: Fed H.10 and FBIL/RBI not ingested (AC2, AC3)
- Gap: no round-trip property test (AC5)
- Requirements: DAT-02, LED-12

### CAP-PORT-01: Consolidated holdings, per-currency cash ladder and NAV across brokers and accounts (tested)

- Files: `backend/src/unknown/analytics/holdings.py`, `backend/src/unknown/analytics/marks.py`, `backend/src/unknown/analytics/snapshots.py`, `backend/src/unknown/api/routes/portfolio.py`, `docs/research/portfolio.md`, `terminal/src/workspaces/portfolio/HoldingsTab.tsx`
- Tests: `test_analytics_holdings::BucketPartitionTest.test_allocation_and_reservation_never_change_nav`; `test_analytics_holdings::MarkQualityTest.test_unpriced_position_is_never_valued_at_zero`; `test_analytics_holdings::MarkQualityTest.test_stale_mark_is_flagged_and_excluded_from_fresh_nav`; `test_analytics_holdings::WeightsAndScopesTest.test_denominator_changes_only_the_weight`; `test_analytics_holdings::SettlementAndLadderTest.test_cash_ladder_partitions_ledger_cash`; `test_analytics_holdings::ReadOnlyTest.test_analytics_connection_cannot_write_the_core`; `test_analytics_routes::RouteTest.test_with_history`; `src/workspaces/portfolio/holdings.render.test.ts::holdings: NAV by owner > names an owner whose NAV is unavailable instead of leaving it out of the chart silently`; `e2e/03-portfolio-agents.spec.ts::portfolio holdings show owners and cash exactly as the API computes them`
- Evidence: Holdings, cash ladder and NAV by owner bucket and currency from the core journal opened read-only; unpriced positions are listed, never valued at zero; GET /api/portfolio/holdings.
- Evidence: Terminal: Portfolio > Holdings shows NAV by owner, positions and the cash ladder as the API computes them.
- Gap: single broker account only: no multi-broker consolidation (AC4)
- Gap: no CSV import (AC7)
- Gap: no FX conversion source for multi-currency totals (unavailable rather than partial)
- Requirements: LED-02, DAT-05

### CAP-PORT-02: Daily, realized and unrealized P&L with tax-lot cost basis, fee finalization and FX translation (tested)

- Files: `backend/src/unknown/analytics/pnl.py`, `backend/src/unknown/analytics/snapshots.py`, `backend/src/unknown/core/ledger.py`, `terminal/src/workspaces/portfolio/PerformanceTab.tsx`
- Tests: `test_analytics_pnl::JournalPnlTest.test_identity_holds_exactly_after_every_event`; `test_analytics_pnl::JournalPnlTest.test_point_in_time`; `test_analytics_stress::FeeRevisionTest.test_revised_final_fees_reduce_available_cash_through_the_observation_path`; `src/workspaces/portfolio/render.test.ts::daily P&L panel: days with an unpriced snapshot > says the component totals leave out the unpriced day, and counts it as a failing day`
- Evidence: Daily P&L between snapshots with an exact identity: realized + income - fees + change in unrealized equals NAV change minus external flows, residual zero in ledger units.
- Evidence: Terminal: Portfolio > Performance shows the daily P&L decomposition and flags unpriced days.
- Gap: no FX translation P&L (AC2)
- Gap: no corporate actions (LED-09 proposed)
- Gap: FIFO only: no specific-ID comparison (AC3)
- Gap: no broker-versus-ledger realized P&L reason codes (AC5)
- Requirements: LED-06, LED-07

### CAP-PORT-03: Exposure, concentration and fund look-through by sector, country, currency and beta (tested)

- Files: `backend/src/unknown/analytics/exposure.py`, `backend/src/unknown/api/routes/portfolio.py`, `terminal/src/workspaces/portfolio/RiskTab.tsx`
- Tests: `test_analytics_exposure::ExposureReportTest.test_groupings_sum_to_totals_and_unclassified_bucket_is_explicit`; `test_analytics_exposure::ExposureReportTest.test_concentration_metrics`; `test_analytics_exposure::ExposureReportTest.test_both_percentage_forms_are_labelled`; `test_analytics_exposure::SicTest.test_alias_recorded_after_the_decision_time_cannot_classify`; `e2e/03-portfolio-agents.spec.ts::exposure and VaR state their basis; the backtest says it has no day yet`
- Evidence: Exposure by instrument, owner, SEC SIC division and currency with an explicit unknown bucket and coverage; HHI, effective N and top-k.
- Evidence: Terminal: Portfolio > Risk shows exposure and concentration with their basis.
- Gap: no fund look-through (AC3, AC4)
- Gap: no country grouping and no beta (AC5) without a benchmark series
- Gap: the terminal's exposure panel is covered by an end-to-end test only (no unit render test)
- Requirements: DAT-05

### CAP-PORT-04: Time-weighted and money-weighted return measurement with GIPS-consistent cash-flow handling (tested)

- Files: `backend/src/unknown/analytics/performance.py`, `backend/src/unknown/analytics/flows.py`, `backend/src/unknown/analytics/snapshots.py`, `terminal/src/workspaces/portfolio/PerformanceTab.tsx`
- Tests: `test_analytics_performance::PublishedExamplesTest.test_ibkr_modified_dietz_example`; `test_analytics_performance::PublishedExamplesTest.test_ibkr_twr_linking_example`; `test_analytics_performance::LedgerDrivenPerformanceTest.test_deposit_at_a_day_boundary_leaves_twr_unchanged_but_moves_mwr`; `test_analytics_performance::FormulaTest.test_short_periods_are_never_annualized`; `test_analytics_snapshots::ExternalFlowTest.test_capital_moves_are_flows_and_pnl_is_not`; `src/workspaces/portfolio/render.test.ts::performance panel: money-weighted return and its uniqueness flag > shows an annualized rate only for a unique root`; `src/workspaces/portfolio/model.test.ts::IRR uniqueness and TWR > reads the TWR, its annualization reason and chain problems`
- Evidence: Daily-linked TWR (start- and end-of-day conventions), Modified Dietz and IRR; IBKR's published examples reproduce; only external flows are flows.
- Evidence: Terminal: Portfolio > Performance shows the TWR and the MWR, with the MWR only for a unique root.
- Gap: no component (sector) returns by daily aggregation (AC5)
- Requirements: DAT-05

### CAP-PORT-05: Ex-post risk and risk-adjusted performance statistics (Sharpe, Sortino, IR, TE, beta, alpha, drawdown, Calmar) (tested)

- Files: `backend/src/unknown/analytics/performance.py`, `terminal/src/workspaces/portfolio/PerformanceTab.tsx`
- Tests: `test_analytics_performance::PublishedExamplesTest.test_ibkr_sharpe_example`; `test_analytics_performance::PublishedExamplesTest.test_ibkr_information_ratio_example`; `test_analytics_performance::PublishedExamplesTest.test_ibkr_beta_example_consistent_normalisation`; `test_analytics_performance::LedgerDrivenPerformanceTest.test_deposit_at_the_trough_does_not_hide_the_drawdown`; `test_analytics_performance::FormulaTest.test_statistics_conventions_and_insufficient_data`
- Evidence: Volatility, Sharpe, Sortino, beta, tracking error, IR, alpha and drawdown on the unit-value series, each tested on published examples.
- Gap: benchmark-relative statistics have no benchmark series to consume (CAP-PORT-06)
- Gap: no risk-free series configured: Sharpe is reported as excess over zero, labelled
- Gap: Portfolio > Performance shows these statistics, but no terminal test asserts their display
- Requirements: DAT-05

### CAP-PORT-10: Value-at-Risk and Expected Shortfall (historical, parametric, Monte Carlo) with backtesting (tested)

- Files: `backend/src/unknown/analytics/var.py`, `backend/src/unknown/analytics/snapshots.py`, `backend/src/unknown/api/routes/portfolio.py`, `terminal/src/workspaces/portfolio/RiskTab.tsx`
- Tests: `test_analytics_var::LossVectorTest.test_matches_independent_reference_with_ties`; `test_analytics_var::LossVectorTest.test_es_dominates_var_and_both_rise_with_confidence`; `test_analytics_var::LossVectorTest.test_atoms_separate_the_two_es_forms`; `test_analytics_var::LossVectorTest.test_kupiec_matches_hand_computation`; `test_analytics_var::HistoricalVarTest.test_overlapping_multi_day_returns`; `test_analytics_var::HistoricalVarTest.test_backtest_counts_exceptions_against_prior_day_forecasts`; `src/workspaces/portfolio/render.test.ts::VaR backtest panel: exceptions > marks each exception by shape in the chart and lists it in words`; `e2e/03-portfolio-agents.spec.ts::exposure and VaR state their basis; the backtest says it has no day yet`
- Evidence: Full-revaluation historical VaR, quantile-integral ES (eq. 6.3) and the IBKR tail-mean form, overlapping multi-day horizons and a Kupiec backtest.
- Evidence: Terminal: Portfolio > Risk shows VaR and ES with their basis, and the backtest with its exceptions and Kupiec test.
- Gap: historical method only: no parametric or Monte Carlo VaR, no component VaR (AC3-AC5)
- Gap: no licensed price history: scenarios come from stored snapshots only
- Requirements: DAT-05

### CAP-PORT-11: Stress testing and scenario analysis: historical replays, custom shocks, factor-propagated and regulator-published scenarios (tested)

- Files: `backend/src/unknown/analytics/stress.py`, `backend/src/unknown/analytics/whatif.py`, `terminal/src/workspaces/portfolio/StressTab.tsx`
- Tests: `test_analytics_stress::ShockScenarioTest.test_uniform_up_move_is_exactly_proportional`; `test_analytics_stress::ShockScenarioTest.test_gap_down_crosses_triggers_without_capping_the_loss_and_the_core_reacts`; `test_analytics_stress::ShockScenarioTest.test_correlated_selloff_with_betas`; `test_analytics_stress::StaleQuoteTest.test_stale_marks_drop_out_of_fresh_nav_and_the_gate_refuses_exits`; `test_analytics_stress::FeeRevisionTest.test_revised_final_fees_reduce_available_cash_through_the_observation_path`; `src/workspaces/portfolio/stress.render.test.ts::shock scenarios: positions left out of the scenario P&L > lists unpriced positions instead of folding them silently into the total`; `e2e/03-portfolio-agents.spec.ts::stress: a hypothetical overnight gap computed on a private copy of the core`
- Evidence: Operational scenarios (overnight gap, correlated selloff, stale quote, fee revision) revalue the real holdings exactly and then run the core's own reaction on a private copy.
- Evidence: Terminal: Portfolio > Stress runs a scenario on demand and labels the result HYPOTHETICAL.
- Gap: no historical replays and no regulator scenarios (Fed, EBA) (AC2, AC3)
- Gap: venue halt, failed cancel and currency move not built (AC5)
- Requirements: DAT-05, RSK-07

### CAP-PORT-12: What-if portfolio sandbox and pre-trade portfolio impact (exposure, cash, VaR/ES, limits, broker margin/commission preview) (tested)

- Files: `backend/src/unknown/analytics/whatif.py`, `backend/src/unknown/api/routes/portfolio.py`, `terminal/src/workspaces/portfolio/WhatIfTab.tsx`
- Tests: `test_analytics_whatif::SharedCodeTest.test_dry_run_equals_the_live_gate_over_random_states`; `test_analytics_whatif::DryRunSemanticsTest.test_unfunded_buy_fails_cash_with_exact_shortfall`; `test_analytics_whatif::DryRunSemanticsTest.test_unsettled_sale_proceeds_do_not_fund_a_buy`; `test_analytics_whatif::DryRunSemanticsTest.test_never_writes_and_becomes_stale_when_the_core_moves`; `test_analytics_whatif::DryRunSemanticsTest.test_stop_latch_blocks_buys_but_not_reductions`; `src/workspaces/portfolio/render.test.ts::what-if impact: headroom amounts carry their currency > formats each headroom amount as money in the session currency`; `e2e/03-portfolio-agents.spec.ts::what-if evaluates an intent through the real gate and creates no order`
- Evidence: A dry run executes the core's own command path on an in-memory copy of the core database, so the decision equals the live gate; it never writes and becomes stale when the core moves.
- Evidence: Terminal: Portfolio > What-if evaluates an intent through the real gate and creates no order.
- Gap: no broker what-if commission and margin preview (CAP-IBKR-10)
- Gap: VaR impact of a hypothetical not computed
- Gap: 250 ms p95 target not benchmarked (AC7)
- Requirements: RSK-02, ORD-08

### CAP-PORT-13: Constrained portfolio optimisation, target allocations and rebalancing trade lists (tested)

- Files: `backend/src/unknown/analytics/allocation.py`, `backend/src/unknown/analytics/linalg.py`, `backend/src/unknown/api/routes/portfolio.py`, `terminal/src/workspaces/portfolio/AllocationTab.tsx`
- Tests: `test_analytics_allocation::MeanVarianceTest.test_two_asset_minimum_variance_matches_the_analytic_solution`; `test_analytics_allocation::MeanVarianceTest.test_random_fixtures_satisfy_constraints_and_kkt`; `test_analytics_allocation::MeanVarianceTest.test_infeasible_constraints_are_named`; `test_analytics_allocation::MeanVarianceTest.test_turnover_limit_is_enforced_by_raising_eta`; `test_analytics_allocation::TradeListTest.test_lot_rounding_down_fees_and_post_trade_checks`; `test_analytics_allocation::ProposalTest.test_end_to_end_proposal_from_ledger_and_snapshots_never_writes`; `src/workspaces/portfolio/render.test.ts::allocation trade list: sizing on stale marks is flagged > says which instruments were sized on a stale mark`; `e2e/03-portfolio-agents.spec.ts::allocation proposals are labelled PROPOSAL ONLY and offer no way to send them`
- Evidence: Equal-weight and inverse-volatility baselines, Ledoit-Wolf shrinkage, eq. 6.2 solver with a turnover limit, lot rounding and post-trade checks; output is a PROPOSAL that must go through the what-if service and the risk gate.
- Evidence: Terminal: Portfolio > Allocation shows proposals labelled PROPOSAL ONLY, with no way to send them.
- Gap: no sector or group caps, no tracking-error minimisation, no allocation-goal drift report (AC6)
- Gap: no integration test that a proposal cannot become an order without the gate (AC7): proposals simply have no order path
- Requirements: AUT-02, RSK-02

### CAP-RES-01: Point-in-time fundamentals, filings and macro vintages for research (bitemporal as-of layer) (tested)

- Files: `backend/src/unknown/data/pit.py`, `backend/src/unknown/data/fundamentals/statements.py`, `backend/src/unknown/research/bars.py`, `backend/src/unknown/data/sources/common.py`
- Tests: `test_pit_store::PointInTimeTests.test_restatement_never_leaks_backwards`; `test_pit_store::PointInTimeTests.test_missing_is_not_zero`; `test_pit_store::PublicationIdentityTests.test_same_instant_different_documents_are_not_a_correction`; `test_research_engine::PointInTimeStoreBridgeTest.test_bar_series_as_of_equals_research_store_as_of`; `test_company_statements::StatementTests.test_audit_mode_knowledge_cutoff`; `test_sources_treasury::TreasuryTests.test_refetch_unchanged_and_revision_never_leaks`
- Evidence: Bitemporal store: as_of(t) for research and as_of(t, knowledge_cutoff) for audit; SEC facts, Treasury, ECB and OpenFIGI outcomes and research bars all read through it.
- Gap: macro vintages (ALFRED) not ingested: FRED needs an owner key
- Gap: no filing text or news layer
- Requirements: DAT-02, DAT-04

### CAP-RES-03: Research workspace with programmatic data query API, notebooks and scheduled jobs (implemented)

- Files: `backend/src/unknown/api/routes/research.py`, `backend/src/unknown/api/routes/company.py`, `backend/src/unknown/api/routes/research_jobs.py`, `backend/src/unknown/research/study.py`, `terminal/src/workspaces/research/ResearchWorkspace.tsx`
- Tests: `test_research_study::ResearchRoutesTest.test_every_endpoint_returns_data_with_provenance`; `test_research_study::ResearchRoutesTest.test_routes_cannot_write`; `test_company_api::CompanyApiTests.test_routes_never_write`; `src/workspaces/research/views.test.ts::research envelope without a registry > shows the API reason verbatim and a study command that runs (with its required --out)`; `e2e/03-research.spec.ts::research: experiments, trials, artifacts with gate verdicts, reports, specifications`
- Evidence: Read-only HTTP query API over the research registry, company data and research jobs; the study runs from a CLI.
- Evidence: Terminal: the Research, Company and Markets & macro workspaces read this API.
- Gap: no notebooks, no programmatic client library and no job scheduler
- Requirements: RES-01

### CAP-RES-04: Causal event-driven backtest engine with explicit fill model and deterministic replay (tested)

- Files: `backend/src/unknown/research/engine.py`, `backend/src/unknown/research/bars.py`, `backend/src/unknown/research/core_replay.py`, `docs/research/README.md`
- Tests: `test_research_engine::CausalityTest.test_same_bar_close_fill_is_rejected_with_a_causality_error`; `test_research_engine::CausalityTest.test_reading_a_future_bar_raises_lookahead`; `test_research_engine::CausalityTest.test_perturbing_the_future_changes_no_earlier_decision`; `test_research_engine::FillRuleTest.test_touch_at_the_limit_is_not_a_fill`; `test_research_engine::DeterminismTest.test_replay_gives_a_byte_identical_event_log`; `test_research_engine::ClosedFormTest.test_buy_and_hold_matches_the_closed_form`; `test_research_core_replay::CoreReplayTest.test_trend_strategy_run_matches_the_core_exactly`
- Evidence: Causal daily-bar engine with explicit fill rules, T+1 cash account and deterministic event log; a second path replays the engine's orders through the real core and agrees exactly on the fixtures.
- Evidence: evaluated on SYNTHETIC fixtures only; no market history has been run
- Gap: daily bars only: no intraday execution window, auction or queue model (book finding BR-DAT-19)
- Gap: WEEKDAYS calendar by default; core replay compares WEEKDAYS runs only
- Gap: no corporate actions
- Requirements: RES-03, RES-05

### CAP-RES-05: Transaction cost, market impact and capacity model for backtests and promotion (tested)

- Files: `backend/src/unknown/research/costs.py`, `backend/src/unknown/research/engine.py`
- Tests: `test_research_costs::IbkrFixedKnownAnswers.test_page_examples`; `test_research_costs::IbkrTieredKnownAnswers.test_tiers_apply_marginally_across_orders_in_the_month`; `test_research_costs::PriceCostTest.test_square_root_impact_formula`; `test_research_costs::PriceCostTest.test_third_party_fee_only_on_sells_and_flagged_unverified`; `test_research_engine::FillRuleTest.test_partial_fill_capped_by_participation_of_bar_volume`; `test_research_engine::DeterminismTest.test_costs_lower_net_results_monotonically`
- Evidence: IBKR Pro Fixed and Tiered schedules (page values of 2026-10-22), half-spread, slippage and square-root impact; fills capped by participation.
- Gap: third-party sell fees are an UNVERIFIED 1 bp assumption
- Gap: no capacity curve (only a capacity field and the participation cap)
- Gap: US only: instruments without a recorded fee schedule are refused
- Requirements: RES-03

### CAP-RES-07: Walk-forward evaluation with purging, embargo and combinatorial purged cross-validation (tested)

- Files: `backend/src/unknown/research/folds.py`, `backend/src/unknown/research/spec.py`, `backend/src/unknown/research/study.py`
- Tests: `test_research_stats::FoldTest.test_walk_forward_known_layout`; `test_research_stats::FoldTest.test_cpcv_counts`; `test_research_stats::FoldTest.test_purge_and_embargo_properties_over_random_configurations`; `test_research_spec::SpecStrictnessTest.test_purge_must_cover_label_horizon_plus_execution_delay`; `test_research_study::StudyTest.test_walk_forward_uses_the_preregistered_purge`
- Evidence: Walk-forward, purged k-fold and CPCV with purge and embargo; zero violations over 10,000 random configurations.
- Evidence: evaluated on SYNTHETIC fixtures only; no market history has been run
- Gap: no market-data evaluation yet
- Requirements: RES-04

### CAP-RES-08: Multiple-testing correction and backtest-overfitting diagnostics (Deflated Sharpe, PBO/CSCV, SPA/StepM, haircuts) (tested)

- Files: `backend/src/unknown/research/stats.py`, `backend/src/unknown/research/promotion.py`, `backend/src/unknown/research/registry.py`
- Tests: `test_research_stats::DeflatedSharpeTest.test_psr_known_answer`; `test_research_stats::DeflatedSharpeTest.test_registry_trial_count_is_the_m_in_n_hat`; `test_research_stats::PboTest.test_s16_enumerates_12870_combinations`; `test_research_stats::PboTest.test_pure_noise_is_near_one_half`; `test_research_stats::MultipleTestingTest.test_bonferroni_and_holm`; `test_research_promotion::EvidenceBindingReviewTest.test_dsr_must_use_the_registry_trial_count`
- Evidence: PSR/DSR with M from the registry's trial count (every status), PBO via CSCV, Holm and Bonferroni.
- Evidence: evaluated on SYNTHETIC fixtures only; no market history has been run
- Gap: no SPA/StepM test
- Gap: the DSR ill-conditioning flag is reported but nothing acts on it
- Gap: the DSR null-calibration test is opt-in (UNKNOWN_SLOW_TESTS=1)
- Requirements: RES-04

### CAP-RES-09: Dependence-aware performance statistics and paired candidate-versus-incumbent comparison (tested)

- Files: `backend/src/unknown/research/stats.py`, `backend/src/unknown/research/study.py`
- Tests: `test_research_stats::NeffTest.test_eq_4_2_known_answers`; `test_research_stats::NeffTest.test_gate_form_includes_the_non_overlap_term`; `test_research_stats::PairedBootstrapTest.test_both_series_use_identical_indices`; `test_research_stats::LongRunVarianceTest.test_gate_uses_the_more_conservative_interval`; `test_research_study::StudyTest.test_paired_bootstrap_used_identical_dates`
- Evidence: Paired stationary block bootstrap with identical indices, flat-top HAC interval, gate on the more conservative bound; bounded n_eff (eq. 4.2 with the non-overlap term).
- Evidence: evaluated on SYNTHETIC fixtures only; no market history has been run
- Gap: coverage under strong dependence is 92.7% at phi = 0.6 (below the 93-97% band); owner decision pending
- Gap: no automatic optimal block length (Politis-White)
- Gap: the AR(1) coverage test is opt-in (UNKNOWN_SLOW_TESTS=1)
- Requirements: RES-04

### CAP-RES-10: Pre-registered experiment and complete trial registry (including failures) (tested)

- Files: `backend/src/unknown/research/registry.py`, `backend/src/unknown/research/bars.py`, `backend/src/unknown/api/routes/research.py`, `terminal/src/workspaces/research/Experiments.tsx`
- Tests: `test_research_registry::PreregistrationTest.test_trials_need_a_preregistration`; `test_research_registry::PreregistrationTest.test_preregistration_is_hashed_with_its_time_and_immutable`; `test_research_registry::TrialRegistryTest.test_every_run_is_a_trial_and_failures_count`; `test_research_registry::TrialRegistryTest.test_interrupted_and_crashed_runs_are_aborted_trials`; `test_research_registry::TrialRegistryTest.test_budget_is_enforced_and_only_the_owner_can_raise_it`; `test_research_registry::HoldoutTest.test_holdout_is_consumed_once_and_every_attempt_is_logged`; `test_research_registry::HoldoutLockReviewTest.test_a_hand_made_grant_does_not_open_the_holdout`; `src/workspaces/research/model.test.ts::trial counts and metrics > lists every segment in protocol order, zeros included, plus unknown ones`; `e2e/03-research.spec.ts::research: experiments, trials, artifacts with gate verdicts, reports, specifications`
- Evidence: Append-only SQLite registry (triggers refuse UPDATE/DELETE): pre-registration hashed with its time, every run a trial including failures and aborts, owner-only budget increases, a holdout consumed once through an issued grant.
- Evidence: Terminal: the Research workspace lists experiments and every trial, failures included, by segment.
- Gap: research registry uses WAL with default synchronous settings
- Requirements: RES-01, RES-04, AUT-04

### CAP-RES-11: Reproducibility manifests and deterministic replay of experiments (tested)

- Files: `backend/src/unknown/research/manifest.py`, `backend/src/unknown/research/registry.py`, `backend/src/unknown/research/fixtures.py`
- Tests: `test_research_registry::ManifestTest.test_manifest_fields_and_hash`; `test_research_registry::ManifestTest.test_replay_is_exact_and_differences_are_named`; `test_research_registry::ManifestTest.test_replay_diff_names_a_changed_interpreter_or_commit`; `test_research_engine::EngineReviewTest.test_a_fixture_recipe_regenerates_non_default_generators`; `test_research_study::StudyTest.test_study_is_deterministic`
- Evidence: Manifests written before a run (git commit and dirty flag, code hash, dataset fingerprints, config and spec hashes, seeds, interpreter); replay compares event-log SHA-256 and names differing fields.
- Gap: no external artifact store (DVC or similar); manifests live in the local registry file
- Requirements: RES-01, RES-05

### CAP-RES-12: Strategy artifact registry, status lifecycle and predeclared promotion gates (tested)

- Files: `backend/src/unknown/research/promotion.py`, `backend/src/unknown/research/spec.py`, `backend/src/unknown/core/handlers/strategy.py`, `backend/src/unknown/domain/principal.py`, `docs/adr/0017-strategy-artifacts-promotion-and-runtime.md`
- Tests: `test_research_promotion::LifecycleTest.test_full_ladder_with_evidence_and_owner_approvals`; `test_research_promotion::LifecycleTest.test_failed_gate_refuses_validation_and_leaves_status_unchanged`; `test_research_promotion::AuthorityTest.test_agents_can_propose_but_never_approve_or_transition`; `test_research_promotion::AuthorityTest.test_the_evaluator_must_not_be_the_author`; `test_research_promotion::SelectionPolicyTest.test_automatic_selection_only_inside_the_owner_approved_policy`; `test_research_promotion::ProtocolTest.test_protocol_is_strict`; `test_research_spec::SpecCoverageTest.test_every_res01_field_is_present_and_required`; `test_strategy_decisions::StrategyTokenBindingTests.test_issued_token_is_bound_to_its_artifact`; `test_research_shadow::ShadowLadderTest.test_shadow_sits_between_validated_and_paper`; `test_research_shadow::ShadowEnvelopeTest.test_automatic_promotion_inside_the_envelope_is_recorded_with_its_evidence_and_envelope`; `test_research_evidence_retention::EvidenceExpiredTest.test_an_active_artifact_on_deleted_data_is_evidence_expired_and_never_runs_again`
- Evidence: Artifact statuses proposed -> reproducible -> validated -> shadow -> paper -> eligible -> active (plus quarantined, retired) in a hash-chained transition log; gates recomputed under an owner-approved protocol; owner approvals need re-authentication within 15 minutes; strategy tokens are bound to one artifact id.
- Evidence: evaluated on SYNTHETIC fixtures only; no market history has been run
- Evidence: A 'shadow' status sits between validated and paper. The owner sets it, or the SYSTEM inside an owner-approved, immutable shadow envelope. Expiry is bounded by the retention of the evidence data, and an artifact whose evidence data was deleted is EVIDENCE_EXPIRED and is never promoted again (BR-DAT-16). Manifests record each dataset's retention and a store deletion expires the artifacts resting on it (sweep_store_deletions); no production schedule runs the sweep and no ingested dataset records a retention deadline yet.
- Gap: ADR-0017 is proposed, not accepted
- Gap: 'signed' transitions are tamper-evident hash chains, not cryptographic signatures
- Gap: PAPER_RECONCILIATION evidence cannot be produced until the IBKR paper account exists: no artifact can reach eligible
- Gap: the core does not consult the promotion registry; mandates list artifact ids set by the owner
- Requirements: RES-01, AUT-01, AUT-03, AUT-04

### CAP-RES-13: Shadow mode and IBKR paper-trading stage with divergence measurement (implemented)

- Files: `backend/src/unknown/core/handlers/strategy.py`, `backend/src/unknown/research/promotion.py`, `backend/src/unknown/strategy/runner.py`, `backend/src/unknown/strategy/artifacts.py`
- Tests: `test_strategy_decisions::StrategyDecisionTests.test_shadow_mode_never_submits_and_fields_are_validated`; `test_strategy_decisions::StrategyDecisionTests.test_no_trade_decision_is_recorded_without_economic_effect`; `test_strategy_shadow_status::ShadowStatusRunnerTest.test_a_shadow_status_artifact_records_shadow_decisions_and_never_runs_live`; `test_strategy_runner::ShadowRunnerTest.test_shadow_records_decisions_without_intents_through_the_process_entry_point`; `test_strategy_runner::CrashResumeTest.test_crash_after_submission_resends_the_same_command_and_records_once`
- Evidence: The core records strategy decisions in live or shadow mode (shadow never submits); promotion has a paper status that requires CORE_REPLAY_MATCH and an owner approval.
- Evidence: The strategy runner (python -m unknown.strategy; ADR-0017) runs shadow mode end to end against a real core for an artifact in 'shadow' or later: decisions are recorded and no intent is sent. Live mode needs 'paper' or later. Deterministic command ids make a resend after a crash record once. Tested against the simulated broker only.
- Gap: the paper stage (live mode on an IBKR paper account) has never run
- Gap: no divergence measurement between shadow, paper and simulation
- Gap: IBKR paper account not available (BRK-02)
- Requirements: RES-05, AUT-03

### CAP-RES-14: Ongoing performance and drift monitoring, quarantine and rollback of active artifacts (implemented)

- Files: `backend/src/unknown/research/promotion.py`, `backend/src/unknown/strategy/runner.py`
- Tests: `test_research_promotion::LifecycleTest.test_quarantine_empties_the_slot_and_retire_needs_the_owner`; `test_research_promotion::SelectionPolicyTest.test_expired_artifacts_cannot_become_eligible`; `test_strategy_runner::ArtifactIdentityAndEligibilityTest.test_status_gates_each_mode`; `test_strategy_transmission_gates::QuarantinedRunOnceCancelsAtWindowEndTest.test_a_refused_artifacts_working_remainder_is_still_cancelled_at_the_windows_end`
- Evidence: Quarantine empties the active slot (OWNER or SYSTEM, needs a MONITORING_INCIDENT); retirement needs the owner; expired artifacts cannot be activated.
- Evidence: The strategy runner reads the registry every cycle. A quarantined, retired or expired artifact makes no new decision, and the remainder of an intent it already sent is still cancelled at the window's end.
- Gap: no performance or drift monitoring produces MONITORING_INCIDENT evidence
- Gap: no rollback activation of a predecessor
- Gap: the core does not consult the registry: while a session mandate lists a quarantined artifact, its strategy token can still submit intents directly to the core, and only the runner refuses (book finding BR-SES-09)
- Requirements: AUT-03

### CAP-OMS-01: Order ticket: order-type, time-in-force and session capability catalog (implemented)

- Files: `backend/src/unknown/policy/model.py`, `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `config/policies/paper-default.v1.json`, `terminal/src/workspaces/orders/ticket.ts`
- Tests: `test_policy_mandate::PolicyParsingTests.test_shipped_policies_parse_to_their_written_values`; `test_policy_mandate::PolicyParsingTests.test_unreleased_capabilities_are_refused`; `test_orders_lifecycle::PretradeLimitTests.test_price_collar_bounds_buy_and_sell_limits`; `test_ibkr_adapter::SubmitTests.test_local_validation_rejects_without_transmitting` (IBKR venv); `src/workspaces/orders/ticket.test.ts::validateTicket > reports every missing field as an error`
- Evidence: The only order vocabulary is the policy's eligibility lists (order_types, time_in_force, venues); pretrade ORDER_TYPE and TIME_IN_FORCE checks refuse anything else before any outbox row exists. The shipped policies enable LMT/DAY only.
- Evidence: The terminal order ticket builds the exact submit_intent payload; the core makes the decision.
- Gap: no capability matrix keyed by (broker, account mode, product, venue, session, order type, TIF); eligibility is a flat list per policy
- Gap: no catalog of broker order types (native vs simulated), no in-place amendment rules (AC3), no Kite encoding (AC4)
- Gap: no paper round-trip evidence per enabled cell (AC2)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: ORD-09, RSK-01

### CAP-OMS-02: Order lifecycle state machine, parent/child model and order blotter (tested)

- Files: `backend/src/unknown/core/handlers/orders.py`, `backend/src/unknown/core/handlers/broker.py`, `backend/src/unknown/gateway/runner.py`, `backend/src/unknown/broker/ibkr/mapping.py`, `docs/adr/0009-order-lifecycle-and-outbox.md`, `terminal/src/workspaces/orders/BlotterView.tsx`, `backend/src/unknown/core/order_machine.py`, `backend/src/unknown/core/desk.py`
- Tests: `test_orders_lifecycle::ReservationTests.test_submit_pending_is_committed_before_the_broker_is_called`; `test_orders_lifecycle::CancelTests.test_fill_during_cancel_pending_consumes_its_share_of_the_hold`; `test_orders_lifecycle::CancelTests.test_cancel_request_releases_nothing_until_broker_confirms`; `test_orders_lifecycle::CallbackTests.test_duplicate_execution_callbacks_have_one_economic_effect`; `test_orders_lifecycle::SubmissionOutcomeTests.test_unknown_stays_unknown_and_reserved_while_broker_unreachable`; `test_reconciliation::ExternalTradeTests.test_manual_trade_is_owned_by_external_with_non_blocking_case`; `test_control_recovery::RedStopTests.test_stop_sends_cancel_for_working_buy_and_keeps_reservation_until_confirmed`; `test_ibkr_adapter::MappingTests.test_status_table`; `test_ibkr_adapter::StatusEventTests.test_identical_status_messages_produce_one_event` (IBKR venv); `src/workspaces/orders/model.test.ts::order states and cancel > classifies open and terminal states like the core`; `e2e/01-flow.spec.ts::a BUY at the FIXTURE ask + 0.02 is accepted and shows FILLED in the blotter`; `test_core_lifecycle_transitions::TableTests.test_table_never_leaves_a_terminal_state_and_names_only_real_states`; `test_core_lifecycle_transitions::TableTests.test_a_full_lifecycle_records_only_declared_transitions`; `test_core_lifecycle_transitions::EnforcementTests.test_undeclared_transition_is_refused_recorded_as_an_anomaly_and_latches`; `test_core_lifecycle_transitions::InactiveOrderTests.test_inactive_buy_keeps_its_reservation_blocks_entries_and_is_canceled`
- Evidence: Local states follow ADR-0009 with quantities, cancel_state and prev_state stored separately; the red stop cancels system orders one by one (no global cancel). The blotter shows the broker order id.
- Evidence: One published transition table (core/order_machine.py; ADR-0009 amendment) is enforced at Context.emit for every handler: an undeclared state change is refused and recorded as OrderTransitionRefused with a blocking ORDER_TRANSITION_REFUSED case and the stop latch, and a terminal state is never left. An IBKR Inactive order is held as UNKNOWN with every reservation (a buy is canceled) until a broker terminal report or the owner's close on evidence.
- Gap: parent/child orders not implemented
- Gap: blotter shows no evidence-level column (AC6)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: ORD-02, ORD-03, ORD-04, ORD-05, ORD-07

### CAP-OMS-03: Pre-trade controls: order presets, precautionary limits and rule-based compliance (tested)

- Files: `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/core/handlers/orders.py`, `backend/src/unknown/policy/model.py`
- Tests: `test_orders_lifecycle::PretradeLimitTests.test_price_collar_bounds_buy_and_sell_limits`; `test_orders_lifecycle::PretradeLimitTests.test_stale_missing_or_delayed_quote_rejects`; `test_orders_lifecycle::PretradeLimitTests.test_max_order_notional`; `test_orders_lifecycle::OrderRateTests.test_order_rate_limit_per_minute`; `test_orders_lifecycle::ReservationTests.test_buy_cash_hold_is_limit_notional_plus_fee_envelope`; `test_desk_commands::RejectionRecordTests.test_rejected_command_is_stored_with_a_command_rejected_event`; `test_desk_commands::PermissionMatrixTests.test_agent_cannot_mutate_the_core_with_well_formed_payloads`; `test_analytics_whatif::SharedCodeTest.test_dry_run_equals_the_live_gate_over_random_states`; `test_strategy_pending_exposure::PretradeWithPendingTest.test_concentration_counts_pending_buys_of_every_open_state`; `test_strategy_pending_exposure::PretradeWithPendingTest.test_gross_exposure_counts_every_open_buy`; `test_core_lifecycle_executions::SelfCrossTests.test_buy_that_would_trade_against_our_working_sell_is_refused_self_cross`
- Evidence: One pure evaluate() runs every check on one materialized snapshot and records all outcomes plus the first decisive code; the decision is stored with accepted and rejected intents (ORD-08).
- Evidence: CONCENTRATION and GROSS_EXPOSURE count pending buys of every open state, and SELF_CROSS refuses an order that would trade against our own working order; each is tested by a rejection against a passing control.
- Gap: order presets (instrument-type and ticker level) not implemented
- Gap: no restricted list, no restrict/notify rule classes, no trading-hours window check
- Gap: liquidity check needs ADV data (not evaluated without it)
- Gap: no broker precaution snapshot in preflight (AC4); no Web API reply allow-list (AC5)
- Requirements: RSK-01, RSK-02, ORD-08

### CAP-OMS-04: IBKR connectivity: TWS/IB Gateway session, authentication, pacing and transport options (US and Europe) (tested)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/config.py`, `backend/src/unknown/broker/ibkr/pacing.py`, `backend/src/unknown/broker/ibkr/serve.py`, `backend/src/unknown/service/core.py`, `backend/src/unknown/core/handlers/control.py`
- Tests: `test_ibkr_adapter::ConnectionTests.test_handshake_verifies_paper_identity_and_sends_only_the_clock_query` (IBKR venv); `test_ibkr_adapter::ConnectionTests.test_missing_next_valid_id_reports_auth_required_and_blocks_requests` (IBKR venv); `test_ibkr_adapter::ConnectivityTests.test_connection_closed_then_reconnect` (IBKR venv); `test_ibkr_adapter::PacingTests.test_never_more_than_r_max_in_any_one_second_window`; `test_ibkr_adapter::PacingTests.test_reserved_share_keeps_cancels_flowing_when_entries_saturate`; `test_ibkr_adapter::ConfigTests.test_live_mode_is_refused`; `test_service_supervisor::SupervisorTests.test_reconnects_with_capped_exponential_backoff`; `test_control_recovery::RestartRecoveryTests.test_restart_without_stop_holds_entries_for_reconciliation_and_owner`
- Evidence: TWS API 10.51.01 socket adapter behind BrokerPort; entries need a clean reconciliation and an owner green start; pacing is a sliding one-second window of floor(lines/2) with a reserved cancel share.
- Evidence: All 179 IBKR adapter, market-data and service tests pass in the IBKR venv (official ibapi, TWS API 10.51.01) on 2026-10-22; 65 of them are skipped in interpreters without ibapi.
- Gap: Web API / FIX transports not implemented (TWS socket only)
- Gap: session record lacks the capability-record version; Gateway restart fault test not run against a real host (AC2)
- Gap: paper and live credential separation is enforced by refusing LIVE, not by a validated pair of profiles (AC5)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-01, BRK-02, BRK-03, BRK-04

### CAP-OMS-06: Execution (fill) and commission event capture with corrections and history backfill (tested)

- Files: `backend/src/unknown/core/handlers/broker.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/mapping.py`, `backend/src/unknown/reconcile/reconciler.py`, `backend/src/unknown/gateway/journal.py`, `backend/src/unknown/gateway/runner.py`
- Tests: `test_orders_lifecycle::CallbackTests.test_duplicate_execution_callbacks_have_one_economic_effect`; `test_orders_lifecycle::CallbackTests.test_execution_correction_reverses_and_rebooks_exactly`; `test_orders_lifecycle::CallbackTests.test_delayed_commission_keeps_estimate_until_released`; `test_orders_lifecycle::CallbackTests.test_commission_before_its_execution_is_deferred_then_applied`; `test_review_fixes::ExecutionLedgerTests.test_f15_every_arrival_order_of_correction_and_commissions_books_one_commission`; `test_replay_randomized::CorrectionCommissionConservationTest.test_correction_after_final_commission_is_charged_once`; `test_ibkr_adapter::MappingTests.test_exec_id_revision`; `test_ibkr_adapter::ExecutionTests.test_corrections_link_to_the_latest_prior_revision` (IBKR venv); `test_ibkr_adapter::ExecutionTests.test_history_gap_across_midnight_requires_owner_acknowledgement` (IBKR venv); `test_gateway_journal::CrashWindowTest.test_crash_between_journal_append_and_apply_loses_nothing`; `test_gateway_journal::DiskFullTest.test_disk_full_on_append_fails_closed_and_recovers_in_order`; `test_core_lifecycle_executions::ChangedRedeliveryTests.test_same_execution_id_with_changed_price_keeps_the_original_and_blocks_until_the_owner_resolves`; `test_core_lifecycle_post_completion::LateFillTests.test_late_fill_after_completion_lands_in_protected_and_is_disposed_and_reconciled`; `test_core_lifecycle_post_completion::BustAndCorrectionTests.test_bust_of_the_wind_down_sell_returns_the_shares_to_protected_for_disposal`
- Evidence: Executions dedupe on a scoped broker key; corrections reverse and rebook linked to the original; fees are estimated at fill and finalized by the commission report; a history gap across the host's midnight needs an owner attestation (broker_admin_action acknowledge_history_gap).
- Evidence: Every broker event is appended to a durable, hash-chained journal (<account>.events) before it is applied, so a crash after an event is journaled loses nothing (an event taken from the adapter but not yet journaled is not covered), and a re-application is a duplicate (BR-ORD-20). The same execution id with changed content is recorded and blocks without booking (BR-ORD-05). Late fills, busts and corrections after a session ended are booked to protected:<id> or unallocated with a blocking case (BR-LED-03).
- Gap: history backfill beyond the broker's window needs statements (CAP-OMS-07, not built)
- Gap: broker-reported realized P&L is not stored or compared
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Gap: an event the adapter received but the gateway had not yet journaled is lost at a crash and recovered only by reconciliation inside the broker's execution-history window
- Requirements: ORD-05, ORD-06, LED-07, LED-10

### CAP-OMS-08: Three-way reconciliation: real-time events, intraday broker snapshot and end-of-day statement vs ledger (tested)

- Files: `backend/src/unknown/reconcile/reconciler.py`, `backend/src/unknown/core/handlers/recon.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/core/handlers/control.py`
- Tests: `test_reconciliation::ComparisonTests.test_position_difference_is_detected_and_blocks`; `test_reconciliation::ComparisonTests.test_overlapping_executions_are_deduplicated_and_watermark_advances_only_on_clean`; `test_reconciliation::IncompleteReconciliationTests.test_incomplete_snapshot_after_restart_keeps_entries_blocked`; `test_reconciliation::ExternalTradeTests.test_manual_trade_cash_need_including_commission_is_covered_from_outside`; `test_reconciliation::SettlementEvidenceTests.test_settlement_requires_exact_broker_cash_agreement`; `test_durability_review::ReconciliationOrderTest.test_the_execution_watermark_comes_from_the_last_clean_run`; `test_replay_randomized::RandomizedReplayEquivalenceTest.test_seeded_scenarios_preserve_invariants_replay_and_conservation`; `test_ibkr_adapter::SnapshotTests.test_complete_snapshot_maps_settled_and_total_cash` (IBKR venv); `test_core_lifecycle_unknown_orders::CloseUnknownOrderTests.test_owner_closes_a_stuck_unknown_order_on_evidence_and_the_account_can_restart`; `test_core_lifecycle_unknown_orders::CloseUnknownOrderTests.test_refused_while_the_broker_may_still_work_the_order`; `test_core_lifecycle_guards::CloseGuardTests.test_a_working_orders_difference_blocks_the_close_and_a_working_order_is_never_closed_on_evidence`
- Evidence: Tier 1 (events matched to intents, external trades attributed) and tier 2 (snapshot, executions with overlap, open orders, lookups, convergence loop) run in one consistent cut; the watermark advances only on a clean run.
- Evidence: Each run records the order ids the broker listed as open, the lookups and the last event it includes. The owner closes a stuck UNKNOWN order only on broker evidence, and only after a complete, fresh reconciliation in this process shows nothing working (resolve_exception close_unknown_order; RB-13).
- Gap: tier 3, the end-of-day statement leg (IBKR Flex), is not implemented: two of three legs only
- Gap: session close does not wait for a statement reconciliation (AC4)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: REC-01, REC-02, REC-03, REC-04

### CAP-OMS-09: Settlement-date calculation (US T+1, India T+1/T+0, EU T+2 to T+1 on 11 Oct 2027) and settled-cash availability (tested)

- Files: `backend/src/unknown/domain/calendar.py`, `backend/src/unknown/domain/markets.py`, `backend/src/unknown/core/handlers/recon.py`, `backend/src/unknown/core/ledger.py`, `config/calendars/`, `config/markets/venues.json`, `backend/src/unknown/service/core.py`
- Tests: `test_reconciliation::SettlementEvidenceTests.test_friday_trade_settles_monday_only_when_broker_shows_it`; `test_reconciliation::SettlementEvidenceTests.test_sale_proceeds_become_available_only_on_broker_evidence`; `test_reconciliation::SettlementEvidenceTests.test_holiday_calendar_moves_expected_date_and_confirmation_waits_for_evidence`; `test_markets::SettlementTests.test_eu_moves_to_t_plus_1_on_trade_date_2027_10_11`; `test_markets::SettlementTests.test_us_and_india_are_t_plus_1`; `test_markets::BusinessDayArithmeticTests.test_india_settlement_holidays_differ_from_trading`; `test_service_calendars::ServiceCalendarTests.test_xnys_settlement_skips_thanksgiving`; `test_service_calendars::ServiceCalendarTests.test_policies_name_calendars_the_service_loads`
- Evidence: The core predicts expected settlement from the policy's settlement calendar and the instrument's settlement days, and confirms it only from broker cash evidence; available cash excludes receivables (LED-03).
- Evidence: The venue registry computes settlement cycles by trade date (US T+1, India T+1, EU T+2 then T+1 from 2027-10-11) on separate trading and settlement calendars.
- Evidence: The core service passes load_calendars(config/calendars) to the desk at start (service/core.py), and every shipped policy names a calendar in that set; until b87451b it loaded WEEKDAYS only, so the paper policy's XNYS settlement calendar silently fell back to weekdays (a 2026-11-25 trade would have been expected to settle on Thanksgiving). The cited test_service_calendars tests check the loader, the shipped policies and XNYS date arithmetic, not the running service.
- Gap: the core does not read the venue registry: its settlement days come from the instrument record, so the EU rule switch is reference data only
- Gap: NSE T+0 settlement type not modelled (AC3); India and EU are not eligible for trading
- Gap: DTC settlement calendar unverified (XNYS used conservatively)
- Gap: no comparison of computed and broker-reported settle dates over a live sample (AC4)
- Gap: a policy naming a calendar that is not loaded still falls back to WEEKDAYS silently in the core (handlers/broker.py and handlers/recon.py) instead of being refused at publish or activation
- Gap: no test starts the core service and checks which calendar its desk settles with, so the b87451b fix has no regression test: with service/core.py reverted to WEEKDAYS only, all 1,205 backend tests still pass (checked 2026-10-22)
- Requirements: LED-03, REC-04, DOM-06

### CAP-OMS-11: Commission, fee and tax-charge estimation with estimated-vs-final tracking (tested)

- Files: `backend/src/unknown/risk/fees.py`, `backend/src/unknown/core/handlers/broker.py`, `backend/src/unknown/research/costs.py`
- Tests: `test_orders_lifecycle::ReservationTests.test_buy_cash_hold_is_limit_notional_plus_fee_envelope`; `test_orders_lifecycle::FeeOverrunTests.test_commission_above_envelope_is_recorded_and_opens_blocking_overcommitment`; `test_orders_lifecycle::CallbackTests.test_delayed_commission_keeps_estimate_until_released`; `test_review_fixes::SettlementAndFeeTests.test_f24_commission_refund_after_settlement_relieves_the_payable`; `test_research_costs::IbkrFixedKnownAnswers.test_page_examples`; `test_research_costs::IbkrFixedKnownAnswers.test_envelope_never_below_charged_commission`
- Evidence: Reservations use a conservative per-share/minimum/cap envelope from the policy; the broker commission report finalizes the fee; an overrun opens a blocking OVERCOMMITTED case. The research cost model reproduces the IBKR Fixed page examples (100 shares at USD 25 -> USD 1.00).
- Gap: fee model values UNVERIFIED pending IBKR schedule confirmation; core and research use two separate implementations (cross-checked by the core replay)
- Gap: regulatory and exchange fees, taxes and Tiered pass-through fees not modelled in the core
- Gap: no effective-dated fee schedule versions in the core policy (AC4); no Kite charges (AC3)
- Requirements: LED-07

### CAP-OMS-14: Simulation, broker-paper and live evidence levels with paper-environment limitations (tested)

- Files: `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/core/handlers/control.py`, `backend/src/unknown/broker/simulated.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/service/fixture_market.py`, `backend/src/unknown/market/quotes.py`
- Tests: `test_simulated_broker::SimulatedBrokerTests.test_capabilities_are_labelled_simulated`; `test_review_fixes::AuthorityTests.test_f21_preflight_refuses_a_broker_reporting_a_live_account`; `test_review_fixes::AuthorityTests.test_f21_preflight_fails_closed_on_unknown_broker_account_mode`; `test_policy_mandate::PublishActivateTests.test_live_policy_publishes_but_activation_is_refused`; `test_quote_consolidation::QuoteConsolidationTests.test_non_consolidated_quote_validates_paper_but_not_live`; `test_ibkr_adapter::ExecutionTests.test_live_execution_and_commission_rows` (IBKR venv); `e2e/01-flow.spec.ts::overview shows SIMULATED in text and colour, and labels fixture data FIXTURE`
- Evidence: Account mode SIMULATED/PAPER/LIVE is part of the account record, the policy and preflight; LIVE is refused; every IBKR execution, commission and status raw record carries evidence_level BROKER_PAPER; fixture quotes are labelled FIXTURE.
- Gap: no paper-limitations register and no UNTESTED labelling of paper-unsupported order types (AC2, AC3)
- Gap: one database per account keeps modes apart, but no query-layer test asserts that reports never aggregate modes (AC1)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: RES-05

### CAP-UX-01: Command line / palette for function, instrument, document and help search (tested)

- Files: `terminal/src/ui/`, `terminal/src/workspaces/registry.ts`
- Tests: `src/ui/ui-logic.test.ts::palette filtering > requires every token and ranks prefix matches first`; `src/workspaces/registry.test.ts::workspace registry > rejects duplicate palette command ids within a workspace`; `e2e/01-flow.spec.ts::keyboard: Cmd/Ctrl+K opens the palette, which navigates to Sessions and to a session`
- Evidence: Cmd/Ctrl+K opens a palette over registered workspaces and their commands; it navigates and never submits orders.
- Evidence: Terminal on 2026-10-22: typecheck clean, 259 vitest unit tests pass, production build succeeds; the Playwright suite (31 tests, Chromium 1243, headless, audio disabled) passes against a real core and API with the FIXTURE market. CI runs the typecheck, unit tests and build; the Playwright suite runs only locally.
- Gap: no instrument, document or help search index in the palette (AC1-AC4)
- Gap: no command history (AC9)
- Gap: APG combobox behaviour and screen-reader passes not verified (AC5, AC6)
- Requirements: TRM-01

### CAP-UX-02: Keyboard-first operation: scoped, rebindable shortcuts, hint mode, history and safe order hotkeys (tested)

- Files: `terminal/src/ui/`, `terminal/src/workspaces/registry.ts`
- Tests: `src/ui/ui-logic.test.ts::two-key shortcuts > matches "g o" within the timeout only`; `src/ui/charKeys.test.ts::single-character shortcut preference (WCAG 2.1.4) > is on by default and can be turned off and on without storage`; `src/workspaces/registry.test.ts::workspace registry > rejects duplicate ids, duplicate shortcuts and malformed entries`; `e2e/01-flow.spec.ts::keyboard: single-character shortcuts are listed in the help and can be turned off (WCAG 2.1.4)`
- Evidence: Two-key workspace shortcuts with a help overlay; single-character shortcuts can be turned off (WCAG 2.1.4).
- Evidence: Terminal on 2026-10-22: typecheck clean, 259 vitest unit tests pass, production build succeeds; the Playwright suite (31 tests, Chromium 1243, headless, audio disabled) passes against a real core and API with the FIXTURE market. CI runs the typecheck, unit tests and build; the Playwright suite runs only locally.
- Gap: shortcuts are not rebindable (AC2, AC7)
- Gap: no hint mode, no order hotkeys, no stop chord from every route (AC5, AC6)
- Gap: no keyboard-only enumeration of every action (AC1)
- Requirements: TRM-01

### CAP-UX-03: Linked panels: numbered/coloured instrument-context channels with source/destination roles (tested)

- Files: `terminal/src/ui/link.ts`, `terminal/src/ui/workstation/Workstation.tsx`, `terminal/src/ui/workstation/layout.ts`, `terminal/src/ui/CommandLine.tsx`, `terminal/src/ui/functions.ts`, `terminal/src/ui/panel.ts`, `terminal/src/ui/StatusBar.tsx`
- Tests: `src/ui/live.test.ts::linked security channels > keeps channels independent and notifies only the changed channel`; `src/ui/live.test.ts::linked security channels > reads the enclosing panel channel by default and keeps the old default-channel API`; `src/ui/functions.test.ts::command execution > publishes the security on the target channel and opens the function there`; `src/ui/functions.test.ts::command execution > uses the channel security when none is typed, and refuses a security function without one`; `src/ui/workstation/layout.test.ts::workstation layout > changing the workspace clears the sub-route; unlinked channels are per panel`; `e2e/05-workstation.spec.ts::command line, linked panels, saved layouts, lock and recovery from a corrupt layout`
- Evidence: Eight numbered, named, coloured channels (FDC3 order) plus unlinked; each workstation panel joins one channel through a React context, so useLinkedSecurity()/useSetLinkedSecurity() follow the panel's channel while the exported link.ts API stays compatible.
- Evidence: A security typed in the command line (or SECURITY FUNCTION) is published on the active panel's channel; linked panels show it and an unlinked panel does not (unit tests and the e2e spec).
- Evidence: Channel membership is saved with the layout; the status bar and command line show the target channel by number and name, never colour alone.
- Gap: No source/destination roles: every panel both sends and receives.
- Gap: Context is a security key (SYMBOL.MIC or core id) only; no ISIN/FIGI/CIK crosswalk object and no portfolio/order/as-of contexts.
- Gap: Propagation latency (p95 target) and the randomized multi-panel invariant harness are not measured.
- Gap: No FDC3 or external-application bridge.
- Gap: Workspaces on this branch do not yet read the linked security; the chart and monitor branches adopt useLinkedSecurity().

### CAP-UX-04: Persistent, versioned multi-panel workspace layouts with templates, lock mode and recovery (tested)

- Files: `terminal/src/ui/workstation/layout.ts`, `terminal/src/ui/workstation/storage.ts`, `terminal/src/ui/workstation/Workstation.tsx`, `terminal/src/ui/workstation/workstation.css`
- Tests: `src/ui/workstation/layout.test.ts::workstation layout > splits a panel, inserts siblings in the same direction and activates the new panel on the same channel`; `src/ui/workstation/layout.test.ts::workstation layout > closes panels, collapses one-child splits, renormalises sizes and never closes the last panel`; `src/ui/workstation/layout.test.ts::workstation layout > resizes a divider within 10% bounds and keeps the sum at 100`; `src/ui/workstation/layout.test.ts::workstation layout > lock mode refuses every structural change but still lets the owner pick the active panel`; `src/ui/workstation/layout.test.ts::workstation layout > validates untrusted documents: every template passes, malformed ones are rejected`; `src/ui/workstation/layout.test.ts::layout persistence > round-trips the draft and named versions, and restores an older version`; `src/ui/workstation/layout.test.ts::layout persistence > recovers from a corrupt store: opens the template, says so and sets the bad text aside`; `src/ui/workstation/layout.test.ts::layout persistence > skips one damaged saved layout and keeps the rest`; `src/ui/workstation/layout.test.ts::layout persistence > never throws when storage is unavailable`; `e2e/05-workstation.spec.ts::command line, linked panels, saved layouts, lock and recovery from a corrupt layout`
- Evidence: Tiled split layout (row/column, resizable by pointer or arrow keys on a focusable separator) where each panel hosts any registered workspace; hash routes still open a single workspace.
- Evidence: Draft autosaved to localStorage; Save creates a new immutable version of a named layout and the last 10 versions are restorable; four own templates; lock mode refuses split, close, resize and workspace changes.
- Evidence: A layout holds only workspace ids, route segments and channels and is validated on load; a corrupt or unknown-schema store is set aside under its own key and a template opens with a plain notice (unit tests and the e2e spec).
- Gap: Browser localStorage only: no backend layout service, no cross-device sync and no concurrent-edit conflict prompt.
- Gap: Panel view state (columns, sort, filters) is not saved, only workspace, sub-route and channel.
- Gap: No layout export/import file, tabs/pages, pop-out windows or keyboard move of panels (resize only).
- Gap: Only schema 1 exists; there is no migration path yet, other schemas are set aside.

### CAP-UX-05: Bookmarks, favourites bar and deep links that restore a panel with instrument and parameters (live or as-of) (implemented)

- Files: `terminal/src/state/router.ts`, `terminal/src/workspaces/registry.ts`
- Tests: `src/workspaces/orders/model.test.ts::orders routes > round-trips every route`; `src/workspaces/audit/audit.test.ts::audit routes > round-trips event routes and compares filters`
- Evidence: Hash routes (#/<workspace>/<id> with filters) restore a workspace and record behind the owner login.
- Gap: no bookmarks or favourites bar (AC1, AC5)
- Gap: no as-of deep links (AC3)
- Gap: no instrument panels to restore
- Requirements: TRM-01

### CAP-UX-06: Accessibility: WCAG 2.2 AA conformance for dense financial screens, with a published conformance report (tested)

- Files: `terminal/e2e/support/a11y.ts`, `terminal/e2e/02-a11y.spec.ts`, `terminal/src/ui/styles.css`
- Tests: `e2e/02-a11y.spec.ts::every workspace passes the accessibility checks (${scheme} theme)`; `e2e/02-a11y.spec.ts::narrow screens: no sideways page scroll, no overlapping header items, STOP stays visible`; `e2e/00-audit-selftest.spec.ts::the accessibility checker reports nothing on a clean page`; `e2e/00-audit-selftest.spec.ts::the focus check catches a control with its outline removed`
- Evidence: An automated pass over 29 views in both themes: accessible names, text and field-edge contrast, tables, ARIA references, page structure, target size and focus indicators; five widths from 390 px.
- Evidence: Terminal on 2026-10-22: typecheck clean, 259 vitest unit tests pass, production build succeeds; the Playwright suite (31 tests, Chromium 1243, headless, audio disabled) passes against a real core and API with the FIXTURE market. CI runs the typecheck, unit tests and build; the Playwright suite runs only locally.
- Gap: no manual WCAG 2.2 audit and no published conformance report (AC2, AC7)
- Gap: grids are not tested against the APG grid keyboard pattern (AC3)
- Gap: no pause control or reduce-motion preference (AC4)
- Gap: AC1 asks for V = 0 in CI: every cited test is a Playwright test, and CI does not run the Playwright suite, so a regression is caught only by a local run
- Requirements: TRM-01

### CAP-UX-09: 'Show calculation' inspector: formula, inputs, units, adjustments and level-by-level lineage to source records (implemented)

- Files: `backend/src/unknown/data/fundamentals/metrics.py`, `backend/src/unknown/api/routes/company.py`, `backend/src/unknown/api/routes/portfolio.py`, `backend/src/unknown/analytics/common.py`, `terminal/src/workspaces/company/Metrics.tsx`
- Tests: `test_company_metrics::MetricTests.test_show_calculation`; `test_company_metrics::MetricTests.test_persist_with_record_metric_and_explain_record`; `test_analytics_review::RouteParameterTest.test_performance_lineage_names_the_nav_field_used`; `e2e/03-research.spec.ts::company: search, tearsheet, point-in-time statements, metrics, DCF and peers`
- Evidence: Company metrics expose a show-calculation tree (formula, inputs, accessions, availability times) and portfolio responses carry lineage blocks with formula ids.
- Evidence: Terminal: Company > Metrics has a 'Show calculation' panel: the formula, inputs down to the source filings, and the EV bridge.
- Gap: the terminal inspector covers company metrics only (end-to-end test); desk and portfolio values have none, and there is no inspector export or latency run (AC1, AC8, AC9)
- Gap: core workspaces show values without lineage ids
- Requirements: DAT-05, TRM-03

### CAP-UX-11: Value display semantics: currency, scale, locale grouping, time zones, sign glyphs, staleness and explicit missingness (tested)

- Files: `terminal/src/format/`, `terminal/src/ui/`, `terminal/src/workspaces/risk/`
- Tests: `src/format/decimal.test.ts::parseDecimal > rejects anything that is not a plain decimal`; `src/format/exact.test.ts::exact decimal arithmetic > adds and subtracts without float error`; `src/workspaces/risk/riskModel.test.ts::marked P&L estimate > fails closed on a stale, delayed or missing bid, like the core`; `src/ui/ui-logic.test.ts::table sorting > sorts decimal strings exactly and keeps empty values last in both directions`; `e2e/01-flow.spec.ts::overview shows SIMULATED in text and colour, and labels fixture data FIXTURE`
- Evidence: Decimal strings are formatted and compared exactly (no floats); missing marks fail closed; SIMULATED mode and FIXTURE data are labelled in text.
- Evidence: Terminal on 2026-10-22: typecheck clean, 259 vitest unit tests pass, production build succeeds; the Playwright suite (31 tests, Chromium 1243, headless, audio disabled) passes against a real core and API with the FIXTURE market. CI runs the typecheck, unit tests and build; the Playwright suite runs only locally.
- Gap: no locale switching (en-IN, de-DE) (AC2)
- Gap: no exchange-local times with offset (AC7)
- Gap: no lint rule for numeric rendering (AC8)
- Requirements: TRM-03, DOM-02

### CAP-UX-12: Live panels: pause/freeze, manual refresh scopes, snapshot-on-gap and point-in-time (as-of) viewing (implemented)

- Files: `terminal/src/state/stream.ts`, `terminal/src/state/queries.ts`
- Tests: `src/state/stream.test.ts::StreamStore sequencing > detects gaps (coalesced or missed notifications) and refetches`; `src/state/stream.test.ts::StreamStore sequencing > treats a lower seq as a reset (restored database) and refetches`; `src/state/stream.test.ts::StreamStore sequencing > manual refresh bumps the version; stop closes the source`
- Evidence: The SSE stream detects gaps and resets and refetches; manual refresh is available.
- Gap: no pause/freeze per panel (AC1, AC2)
- Gap: no as-of viewing (AC5)
- Gap: no reduce-motion preference (AC6)
- Requirements: TRM-01

### CAP-AGT-01: Bounded agent jobs: envelope, deadlines, turn/tool/retry caps, no unbounded spawning (tested)

- Files: `backend/src/unknown/agents/envelope.py`, `backend/src/unknown/agents/jobstore.py`, `backend/src/unknown/agents/worker.py`, `backend/src/unknown/agents/runner.py`, `backend/src/unknown/agents/submission.py`
- Tests: `test_agents_runner::RunnerTest.test_max_turns_stops_the_loop`; `test_agents_runner::RunnerTest.test_max_tool_calls`; `test_agents_runner::RunnerTest.test_output_after_deadline_is_discarded_not_submitted`; `test_agents_envelope::SchedulerTest.test_depth_limit_and_child_cannot_widen`; `test_agents_envelope::SchedulerTest.test_dedupe_returns_one_execution_and_shared_output`; `test_agents_jobstore::JobStoreTest.test_resubmitting_the_same_job_is_idempotent_and_reusing_its_id_is_refused`; `test_agents_jobstore::JobStoreTest.test_past_deadlines_and_child_jobs_are_refused`; `test_agents_worker::WorkerFailsClosedTest.test_cancel_while_running_discards_the_output`
- Evidence: Envelopes fix role, schema, prompt hash, input hashes, deadline, turn/tool/retry/token caps and the spend cap before any call; the durable job store and the model worker run one job at a time under per-role and global limits.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: kill_grace_s is not in the envelope; a provider call in flight at the deadline is not interrupted (the output is discarded)
- Gap: relevance_end is recorded but no order-intent service consumes proposals yet
- Gap: child jobs: the envelope supports depth 1 but the durable job store accepts root jobs only
- Gap: no live research job has run on this Mac: the model is not approved in config/models/approved.json and no key is installed
- Requirements: AI-01, AUT-04, AI-03

### CAP-AGT-02: Agent cost budgets: pre-call reservation, hierarchical caps, provider hard limits and billing reconciliation (tested)

- Files: `backend/src/unknown/agents/budget.py`, `backend/src/unknown/agents/tariffs.py`, `backend/src/unknown/agents/config.py`, `backend/src/unknown/agents/worker.py`
- Tests: `test_agents_budget::LedgerTest.test_paid_calls_disabled_without_policy_or_with_zero_budget`; `test_agents_budget::LedgerTest.test_refuses_at_each_cap`; `test_agents_budget::LedgerTest.test_concurrent_workers_never_exceed_budget`; `test_agents_budget::LedgerTest.test_unconfirmed_stays_consumed_until_reconciled_and_overrun_is_recorded`; `test_agents_runner::RunnerTest.test_local_budget_refusal_happens_before_any_call`; `test_agents_worker::WorkerFailsClosedTest.test_budget_exhaustion_fails_the_job_without_any_network_call`
- Evidence: Reserve-before-call in one BEGIN IMMEDIATE transaction against lifetime/month/day/job/role/provider caps; unknown usage stays reserved; the owner's caps (USD 200 lifetime, 15/day, 2/job) live outside the repository.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: provider cost-report reconciliation is manual: reconcile() takes an owner evidence string; usage/cost APIs not integrated
- Gap: the concurrency property test uses 10 threads, not 10,000 randomized trials (AC3)
- Gap: no provider-side hard spend limit is recorded
- Requirements: AI-05, GATE-01

### CAP-AGT-03: Per-role tool allowlists and capability manifests enforced outside the model (tested)

- Files: `backend/src/unknown/agents/tools.py`, `backend/src/unknown/domain/principal.py`, `backend/src/unknown/service/ipc.py`, `backend/src/unknown/core/desk.py`
- Tests: `test_agents_evidence::ToolGatewayTest.test_registry_refuses_write_capable_tools`; `test_agents_evidence::ToolGatewayTest.test_non_allowlisted_tool_is_refused_and_never_executed`; `test_agents_evidence::ToolGatewayTest.test_arguments_validated_and_rights_checked`; `test_desk_commands::PermissionMatrixTests.test_permission_table_matches_adr_0007_for_every_role_and_kind`; `test_desk_commands::PermissionMatrixTests.test_agent_cannot_mutate_the_core_with_well_formed_payloads`; `test_agents_static::StaticTest.test_agents_never_import_execution_or_ledger_code`
- Evidence: A default-deny tool gateway checks the envelope allowlist, registration, argument schema and data rights; the registry refuses write-capable tools; the core permission table lets AGENT only submit typed research proposals.
- Gap: no owner-signed capability manifests (AC3)
- Gap: no per-role privilege surface report (AC5)
- Requirements: AUT-04, SEC-02, AI-01

### CAP-AGT-05: Owner intervention controls: pause roles, cancel jobs, disable model-dependent decisions, red stop with order cancel (tested)

- Files: `backend/src/unknown/core/handlers/control.py`, `backend/src/unknown/api/routes/research_jobs.py`, `backend/src/unknown/agents/jobstore.py`, `backend/src/unknown/agents/config.py`, `backend/src/unknown/service/core.py`
- Tests: `test_control_recovery::RedStopTests.test_owner_stop_latches_durably_with_actor_reason_and_command`; `test_control_recovery::RestartRecoveryTests.test_stop_survives_restart_and_restart_sets_recovery_hold`; `test_desk_commands::PermissionMatrixTests.test_every_forbidden_pair_is_rejected_unauthorized_by_the_desk`; `test_api_research_jobs::ResearchJobRoutesTest.test_submit_list_detail_and_cancel`; `test_agents_worker::WorkerFailsClosedTest.test_a_role_without_a_concurrency_limit_never_runs`; `e2e/01-flow.spec.ts::red STOP shows its command id and progress, and a new BUY is rejected with STOP_LATCH`; `test_core_lifecycle_stop::MemoryLatchTests.test_stop_with_an_unwritable_database_latches_in_memory_cancels_buys_and_is_recorded_later`; `test_core_lifecycle_stop::MemoryLatchTests.test_the_latch_file_carries_an_unrecorded_stop_across_a_restart`; `test_core_lifecycle_stop::MemoryLatchTests.test_a_corrupt_control_row_cannot_block_the_red_stop`; `test_core_lifecycle_guards::MemoryLatchGuardTests.test_an_unreadable_ledger_still_cancels_every_buy_the_broker_lists_as_ours`; `test_core_lifecycle_guards::MemoryLatchGuardTests.test_every_exposure_increasing_command_waits_while_the_stop_is_latched_only_in_memory`
- Evidence: The red stop latches durably before any cancel and only the owner can start again; the owner cancels research jobs over the API; removing a role's concurrency limit or the agents policy file stops that role or all paid calls.
- Evidence: A red stop is never blocked by persistence. When the database cannot record it, the core latches it in memory and in an fsync'd file, refuses entries, transmits no buy and cancels open buys at the broker directly. It records the stop with that evidence as soon as writes work (ADR-0010 amendment, RB-12).
- Gap: no 'disable model-dependent decisions' switch: no model output feeds orders today
- Gap: no kill-latency measurement (AC6)
- Gap: orders do not carry an originating job id (AC5)
- Requirements: RSK-03, AUT-01, AI-01

### CAP-AGT-06: Prompt-injection defence for filings, news, web pages, owner uploads and tool outputs (tested)

- Files: `backend/src/unknown/agents/evidence.py`, `backend/src/unknown/agents/tools.py`, `backend/src/unknown/agents/runner.py`, `docs/security/threat-model.md`
- Tests: `test_agents_runner::RunnerTest.test_prompt_injection_document_has_zero_effect`; `test_agents_evidence::UntrustedTextTest.test_data_block_cannot_be_closed_by_the_document`; `test_agents_evidence::ToolGatewayTest.test_injected_tool_output_changes_no_permission`; `test_agents_runner::RunnerTest.test_unverified_claims_never_become_evidence`
- Evidence: Documents are rendered as untrusted data behind a random nonce; tool authority comes only from the envelope; claims are verified against spans before they become evidence (threat model T10).
- Gap: no injection corpus or canary scanner (AC1, AC2)
- Gap: not a required release-gate job (AC4)
- Requirements: AI-04

### CAP-AGT-07: Secret and sensitive-data isolation for agents (credentials, account ids, owner data) (implemented)

- Files: `backend/src/unknown/agents/worker.py`, `backend/src/unknown/api/routes/research_jobs.py`, `backend/src/unknown/service/paths.py`, `backend/src/unknown/agents/config.py`
- Tests: `test_agents_static::StaticTest.test_only_the_worker_reaches_the_core_socket_and_it_never_holds_the_owner_token`; `test_api_research_jobs::ApiNeverHoldsProviderCodeTest.test_route_sources_never_touch_keys_or_providers`; `test_agents_static::StaticTest.test_agents_never_import_execution_or_ledger_code`
- Evidence: Agents hold no broker credentials and no ledger handle; only the model worker reads the provider key and it asserts only AGENT on the core socket; account ids live outside the repository.
- Gap: the worker runs as the same OS user as the core, so a same-user process can read every token file (book finding BR-OPS-04)
- Gap: no canary scan of recorded model requests for credentials or account ids (AC3, AC4)
- Requirements: SEC-01, SEC-02

### CAP-AGT-08: Data-rights gating before content enters a model context (licence, entitlement, retention, provider) (tested)

- Files: `backend/src/unknown/agents/gate.py`, `backend/src/unknown/data/registry.py`, `config/models/provider_policies.json`
- Tests: `test_agents_gate::GateTest.test_dataset_without_model_processing_rights_is_blocked`; `test_agents_gate::GateTest.test_unknown_facts_block`; `test_agents_gate::GateTest.test_retention_ceiling`; `test_agents_gate::GateTest.test_stale_policy_blocks`; `test_agents_runner::RunnerTest.test_restricted_dataset_is_never_sent`
- Evidence: Every artifact in a provider payload (generation, token counting or tokenizing) needs registry rights, owner data policy, a fresh provider policy, a retention ceiling and training off.
- Gap: no label propagation to derived summaries and exports (AC2)
- Gap: no canary scan of recorded requests (AC1)
- Requirements: DAT-01, AI-04

### CAP-AGT-09: Agent audit trail: tamper-evident lineage from evidence to job to proposal to order (tested)

- Files: `backend/src/unknown/agents/audit.py`, `backend/src/unknown/core/replay.py`, `backend/src/unknown/core/desk.py`, `backend/src/unknown/core/handlers/control.py`
- Tests: `test_agents_audit::AuditTrailTest.test_records_are_chained_and_queryable_by_job`; `test_agents_audit::AuditTrailTest.test_edits_deletions_and_reattribution_are_detected`; `test_agents_audit::AuditTrailTest.test_update_and_delete_are_refused`; `test_agents_worker::WorkerRecoveryTest.test_audit_trail_is_shared_and_verifiable_after_restart`; `test_desk_commands::HashChainTests.test_tampered_payload_is_detected`; `test_research_proposal_v2::AgentEmitsVersionTwoTest.test_typed_fields_come_from_the_job_and_the_span_provenance`; `test_research_proposal_v2::ProposalVersionTwoValidationTest.test_valid_v2_proposals_are_recorded_with_the_core_label`
- Evidence: An append-only, hash-chained trail of every runner record and worker action per job (evidence span hashes, envelope, reservations, gate decisions, proposal, core proposal id), beside the core's hash-chained event log.
- Evidence: Proposals are research_proposal v2. They carry typed evidence references (dataset, observation id, span hash, observed and available times), instrument ids checked against the instrument master, and an as-of. All of these come from the job and the store provenance, never from model output, and the core labels each proposal (BR-DAT-12).
- Gap: proposal-to-order lineage: no order intent consumes research proposals yet
- Gap: tail truncation of a chain is detectable only against an externally recorded head
- Gap: licensed-payload canary scan not implemented
- Requirements: DOM-04

### CAP-AGT-10: Grounded research Q&A over filings and permitted documents with span-level citations and generated query code (implemented)

- Files: `backend/src/unknown/agents/runner.py`, `backend/src/unknown/agents/evidence.py`, `backend/src/unknown/agents/pit_evidence.py`, `backend/src/unknown/api/routes/research_jobs.py`, `terminal/src/workspaces/research-jobs/JobDetailView.tsx`
- Tests: `test_agents_pit_evidence::EvidenceTest.test_pinned_evidence_ignores_later_knowledge_and_later_publications`; `test_agents_evidence::ClaimVerificationTest.test_verified_claim`; `test_agents_runner::RunnerTest.test_happy_path_submits_a_verified_proposal_and_settles_exactly`; `test_api_research_jobs::TerminalToCoreEndToEndTest.test_question_to_recorded_proposal`; `test_research_proposal_v2::WorkerEmitsVersionTwoEndToEndTest.test_agent_job_end_to_end_records_a_typed_v2_proposal`; `src/workspaces/research-jobs/render.test.ts::proposal panel, schema version 2 > shows the verified claims and the typed evidence references instead of "None cited."`
- Evidence: A research question becomes a pinned job over point-in-time XBRL fact spans; cited spans, quotes and numbers are verified before a proposal is recorded in the core.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Evidence: Proposals are research_proposal v2: a claim is evidence only when every span it cites yields a typed reference from store provenance (BR-DAT-12). The terminal's Research jobs workspace shows the claims and references.
- Gap: filing and document text is not ingested: spans are XBRL facts only
- Gap: no generated query code (AC3)
- Gap: no evaluation set or citation metrics (AC1)
- Gap: no live research job has run on this Mac: the model is not approved in config/models/approved.json and no key is installed
- Requirements: AI-04, DAT-05

### CAP-AGT-12: Model/provider registry with version pinning, measured routing, fallback and change control (implemented)

- Files: `backend/src/unknown/agents/catalog.py`, `config/models/approved.json`, `backend/src/unknown/agents/runner.py`
- Tests: `test_agents_catalog::CatalogTest.test_new_ids_are_unreviewed_until_approved`; `test_agents_catalog::CatalogTest.test_aliases_cannot_be_pinned`; `test_agents_runner::RunnerTest.test_unapproved_or_unverified_candidates_are_not_routed`; `test_agents_runner::RunnerTest.test_transient_error_falls_back_after_retry_budget`
- Evidence: Model ids are discovered at runtime and routable only after an owner approval per role; aliases cannot be pinned; a transient error falls back to the next approved candidate after the retry budget.
- Gap: no evaluation records, so routing is by owner order, not measured quality (AC4)
- Gap: no routing log with scores (AC3)
- Gap: approved.json ships empty
- Requirements: AI-02

### CAP-IBKR-01: TWS API SDK distribution, licensing and TWS/IB Gateway version pinning (tested)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `scripts/setup/ibkr_venv.sh`, `docs/integrations/ibkr.md`, `docs/adr/0014-ibkr-adapter-boundary-and-order-identity.md`
- Tests: `test_ibkr_adapter::ImportBoundaryTests.test_package_import_never_imports_ibapi`; `test_ibkr_adapter::ImportBoundaryTests.test_constructor_raises_a_clear_error_when_ibapi_is_absent`; `test_ibkr_adapter::ImportBoundaryTests.test_no_ibapi_source_is_vendored_in_the_repository`; `test_ibkr_adapter::SdkConformanceTests.test_sdk_pin_is_enforced` (IBKR venv); `test_ibkr_adapter::SdkConformanceTests.test_every_eclient_method_the_adapter_calls_exists_with_this_signature` (IBKR venv)
- Evidence: The GPL TWS API client is never vendored; it is installed from the official zip with a pinned SHA-256 and the adapter refuses an ibapi whose version or fingerprint differs (ADR-0014).
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Gap: no per-session version attestation event with host build and installer hash (AC2)
- Gap: host build pin and read-only on mismatch not implemented (AC3); error 503 handling not tested (AC4)
- Gap: no weekly upgrade check (AC6)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-02, LAW-01

### CAP-IBKR-02: TWS/IB Gateway socket connection, paper vs live ports and account-identity isolation (tested)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/config.py`
- Tests: `test_ibkr_adapter::ConfigTests.test_documented_live_ports_are_refused`; `test_ibkr_adapter::ConfigTests.test_port_must_match_paper_profile_unless_attested`; `test_ibkr_adapter::ConnectionTests.test_handshake_verifies_paper_identity_and_sends_only_the_clock_query` (IBKR venv); `test_ibkr_adapter::ConnectionTests.test_unexpected_managed_accounts_latch_disconnected_unsafe` (IBKR venv); `test_ibkr_adapter::ConnectionTests.test_missing_next_valid_id_reports_auth_required_and_blocks_requests` (IBKR venv); `test_ibkr_adapter::ConnectionTests.test_host_down_maps_502` (IBKR venv); `test_ibkr_adapter::ConnectionTests.test_late_handshake_with_wrong_account_latches_unsafe` (IBKR venv)
- Evidence: Only PAPER profiles are accepted; documented live ports are refused; the managed-account list at handshake must equal the configured paper id, otherwise DISCONNECTED_UNSAFE is latched and never retried.
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Gap: no persisted session record with server_version and connect time (AC5)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-02, EXE-01

### CAP-IBKR-03: Order identifiers and ownership: nextValidId, orderId, permId, clientId, binding and cancel scope (tested)

- Files: `backend/src/unknown/broker/ibkr/ids.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/mapping.py`
- Tests: `test_ibkr_adapter::OrderIdStoreTests.test_allocation_is_persisted_before_return_and_survives_restart`; `test_ibkr_adapter::OrderIdStoreTests.test_prepare_transmit_marks_before_send_and_replaces_stale_unsent_ids`; `test_ibkr_adapter::OrderIdTests.test_restart_after_crash_never_reuses_an_id_and_never_resends` (IBKR venv); `test_ibkr_adapter::OrderIdTests.test_duplicate_order_id_103_rejects_and_resyncs_before_next_entry` (IBKR venv); `test_ibkr_adapter::OpenOrderAndLookupTests.test_open_orders_report_ours_and_foreign` (IBKR venv); `test_ibkr_adapter::OpenOrderAndLookupTests.test_lookup_completed_orders_by_perm_id` (IBKR venv); `test_ibkr_adapter::MappingTests.test_foreign_orders_never_share_our_order_id_namespace`
- Evidence: Order ids are max(nextValidId, persisted max + 1), fsynced before placeOrder; error 103 rejects and resyncs; open and completed orders are matched by permId; client id 0 is refused.
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Gap: whether orderRef is echoed for individual IBKR Pro accounts is unverified (paper check pending)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-02, ORD-03

### CAP-IBKR-04: Normalizing orderStatus, openOrder and order errors into intent, transmission and economic states (tested)

- Files: `backend/src/unknown/broker/ibkr/mapping.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/core/handlers/broker.py`, `backend/src/unknown/core/order_machine.py`
- Tests: `test_ibkr_adapter::MappingTests.test_status_table`; `test_ibkr_adapter::StatusEventTests.test_identical_status_messages_produce_one_event` (IBKR venv); `test_ibkr_adapter::StatusEventTests.test_warn_inactive_and_unknown_statuses` (IBKR venv); `test_ibkr_adapter::SubmitTests.test_error_201_then_cancelled_is_rejected` (IBKR venv); `test_ibkr_adapter::SubmitTests.test_error_202_price_check_is_rejected` (IBKR venv); `test_orders_lifecycle::CancelTests.test_fill_during_cancel_pending_consumes_its_share_of_the_hold`; `test_core_lifecycle_transitions::InactiveOrderTests.test_inactive_buy_keeps_its_reservation_blocks_entries_and_is_canceled`; `test_core_lifecycle_transitions::InactiveOrderTests.test_reconciliation_lookup_inactive_holds_until_the_broker_reports_a_terminal_state`; `test_core_lifecycle_review::TerminalOrderHeldAtBrokerTests.test_a_closed_order_the_broker_lists_as_inactive_is_flagged_and_canceled`
- Evidence: A tested status table maps documented IBKR statuses; identical messages are de-duplicated; 201/202 followed by Cancelled is REJECTED with the reason; executions after PendingCancel are booked.
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Evidence: INACTIVE is no longer a terminal broker status: the core holds an Inactive order as UNKNOWN with every reservation, opens the unknown-order case and cancels a buy; a locally terminal order that a reconciliation finds Inactive is a blocking anomaly (ADR-0009 amendment).
- Gap: the real IBKR sequences that produce Inactive are not observed (docs/integrations/ibkr.md section 5, item 4)
- Gap: behaviour with precaution dialogs not bypassed is an open question
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-02, ORD-07

### CAP-IBKR-05: Execution and commission/fee capture with execId correction handling and bounded history (tested)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/mapping.py`, `backend/src/unknown/core/handlers/broker.py`
- Tests: `test_ibkr_adapter::ExecutionTests.test_live_execution_and_commission_rows` (IBKR venv); `test_ibkr_adapter::ExecutionTests.test_commission_before_execution_is_held_and_joined` (IBKR venv); `test_ibkr_adapter::ExecutionTests.test_corrections_link_to_the_latest_prior_revision` (IBKR venv); `test_ibkr_adapter::ExecutionTests.test_manual_and_foreign_executions_are_external` (IBKR venv); `test_ibkr_adapter::ExecutionTests.test_history_gap_across_midnight_requires_owner_acknowledgement` (IBKR venv); `test_ibkr_adapter::MappingTests.test_unset_values_map_to_none_never_zero`; `test_broker_admin::CoreRecordTests.test_history_gap_attestation_requires_a_fresh_reconciliation`
- Evidence: execDetails and commissionReport are captured with execId revision handling, commission-before-execution joining and a HISTORY_GAP when downtime crosses the host's midnight; the owner attests a gap through broker_admin_action.
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Gap: whether a corrected execution gets its own commission report is unverified; lastNDays coverage unverified
- Gap: statement backfill (Flex) not implemented
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-02, ORD-06, LED-07

### CAP-IBKR-06: Account values, positions and portfolio snapshots for reconciliation (tested)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/reconcile/reconciler.py`, `backend/src/unknown/core/handlers/recon.py`, `backend/src/unknown/core/ledger.py`
- Tests: `test_ibkr_adapter::SnapshotTests.test_complete_snapshot_maps_settled_and_total_cash` (IBKR venv); `test_ibkr_adapter::SnapshotTests.test_incomplete_cases` (IBKR venv); `test_ibkr_adapter::SnapshotTests.test_never_a_third_account_summary_subscription` (IBKR venv); `test_ibkr_adapter::SnapshotTests.test_account_type_mismatch_is_incomplete` (IBKR venv); `test_reconciliation::ComparisonTests.test_position_difference_is_detected_and_blocks`
- Evidence: Account snapshots map SettledCash and TotalCashValue with completeness flags; buying power is never used; position differences block entries until resolved.
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Gap: the real AccountType string and paper SettledCash behaviour are unverified
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-02, REC-01

### CAP-IBKR-07: Connectivity loss and restore handling (1100/1101/1102/1300/2110, farm codes, socket EOF) and host-side order resubmission (tested)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/service/core.py`
- Tests: `test_ibkr_adapter::ConnectivityTests.test_system_codes_map_to_connectivity_events` (IBKR venv); `test_ibkr_adapter::ConnectivityTests.test_connection_closed_then_reconnect` (IBKR venv); `test_ibkr_adapter::ConnectivityTests.test_socket_port_reset_1300_drops_the_session` (IBKR venv); `test_ibkr_adapter::SubmitTests.test_upstream_loss_blocks_new_entries_until_restored` (IBKR venv); `test_ibkr_marketdata::QuoteTests.test_connectivity_loss_blanks_quotes_and_1101_requires_resubscribe`; `test_control_recovery::ConnectivityTests.test_disconnect_event_forces_reconciliation_before_entries`
- Evidence: 1100/1101/1102/1300/2110 and socket EOF map to connectivity events; entries stay blocked until a reconciliation; market data is blanked and resubscribed after 1101.
- Evidence: All 179 tests of test_ibkr_adapter, test_ibkr_marketdata, test_ibkr_serve and test_service_supervisor pass in the IBKR venv (official ibapi client, version string 1051.1, i.e. TWS API 10.51.01; fake host, no network) on 2026-10-22; 65 of those tests are skipped in interpreters without ibapi.
- Gap: no MAINTENANCE label for published reset windows (AC5)
- Gap: host-side resubmission is an owner setting (recommended off) recorded only as documentation
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-04, OPS-03

### CAP-IBKR-08: Broker-aware request pacing, market-data line budget and historical-data limits (tested)

- Files: `backend/src/unknown/broker/ibkr/pacing.py`, `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/broker/ibkr/history.py`
- Tests: `test_ibkr_adapter::PacingTests.test_never_more_than_r_max_in_any_one_second_window`; `test_ibkr_adapter::PacingTests.test_reserved_share_keeps_cancels_flowing_when_entries_saturate`; `test_ibkr_adapter::PacingTests.test_error_100_penalty_halts_non_critical_classes_only`; `test_ibkr_marketdata::HistoricalPacerTests.test_the_61st_weighted_request_in_ten_minutes_is_refused`; `test_ibkr_marketdata::HistoricalPacerTests.test_ten_minute_soak_against_a_mock_enforcing_ibkr_rules`; `test_ibkr_marketdata::QuoteTests.test_line_budget_refuses_locally_before_error_101`; `test_ibkr_marketdata::ConfigTests.test_split_host_budget_never_exceeds_r_max`
- Evidence: A sliding one-second window of floor(lines/2) shared by every request class with a reserved cancel share; local historical limits (15 s identical, 60 weighted per 10 min, 50 open); line budget refuses before error 101.
- Gap: whether placeOrder, cancelOrder and what-if count equally toward lines/2 is unverified
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-03

### CAP-IBKR-09: Daily auto-restart, weekly re-authentication and 2FA constraints for unattended paper operation (implemented)

- Files: `backend/src/unknown/broker/ibkr/serve.py`, `backend/src/unknown/service/core.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `docs/deploy/macos.md`
- Tests: `test_service_supervisor::SupervisorTests.test_logged_out_host_is_not_reconnected_while_the_socket_is_up`; `test_service_supervisor::SupervisorTests.test_identity_unsafe_latch_is_never_retried`; `test_ibkr_adapter::ConnectionTests.test_weekly_login_failure_message_reports_auth_required` (IBKR venv); `test_control_recovery::RestartRecoveryTests.test_stop_survives_restart_and_restart_sets_recovery_hold`
- Evidence: The service reconnects with capped backoff but never logs in for the owner; a weekly re-login condition reports AUTH_REQUIRED; the stop latch survives restarts. Login automation (IBC) is excluded by policy (threat model T15).
- Gap: no owner notification or reminders for AUTH_REQUIRED (alerting not built, CAP-OPS-06)
- Gap: daily auto-restart has not been exercised against a real IB Gateway (AC1)
- Gap: no deny-list dependency check (AC4)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-04, OPS-04

### CAP-IBKR-11: Cash vs margin account semantics and enforcement of long-only, no-short, no-margin behaviour (tested)

- Files: `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/policy/model.py`
- Tests: `test_orders_lifecycle::ReservationTests.test_sell_reserves_owned_quantity_and_unowned_sell_is_rejected`; `test_ibkr_adapter::SnapshotTests.test_account_type_mismatch_is_incomplete` (IBKR venv); `test_ibkr_adapter::StatusEventTests.test_locate_hold_blocks_entries` (IBKR venv); `test_ibkr_adapter::SubmitTests.test_short_sale_signal_blocks_entries_until_owner_clears` (IBKR venv); `test_policy_mandate::PolicyParsingTests.test_unreleased_capabilities_are_refused`
- Evidence: Sells need owned unreserved quantity; the adapter builds stock contracts only; a locate hold or short-sale signal blocks entries; an AccountType different from the configured one makes snapshots incomplete.
- Gap: no explicit refusal test for secType SBL or exchange PREBORROW (AC2): only STK contracts are built
- Gap: the real cash-account AccountType string is unknown until the paper account exists
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: RSK-01, BRK-02

### CAP-IBKR-12: Market data on paper accounts: sharing from live, API acknowledgements, delayed-data labelling and rights (tested)

- Files: `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/market/quotes.py`, `backend/src/unknown/risk/pretrade.py`
- Tests: `test_ibkr_marketdata::QuoteTests.test_type_is_taken_from_the_provider_not_the_request`; `test_ibkr_marketdata::QuoteTests.test_every_market_data_type_maps_to_one_status_and_only_live_is_live`; `test_ibkr_marketdata::QuoteTests.test_non_live_quotes_fail_the_real_pretrade_check`; `test_ibkr_marketdata::QuoteTests.test_terminal_entitlement_frees_its_line_on_maintain_and_10197_can_resume`; `test_ibkr_marketdata::BarStoreTests.test_fixture_rows_are_excluded_by_default_and_storage_needs_rights`
- Evidence: Quotes carry the delivered market-data type; delayed or frozen quotes fail pretrade; 10197 is a specific state; storage needs registry rights. Owner account facts (API acknowledgement signed, non-consolidated free feed) are in STATUS.md.
- Gap: no registry entry exists for IBKR market data, so bars are not stored and no market data can reach a model
- Gap: no IBKR session has delivered a quote yet
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: DAT-01, DAT-06

### CAP-IBKR-13: Paper-trading simulation fidelity limits and evidence-level labelling (implemented)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/research/promotion.py`
- Tests: `test_ibkr_adapter::ExecutionTests.test_live_execution_and_commission_rows` (IBKR venv); `test_research_promotion::LifecycleTest.test_missing_evidence_refuses`
- Evidence: Every IBKR raw record carries evidence_level BROKER_PAPER; promotion to eligible needs PAPER_RECONCILIATION evidence.
- Gap: no local rejection of paper-unsupported order types (AC2); no CA_NOT_PROCESSED flag (AC3); paper equity resets not modelled (AC4)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: RES-05

### CAP-DATA-01: Provider and entitlement registry with enforced usage rights (tested)

- Files: `backend/src/unknown/data/registry.py`, `config/providers/`, `backend/src/unknown/agents/gate.py`, `backend/src/unknown/broker/ibkr/history.py`
- Tests: `test_data_sec::RegistryTests.test_sec_registered_with_rights_and_limit`; `test_sources_treasury::TreasuryTests.test_registry_entry`; `test_sources_openfigi::OpenFigiTests.test_registry_entry`; `test_agents_gate::GateTest.test_dataset_without_model_processing_rights_is_blocked`; `test_ibkr_marketdata::BarStoreTests.test_registry_entry_granting_storage_opens_the_gate`; `test_data_sec::FetcherTests.test_token_bucket_paces`
- Evidence: Versioned provider entries under config/providers (sec-edgar, sec-edgar-reference, us-treasury, ecb, openfigi, owner-research-input) with per-dataset rights; the agent data-use gate and the IBKR bar store consult the registry.
- Gap: no registry entry for IBKR market data, so historical bars are returned but not stored
- Gap: no rights_version on ingested rows and no terms-hash monitor (AC6, AC7)
- Gap: no deletion workflow on termination (AC8)
- Gap: the SEC limiter is per process, not shared across processes (AC5)
- Gap: FRED not registered (needs an owner key)
- Requirements: DAT-01

### CAP-DATA-02: SEC EDGAR filings, XBRL company facts and bulk files under fair-access rules (tested)

- Files: `backend/src/unknown/data/sec.py`, `backend/src/unknown/data/http.py`, `backend/src/unknown/data/pit.py`, `backend/src/unknown/data/fundamentals/filings.py`
- Tests: `test_data_sec::SecIngestTests.test_ingest_point_in_time`; `test_data_sec::SecIngestTests.test_acceptance_time_parsing`; `test_data_sec::SecIngestTests.test_reingest_is_idempotent_and_ticker_not_backdated`; `test_data_sec::FetcherTests.test_user_agent_required`; `test_pit_store::PointInTimeTests.test_restatement_never_leaks_backwards`
- Evidence: live run 2026-10-22 against data.sec.gov (owner-declared User-Agent): Apple 25,135 and Microsoft 32,711 observations; 0 false corrections, 0 same-instant conflicts, 100% acceptance-time coverage via additional submissions files
- Gap: bulk files (FSDS, daily index) and filing documents not ingested (AC3, AC6)
- Gap: no nightly sync or staleness status
- Gap: rate limit is per process, not a global limiter (AC2)
- Requirements: DAT-01, DAT-03

### CAP-DATA-04: Instrument identifier mapping with OpenFIGI (ISIN/CUSIP/SEDOL/ticker to FIGI) (tested)

- Files: `backend/src/unknown/data/sources/openfigi.py`, `backend/src/unknown/data/sources/identifiers.py`, `config/providers/`
- Tests: `test_sources_openfigi::OpenFigiTests.test_batching_and_sliding_window_without_key`; `test_sources_openfigi::OpenFigiTests.test_key_raises_limits_and_is_sent_only_as_header`; `test_sources_openfigi::OpenFigiTests.test_429_waits_retry_after_and_413_splits`; `test_sources_openfigi::OpenFigiTests.test_ingest_stores_every_outcome_and_never_auto_selects`
- Evidence: Live smoke of one ISIN ran once on 2026-10-22 (test_sources_live, opt-in).
- Gap: no 400-ISIN coverage report per market (AC3)
- Gap: historical joins by identifier validity rely on receipt-time snapshots only (AC4)
- Requirements: DAT-01, DOM-03

### CAP-DATA-06: US Treasury yield curves and Fiscal Data datasets (implemented)

- Files: `backend/src/unknown/data/sources/treasury.py`, `backend/src/unknown/data/sources/common.py`
- Tests: `test_sources_treasury::TreasuryTests.test_parse_exact_values_missing_tenor_and_display_field`; `test_sources_treasury::TreasuryTests.test_live_capture_and_availability`; `test_sources_treasury::TreasuryTests.test_refetch_unchanged_and_revision_never_leaks`; `test_sources_treasury::TreasuryTests.test_capture_rule_bounds`
- Evidence: Daily par yield curve XML parsed exactly; capture-time availability with a flagged end-of-day bound for backfills; revisions are later publications. Live smoke of the current month ran once (test_sources_live, opt-in).
- Gap: real yields, bill rates and Fiscal Data datasets not ingested (AC4)
- Gap: no multi-year backfill row-count check (AC1)
- Gap: no discount-factor derivation (AC3)
- Gap: reuse terms of the Treasury feed not reviewed
- Requirements: DAT-01, DAT-02

### CAP-DATA-07: Euro-area and EU statistics: ECB Data Portal (with vintages) and Eurostat (implemented)

- Files: `backend/src/unknown/data/sources/ecb.py`, `backend/src/unknown/data/sources/common.py`
- Tests: `test_sources_ecb::EcbTests.test_url_and_registry`; `test_sources_ecb::EcbTests.test_exact_values_and_availability`; `test_sources_ecb::EcbTests.test_rates_as_of_previous_fixing_and_normalize`
- Evidence: ECB euro reference rates (EXR) only; live smoke of the last observation ran once (test_sources_live, opt-in).
- Gap: only the EXR dataflow: no other ECB series, no updatedAfter delta loads or vintages (AC1, AC2)
- Gap: Eurostat not built (AC3, AC5)
- Requirements: DAT-01, DAT-02

### CAP-DATA-10: IBKR market data entitlements, lines, costs and API compliance (tested)

- Files: `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/api/routes/marketdata.py`, `backend/src/unknown/market/quotes.py`
- Tests: `test_ibkr_marketdata::QuoteTests.test_live_quote_is_labelled_non_consolidated_and_feeds_the_book`; `test_ibkr_marketdata::ConfigTests.test_line_budget_headroom_and_refusal`; `test_ibkr_marketdata::QuoteTests.test_line_budget_refuses_locally_before_error_101`; `test_ibkr_marketdata::QuoteTests.test_entitlement_errors_are_specific_unavailable_states`
- Evidence: The owner's US feed (Cboe One + IEX, fee waived) is labelled non-consolidated; the line allocator refuses beyond the budget; entitlement errors are specific states.
- Gap: no preflight check of PRO status, API acknowledgement or subscriptions (AC1)
- Gap: no cost panel (AC5)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: DAT-01, LAW-01

### CAP-MODEL-01: Runtime model catalog discovery and capability registry (tested)

- Files: `backend/src/unknown/agents/catalog.py`, `backend/src/unknown/agents/providers/anthropic.py`, `backend/src/unknown/agents/providers/openai.py`, `backend/src/unknown/agents/providers/xai.py`, `backend/src/unknown/agents/providers/gemini.py`
- Tests: `test_agents_catalog::CatalogTest.test_shipped_approvals_file_is_empty`; `test_agents_catalog::CatalogTest.test_approved_id_missing_becomes_non_routable_with_alert`; `test_agents_catalog::CatalogTest.test_failed_refresh_keeps_last_good_and_auth_failure_blocks`; `test_agents_static::StaticTest.test_no_model_id_literals_in_application_source`; `test_agents_providers::OpenAIDocumentedShapesTest.test_model_object`; `test_agents_providers::XAIDocumentedShapesTest.test_language_model_prices_are_stored_unchanged_and_converted_only_for_cross_check`; `test_agents_providers::AnthropicAdapterTest.test_count_tokens_and_list_models`
- Evidence: Runtime catalog snapshots with diff, approval, routability and alerts; no model id literals in application source.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: Mistral not implemented
- Gap: OpenAI, xAI and Gemini adapters build verified fragments only and are disabled
- Gap: no UI for capability flags (AC7)
- Requirements: AI-02

### CAP-MODEL-02: Schema-constrained structured output with local validation (tested)

- Files: `backend/src/unknown/agents/schema.py`, `backend/src/unknown/agents/runner.py`, `config/models/schemas/research_answer.v1.json`
- Tests: `test_agents_schema::CompatibilityTest.test_rejections_without_network`; `test_agents_schema::CompatibilityTest.test_best_effort_and_not_enforced_move_to_local_validation`; `test_agents_schema::ValidatorTest.test_unsupported_keywords_fail_closed`; `test_agents_runner::RunnerTest.test_schema_invalid_output_abstains`; `test_agents_runner::RunnerTest.test_refusal_becomes_typed_abstention`; `test_agents_runner::RunnerTest.test_truncation_retries_once_with_more_output`
- Evidence: Every output is validated locally against the full schema; per-provider compatibility checks refuse unsupported schemas without network; refusals become typed abstentions.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: no conformance corpus or measured schema_conformance_rate threshold (AC3)
- Gap: no licensed-enum check (AC7)
- Requirements: AI-01, AI-04

### CAP-MODEL-03: Read-only tool calling with allowlists and per-provider tool_choice mapping (tested)

- Files: `backend/src/unknown/agents/tools.py`, `backend/src/unknown/agents/runner.py`, `backend/src/unknown/agents/providers/anthropic.py`
- Tests: `test_agents_evidence::ToolGatewayTest.test_non_allowlisted_tool_is_refused_and_never_executed`; `test_agents_evidence::ToolGatewayTest.test_registry_refuses_write_capable_tools`; `test_agents_evidence::ToolGatewayTest.test_tool_call_budget`; `test_agents_runner::RunnerTest.test_allowed_tool_runs_through_the_gateway`
- Evidence: Read-only tools through the default-deny gateway; the Anthropic adapter never sends tool_choice (it is a forbidden parameter).
- Gap: provider tool_choice mappings and tool-count limits for OpenAI, xAI and Mistral not built (AC3-AC5)
- Requirements: AI-01

### CAP-MODEL-04: Token counting preflight and context-fit check (tested)

- Files: `backend/src/unknown/agents/runner.py`, `backend/src/unknown/agents/providers/base.py`, `backend/src/unknown/agents/providers/anthropic.py`, `backend/src/unknown/agents/gate.py`
- Tests: `test_agents_providers::AnthropicAdapterTest.test_count_tokens_and_list_models`; `test_agents_providers::RetryAndBreakerTest.test_local_estimate_is_conservative`; `test_agents_runner::RunnerTest.test_restricted_dataset_is_never_sent`; `test_agents_providers::XAIDocumentedShapesTest.test_tokenize_shapes_and_usage_locations`
- Evidence: Each turn counts tokens (provider endpoint, else a conservative local estimate) after a data-use gate decision and before reserving and calling.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: no est_error calibration report per provider/model (AC4, AC5)
- Requirements: AI-03, AI-05

### CAP-MODEL-05: Effective-dated tariff registry, cost accounting and budget enforcement (tested)

- Files: `backend/src/unknown/agents/tariffs.py`, `backend/src/unknown/agents/budget.py`, `config/models/tariffs.json`
- Tests: `test_agents_tariffs::TariffMathTest.test_book_equation_19_1`; `test_agents_tariffs::TariffMathTest.test_effective_dating`; `test_agents_tariffs::ShippedTariffFileTest.test_grok_200k_all_tokens_rule`; `test_agents_tariffs::ShippedTariffFileTest.test_openai_long_context_is_refused`; `test_agents_tariffs::ShippedTariffFileTest.test_only_the_owner_authorized_row_is_approved`; `test_agents_static::StaticTest.test_no_price_literals_in_application_source`
- Evidence: Effective-dated tariff rows as decimal strings; exact cost and worst case; only the owner-authorized claude-opus-5-5 row is approved.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: no pricing-page hash monitor that sets rows to needs_review (AC7)
- Requirements: AI-05

### CAP-MODEL-06: Rate-limit headers, error taxonomy, retry and circuit-breaker policy (tested)

- Files: `backend/src/unknown/agents/providers/base.py`, `backend/src/unknown/agents/runner.py`, `backend/src/unknown/agents/providers/anthropic.py`
- Tests: `test_agents_providers::RetryAndBreakerTest.test_retry_policy`; `test_agents_providers::RetryAndBreakerTest.test_no_retry_after_deadline_property`; `test_agents_providers::RetryAndBreakerTest.test_breaker`; `test_agents_providers::AnthropicAdapterTest.test_error_mapping`; `test_agents_runner::RunnerTest.test_quota_and_auth_never_fall_back`; `test_agents_runner::RunnerTest.test_transient_error_retries_after_retry_after`
- Evidence: One error taxonomy, an owner-configured retry policy applied by the runner (adapters never retry), retry-after honoured, no retry after the deadline, and a circuit breaker.
- Gap: streaming (SSE) errors not handled: calls are not streamed (AC5)
- Requirements: AI-03

### CAP-MODEL-08: Prompt caching with stable prefixes and normalized cache accounting (implemented)

- Files: `backend/src/unknown/agents/providers/anthropic.py`, `backend/src/unknown/agents/tariffs.py`
- Tests: `test_agents_providers::AnthropicAdapterTest.test_usage_normalization`; `test_agents_tariffs::ShippedTariffFileTest.test_haiku_band_counts_cache_reads`
- Evidence: Cache usage is normalized (total input = cache read + cache creation + input) and priced; the Anthropic adapter deliberately sends no cache_control, so no cache writes are billed.
- Gap: prompt caching itself is not used: no stable-prefix templates, breakpoints or cache report (AC1, AC2, AC5)
- Requirements: AI-03

### CAP-MODEL-09: Provider data-usage, retention and training-policy gate (tested)

- Files: `backend/src/unknown/agents/gate.py`, `backend/src/unknown/agents/config.py`, `config/models/provider_policies.json`
- Tests: `test_agents_gate::GateTest.test_unknown_facts_block`; `test_agents_gate::GateTest.test_training_and_residency`; `test_agents_gate::GateTest.test_other_providers`; `test_agents_gate::GateTest.test_policy_version_changes_with_owner_policy`; `test_agents_gate::GateTest.test_allows_public_dataset_and_records_decision`
- Evidence: Provider policy records (retention, training, residency, freshness) gate every payload; unknown facts block.
- Gap: Gemini unpaid-project and batch-endpoint rules are data only: those adapters are disabled
- Requirements: AI-04, DAT-01

### CAP-MODEL-10: Inference region and data-residency routing for US, Europe and India (tested)

- Files: `backend/src/unknown/agents/gate.py`, `backend/src/unknown/agents/providers/anthropic.py`, `backend/src/unknown/agents/providers/xai.py`
- Tests: `test_agents_runner::RunnerTest.test_model_id_and_geo_mismatch_are_quarantined`; `test_agents_providers::XAIDocumentedShapesTest.test_us_endpoint_rejects_other_models_locally`; `test_agents_gate::GateTest.test_training_and_residency`; `test_agents_providers::AnthropicAdapterTest.test_request_shape`
- Evidence: inference_geo is explicit on every Anthropic request and a reported geo that differs is quarantined; residency rules are part of the gate.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Gap: no EU-only routing to an OpenAI project (AC1): other providers disabled
- Gap: regional uplift not applied in cost records (AC5)
- Requirements: AI-02, LAW-01

### CAP-MODEL-11: Model version pinning, lifecycle tracking and per-model parameter compatibility (tested)

- Files: `backend/src/unknown/agents/catalog.py`, `backend/src/unknown/agents/providers/anthropic.py`
- Tests: `test_agents_catalog::CatalogTest.test_aliases_cannot_be_pinned`; `test_agents_catalog::CatalogTest.test_staleness_lifecycle_retirement_and_capabilities`; `test_agents_catalog::CatalogTest.test_fingerprint_change_alerts`; `test_agents_providers::AnthropicAdapterTest.test_effort_must_be_explicit`
- Evidence: Aliases cannot be pinned; lifecycle and retirement are tracked; fingerprint changes alert; per-model forbidden parameters are refused locally.
- Gap: no regression-evaluation artifact required to replace a pin (AC4)
- Requirements: AI-02

### CAP-MODEL-12: Evidence-cited document Q&A over permitted filings (terminal AI-assistant functional parity) (tested)

- Files: `backend/src/unknown/agents/evidence.py`, `backend/src/unknown/agents/pit_evidence.py`, `backend/src/unknown/agents/runner.py`, `backend/src/unknown/agents/gate.py`, `terminal/src/workspaces/research-jobs/JobDetailView.tsx`
- Tests: `test_agents_evidence::ClaimVerificationTest.test_fabricated_span_and_misquote_are_unsupported`; `test_agents_evidence::ClaimVerificationTest.test_numeric_claims_against_typed_facts`; `test_agents_evidence::ClaimVerificationTest.test_claim_without_quote_is_uncertain`; `test_agents_runner::RunnerTest.test_prompt_injection_document_has_zero_effect`; `test_agents_runner::RunnerTest.test_restricted_dataset_is_never_sent`; `src/workspaces/research-jobs/render.test.ts::proposal panel > fences model text off as not advice and labels the recorded proposal advisory only`; `src/workspaces/research-jobs/render.test.ts::proposal panel, schema version 2 > shows the verified claims and the typed evidence references instead of "None cited."`
- Evidence: Verified claims need existing spans with verbatim quotes; numeric claims are checked exactly against typed XBRL facts; unverified claims are labelled and never become evidence.
- Evidence: One live call of claude-opus-5-5 ran in CI (GitHub Actions run 39798075556, workflow live-anthropic, 2026-10-22, commit 7eae251): tests.test_agents_live_anthropic passed; token count by the provider endpoint (1,306 prompt tokens); reserved and settled USD 0.00822400.
- Evidence: Terminal: the Research jobs workspace shows the cited claims and their typed evidence references.
- Gap: no frozen evaluation set or per-provider citation metrics (AC6)
- Gap: filing text is not ingested: spans are XBRL facts
- Requirements: AI-04, DAT-05

### CAP-LAW-03: US cash-account settled-funds and freeriding gate (Regulation T 220.8; Investor.gov cash-account bulletin) (tested)

- Files: `backend/src/unknown/core/ledger.py`, `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/core/handlers/recon.py`, `backend/src/unknown/research/engine.py`
- Tests: `test_reconciliation::SettlementEvidenceTests.test_sale_proceeds_become_available_only_on_broker_evidence`; `test_sessions::SuccessorFundingTest.test_request_not_funded_from_unsettled_receivables_until_settlement_confirms_them`; `test_analytics_whatif::DryRunSemanticsTest.test_unsettled_sale_proceeds_do_not_fund_a_buy`; `test_research_engine::CashAccountTest.test_unsettled_sale_proceeds_cannot_fund_a_buy_until_t_plus_1`; `test_review_fixes::SettlementAndFeeTests.test_f25_sell_whose_fees_exceed_its_proceeds_never_overstates_available_cash`
- Evidence: Buys are funded only from settled cash: available cash = CASH + PAYABLE + FEES_ACCRUED - holds - net payments due on sales; receivables are never spendable and settlement is confirmed only by broker evidence. This is stricter than Regulation T: unsettled proceeds cannot fund any buy, so a freeride cannot start.
- Gap: no explicit freeriding rule or reason code, and no freeze-mode handling (the stricter settled-cash rule makes them moot today)
- Gap: no 10,000-sequence property test with holidays (AC4)
- Gap: legal applicability not reviewed (LAW-01)
- Requirements: LED-03, RSK-01, LAW-01

### CAP-LAW-04: Long-only enforcement and Regulation SHO short-sale gate (tested)

- Files: `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/core/handlers/orders.py`, `backend/src/unknown/policy/model.py`, `backend/src/unknown/broker/ibkr/adapter.py`
- Tests: `test_orders_lifecycle::ReservationTests.test_sell_reserves_owned_quantity_and_unowned_sell_is_rejected`; `test_review_fixes::SystemLatchTests.test_f36_locally_terminal_sell_still_working_blocks_a_second_sale`; `test_policy_mandate::PolicyParsingTests.test_unreleased_capabilities_are_refused`; `test_simulated_broker::SimulatedBrokerTests.test_short_sale_and_overspend_rejected`; `test_research_engine::CashAccountTest.test_selling_more_than_owned_is_rejected`; `test_ibkr_adapter::SubmitTests.test_short_sale_signal_blocks_entries_until_owner_clears` (IBKR venv)
- Evidence: A sell needs owned, unreserved session quantity (OWNED_QUANTITY) and reserves it; position-related open cases block further sells; an IBKR short-sale signal latches an entry block that only the owner clears.
- Gap: no 'shorting disabled' reason string; the rejection code is OWNED_QUANTITY
- Gap: no property test over random cancel/replace races (AC4)
- Gap: Regulation SHO applicability not reviewed (LAW-01)
- Requirements: RSK-01

### CAP-LAW-05: Owner-side pre-trade risk controls benchmarked to SEC Rule 15c3-5, NSE algo risk-control table and MiFID II Art. 17 (tested)

- Files: `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/policy/model.py`, `backend/src/unknown/core/handlers/control.py`, `backend/src/unknown/core/handlers/orders.py`
- Tests: `test_policy_mandate::PolicyParsingTests.test_every_missing_field_is_rejected_never_defaulted`; `test_policy_mandate::PolicyParsingTests.test_out_of_range_values_are_rejected`; `test_orders_lifecycle::PretradeLimitTests.test_price_collar_bounds_buy_and_sell_limits`; `test_orders_lifecycle::PretradeLimitTests.test_stale_missing_or_delayed_quote_rejects`; `test_control_recovery::LossBudgetTests.test_daily_loss_breach_latches_stop`; `test_control_recovery::RestartRecoveryTests.test_stop_survives_restart_and_restart_sets_recovery_hold`; `test_agents_static::StaticTest.test_agents_never_import_execution_or_ledger_code`
- Evidence: Owner-side controls: strict policy limits (missing or out-of-range values refused), collar, freshness, notional, rate, loss budgets and a durable stop latch; every order passes the single pretrade gate.
- Gap: no kill switch on unacknowledged order bursts (AC3); the order-rate limit is per minute only
- Gap: no 'no bypass' integration test that routes an adapter call around the gate (AC4)
- Gap: NSE and MiFID II benchmarks not mapped (India and EU are not eligible)
- Gap: legal benchmarking not reviewed (LAW-01)
- Requirements: RSK-02, RSK-03, LAW-01

### CAP-LAW-10: Market-abuse conduct controls: self-cross prevention, order-intent logging, layering/spoofing pattern review (EU MAR; NSE 73992; book chapter 25) (implemented)

- Files: `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/core/handlers/orders.py`, `backend/src/unknown/core/handlers/strategy.py`
- Tests: `test_strategy_decisions::StrategyDecisionTests.test_no_trade_decision_is_recorded_without_economic_effect`; `test_desk_commands::RejectionRecordTests.test_rejected_command_is_stored_with_a_command_rejected_event`; `test_core_lifecycle_executions::SelfCrossTests.test_buy_that_would_trade_against_our_working_sell_is_refused_self_cross`; `test_core_lifecycle_executions::SelfCrossTests.test_sell_that_would_trade_against_our_working_buy_is_refused_self_cross`
- Evidence: Pretrade refuses an order whose limit would cross one of the account's own working opposite-side orders (SELF_CROSS); every intent, accepted or rejected, and every strategy decision is logged in the hash-chained event log.
- Gap: no order-to-trade or fast-cancel ratios, no layering/spoofing review, no review gate on green start (AC2, AC3)
- Gap: self-cross is checked within one account only (one account trades today)
- Requirements: RSK-02, ORD-08

### CAP-LAW-11: IBKR API market-data entitlement, subscriber status and usage-rights gate (tested)

- Files: `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/market/quotes.py`, `backend/src/unknown/risk/pretrade.py`, `backend/src/unknown/data/registry.py`
- Tests: `test_ibkr_marketdata::QuoteTests.test_entitlement_errors_are_specific_unavailable_states`; `test_ibkr_marketdata::QuoteTests.test_non_live_quotes_fail_the_real_pretrade_check`; `test_ibkr_marketdata::ConfigTests.test_consolidation_claims_need_evidence_and_known_values`; `test_ibkr_marketdata::BarStoreTests.test_fixture_rows_are_excluded_by_default_and_storage_needs_rights`; `test_quote_consolidation::QuoteConsolidationTests.test_non_consolidated_quote_validates_paper_but_not_live`
- Evidence: Entitlement errors become specific unavailable states; non-live quotes fail the real pretrade gate; LIVE requires consolidated quotes; bar storage needs a registry entry granting it. The owner's subscriber status (shown as Non-Commercial Professional) is recorded in STATUS.md, not in a dated attestation record.
- Gap: no dated subscriber-status attestation record (AC4); no non-display use flag per consumer
- Gap: promotion does not check API entitlement per instrument (AC1)
- Gap: no output scan for raw IBKR quote fields (AC3)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: LAW-01, DAT-01

### CAP-LAW-12: IBKR account-terms gate for unattended automated trading (Client Agreement Form 3203; Relationship Summary Form 4024; API user certification) (implemented)

- Files: `backend/src/unknown/core/handlers/admin.py`, `backend/src/unknown/broker/ibkr/config.py`, `scripts/hooks/pre-commit`, `.github/workflows/ci.yml`
- Tests: `test_review_fixes::AuthorityTests.test_f22_paper_account_ownership_needs_certificate_or_attestation`; `test_ibkr_adapter::ConfigTests.test_config_file_must_live_outside_git`
- Evidence: Outside SIMULATED, execution ownership needs a release certificate or an owner attestation; the IBKR config (with the account id) must live outside any git work tree; CI and a pre-commit hook block account-id and key patterns.
- Gap: no broker-terms record, no Form 3203 change monitor, no withdrawal-permission check, no automation gate (AC1-AC3)
- Gap: LIVE automation is refused outright, so the gate is not needed before the live release gate (GATE-01)
- Requirements: LAW-01, GATE-01, EXE-01

### CAP-OPS-01: Mac process supervision with launchd (restart drill, crash-loop handling, sleep and power settings) (implemented)

- Files: `scripts/deploy/macos/render_launchd.py`, `docs/deploy/macos.md`, `backend/src/unknown/service/core.py`
- Tests: `test_control_recovery::RestartRecoveryTests.test_stop_survives_restart_and_restart_sets_recovery_hold`; `test_durability_review::ServiceHeartbeatTest.test_failing_cycles_keep_the_lease_and_a_takeover_stops_the_old_core`
- Evidence: render_launchd.py writes com.unknown.core/api/backup agents (KeepAlive SuccessfulExit=false, ThrottleInterval) with plistlib; it does not load them. A restart keeps the stop latch and requires reconciliation (tested in the core).
- Gap: the renderer has no test and no plist has been loaded on this Mac; no SIGKILL/crash-loop drill (AC1, AC3, AC4)
- Gap: no DEGRADED state after repeated restarts; no energy-settings snapshot (AC6)
- Requirements: OPS-01, OPS-04

### CAP-OPS-02: SQLite ledger durability settings, version advisory gate and crash/power-loss tests (macOS F_FULLFSYNC, Linux EBS) (implemented)

- Files: `backend/src/unknown/store/db.py`, `backend/src/unknown/store/schema.py`, `backend/src/unknown/gateway/journal.py`
- Tests: `test_desk_commands::WriterLeaseTests.test_second_writer_is_refused_while_the_lease_is_live`; `test_desk_commands::IdempotencyTests.test_command_identity_survives_restart`; `test_ops_backup::BackupReviewTests.test_corrupt_backup_fails_integrity_even_without_sidecar`; `test_gateway_journal::DiskFullTest.test_disk_full_on_append_fails_closed_and_recovers_in_order`; `test_gateway_journal::DiskFullTest.test_entries_and_marks_are_append_only`; `test_gateway_journal::CrashWindowTest.test_crash_after_apply_before_mark_is_a_duplicate_for_each_event_kind`
- Evidence: Every core connection sets WAL, synchronous=FULL and fullfsync on macOS, with foreign keys on; amount columns are decimal TEXT.
- Evidence: The broker-event journal is opened through the same store.db.connect settings, with append-only triggers and a hash chain; a simulated SQLITE_FULL on append fails closed (stop latched, reconciliation required) and recovers in order. These are simulated failures, not a real full disk or power loss.
- Gap: no test reads back the PRAGMA values at open (AC1)
- Gap: SQLite version advisory gate not automated (AC2)
- Gap: no SIGKILL or power-loss campaign (AC4, AC5); network filesystems are not refused (AC6)
- Gap: commit latency measured only by the core benchmark, not as release evidence (AC7)
- Requirements: SEC-03, PRF-01

### CAP-OPS-03: Consistent encrypted ledger backups and an isolated restore drill with post-backup broker reconciliation (tested)

- Files: `backend/src/unknown/ops/backup.py`, `backend/src/unknown/cli.py`, `backend/src/unknown/core/desk.py`, `docs/runbooks/README.md`
- Tests: `test_ops_backup::BackupTests.test_backup_while_the_core_runs_then_drill_proves_it_restores`; `test_ops_backup::BackupTests.test_tampered_backup_fails_the_drill`; `test_ops_backup::BackupReviewTests.test_marker_matches_the_snapshot`; `test_ops_backup::BackupReviewTests.test_restore_refuses_while_the_core_holds_the_lease`; `test_ops_backup::BackupReviewTests.test_restore_moves_wal_and_shm_aside_and_the_next_start_latches`; `test_control_recovery::RestoreDetectionTests.test_restore_of_backup_taken_in_current_process_is_detected`; `test_review_fixes::RestoreDetectionTests.test_f20_backup_copied_to_another_directory_without_its_epoch_file`
- Evidence: `unknown.cli backup`, `restore-drill` and `restore`: online SQLite backup from a read-only connection, a sidecar with epoch, last sequence, integrity check and SHA-256, and a drill that verifies the hash chain and replay in a temporary directory. A restored database latches the stop and marks in-flight orders UNKNOWN.
- Gap: backups are not encrypted and not copied off-host (AC3)
- Gap: no recorded scheduled drill with RPO/RTO and no broker reconciliation step in the drill (AC5, AC6)
- Requirements: SEC-03, OPS-04

### CAP-OPS-04: IB Gateway supervision: pinned install, daily auto-restart, weekly manual reauthentication and connectivity-state handling (implemented)

- Files: `backend/src/unknown/broker/ibkr/adapter.py`, `backend/src/unknown/broker/ibkr/marketdata.py`, `backend/src/unknown/broker/ibkr/serve.py`, `backend/src/unknown/core/handlers/broker.py`, `docs/deploy/macos.md`
- Tests: `test_ibkr_adapter::ConnectivityTests.test_system_codes_map_to_connectivity_events` (IBKR venv); `test_ibkr_adapter::ConnectivityTests.test_connection_closed_then_reconnect` (IBKR venv); `test_ibkr_adapter::ConnectivityTests.test_socket_port_reset_1300_drops_the_session` (IBKR venv); `test_ibkr_adapter::SubmitTests.test_upstream_loss_blocks_new_entries_until_restored` (IBKR venv); `test_ibkr_adapter::ConnectionTests.test_client_id_in_use_and_identity_change_latch` (IBKR venv); `test_ibkr_marketdata::QuoteTests.test_connectivity_loss_blanks_quotes_and_1101_requires_resubscribe`; `test_service_supervisor::SupervisorTests.test_reconnects_with_capped_exponential_backoff`; `test_service_supervisor::SupervisorTests.test_logged_out_host_is_not_reconnected_while_the_socket_is_up`; `test_control_recovery::ConnectivityTests.test_disconnect_event_forces_reconciliation_before_entries`
- Evidence: The connectivity-state part is built and tested offline with a fake host (the same behaviour as CAP-IBKR-07): 1100 marks the session down and refuses transmission until 1101 or 1102 restores it; after 1101 (data lost) the market-data session blanks quotes and resubscribes each subscription on its next maintenance pass; socket EOF and 1300 drop the session and requests are refused until a reconnect; client id in use reports AUTH_REQUIRED.
- Evidence: The IBKR service (python -m unknown.broker.ibkr.serve) reconnects with capped exponential backoff and never logs in for the owner; a disconnect makes the core require a clean reconciliation before any entry or transmission. The owner's manual login, weekly re-login and Gateway settings are documented in docs/deploy/macos.md.
- Gap: IB Gateway itself is not supervised: no pinned Gateway install, no launchd job for it, no daily auto-restart check against the reset windows, and no Gateway version or settings hash recorded per launch (AC5, AC6)
- Gap: in-flight orders are not set to UNKNOWN on a disconnect: they keep their state, and entries wait for a clean reconciliation (AC1)
- Gap: no reauthentication due time is shown or alerted (AC3); AUTH_REQUIRED is reported, but alerting is not built (CAP-OPS-06)
- Gap: no reset-window configuration, so a 1100 inside an IBKR reset window is not classified as expected
- Gap: API port exposure and the gateway versus Client Portal username check are not implemented (AC4, AC7)
- Gap: not paper-verified: no IBKR paper session has run (BRK-02)
- Requirements: BRK-04, OPS-04

### CAP-OPS-05: Layered health and readiness model (alive, writable, fresh, authenticated, eligible) with explicit inactivity reasons (tested)

- Files: `backend/src/unknown/core/handlers/control.py`, `backend/src/unknown/core/desk.py`, `backend/src/unknown/service/core.py`, `terminal/src/workspaces/operations/model.ts`
- Tests: `test_control_recovery::GreenStartPreflightTests.test_failed_preflight_lists_every_blocker_by_name_and_keeps_latch`; `test_control_recovery::GreenStartPreflightTests.test_preflight_reports_broker_disconnected`; `test_control_recovery::GreenStartPreflightTests.test_preflight_reports_authentication_required`; `test_control_recovery::GreenStartPreflightTests.test_preflight_reports_stale_market_data_for_held_positions`; `test_review_fixes::AuthorityTests.test_f41_preflight_fails_closed_without_broker_server_time`; `src/workspaces/operations/model.test.ts::healthChecks > flags skew beyond the policy limit, low disk and repeated cycle failures`; `test_core_lifecycle_stop::MemoryLatchTests.test_stop_with_an_unwritable_database_latches_in_memory_cancels_buys_and_is_recorded_later`; `test_gateway_journal::ServiceWiringTest.test_service_replays_the_journal_before_new_events`
- Evidence: A health snapshot (connected, authenticated, server time, disk, cycle errors) feeds named preflight blockers; entries need a clean reconciliation; the terminal shows each check.
- Evidence: Health also reports the memory stop latch and the broker-event journal (unapplied entries, last failure); preflight names STOP_LATCHED_IN_MEMORY while a stop is latched only in memory.
- Gap: no explicit layered states (alive, writable, fresh, authenticated, eligible) with one primary reason (AC1)
- Gap: no reset-window configuration or MARKET_CLOSED label (AC3-AC5); alert layer missing (CAP-OPS-06)
- Requirements: OPS-03, RSK-04

### CAP-OPS-10: Secrets management: Mac Keychain and AWS Secrets Manager, broker credentials never stored, paper/live separation, rotation (implemented)

- Files: `backend/src/unknown/service/paths.py`, `backend/src/unknown/service/ipc.py`, `backend/src/unknown/agents/config.py`, `backend/src/unknown/broker/ibkr/config.py`, `scripts/hooks/pre-commit`, `.github/workflows/ci.yml`, `docs/security/threat-model.md`
- Tests: `test_agents_budget::OwnerPolicyTest.test_loads_private_file_and_refuses_unsafe_modes`; `test_agents_budget::OwnerPolicyTest.test_api_key_resolution`; `test_ibkr_adapter::ConfigTests.test_config_file_must_live_outside_git`; `test_agents_static::StaticTest.test_only_the_worker_reaches_the_core_socket_and_it_never_holds_the_owner_token`
- Evidence: Secrets live under <UNKNOWN_HOME>/secrets with mode 0600 (group- or world-readable token files are refused); the IBKR login password is never stored (the owner logs in to IB Gateway); a CI identifier guard and pre-commit hook scan for account ids and key prefixes. Threat model T1, T12, T15.
- Gap: no Keychain or AWS Secrets Manager integration; every process runs as the same OS user, so isolation is by file mode only (book finding BR-OPS-04)
- Gap: no rotation procedure or record (AC4)
- Gap: no negative OS-level test that a research worker cannot read broker-adjacent secrets (AC2)
- Requirements: SEC-01, SEC-02

### CAP-OPS-14: Instance recovery and single-writer fencing: automatic EC2 recovery, manual ownership transfer, competing-session detection (tested)

- Files: `backend/src/unknown/store/db.py`, `backend/src/unknown/core/handlers/admin.py`, `backend/src/unknown/core/handlers/broker.py`, `backend/src/unknown/service/core.py`
- Tests: `test_control_recovery::ExecutionOwnershipTests.test_second_writer_cannot_open_live_database_and_stale_writer_is_fenced`; `test_control_recovery::ExecutionOwnershipTests.test_second_deployment_cannot_acquire_execution_ownership_while_held`; `test_control_recovery::ExecutionOwnershipTests.test_transmitted_orders_carry_deployment_epoch_tag`; `test_control_recovery::ForeignActivityTests.test_foreign_open_order_tagged_by_another_deployment_latches_stop`; `test_desk_commands::WriterLeaseTests.test_takeover_fences_the_previous_writer`; `test_durability_review::ServiceHeartbeatTest.test_failing_cycles_keep_the_lease_and_a_takeover_stops_the_old_core`
- Evidence: Writer lease with fencing epoch; execution ownership acquire/release with release certificates (ADR-0011); foreign orders and executions latch the stop.
- Gap: automatic EC2 recovery not configured (no AWS resources provisioned)
- Gap: no manual transfer drill recorded (AC3)
- Gap: competing IBKR login detection not tested against a real host (AC4)
- Requirements: EXE-01
