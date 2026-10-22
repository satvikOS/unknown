# Markets: US, India and EU equity venues

Owner brief: "Stocks first; US, India and EU markets must be accommodated."

This increment adds venue reference data for cash equities. It covers trading calendars, settlement calendars, settlement cycles by trade date, and a venue registry. It does **not** change trading eligibility. Which venues the desk may trade is set by the owner policy (`config/policies/*.json`, still US only) behind release gates (ADR-0009, CLAUDE.md "Scope and sequencing"). Settlement dates computed here are expectations. Settlement is confirmed only from broker evidence (ADR-0006).

Status (ADR-0001): `tested`, meaning offline deterministic tests in `backend/tests/test_markets.py` (31 tests). Nothing here is paper-verified or live-verified.

Capabilities served: CAP-INS-07 (exchange calendars, half days, special sessions), CAP-INS-08 (settlement calendars), CAP-OMS-09 (settlement-date calculation), CAP-INS-14 (MIC linkage).

## What exists

| Path | Content |
|---|---|
| `config/calendars/XNSE.json`, `XBOM.json` | NSE and BSE equity trading calendars for 2026, including the Diwali **Muhurat** special session |
| `config/calendars/NCL-CM.json`, `ICCL-EQ.json` | Settlement calendars: NSE Clearing (CM segment) and Indian Clearing Corporation (BSE), 2026 |
| `config/calendars/XETR.json` | Xetra trading calendar for 2026–2032 |
| `config/calendars/XPAR.json`, `XAMS.json`, `XBRU.json`, `XLIS.json` | Euronext Paris, Amsterdam, Brussels and Lisbon, 2026 |
| `config/calendars/XMIL.json`, `XMAD.json` | Borsa Italiana / Euronext Milan and BME Madrid, 2026 |
| `config/calendars/T2S-EUR.json` | T2S euro settlement closing days, 2026–2032 |
| `config/markets/venues.json` | Venue registry: MIC, operating/segment MIC, country, currency, time zone, calendars, settlement cycles with sources, lot/tick conventions where a primary source states them, IBKR exchange code |
| `backend/src/unknown/domain/markets.py` | Loads and validates the registry. `VenueRegistry.expected_settlement_date(mic, trade_date)` |
| `backend/src/unknown/domain/calendar.py` | Additive changes (below). `load_calendar` still reads `XNYS.json` unchanged |

`XNYS.json` is unchanged.

### Calendar model (additive)

The file schema is `XNYS.json`'s. The following keys are optional additions:

- `kind`: `trading` or `settlement`. Trading days and settlement days differ. Indian bank holidays such as 2026-02-19 are NSE trading days but NSE Clearing holidays. Xetra's 24 and 31 December are settlement days without trading.
- `regular_session`, `timezone`, and `session_timezone`. `session_timezone` is the zone the times are stated in. Euronext states every market's hours in CET, so Lisbon's are stored in a CET zone.
- `early_closes`: entries are `[date, "HH:MM" | null, label]`. `null` means the venue announced a shortened day but has not published its close time.
- `special_sessions` means a session on a day that is not an ordinary business day. `is_business_day()` stays `False` on that day and `is_trading_day()` is `True`. The loader refuses a special session placed on an ordinary weekday, so a Muhurat session can never pass as a normal day.
- `session_times_pending`: a trading day whose hours the venue has not fixed yet.
- `BusinessCalendar.session(day)` raises `SessionTimeUnknown` whenever the hours are not in the sourced data. It never falls back to the regular hours. Every query beyond `valid_through` raises `CalendarHorizonError`, as for XNYS.

## Calendars and their sources (all retrieved 2026-10-22)

| Calendar | Horizon | Primary source | Notes |
|---|---|---|---|
| XNSE | 2026 | https://www.nseindia.com/api/holiday-master?type=trading (data behind https://www.nseindia.com/resources/exchange-communication-holidays); circular NSE/CMTR/71775 | `holiday-year-list` returned only `2026`; the 2027 query returned `{}` |
| NCL-CM | 2026 | https://www.nseindia.com/api/holiday-master?type=clearing; circular NCL/CMPT/71904 | Rule: "all intervening holidays, which include bank holidays, NSE holidays, Saturdays and Sundays are excluded" |
| XBOM | 2026 | BSE notice 20251212-8 and 20260112-8 (`https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=…`) | See open questions 1 and 2 |
| ICCL-EQ | 2026 | BSE notice 20251224-1 (ICCL settlement holidays 2026); ICCL notice 20260109-67 (15 Jan 2026) | |
| XETR | 2026–2032 | https://www.cashmarket.deutsche-boerse.com/cash-en/trading/trading-calendar-and-trading-hours (and the 2026 PDF) | "The overview is for information purposes only. Changes for future years are not excluded." |
| XPAR, XAMS, XBRU, XLIS | 2026 | https://www.euronext.com/en/trading/trading-hours-holidays; notice IF251107CADE; trading-hours appendix https://www.euronext.com/en/media/1927/download (effective 19 Oct 2026) | 24 and 31 Dec are half days with no close time published yet |
| XMIL | 2026 | https://www.borsaitaliana.it/borsaitaliana/calendario-e-orari-di-negoziazione/calendario-borsa-orari-di-negoziazione.en.htm | Closures for the Trading After Hours MTF only are not encoded |
| XMAD | 2026 | https://www.bolsasymercados.es/en/bme-exchange/trading/trading-calendar.html; BME press release of 20/11/2025; https://www.bolsasymercados.es/en/bme-exchange/trading-hours.html | Early closes at 14:00 CET on 24 and 31 Dec |
| T2S-EUR | 2026–2032 | https://www.ecb.europa.eu/paym/target/t2s/html/index.en.html | The ECB states a rule. Easter dates were cross-checked against Deutsche Börse's table and the Gregorian computus |

The NSE, NCL, BSE, ICCL and Xetra holidays were cross-checked mechanically against the retrieved files. The Euronext, Borsa Italiana and BME lists are short and were checked by reading the source text. Each calendar file lists all of its sources, with URL and retrieval date, under `sources`. Several static documents were retrieved on 2026-10-22, and their SHA-256 hashes (first 16 hex digits) are:

| Document | SHA-256 prefix |
|---|---|
| NSE CMTR76941.pdf (Muhurat) | ce94ee1c54d98ecc |
| NCL CMPT77037.pdf (Muhurat settlement) | 4845536a93998989 |
| NSE CMTR71775.pdf | aa97e0afc0ce3940 |
| NCL CMPT71904.pdf | b08d3c4462cfbbc0 |
| NSE EQUITY_L.csv | 342103bdea9b4810 |
| Deutsche Börse trading calendar 2026 PDF | 1edfc7b737ae1f5f |
| Euronext 2026 holiday notice PDF | 617bc559510ef3a3 |
| Euronext trading-hours appendix (xlsx) | 84d28425ec3eee10 |
| ECB list of T2S CSDs PDF | 6dec4845dbaaa23e |
| SEBI circular 2021/628 PDF | 1b1a1655835e1927 |
| SEBI circular 2024/20 PDF | 8ed1362f7732c738 |
| ISO 10383 MIC CSV | 09a0d07b7b834814 |

### Diwali Muhurat session

- **NSE.** On Sunday 2026-11-08 the normal market runs 14:20–15:30 IST. For Closing Auction Session securities, continuous trading ends at 15:15. Pre-open runs 14:05–14:20. Source: NSE/CMTR/76941 of 2026-10-19, which says "All trades executed in this Diwali Muhurat trading session shall result in settlement obligations." NSE Clearing NCL/CMPT/77037 settles those trades on Monday 2026-11-09, and the tests assert exactly that date. They also assert that 2026-11-06 trades settle on 11-09 and that 2026-11-09 trades settle on 11-11, as the circular's Annexure A states.
- **BSE.** The session date comes from notice 20251212-8. BSE's timing notice could not be retrieved, so the times are `null` and `session()` raises.

## Settlement cycles (by trade date)

| Venues | Cycle | Primary source |
|---|---|---|
| XNYS | T+1 from trade date 2024-05-28 | 17 CFR 240.15c6-1 (eCFR, current): "not later than the first business day after the date of the contract". SEC press release 2023-29: "The compliance date for the final rules is May 28, 2024." |
| XNSE, XBOM | T+1 from 2023-01-27 | SEBI SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/20: the move to T+1 was "fully implemented w.e.f. January 27, 2023". Also SEBI/HO/MRD2/DCAP/P/CIR/2021/628 (optional T+1) and the NSE settlement-cycle page |
| EU (7 venues) | T+2 from 2016-01-01 | CSDR (EU) No 909/2014 Art. 5(2): "no later than on the second business day after the trading takes place". Art. 76(3) applies it from 1 Jan 2015, and from 1 Jan 2016 at the latest for derogated venues; the later date is encoded |
| EU (7 venues) | T+1 from trade date **2027-10-11** | Regulation (EU) 2025/2075 of 8 October 2025, OJ L 2025/2075 of 14.10.2025, Art. 1(1) and Art. 2: "It shall apply from 11 October 2027." The date was verified in the Official Journal text, retrieved through the EU Publications Office (CELEX 32025R2075) because EUR-Lex refused automated retrieval |

In the EU, a "business day" is the settlement system's business day (CSDR Art. 2(1)(14), which refers to Directive 98/26/EC). EU settlement is therefore counted on the T2S euro calendar. On 24 and 31 December, T2S settles while Xetra and Milan do not trade.

A worked transition example: an Xetra trade on Friday 2027-10-08 settles on Tuesday 2027-10-12 (T+2), and a trade on Monday 2027-10-11 also settles on 2027-10-12 (T+1).

The instrument master's fixed `Instrument.settlement_days` cannot express that change. For EU listings, the registry has to be asked by trade date. `domain/instruments.py` is unchanged; this is a follow-up for the core.

Trade dates before the first sourced rule raise `NoSettlementRule`. A trade on a non-trading day raises `NotATradingDay`. A settlement date beyond a settlement calendar's horizon raises `CalendarHorizonError`; for example, an NSE trade on 2026-12-31 cannot be resolved until NSE Clearing publishes 2027.

## Lot and tick conventions

These are recorded only where a primary source states them:

- **NSE lot.** The market lot is a per-security field in NSE's `EQUITY_L.csv`. On 2026-10-22 it was 1 for 2,608 of 2,611 securities, 50 for 2 and 100 for 1, so it must be read per instrument.
- **EU tick.** The tick follows the MiFID II regime, Delegated Regulation (EU) 2017/588 Art. 2: liquidity band × price range. It is not encoded; take each instrument's tick table from broker contract details.
- **Everything else is `null`.** No primary source was retrieved for US tick or lot, BSE lot or tick, EU lot, or NSE tick.

## Identifiers: ISIN and the instrument master (documentation only)

ISIN (ISO 6166) identifies a security, not a listing. The same ISIN trades on NSE and BSE, and an EU share can trade on several venues. `domain/instruments.py` already keys one immutable `Instrument` per tradable listing (`venue_mic`, trading currency) and stores external identifiers as versioned `ProviderMapping` children. The proposed mapping, with no code changed:

- **NSE listing.** `ProviderMapping(provider="nse", external_id="<SYMBOL>|<SERIES>")`, with `details` holding ISIN, MARKET LOT, FACE VALUE and DATE OF LISTING from `EQUITY_L.csv`. A symbol change (NSE `symbolchange.csv`) or a series change creates a new mapping version (CAP-INS-05, CAP-INS-06).
- **BSE listing.** `ProviderMapping(provider="bse", external_id="<scrip code>")`. A BSE scrip master was not retrieved, so this is open question 2.
- **Cross-venue link.** Each listing gets `ProviderMapping(provider="isin", external_id=<ISIN>)`. Listings that share an ISIN are the same issue. ISINs can change on corporate actions (for example a face-value split), so the ISIN mapping is versioned with validity dates, not treated as a permanent key.
- **EU listings.** There is one `Instrument` per (MIC, currency). The IBKR contract maps as `provider="ibkr"` (conId), with the IBKR exchange code from `venues.json` (`IBIS`, `SBF`, `AEB`, `ENEXT.BE`, `BVL`, `BVME`, `BM`). The listing venue is recorded through `attributes["ibkr.primaryExchange"]`, following the existing pattern.
- **US ISINs.** They embed the CUSIP. Whether storing them raises CUSIP licensing questions belongs to CAP-INS-03; nothing here decides it.

## Broker access reality for the owner (documentation only; eligibility unchanged)

| Market | IBKR US account | Evidence (retrieved 2026-10-22) |
|---|---|---|
| NSE | **No.** "Trading of products listed on the National Stock Exchange of India is limited to Indian residents and Non-Resident Indians (NRIs). Trading must be conducted via an account opened through IB India." | IBKR FAQ "How do I add India trading permissions?", https://www.interactivebrokers.com/lib/cstools/faq/#/content/40342168 (reached from https://ibkb.interactivebrokers.com/node/848) |
| BSE | **No.** IBKR lists no BSE exchange. | IBKR exchange list https://www.interactivebrokers.com/webrest/exchanges/ (the data behind https://www.interactivebrokers.com/en/trading/products-exchanges.php): `NSE` is the only Indian entry |
| Xetra, Euronext Paris/Amsterdam/Brussels/Lisbon, Borsa Italiana, BME | **Offered by IBKR as stock exchanges** (`IBIS`, `SBF`, `AEB`, `ENEXT.BE`, `BVL`, `BVME`, `BM`). The market-data page lists 15-minute delayed data for FWB/IBIS/XETRA, Euronext AEB/SBF/MATIF/BELFOX, Borsa Italiana BVME/IDEM and Bolsa de Madrid BM. Lisbon and Brussels are not named separately there. | Same exchange list; https://www.interactivebrokers.com/en/pricing/market-data-pricing.php |

What the sources do *not* settle is in open questions 9 and 10. India access paths outside IBKR are an IB India account (resident or NRI status required) or an Indian broker API (CAP-OMS-05). Both sit behind CAP-LAW-06/07 (SEBI retail algo rules) and CAP-LAW-08 (RBI/FEMA). None of this is authorised.

## Maintenance

Calendars are refreshed by a manual, owner-approved retrieval, not a scheduled scraper. NSE's terms of use prohibit "systematic or automated data collection" from its website (CAP-DATA-09). On each refresh:

1. Bump `version`.
2. Update `retrieved_on` and the `sources` list.
3. Extend `valid_through` only as far as the source goes.
4. Run `test_markets`.

The India, Euronext, Milan and Madrid horizons end on **2026-12-31**. Refresh them when the venues publish 2027: Indian settlement for trades on 2026-12-31 is already unresolvable.

## Open questions

1. **BSE live data.** BSE's holiday page (`/static/markets/marketinfo/listholi`, data from `api.bseindia.com`) returned HTTP 403 to automated retrieval, including from a headless browser. BSE notice pages after early March 2026 are not rendered server-side. As a result:
   - BSE amendments published after March 2026 are unverified.
   - BSE's Muhurat timing notice and BSE regular trading hours are not recorded.
   - Someone with an ordinary browser should confirm these and update `XBOM.json`.
2. **ICCL and BSE scrip master.** ICCL's site (icclindia.com) did not respond. The 15 Jan 2026 ICCL settlement holiday comes from an EGR-segment ICCL notice citing the RBI circular. No BSE scrip-code master was retrieved.
3. **Euronext half-day close times** for 24 and 31 Dec 2026. These await Euronext's 2026 end-of-year appendix; only the 2025 appendix was linked from the Euronext page on 2026-10-22.
4. **Xetra hours on 30 Dec 2026.** Deutsche Börse says "deviating trading hours may apply" and that a circular will follow.
5. **2027 lists.** NSE, BSE, NCL, ICCL, Euronext, Borsa Italiana and BME had not published 2027 lists on 2026-10-22.
6. **EU T+1 transition semantics.** This build encodes "trades executed on or after 2027-10-11 are T+1", so 2027-10-12 receives both the last T+2 trades and the first T+1 trades. That follows the regulation's application date and Euronext's "go-live ... 11 October 2027". ESMA's site returned HTTP 403, so its T+1 pages were not read. Confirm with the broker or CSD before relying on 2027-10 dates.
7. **EU venue-to-CSD mapping.** The registry assumes each venue's domestic shares settle at its national CSD (Clearstream Banking AG, Euroclear France/Netherlands/Belgium, Euronext Securities Porto/Milan, Iberclear). The ECB lists all of these as T2S-connected, but no venue source was retrieved for the mapping. CSD-specific closing days beyond T2S's are not verified.
8. **US settlement calendar.** The DTC settlement calendar is still not verified; XNYS is used as a conservative expectation calendar, as before.
9. **IBKR EU permissions.** Whether the owner's IBKR account currently has, or can be granted, European stock trading permissions is an account setting this repository cannot see. Check it in Client Portal before any EU paper test. Separately, restrictions on US persons buying EU-domiciled funds or ETFs were not verified; "stocks first" makes this lower priority.
10. **Owner residency and NRI status.** This decides whether IB India is even an option (CAP-LAW-08). It is an owner question.
11. **NSE tick-size schedule** (price-band-based) was not retrieved, so the tick is `null`.
