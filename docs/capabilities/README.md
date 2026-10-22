# Capability register

Machine-readable source: `register.json` (import tool: `scripts/capabilities/build_register.py`).
Reference products: Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro and IBKR, scoped only by what their
public documentation and product pages show (URLs and quotes per capability). Status vocabulary follows ADR-0001;
nothing is `paper-verified` or `live-verified` yet.

Parity is tracked in six separate dimensions per capability (functional, data coverage, data quality, latency,
usability, operational reliability). Proprietary datasets, interfaces and closed-network functions are not copied;
each capability records lawful alternatives and what cannot be replicated.

## Summary by area

| Area | Capabilities | proposed | specified | implemented | tested | paper-verified | live-verified |
|---|---|---|---|---|---|---|---|
| instruments | 14 | 12 | 0 | 2 | 0 | 0 | 0 |
| market-data | 13 | 13 | 0 | 0 | 0 | 0 | 0 |
| company-research | 14 | 13 | 0 | 0 | 1 | 0 | 0 |
| news-documents | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| macro-rates-fx | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| portfolio-risk | 13 | 13 | 0 | 0 | 0 | 0 | 0 |
| multiasset | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| strategy-research | 14 | 13 | 0 | 0 | 1 | 0 | 0 |
| execution-oms | 14 | 7 | 0 | 7 | 0 | 0 | 0 |
| workspace-ux | 12 | 12 | 0 | 0 | 0 | 0 | 0 |
| agent-operations | 14 | 12 | 0 | 2 | 0 | 0 | 0 |
| ibkr-integration | 14 | 14 | 0 | 0 | 0 | 0 | 0 |
| data-providers | 14 | 12 | 0 | 1 | 1 | 0 | 0 |
| model-providers | 12 | 12 | 0 | 0 | 0 | 0 | 0 |
| regulatory | 13 | 13 | 0 | 0 | 0 | 0 | 0 |
| ops-aws | 14 | 10 | 0 | 4 | 0 | 0 | 0 |

## Capabilities


### instruments

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-INS-01 | P1 | Security search and instrument discovery | Bloomberg Terminal, IBKR TWS API, LSEG Workspace / LSEG Data Library, OpenFIGI ( | proposed | not yet implemented |
| CAP-INS-02 | P0 | Identifier cross-reference and FIGI mapping | IBKR TWS API, LSEG Data Platform / Workspace, LSEG DataScope Select, OpenFIGI (B | proposed | not yet implemented |
| CAP-INS-03 | P0 | Licensed identifier rights enforcement (CUSIP, SEDOL, RIC) | Bloomberg / S&P Capital IQ (library comparison), CUSIP Global Services (CGS) ter | proposed | not yet implemented |
| CAP-INS-04 | P0 | Broker contract resolution and verification (IBKR conid, Indian broker instrument tokens) | IBKR TWS API, Zerodha Kite Connect (India broker reference) | proposed | not yet implemented |
| CAP-INS-05 | P1 | India listing master (NSE symbol/series, BSE code, ISIN, symbol changes) | NSE (exchange), NSE Data & Analytics, Zerodha Kite Connect | proposed | not yet implemented |
| CAP-INS-06 | P0 | Point-in-time instrument master and symbology history (ticker changes, reuse, name changes) | Bloomberg Terminal, Bloomberg enterprise data (reference data catalog page), LSE | implemented | no symbology source ingested yet (OpenFIGI/SEC tickers lack history) |
| CAP-INS-07 | P0 | Exchange trading calendars, sessions, half days and ad hoc closures (US, India, Europe) | Deutsche Börse Xetra / Börse Frankfurt (exchange), Euronext (exchange), IBKR TWS | implemented | no other venues |
| CAP-INS-08 | P0 | Settlement and clearing business-day calendars (T+n by market and currency) | Deutsche Börse Xetra / Börse Frankfurt (exchange), ECB T2 (central bank), Federa | proposed | not yet implemented |
| CAP-INS-09 | P0 | Splits, reverse splits, bonus issues, face-value splits and price/volume adjustment factors | Bloomberg Terminal, IBKR TWS API, LSEG Corporate Actions (data product), LSEG Wo | proposed | not yet implemented |
| CAP-INS-10 | P0 | Cash dividends: declaration, ex, record and pay dates, amounts, receivables and total-return series | Bloomberg Terminal, IBKR TWS API, LSEG Corporate Actions (data product), LSEG Wo | proposed | not yet implemented |
| CAP-INS-11 | P1 | Mergers, acquisitions, spin-offs/demergers, rights issues, tender offers and buybacks | Bloomberg Terminal, Bloomberg enterprise data (reference data catalog page), LSE | proposed | not yet implemented |
| CAP-INS-12 | P1 | Delistings, suspensions, deficiency flags and survivorship-free universes | Nasdaq (exchange), SEC (regulator) | proposed | not yet implemented |
| CAP-INS-13 | P2 | Forward corporate-event calendar and alerts (ex-dates, splits, reorganizations, earnings dates) | Bloomberg Terminal, IBKR TWS API, LSEG Workspace / Data Library, S&P Capital IQ  | proposed | not yet implemented |
| CAP-INS-14 | P2 | Venue (ISO 10383 MIC) and issuer (LEI, PermID) reference linkage | Bloomberg enterprise data (reference data catalog page), ESMA FIRDS (regulator), | proposed | not yet implemented |

### market-data

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-MKT-01 | P0 | Level 1 quotes with explicit real-time / delayed / frozen / snapshot status, source and age | Bloomberg B-PIPE, IBKR (all APIs), IBKR TWS API, IBKR Web API, LSEG Workspace, L | proposed | not yet implemented |
| CAP-MKT-02 | P1 | Consolidated (NBBO / tape) versus single-venue and derived best quotes across US, Europe and India | Bloomberg B-PIPE, IBKR Web API, LSEG Workspace, US SIP (CTA Plan) | proposed | not yet implemented |
| CAP-MKT-03 | P1 | Trades and time & sales (live tick-by-tick and historical) with conditions, corrections and halts | Bloomberg B-PIPE, IBKR TWS, IBKR TWS API, LSEG Workspace | proposed | not yet implemented |
| CAP-MKT-04 | P0 | Intraday bars (seconds to hours), live-updating and historical, with explicit completeness | IBKR TWS API, IBKR Web API, LSEG Data Library (Workspace) | proposed | not yet implemented |
| CAP-MKT-05 | P0 | Daily/weekly/monthly bars with raw and versioned split/dividend-adjusted series | IBKR TWS API, LSEG Data Library (Workspace) | proposed | not yet implemented |
| CAP-MKT-06 | P2 | Market depth (Level 2) by price and by order, where entitled | Bloomberg B-PIPE, IBKR TWS, IBKR TWS API, LSEG Workspace / Data Library | proposed | not yet implemented |
| CAP-MKT-07 | P1 | Price charts with technical studies, comparisons and annotations | Bloomberg Terminal, IBKR TWS API, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-MKT-08 | P0 | Watchlists / monitors with per-cell status, as-of dates and market-data line budgeting | Bloomberg Terminal, IBKR (all platforms), IBKR Web API, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-MKT-09 | P1 | Alerts on price, volume, time, study values, news and filings | Bloomberg Terminal, IBKR Web API, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-MKT-10 | P1 | Equity screening with point-in-time criteria, reproducible as-of runs and real-time scans | Bloomberg Terminal, Bloomberg Terminal (via MATLAB Datafeed Toolbox), IBKR TWS A | proposed | not yet implemented |
| CAP-MKT-11 | P0 | Market-data entitlement, professional status, display vs non-display use and fee registry | Bloomberg B-PIPE, IBKR (all APIs), IBKR (all platforms) | proposed | not yet implemented |
| CAP-MKT-12 | P1 | India equity market data (NSE and BSE): quotes, depth, candles, instruments and licensing | BSE India (via Deutsche Börse licensing), Zerodha Kite Connect, Zerodha Kite Con | proposed | not yet implemented |
| CAP-MKT-13 | P2 | European equity market data: per-venue quotes, delayed public data and the EU consolidated tape | Bloomberg B-PIPE, EU consolidated tape (EuroCTP), LSEG Workspace | proposed | not yet implemented |

### company-research

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-CO-01 | P0 | Issuer, filer and listing identity resolution for company research | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-CO-02 | P0 | US SEC filings feed, archive and full-text search | Bloomberg Terminal, LSEG Workspace / LSEG fundamentals, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-CO-03 | P1 | India and Europe company filings ingestion (ESEF, UKSEF, NSE/BSE, MCA) under explicit rights gates | IBKR TWS Fundamentals Explorer, LSEG Workspace / LSEG fundamentals | proposed | not yet implemented |
| CAP-CO-04 | P1 | As-reported financial statements from XBRL (income, balance sheet, cash flow, segments) | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace / LSEG fundam | proposed | not yet implemented |
| CAP-CO-05 | P1 | Standardized line items, currency/unit normalization, calendarization and TTM | Bloomberg Terminal, LSEG Workspace / LSEG fundamentals, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-CO-06 | P1 | Point-in-time fundamentals, amendment and restatement history | Bloomberg (Enterprise Data), LSEG Workspace / LSEG fundamentals, S&P Capital IQ  | tested | live SEC fetch not yet run (needs owner User-Agent in ~/.unknown/config/data.jso |
| CAP-CO-07 | P1 | Valuation multiples, financial ratios and documented enterprise-value bridge | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace / LSEG fundam | proposed | not yet implemented |
| CAP-CO-08 | P1 | Analyst consensus estimates, revisions, actuals and earnings surprise (licensed) | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace (I/B/E/S), S& | proposed | not yet implemented |
| CAP-CO-09 | P1 | Ownership: institutional (13F), insider (Forms 3/4/5), major holders (13D/13G) and non-US shareholding disclosures | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, LSEG Workspace, S&P Capital  | proposed | not yet implemented |
| CAP-CO-10 | P1 | Peer group construction and trading comparables (current and point-in-time) | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-CO-11 | P1 | DCF and WACC valuation with sensitivity surface and valuation range summary | Bloomberg Terminal, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-CO-12 | P1 | Company tearsheet / overview page with sourced key facts | Bloomberg Terminal, IBKR TWS Fundamentals Explorer, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-CO-13 | P2 | Spreadsheet export with live formulas and a programmatic data function (Excel plug-in parity) | Bloomberg Terminal, LSEG Data Library for Python, LSEG Workspace, S&P Capital IQ | proposed | not yet implemented |
| CAP-CO-14 | P2 | Filing and document question answering with verifiable citations | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |

### news-documents

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-NEWS-01 | P0 | Rights registry and cloud-model processing gate for news, transcripts and documents | IBKR (TWS API), LSEG Workspace | proposed | not yet implemented |
| CAP-NEWS-02 | P0 | Real-time SEC EDGAR filing stream with 8-K item typing | LSEG (Intelligent Tagging NLP service; the fact sheet names Eikon and Reuters Ne | proposed | not yet implemented |
| CAP-NEWS-03 | P1 | India, UK and EU regulatory disclosure ingestion | LSEG Workspace, S&P Capital IQ (Key Developments dataset; the page names 'S&P Ca | proposed | not yet implemented |
| CAP-NEWS-04 | P1 | Licensed real-time newswire ingestion with history backfill | Bloomberg Terminal, IBKR (TWS API), LSEG (Machine Readable News feed; Workspace  | proposed | not yet implemented |
| CAP-NEWS-05 | P1 | Syndication deduplication, story clustering and revision tracking | LSEG Workspace, S&P Capital IQ (Key Developments dataset; Capital IQ Pro not nam | proposed | not yet implemented |
| CAP-NEWS-06 | P0 | Issuer and instrument entity tagging with point-in-time identifier resolution | LSEG (Intelligent Tagging NLP service; the fact sheet names Eikon and Reuters Ne | proposed | not yet implemented |
| CAP-NEWS-07 | P1 | Earnings and investor call transcripts with speaker turns, versions and live streaming | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-NEWS-08 | P1 | Full-text search across filings, announcements, transcripts and news | Bloomberg Terminal, LSEG (news products page; Workspace not named on page), S&P  | proposed | not yet implemented |
| CAP-NEWS-09 | P1 | Typed corporate event detection from filings, announcements and news | LSEG (Intelligent Tagging NLP service; the fact sheet names Eikon and Reuters Ne | proposed | not yet implemented |
| CAP-NEWS-10 | P1 | Scheduled corporate event calendar (earnings dates, calls, board meetings) | IBKR (TWS API), LSEG Workspace | proposed | not yet implemented |
| CAP-NEWS-11 | P2 | Sentiment scoring for news, filings and transcripts | LSEG (MarketPsych Analytics data feeds; Workspace not named on page), LSEG (Star | proposed | not yet implemented |
| CAP-NEWS-12 | P1 | Grounded summaries and multi-document Q&A with span-level citations | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-NEWS-13 | P1 | Alerts on filings, announcements, news and events for watchlists | IBKR (TWS API), LSEG Workspace | proposed | not yet implemented |
| CAP-NEWS-14 | P2 | Event studies and news-impact analytics with point-in-time availability | LSEG (StarMine model fact sheet; Workspace not named in document), S&P Capital I | proposed | not yet implemented |

### macro-rates-fx

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-MAC-01 | P1 | Economic release calendar (scheduled releases for the US, euro area/EU and India) | Bloomberg (enterprise data catalogue), Bloomberg Terminal, Bloomberg point-in-ti | proposed | not yet implemented |
| CAP-MAC-02 | P1 | Release-time capture of actuals, revisions to prior and first prints | Bloomberg (EcoNext delivery for ECO data), IBKR (Econoday on Interactive Brokers | proposed | not yet implemented |
| CAP-MAC-03 | P1 | Consensus forecasts, point-in-time consensus snapshots and surprise metrics | Bloomberg Terminal, Bloomberg point-in-time economic dataset (trade press), IBKR | proposed | not yet implemented |
| CAP-MAC-04 | P1 | Macro series vintages and point-in-time (as-of) queries | Bloomberg point-in-time economic dataset (trade press), LSEG economic data, LSEG | proposed | not yet implemented |
| CAP-MAC-05 | P1 | Macro time-series catalog, retrieval, transformations and country-indicator matrix | Bloomberg Terminal, Bloomberg Terminal (economics), LSEG economic data, S&P Capi | proposed | not yet implemented |
| CAP-MAC-06 | P1 | Central bank meeting calendar, decisions and policy-rate history (Fed, ECB, RBI) | Bloomberg (enterprise data catalogue, ECO), IBKR (Econoday) | proposed | not yet implemented |
| CAP-MAC-07 | P1 | US Treasury par/real yield curves, H.15, fitted zero curve and overnight reference rates | IBKR TWS API, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Universit | proposed | not yet implemented |
| CAP-MAC-08 | P1 | Euro area government yield curves (ECB), EUR short-term rate and UK gilt/OIS curves (BoE) | Bloomberg MAC3 (risk model), LSEG/Refinitiv Datastream (2022 fact sheet hosted b | proposed | not yet implemented |
| CAP-MAC-09 | P1 | India policy rates, money-market benchmarks and government securities curve | LSEG Reuters Polls, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Uni | proposed | not yet implemented |
| CAP-MAC-10 | P1 | Yield-curve analytics: interpolation, bootstrapping, forwards, spreads, curve history and factor decomposition | Bloomberg MAC3, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Univers | proposed | not yet implemented |
| CAP-MAC-11 | P2 | Market-implied policy-rate path and meeting probabilities, with published-proxy fallback | Bloomberg Terminal, LSEG Reuters Polls | proposed | not yet implemented |
| CAP-MAC-12 | P0 | Official FX reference rates (ECB, Fed H.10, FBIL/RBI) for valuation and reporting | Bloomberg MAC3, LSEG/Refinitiv Datastream (2022 fact sheet hosted by the Univers | proposed | not yet implemented |
| CAP-MAC-13 | P1 | Tradable FX quotes, cross-rate matrix and currency attribution of foreign equity returns | IBKR TWS API, IBKR Trader Workstation, LSEG/Refinitiv Datastream (2022 fact shee | proposed | not yet implemented |
| CAP-MAC-14 | P1 | Cross-asset and cross-market correlation, rolling and shrinkage estimates, and macro-factor betas | Bloomberg MAC3, IBKR Trader Workstation, LSEG/Refinitiv Datastream (2022 fact sh | proposed | not yet implemented |

### portfolio-risk

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-PORT-01 | P0 | Consolidated holdings, per-currency cash ladder and NAV across brokers and accounts | Bloomberg PORT, IBKR PortfolioAnalyst, IBKR TWS Risk Navigator, S&P Capital IQ P | proposed | not yet implemented |
| CAP-PORT-02 | P0 | Daily, realized and unrealized P&L with tax-lot cost basis, fee finalization and FX translation | IBKR TWS API, IBKR TWS Risk Navigator, Zerodha Kite Connect | proposed | not yet implemented |
| CAP-PORT-03 | P0 | Exposure, concentration and fund look-through by sector, country, currency and beta | Bloomberg PORT, IBKR PortfolioAnalyst, IBKR TWS Risk Navigator, S&P Capital IQ P | proposed | not yet implemented |
| CAP-PORT-04 | P0 | Time-weighted and money-weighted return measurement with GIPS-consistent cash-flow handling | GIPS 2020 (CFA Institute), IBKR PortfolioAnalyst | proposed | not yet implemented |
| CAP-PORT-05 | P1 | Ex-post risk and risk-adjusted performance statistics (Sharpe, Sortino, IR, TE, beta, alpha, drawdown, Calmar) | Bloomberg PORT, GIPS 2020 (CFA Institute), IBKR PortfolioAnalyst | proposed | not yet implemented |
| CAP-PORT-06 | P1 | Benchmark registry: index levels, total-return proxies, blended benchmarks and constituent data rights | Bloomberg PORT, IBKR PortfolioAnalyst, S&P Capital IQ Portfolio Analytics, S&P C | proposed | not yet implemented |
| CAP-PORT-07 | P1 | Holdings-based Brinson performance attribution (allocation, selection, interaction) with multi-period linking and contribution to return | Bloomberg PORT, Bloomberg PORT Enterprise, IBKR PortfolioAnalyst, LSEG Workspace | proposed | not yet implemented |
| CAP-PORT-08 | P1 | Equity factor risk model and ex-ante risk decomposition (total, active, factor vs specific, marginal contributions) | Bloomberg PORT, LSEG Workspace, S&P Capital IQ Pro Portfolio Analytics | proposed | not yet implemented |
| CAP-PORT-09 | P2 | Returns-based factor and style attribution using public factor libraries | Bloomberg PORT, LSEG Workspace Portfolio Analytics, S&P Capital IQ Pro Portfolio | proposed | not yet implemented |
| CAP-PORT-10 | P1 | Value-at-Risk and Expected Shortfall (historical, parametric, Monte Carlo) with backtesting | Basel Committee (BIS) market risk standard, Bloomberg PORT, IBKR TWS Risk Naviga | proposed | not yet implemented |
| CAP-PORT-11 | P1 | Stress testing and scenario analysis: historical replays, custom shocks, factor-propagated and regulator-published scenarios | Bloomberg PORT, EBA 2025 EU-wide stress test, Federal Reserve supervisory stress | proposed | not yet implemented |
| CAP-PORT-12 | P0 | What-if portfolio sandbox and pre-trade portfolio impact (exposure, cash, VaR/ES, limits, broker margin/commission preview) | IBKR TWS API, IBKR TWS Risk Navigator, S&P Capital IQ Portfolio Analytics | proposed | not yet implemented |
| CAP-PORT-13 | P1 | Constrained portfolio optimisation, target allocations and rebalancing trade lists | Bloomberg PORT, IBKR PortfolioAnalyst, LSEG Workspace, S&P Capital IQ Portfolio  | proposed | not yet implemented |

### multiasset

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-MA-01 | P0 | Research-only eligibility gate for options, bonds, futures, FX, crypto and other non-equity classes | IBKR Options Strategy Builder, IBKR Portfolio Performance Profile, IBKR TWS API, | proposed | not yet implemented |
| CAP-MA-02 | P1 | Option chains and derivative contract master (US, India, Europe) | Bloomberg Terminal, IBKR TWS API, IBKR TWS OptionTrader, LSEG Workspace for equi | proposed | not yet implemented |
| CAP-MA-03 | P1 | Option fair-value models and implied volatility solver with arbitrage bounds | Bloomberg Terminal, IBKR TWS API, IBKR TWS OptionTrader, LSEG Workspace for equi | proposed | not yet implemented |
| CAP-MA-04 | P1 | Option Greeks with explicit unit conventions and position-level aggregation | Bloomberg Terminal, IBKR Options Trading Tools, IBKR TWS API, LSEG Workspace for | proposed | not yet implemented |
| CAP-MA-05 | P1 | Implied volatility smiles, surfaces, term structure and IV-vs-realised history | Academic method (Gatheral and Jacquier, 'Arbitrage-free SVI volatility surfaces' | proposed | not yet implemented |
| CAP-MA-06 | P2 | Multi-leg option strategy builder, payoff and scenario P&L (research-only) | Bloomberg Terminal, IBKR Option Strategy Lab, IBKR Options Strategy Builder, IBK | proposed | not yet implemented |
| CAP-MA-07 | P2 | Market-implied probability distribution and user-view trade evaluation (Probability Lab analog) | IBKR Options Trading Tools, IBKR Probability Lab | proposed | not yet implemented |
| CAP-MA-08 | P1 | Portfolio risk slices, what-if portfolios and custom price/date/volatility scenarios | Bloomberg PORT, IBKR Portfolio Performance Profile, IBKR Risk Navigator, IBKR TW | proposed | not yet implemented |
| CAP-MA-09 | P1 | Government yield curves (par, zero, forward) for US, euro area and India with vintages | ECB euro area yield curves, LSEG Workspace for fixed income, S&P Capital IQ Pro, | proposed | not yet implemented |
| CAP-MA-10 | P2 | Bond calculator: price/yield, accrued interest, duration, convexity, DV01 and cash flows | Bloomberg Terminal, IBKR Bond Search, LSEG Workspace for fixed income, LSEG Yiel | proposed | not yet implemented |
| CAP-MA-11 | P2 | Corporate bond trade prints (TRACE) and credit spread analytics (G-spread, Z-spread, relative value) | Bloomberg FIW, Bloomberg PORT, FINRA Fixed Income Data, LSEG Workspace for fixed | proposed | not yet implemented |
| CAP-MA-12 | P2 | Futures curves, calendar spreads, carry/roll analytics and continuous series (research construction) | Eurex (exchange), IBKR TWS API, LSEG Workspace for fixed income, LSEG option vol | proposed | not yet implemented |
| CAP-MA-13 | P1 | FX spot, cross rates, reference fixings and covered-interest-parity forward analytics | Bloomberg Terminal, IBKR TWS API, LSEG Workspace FX trading | proposed | not yet implemented |
| CAP-MA-14 | P3 | Crypto spot market data and analytics (research-only, no custody or keys) | Coinbase Exchange API, IBKR TWS API, IBKR cryptocurrency trading | proposed | not yet implemented |

### strategy-research

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-RES-01 | P0 | Point-in-time fundamentals, filings and macro vintages for research (bitemporal as-of layer) | Bloomberg Company Financials, Estimates and Pricing Point-in-Time, Bloomberg Com | tested | macro vintages (ALFRED) not ingested |
| CAP-RES-02 | P0 | Survivorship-free universe membership, security master and adjusted price history for backtests | Bloomberg investment research data (PiT), CRSP US Stock Databases (Morningstar), | proposed | not yet implemented |
| CAP-RES-03 | P1 | Research workspace with programmatic data query API, notebooks and scheduled jobs | Bloomberg API (BLPAPI) library, Bloomberg BQuant Desktop / BQuant Enterprise, Ke | proposed | not yet implemented |
| CAP-RES-04 | P0 | Causal event-driven backtest engine with explicit fill model and deterministic replay | Academic method: Lopez de Prado, Advances in Financial Machine Learning (Wiley,  | proposed | not yet implemented |
| CAP-RES-05 | P0 | Transaction cost, market impact and capacity model for backtests and promotion | Academic: Almgren and Chriss, Optimal execution of portfolio transactions, Journ | proposed | not yet implemented |
| CAP-RES-06 | P1 | Cross-sectional factor and signal research (IC, quantile portfolios, neutralization, decay, turnover) | Academic: Harvey, Liu and Zhu, ...and the Cross-Section of Expected Returns (NBE | proposed | not yet implemented |
| CAP-RES-07 | P0 | Walk-forward evaluation with purging, embargo and combinatorial purged cross-validation | Academic: Bailey, Borwein, Lopez de Prado and Zhu, The Probability of Backtest O | proposed | not yet implemented |
| CAP-RES-08 | P0 | Multiple-testing correction and backtest-overfitting diagnostics (Deflated Sharpe, PBO/CSCV, SPA/StepM, haircuts) | Academic: Bailey and Lopez de Prado, The Deflated Sharpe Ratio, Academic: Bailey | proposed | not yet implemented |
| CAP-RES-09 | P0 | Dependence-aware performance statistics and paired candidate-versus-incumbent comparison | Academic: Ledoit and Wolf (2008), Robust performance hypothesis testing with the | proposed | not yet implemented |
| CAP-RES-10 | P0 | Pre-registered experiment and complete trial registry (including failures) | Academic: Bailey and Lopez de Prado, The Deflated Sharpe Ratio, Bloomberg BQuant | proposed | not yet implemented |
| CAP-RES-11 | P0 | Reproducibility manifests and deterministic replay of experiments | Academic: Lyle, Siano and Yohn (2024), on restandardized Compustat data, DVC (op | proposed | not yet implemented |
| CAP-RES-12 | P0 | Strategy artifact registry, status lifecycle and predeclared promotion gates | FCA, Algorithmic Trading Compliance in Wholesale Markets (summary of RTS 6 Artic | proposed | not yet implemented |
| CAP-RES-13 | P0 | Shadow mode and IBKR paper-trading stage with divergence measurement | FCA review (summary of RTS 6), IBKR Paper Trading Account (Client Portal guide), | proposed | not yet implemented |
| CAP-RES-14 | P1 | Ongoing performance and drift monitoring, quarantine and rollback of active artifacts | Academic: Bailey et al., The Probability of Backtest Overfitting, FCA review (su | proposed | not yet implemented |

### execution-oms

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-OMS-01 | P0 | Order ticket: order-type, time-in-force and session capability catalog | Bloomberg EMSX, IBKR TWS API, IBKR Trader Workstation, LSEG REDI on Workspace, Z | proposed | not yet implemented |
| CAP-OMS-02 | P0 | Order lifecycle state machine, parent/child model and order blotter | Bloomberg EMSX, IBKR TWS API, LSEG REDI on Workspace, Zerodha Kite Connect v3 | implemented | parent/child orders not implemented |
| CAP-OMS-03 | P0 | Pre-trade controls: order presets, precautionary limits and rule-based compliance | Bloomberg AIM, IBKR Org Portal, IBKR Pre-Trade Compliance, IBKR TWS API, IBKR Tr | implemented | order presets not implemented |
| CAP-OMS-04 | P0 | IBKR connectivity: TWS/IB Gateway session, authentication, pacing and transport options (US and Europe) | Bloomberg EMSX, IBKR FIX, IBKR TWS API, IBKR Web API, LSEG Autex Trade Route | proposed | not yet implemented |
| CAP-OMS-05 | P2 | India broker adapter (Kite Connect) with SEBI retail-algo controls | SEBI, Zerodha Kite Connect v3 | proposed | not yet implemented |
| CAP-OMS-06 | P0 | Execution (fill) and commission event capture with corrections and history backfill | Bloomberg EMSX, IBKR TWS API, IBKR Web API, Zerodha Kite Connect v3 | implemented | IBKR adapter mapping in progress |
| CAP-OMS-07 | P0 | Broker statements and trade confirmations ingestion (IBKR Flex, activity statements) | IBKR Client Portal, IBKR Flex Web Service, IBKR Web API, IBKR Web API (Account M | proposed | not yet implemented |
| CAP-OMS-08 | P0 | Three-way reconciliation: real-time events, intraday broker snapshot and end-of-day statement vs ledger | Bloomberg AIM, IBKR Flex Queries, IBKR TWS API, LSEG REDI on Workspace, Zerodha  | implemented | end-of-day statement leg (Flex) not implemented: two of three legs only |
| CAP-OMS-09 | P0 | Settlement-date calculation (US T+1, India T+1/T+0, EU T+2 to T+1 on 11 Oct 2027) and settled-cash availability | European Commission, Federal Reserve Board, IBKR TWS API, NSE Clearing, NYSE, Na | implemented | India and EU settlement calendars/rules not implemented |
| CAP-OMS-10 | P1 | Corporate-action processing: mandatory events, voluntary elections and lot/basis adjustments | IBKR Client Portal, IBKR Flex Queries, IBKR Web API, NSE, SEC EDGAR | proposed | not yet implemented |
| CAP-OMS-11 | P0 | Commission, fee and tax-charge estimation with estimated-vs-final tracking | Bloomberg EMSX, IBKR, IBKR TWS API, Zerodha Kite Connect v3 | implemented | fee model values UNVERIFIED pending IBKR schedule confirmation |
| CAP-OMS-12 | P1 | Execution algorithms: broker algos (IBKR) and in-house parent/child scheduling (TWAP, participation) | Bloomberg AIM, Bloomberg EMSX, IBKR Paper Trading, IBKR TWS API, IBKR Web API, L | proposed | not yet implemented |
| CAP-OMS-13 | P1 | Transaction cost analysis (arrival, price improvement, VWAP, markouts, daily range) | Bloomberg BTCA, IBKR Client Portal, IBKR Org Portal | proposed | not yet implemented |
| CAP-OMS-14 | P0 | Simulation, broker-paper and live evidence levels with paper-environment limitations | IBKR Flex Web Service, IBKR Paper Trading, IBKR TWS API | implemented | BROKER_PAPER evidence level awaits the IBKR adapter |

### workspace-ux

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-UX-01 | P0 | Command line / palette for function, instrument, document and help search | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro, W3C WAI-ARIA Authoring P | proposed | not yet implemented |
| CAP-UX-02 | P0 | Keyboard-first operation: scoped, rebindable shortcuts, hint mode, history and safe order hotkeys | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace, LSEG Workspace (acc | proposed | not yet implemented |
| CAP-UX-03 | P1 | Linked panels: numbered/coloured instrument-context channels with source/destination roles | Bloomberg Terminal, Bloomberg Terminal (via HERE Core 'Bloomberg integration v1. | proposed | not yet implemented |
| CAP-UX-04 | P1 | Persistent, versioned multi-panel workspace layouts with templates, lock mode and recovery | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace, S&P Capital IQ (pla | proposed | not yet implemented |
| CAP-UX-05 | P2 | Bookmarks, favourites bar and deep links that restore a panel with instrument and parameters (live or as-of) | Bloomberg Terminal, LSEG Workspace | proposed | not yet implemented |
| CAP-UX-06 | P1 | Accessibility: WCAG 2.2 AA conformance for dense financial screens, with a published conformance report | LSEG Workspace, W3C WAI-ARIA APG, W3C WCAG 2.2 | proposed | not yet implemented |
| CAP-UX-07 | P1 | Export any grid to CSV/XLSX with provenance, exact decimals, injection protection and licence gating; validated re-import | Bloomberg Terminal, IBKR Client Portal, IBKR Trader Workstation, LSEG Workspace, | proposed | not yet implemented |
| CAP-UX-08 | P2 | Optional spreadsheet bridge: custom functions, formula builder and audit from Excel to the private API | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-UX-09 | P0 | 'Show calculation' inspector: formula, inputs, units, adjustments and level-by-level lineage to source records | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-UX-10 | P1 | Data-item dictionary and contextual function help (definitions, formulas, units, sources, coverage, rights) | Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |
| CAP-UX-11 | P0 | Value display semantics: currency, scale, locale grouping, time zones, sign glyphs, staleness and explicit missingness | Bloomberg Terminal, IBKR Trader Workstation, LSEG Workspace (anti-pattern from i | proposed | not yet implemented |
| CAP-UX-12 | P1 | Live panels: pause/freeze, manual refresh scopes, snapshot-on-gap and point-in-time (as-of) viewing | LSEG Workspace, LSEG Workspace (ACR baseline, March 2025), S&P Capital IQ Pro, W | proposed | not yet implemented |

### agent-operations

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-AGT-01 | P0 | Bounded agent jobs: envelope, deadlines, turn/tool/retry caps, no unbounded spawning | Bloomberg Terminal, Claude Agent SDK, LSEG Workspace, OWASP Top 10 for LLM Appli | proposed | not yet implemented |
| CAP-AGT-02 | P0 | Agent cost budgets: pre-call reservation, hierarchical caps, provider hard limits and billing reconciliation | Claude API (Anthropic), Claude Agent SDK, LSEG Workspace, OWASP Top 10 for LLM A | proposed | not yet implemented |
| CAP-AGT-03 | P0 | Per-role tool allowlists and capability manifests enforced outside the model | Bloomberg Terminal, FINRA 2026 Annual Regulatory Oversight Report, Model Context | implemented | per-tool allowlists for model calls not implemented (I6) |
| CAP-AGT-04 | P0 | Human-in-the-loop approval queue binding owner decisions to exact proposals | FINRA 2026 Annual Regulatory Oversight Report, IBKR, Model Context Protocol spec | proposed | not yet implemented |
| CAP-AGT-05 | P0 | Owner intervention controls: pause roles, cancel jobs, disable model-dependent decisions, red stop with order cancel | EU MiFID II RTS 6 (Delegated Regulation 2017/589), UK onshored copy, IBKR TWS AP | proposed | not yet implemented |
| CAP-AGT-06 | P0 | Prompt-injection defence for filings, news, web pages, owner uploads and tool outputs | Bloomberg Terminal, Claude Agent SDK, NIST AI 600-1 Generative AI Profile, OWASP | proposed | not yet implemented |
| CAP-AGT-07 | P0 | Secret and sensitive-data isolation for agents (credentials, account ids, owner data) | IBKR, IBKR TWS API, OWASP Top 10 for LLM Applications 2025, SEBI | proposed | not yet implemented |
| CAP-AGT-08 | P0 | Data-rights gating before content enters a model context (licence, entitlement, retention, provider) | LSEG Workspace, NIST AI RMF 1.0 (AI 100-1), S&P Global / Kensho | proposed | not yet implemented |
| CAP-AGT-09 | P0 | Agent audit trail: tamper-evident lineage from evidence to job to proposal to order | FINRA 2026 Annual Regulatory Oversight Report, Model Context Protocol specificat | implemented | agent job lineage not implemented (I6) |
| CAP-AGT-10 | P1 | Grounded research Q&A over filings and permitted documents with span-level citations and generated query code | Bloomberg Terminal, LSEG Workspace, OWASP Top 10 for LLM Applications 2025, S&P  | proposed | not yet implemented |
| CAP-AGT-11 | P1 | AI summaries of news and earnings events filtered to holdings and watchlists, with provenance | IBKR, LSEG Workspace, NIST AI 600-1 Generative AI Profile, OWASP Top 10 for LLM  | proposed | not yet implemented |
| CAP-AGT-12 | P1 | Model/provider registry with version pinning, measured routing, fallback and change control | Bloomberg Terminal, LSEG Workspace, NIST AI 600-1 Generative AI Profile, NIST AI | proposed | not yet implemented |
| CAP-AGT-13 | P1 | Agent observability: traces, metrics, cost and quality dashboards with service objectives | NIST AI 600-1 Generative AI Profile, NIST AI RMF 1.0 (AI 100-1), OpenAI Agents S | proposed | not yet implemented |
| CAP-AGT-14 | P1 | Agent evaluation suites, red-team tests and release gate for model, prompt, tool and manifest changes | LSEG Workspace, NIST AI 600-1 Generative AI Profile, NIST AI RMF 1.0 (AI 100-1), | proposed | not yet implemented |

### ibkr-integration

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-IBKR-01 | P0 | TWS API SDK distribution, licensing and TWS/IB Gateway version pinning | IBKR IB Gateway, IBKR TWS, IBKR TWS / IB Gateway, IBKR TWS API, PyPI (third-part | proposed | not yet implemented |
| CAP-IBKR-02 | P0 | TWS/IB Gateway socket connection, paper vs live ports and account-identity isolation | IBKR Campus, IBKR IB Gateway, IBKR Paper Trading Account, IBKR TWS API, IBKR TWS | proposed | not yet implemented |
| CAP-IBKR-03 | P0 | Order identifiers and ownership: nextValidId, orderId, permId, clientId, binding and cancel scope | IBKR TWS API | proposed | not yet implemented |
| CAP-IBKR-04 | P0 | Normalizing orderStatus, openOrder and order errors into intent, transmission and economic states | IBKR TWS API | proposed | not yet implemented |
| CAP-IBKR-05 | P0 | Execution and commission/fee capture with execId correction handling and bounded history | IBKR Client Portal, IBKR TWS API, IBKR Web API | proposed | not yet implemented |
| CAP-IBKR-06 | P0 | Account values, positions and portfolio snapshots for reconciliation | IBKR TWS API | proposed | not yet implemented |
| CAP-IBKR-07 | P0 | Connectivity loss and restore handling (1100/1101/1102/1300/2110, farm codes, socket EOF) and host-side order resubmission | IBKR TWS API, IBKR platform | proposed | not yet implemented |
| CAP-IBKR-08 | P0 | Broker-aware request pacing, market-data line budget and historical-data limits | IBKR Market Data, IBKR Orders, IBKR TWS API, IBKR Web API | proposed | not yet implemented |
| CAP-IBKR-09 | P0 | Daily auto-restart, weekly re-authentication and 2FA constraints for unattended paper operation | IBKR Excel/DDE docs, IBKR IB Gateway, IBKR TWS API, IBKR Web API | proposed | not yet implemented |
| CAP-IBKR-10 | P1 | Pre-trade what-if preview for commission and margin impact | IBKR TWS API, IBKR Web API | proposed | not yet implemented |
| CAP-IBKR-11 | P0 | Cash vs margin account semantics and enforcement of long-only, no-short, no-margin behaviour | IBKR Campus glossary, IBKR Market Data / Users, IBKR Paper Trading Account, IBKR | proposed | not yet implemented |
| CAP-IBKR-12 | P0 | Market data on paper accounts: sharing from live, API acknowledgements, delayed-data labelling and rights | IBKR Market Data, IBKR TWS API | proposed | not yet implemented |
| CAP-IBKR-13 | P0 | Paper-trading simulation fidelity limits and evidence-level labelling | IBKR Campus, IBKR Paper Trading Account, IBKR TWS API | proposed | not yet implemented |
| CAP-IBKR-14 | P2 | IBKR Web API (REST + WebSocket) via Client Portal Gateway as a secondary adapter path for individual accounts | IBKR Web API | proposed | not yet implemented |

### data-providers

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-DATA-01 | P0 | Provider and entitlement registry with enforced usage rights | FRED (Federal Reserve Bank of St. Louis), Interactive Brokers, LSEG Workspace, M | implemented | only SEC registered so far |
| CAP-DATA-02 | P0 | SEC EDGAR filings, XBRL company facts and bulk files under fair-access rules | SEC DERA, SEC EDGAR | tested | submissions additional files, bulk files and filing documents not ingested |
| CAP-DATA-03 | P1 | European issuer filings (ESEF iXBRL) via filings.xbrl.org | filings.xbrl.org (XBRL International) | proposed | not yet implemented |
| CAP-DATA-04 | P0 | Instrument identifier mapping with OpenFIGI (ISIN/CUSIP/SEDOL/ticker to FIGI) | OpenFIGI (Bloomberg) | proposed | not yet implemented |
| CAP-DATA-05 | P1 | Macro time series with point-in-time vintages from FRED/ALFRED | FRED API, FRED/ALFRED | proposed | not yet implemented |
| CAP-DATA-06 | P1 | US Treasury yield curves and Fiscal Data datasets | US Treasury, US Treasury Fiscal Data | proposed | not yet implemented |
| CAP-DATA-07 | P1 | Euro-area and EU statistics: ECB Data Portal (with vintages) and Eurostat | ECB, ECB Data Portal, Eurostat | proposed | not yet implemented |
| CAP-DATA-08 | P2 | India macro and monetary statistics from RBI DBIE (manual, rights-limited) | Reserve Bank of India | proposed | not yet implemented |
| CAP-DATA-09 | P0 | India equity market data: NSE/BSE restrictions and licensed paths (NSE Data, Zerodha Kite, IBKR) | Interactive Brokers, NSE Data & Analytics, NSE India, Zerodha Kite Connect | proposed | not yet implemented |
| CAP-DATA-10 | P0 | IBKR market data entitlements, lines, costs and API compliance | Interactive Brokers | proposed | not yet implemented |
| CAP-DATA-11 | P0 | Commercial US/global equity price data vendors (Massive, Tiingo, EODHD, Intrinio, Alpha Vantage) | Alpha Vantage, EOD Historical Data (EODHD), Intrinio, Massive (formerly Polygon. | proposed | not yet implemented |
| CAP-DATA-12 | P1 | Standardized and as-reported fundamentals from commercial vendors (FMP, EODHD, Intrinio, Massive, Tiingo) | EOD Historical Data (EODHD), Financial Modeling Prep (FMP), Intrinio | proposed | not yet implemented |
| CAP-DATA-13 | P1 | Analyst estimates, revisions, ratings and price targets (licensed only) | Benzinga, Financial Modeling Prep (FMP), Intrinio, LSEG Workspace | proposed | not yet implemented |
| CAP-DATA-14 | P1 | Licensed news, press releases and earnings-call transcripts | Benzinga, Intrinio, LSEG Workspace, Tiingo | proposed | not yet implemented |

### model-providers

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-MODEL-01 | P1 | Runtime model catalog discovery and capability registry | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-02 | P1 | Schema-constrained structured output with local validation | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-03 | P1 | Read-only tool calling with allowlists and per-provider tool_choice mapping | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-04 | P1 | Token counting preflight and context-fit check | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-05 | P1 | Effective-dated tariff registry, cost accounting and budget enforcement | Anthropic Claude API, Google Gemini API, Mistral, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-06 | P1 | Rate-limit headers, error taxonomy, retry and circuit-breaker policy | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-07 | P2 | Asynchronous batch processing for research backfills | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-08 | P2 | Prompt caching with stable prefixes and normalized cache accounting | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-09 | P1 | Provider data-usage, retention and training-policy gate | Anthropic Claude API, Google Gemini API, Mistral API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-10 | P1 | Inference region and data-residency routing for US, Europe and India | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-11 | P1 | Model version pinning, lifecycle tracking and per-model parameter compatibility | Anthropic Claude API, Google Gemini API, OpenAI API, xAI API | proposed | not yet implemented |
| CAP-MODEL-12 | P1 | Evidence-cited document Q&A over permitted filings (terminal AI-assistant functional parity) | LSEG Workspace, S&P Capital IQ Pro | proposed | not yet implemented |

### regulatory

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-LAW-01 | P0 | Dated regulatory source register and applicability record (S11-S21, S41, S42) with change detection | IRS, Interactive Brokers LLC Client Agreement (Form 3203, 10 January 2025), NSE  | proposed | not yet implemented |
| CAP-LAW-02 | P0 | US day-trading / intraday-margin regime gate (FINRA Regulatory Notice 26-10, new Rule 4210(d)(2)) | FINRA Regulatory Notice 26-10, FINRA Regulatory Notice 26-10 (published April 20 | proposed | not yet implemented |
| CAP-LAW-03 | P0 | US cash-account settled-funds and freeriding gate (Regulation T 220.8; Investor.gov cash-account bulletin) | Federal Reserve Regulation T, 12 CFR 220.8 (eCFR, Title 12 up to date as of 10/2 | proposed | not yet implemented |
| CAP-LAW-04 | P0 | Long-only enforcement and Regulation SHO short-sale gate | SEC Regulation SHO key points (S42), SEC Regulation SHO key points (S42), modifi | proposed | not yet implemented |
| CAP-LAW-05 | P0 | Owner-side pre-trade risk controls benchmarked to SEC Rule 15c3-5, NSE algo risk-control table and MiFID II Art. 17 | Directive 2014/65/EU (MiFID II) Article 17 (original OJ text via Publications Of | proposed | not yet implemented |
| CAP-LAW-06 | P1 | India retail algorithmic/API trading eligibility gate (SEBI Feb 4, 2025 circular; SEBI Sep 30, 2025 glide path; NSE implementation standards and consolidated circular 73992) | NSE Consolidated NNF Circular NSE/INVG/73992, NSE Consolidated NNF Circular NSE/ | proposed | not yet implemented |
| CAP-LAW-07 | P1 | India API order runtime constraints: OPS throttle, no algorithmic market orders, session expiry, broker daily caps | NSE Consolidated NNF Circular NSE/INVG/73992, s.8.6 risk-control table, NSE NSE/ | proposed | not yet implemented |
| CAP-LAW-08 | P1 | India exchange-control gate for overseas accounts after relocation (RBI Forex FAQ, LRS FAQ) | Interactive Brokers market data minimum equity requirements, RBI Forex Transacti | proposed | not yet implemented |
| CAP-LAW-09 | P1 | EU MiFID II own-account dealing exemption gate (Art. 2(1)(d), Art. 4(1)(39)-(41), Art. 17) | Directive 2014/65/EU (MiFID II), Art. 2(1)(d) (original OJ text via Publications | proposed | not yet implemented |
| CAP-LAW-10 | P1 | Market-abuse conduct controls: self-cross prevention, order-intent logging, layering/spoofing pattern review (EU MAR; NSE 73992; book chapter 25) | MAR Art. 15, MAR Art. 16(2), MAR Art. 2(4), MAR Art. 39(2), NSE Consolidated NNF | proposed | not yet implemented |
| CAP-LAW-11 | P0 | IBKR API market-data entitlement, subscriber status and usage-rights gate | Interactive Brokers API documentation, Interactive Brokers API documentation (ci | proposed | not yet implemented |
| CAP-LAW-12 | P0 | IBKR account-terms gate for unattended automated trading (Client Agreement Form 3203; Relationship Summary Form 4024; API user certification) | IBKR Client Agreement (Form 3203), section 39, IBKR Client Agreement (Form 3203) | proposed | not yet implemented |
| CAP-LAW-13 | P1 | US tax records for an automated trader: trader-status evidence, section 475(f) election calendar, wash-sale tracking (IRS Topic 429) | IRS Topic 429, IRS Topic 429 Traders in securities (S21), last reviewed 24-Sep-2 | proposed | not yet implemented |

### ops-aws

| ID | Priority | Capability | Reference scope | Status | Main gap |
|---|---|---|---|---|---|
| CAP-OPS-01 | P0 | Mac process supervision with launchd (restart drill, crash-loop handling, sleep and power settings) | Apple macOS, Apple macOS launchd (platform supervisor; not a reference terminal) | proposed | not yet implemented |
| CAP-OPS-02 | P0 | SQLite ledger durability settings, version advisory gate and crash/power-loss tests (macOS F_FULLFSYNC, Linux EBS) | IBKR TWS API, SQLite, SQLite (storage engine; not a reference terminal) | implemented | SQLite version advisory gate not automated |
| CAP-OPS-03 | P0 | Consistent encrypted ledger backups and an isolated restore drill with post-backup broker reconciliation | IBKR Flex Web Service, IBKR TWS API, SQLite | implemented | encryption and off-host copy not automated |
| CAP-OPS-04 | P0 | IB Gateway supervision: pinned install, daily auto-restart, weekly manual reauthentication and connectivity-state handling | IBC (third-party login automation; not IBKR), IBKR IB Gateway, IBKR TWS API, IBK | proposed | not yet implemented |
| CAP-OPS-05 | P0 | Layered health and readiness model (alive, writable, fresh, authenticated, eligible) with explicit inactivity reasons | Amazon EC2 (deployment platform), IBKR, IBKR APIs, IBKR TWS API | implemented | data-freshness and alert layers incomplete |
| CAP-OPS-06 | P0 | Operational alerting: severity, deduplication, runbook links, dead-man detection (local and CloudWatch) | Amazon CloudWatch, Amazon CloudWatch (deployment platform), IBKR TWS API, LSEG W | proposed | not yet implemented |
| CAP-OPS-07 | P1 | Spend cap enforced before any AWS provisioning (AWS Budgets, budget actions, IaC gate) and all-in operating cost | AWS Budgets, AWS Budgets (self-hosting cost control; not a reference-terminal fe | proposed | not yet implemented |
| CAP-OPS-08 | P1 | AWS Region and instance selection (us-east-1 vs ap-south-1) by measured broker latency, eligibility and availability, with pricing linked not assumed | Amazon EC2, IBKR, IBKR IB Gateway, IBKR TWS / IB Gateway | proposed | not yet implemented |
| CAP-OPS-09 | P1 | Private administrative access via Systems Manager Session Manager (no inbound ports), egress allowlist and gateway API exposure control | AWS Systems Manager, AWS Systems Manager Session Manager, IBKR, IBKR TWS API | proposed | not yet implemented |
| CAP-OPS-10 | P0 | Secrets management: Mac Keychain and AWS Secrets Manager, broker credentials never stored, paper/live separation, rotation | AWS Secrets Manager, Apple macOS Keychain, Bloomberg Terminal (Bloomberg Anywher | proposed | not yet implemented |
| CAP-OPS-11 | P1 | IAM least privilege for execution and research roles, MFA for humans, and IMDSv2-only instance metadata | AWS IAM, Amazon EC2, IBKR | proposed | not yet implemented |
| CAP-OPS-12 | P1 | Encryption at rest on Mac and AWS (FileVault, EBS encryption by default, KMS keys, encrypted backups) | AWS Backup, Amazon EBS, Apple macOS FileVault | proposed | not yet implemented |
| CAP-OPS-13 | P1 | AWS Backup plans, vault lock and scheduled restore testing for the execution host (secondary to application-consistent SQLite backups) | AWS Backup, IBKR Flex Web Service | proposed | not yet implemented |
| CAP-OPS-14 | P1 | Instance recovery and single-writer fencing: automatic EC2 recovery, manual ownership transfer, competing-session detection | Amazon EC2, IBKR, IBKR TWS API | implemented | automatic EC2 recovery not configured (no AWS resources provisioned) |
