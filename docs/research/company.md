# Company research and free official data sources

Status (ADR-0001 vocabulary): **tested** with synthetic fixtures on Python 3.14 and 3.12; the four free sources were
**live-smoke-tested once** on 2026-10-22 (results below). Nothing here is paper- or live-verified, and nothing here is
evidence of investment value. Increments I4/I7. Book chapters 11 and 17. Capability records: CAP-CO-01, 02, 04, 05, 06,
07, 10, 11, 12; CAP-INS-01, 02, 06; CAP-DATA-04, 06, 07; CAP-MAC-04, 05, 07, 10, 12.

Code: `backend/src/unknown/data/fundamentals/` (analytics) and `backend/src/unknown/data/sources/` (ingestion).
API: route module `backend/src/unknown/api/routes/company.py`. Tests: `backend/tests/test_company_*.py`,
`backend/tests/test_sources_*.py`.

## Principles

- **Point in time.** Every number is computed from `ResearchStore.as_of(entity, field, t)`: only publications with
  `available_time <= t`. For SEC facts that is the filing's acceptance time. A restatement filed after `t` never
  reaches a statement, ratio or valuation as of `t`. `known_at` (audit mode) further restricts to what this system
  had ingested by then.
- **Exact arithmetic.** Accounting quantities are `Fixed`. Ratios are exact quotients rounded HALF_EVEN to 10 decimal
  places. FCFF is rounded HALF_EVEN to whole currency units. The DCF uses Python `decimal` with an explicit context
  (34 significant digits, ROUND_HALF_EVEN). Floats are refused everywhere. Nothing here is posted to the ledger, and
  no core module imports these packages (a test checks the ECB rates).
- **Missing is not zero.** A missing value has a reason (`NOT_REPORTED`, `NOT_APPLICABLE`, `NOT_DERIVABLE`,
  `MISSING_INPUT`, `NON_CONTIGUOUS`, `FISCAL_TRANSITION`). A metric has a status (`OK`, `NM`, `MISSING`,
  `INCOMPLETE`, `SUPPRESSED`, `NO_PRICE`).
- **Lineage.** Every cell lists the concept, observation ids, accession, form, availability time and an SEC
  filing-index link. Every metric has a formula `id@version`, its expression, its inputs and its source dates.

## Standardized statements (`std-map@1`)

`fundamentals/mapping.py` holds the project's own versioned mapping. Vendor standardizations are proprietary and are
not copied (CAP-CO-05).

- **Precedence.** For each period, the first concept in the list that has a fact for exactly that period wins. A filer
  that moved from `SalesRevenueNet` to `RevenueFromContractWithCustomerExcludingAssessedTax` (ASC 606) therefore
  keeps one continuous line. Each cell shows which concept it used. Only semantically equivalent concepts are
  alternates; partial ones (goods-only revenue, `DebtCurrent`) are deliberately excluded.
- **Signs and units** are kept as XBRL reports them. Payments are positive, so FCF = CFO - capex. Money items use the
  filer's reporting currency, per-share items `<ccy>/shares`, share counts `shares`.
- **Per-share values and share counts are never summed or differenced.** They are not derived for Q4 and have no TTM.
- **Coverage.** The statement response counts the concepts the issuer reported that the mapping does not use.
  Company extension concepts are not in companyfacts at all (CAP-CO-04). On the live Apple data, for example, 450 of
  505 reported concepts are unmapped detail lines.

| Item | Statement | Type | Unit | Concepts in precedence order | Computation |
|---|---|---|---|---|---|
| `revenue` | income | duration | money | `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`<br>`us-gaap:Revenues`<br>`us-gaap:SalesRevenueNet`<br>`us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax`<br>`ifrs-full:Revenue` | — |
| `cost_of_revenue` | income | duration | money | `us-gaap:CostOfGoodsAndServicesSold`<br>`us-gaap:CostOfRevenue`<br>`us-gaap:CostOfGoodsSold`<br>`ifrs-full:CostOfSales` | — |
| `gross_profit` | income | duration | money | `us-gaap:GrossProfit`<br>`ifrs-full:GrossProfit` | fallback: revenue - cost_of_revenue |
| `research_and_development` | income | duration | money | `us-gaap:ResearchAndDevelopmentExpense`<br>`us-gaap:ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost` | — |
| `selling_general_admin` | income | duration | money | `us-gaap:SellingGeneralAndAdministrativeExpense` | — |
| `operating_expenses` | income | duration | money | `us-gaap:OperatingExpenses` | — |
| `operating_income` | income | duration | money | `us-gaap:OperatingIncomeLoss`<br>`ifrs-full:ProfitLossFromOperatingActivities` | — |
| `ebit` | income | duration | money | — | always: operating_income |
| `interest_expense` | income | duration | money | `us-gaap:InterestExpense`<br>`us-gaap:InterestExpenseNonoperating`<br>`us-gaap:InterestExpenseDebt`<br>`ifrs-full:FinanceCosts` | — |
| `pretax_income` | income | duration | money | `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest`<br>`us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments`<br>`ifrs-full:ProfitLossBeforeTax` | — |
| `income_tax` | income | duration | money | `us-gaap:IncomeTaxExpenseBenefit`<br>`ifrs-full:IncomeTaxExpenseContinuingOperations` | — |
| `net_income` | income | duration | money | `us-gaap:NetIncomeLoss`<br>`ifrs-full:ProfitLossAttributableToOwnersOfParent` | — |
| `net_income_including_nci` | income | duration | money | `us-gaap:ProfitLoss`<br>`ifrs-full:ProfitLoss` | — |
| `ebitda` | income | duration | money | — | always: operating_income + depreciation_amortization |
| `eps_basic` | per_share | duration | per_share | `us-gaap:EarningsPerShareBasic`<br>`ifrs-full:BasicEarningsLossPerShare` | — |
| `eps_diluted` | per_share | duration | per_share | `us-gaap:EarningsPerShareDiluted`<br>`us-gaap:EarningsPerShareBasicAndDiluted`<br>`ifrs-full:DilutedEarningsLossPerShare` | — |
| `dividends_declared_per_share` | per_share | duration | per_share | `us-gaap:CommonStockDividendsPerShareDeclared` | — |
| `weighted_shares_basic` | shares | duration | shares | `us-gaap:WeightedAverageNumberOfSharesOutstandingBasic` | — |
| `weighted_shares_diluted` | shares | duration | shares | `us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding` | — |
| `shares_outstanding_cover` | shares | instant | shares | `dei:EntityCommonStockSharesOutstanding` | — |
| `shares_outstanding_balance` | shares | instant | shares | `us-gaap:CommonStockSharesOutstanding` | — |
| `cash_and_equivalents` | balance | instant | money | `us-gaap:CashAndCashEquivalentsAtCarryingValue`<br>`us-gaap:Cash`<br>`ifrs-full:CashAndCashEquivalents` | — |
| `short_term_investments` | balance | instant | money | `us-gaap:MarketableSecuritiesCurrent`<br>`us-gaap:ShortTermInvestments`<br>`us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent`<br>`ifrs-full:CurrentInvestments` | — |
| `receivables` | balance | instant | money | `us-gaap:AccountsReceivableNetCurrent`<br>`ifrs-full:TradeAndOtherCurrentReceivables` | — |
| `inventory` | balance | instant | money | `us-gaap:InventoryNet`<br>`ifrs-full:Inventories` | — |
| `current_assets` | balance | instant | money | `us-gaap:AssetsCurrent`<br>`ifrs-full:CurrentAssets` | — |
| `total_assets` | balance | instant | money | `us-gaap:Assets`<br>`ifrs-full:Assets` | — |
| `accounts_payable` | balance | instant | money | `us-gaap:AccountsPayableCurrent`<br>`ifrs-full:TradeAndOtherCurrentPayables` | — |
| `current_liabilities` | balance | instant | money | `us-gaap:LiabilitiesCurrent`<br>`ifrs-full:CurrentLiabilities` | — |
| `long_term_debt_current` | balance | instant | money | `us-gaap:LongTermDebtCurrent`<br>`us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent` | — |
| `long_term_debt_noncurrent` | balance | instant | money | `us-gaap:LongTermDebtNoncurrent`<br>`us-gaap:LongTermDebtAndCapitalLeaseObligations`<br>`ifrs-full:NoncurrentPortionOfNoncurrentBorrowings` | — |
| `long_term_debt_total` | balance | instant | money | `us-gaap:LongTermDebt` | — |
| `commercial_paper` | balance | instant | money | `us-gaap:CommercialPaper` | — |
| `short_term_borrowings` | balance | instant | money | `us-gaap:ShortTermBorrowings`<br>`ifrs-full:ShorttermBorrowings` | — |
| `total_debt` | balance | instant | money | — | always: long_term_debt_current (optional) + long_term_debt_noncurrent (optional) + commercial_paper (optional) + short_term_borrowings (optional) |
| `operating_lease_liabilities` | balance | instant | money | `us-gaap:OperatingLeaseLiability`<br>`ifrs-full:LeaseLiabilities` | — |
| `total_liabilities` | balance | instant | money | `us-gaap:Liabilities`<br>`ifrs-full:Liabilities` | — |
| `preferred_stock` | balance | instant | money | `us-gaap:PreferredStockValue`<br>`us-gaap:PreferredStockValueOutstanding` | — |
| `noncontrolling_interest` | balance | instant | money | `us-gaap:MinorityInterest`<br>`ifrs-full:NoncontrollingInterests` | — |
| `equity_parent` | balance | instant | money | `us-gaap:StockholdersEquity`<br>`ifrs-full:EquityAttributableToOwnersOfParent` | — |
| `equity_total` | balance | instant | money | `us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`<br>`ifrs-full:Equity` | fallback: equity_parent + noncontrolling_interest (optional) |
| `net_debt` | balance | instant | money | — | always: total_debt - cash_and_equivalents - short_term_investments (optional) |
| `invested_capital` | balance | instant | money | — | always: total_debt (optional) + equity_total - cash_and_equivalents |
| `operating_working_capital` | balance | instant | money | — | always: current_assets - cash_and_equivalents - short_term_investments (optional) - current_liabilities + long_term_debt_current (optional) + commercial_paper (optional) + short_term_borrowings (optional) |
| `liabilities_and_equity` | balance | instant | money | `us-gaap:LiabilitiesAndStockholdersEquity`<br>`ifrs-full:EquityAndLiabilities` | — |
| `equity_method_investments` | balance | instant | money | `us-gaap:EquityMethodInvestments` | — |
| `pension_funded_status` | balance | instant | money | `us-gaap:DefinedBenefitPlanFundedStatusOfPlan` | — |
| `cfo` | cash_flow | duration | money | `us-gaap:NetCashProvidedByUsedInOperatingActivities`<br>`us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations`<br>`ifrs-full:CashFlowsFromUsedInOperatingActivities` | — |
| `capex` | cash_flow | duration | money | `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`<br>`ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` | — |
| `cfi` | cash_flow | duration | money | `us-gaap:NetCashProvidedByUsedInInvestingActivities`<br>`us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations`<br>`ifrs-full:CashFlowsFromUsedInInvestingActivities` | — |
| `cff` | cash_flow | duration | money | `us-gaap:NetCashProvidedByUsedInFinancingActivities`<br>`us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations`<br>`ifrs-full:CashFlowsFromUsedInFinancingActivities` | — |
| `depreciation` | cash_flow | duration | money | `us-gaap:Depreciation` | — |
| `amortization_of_intangibles` | cash_flow | duration | money | `us-gaap:AmortizationOfIntangibleAssets` | — |
| `depreciation_amortization` | cash_flow | duration | money | `us-gaap:DepreciationDepletionAndAmortization`<br>`us-gaap:DepreciationAmortizationAndAccretionNet`<br>`us-gaap:DepreciationAndAmortization`<br>`ifrs-full:DepreciationAndAmortisationExpense` | fallback: depreciation + amortization_of_intangibles |
| `share_based_compensation` | cash_flow | duration | money | `us-gaap:ShareBasedCompensation`<br>`us-gaap:AllocatedShareBasedCompensationExpense` | — |
| `dividends_paid` | cash_flow | duration | money | `us-gaap:PaymentsOfDividends`<br>`us-gaap:PaymentsOfDividendsCommonStock`<br>`ifrs-full:DividendsPaidClassifiedAsFinancingActivities` | — |
| `share_repurchases` | cash_flow | duration | money | `us-gaap:PaymentsForRepurchaseOfCommonStock` | — |
| `free_cash_flow` | cash_flow | duration | money | — | always: cfo - capex |

### Fiscal periods (`fundamentals/periods.py`)

- **Classification by dates, not filing labels.** A fact's period comes from its start and end dates. The companyfacts
  `fy`/`fp` fields describe the *filing*: a 10-K carries prior-year comparatives tagged `FY`, and a Q2 10-Q carries
  both a 3-month and a 6-month period. Duration classes use inclusive day counts within the SEC frames tolerance:
  Q 61-121, H1 152-213, 9M 243-304, FY 335-395. So 52-53 week years (364 or 371 days) and 13- or 14-week quarters
  need no calendar-quarter assumption.
- **Fiscal years** are the reported annual periods.
- **Fiscal starts** come from three places: annual starts, the day after each annual end, and YTD starts. A 3-month
  period that begins exactly 364, 365, 366 or 371 days after a known start is also a start. This handles a Q1 10-Q
  filed before a late 10-K.
- **Quarters** are cut at the YTD ends and the reported 3-month periods.
- **Labels.** `fiscal_year` is the calendar year in which the fiscal year ends. This is the project's convention;
  filers' own names can differ, for example retailers whose years end in January.
- **Fiscal calendar breaks.** Annual periods that do not abut mark a fiscal-year change. Quarters inside the gap are
  flagged `transition_period` and excluded from TTM and growth (CAP-CO-05).

### Derived quarters and TTM

- **Derived quarters (`DERIVED_QUARTER`, flagged).** Q2 = 6M YTD - Q1, Q3 = 9M YTD - 6M YTD, Q4 = FY - 9M YTD.
  Otherwise the YTD value minus the reported prior quarters is used. Both inputs must come from one concept and be
  available at `t`. Cash-flow statements in 10-Qs are YTD only, so most cash-flow quarters are derived. When an input
  comes from an amended filing (10-K/A, 10-Q/A) the cell is also flagged `derived_from_amended_filing`: for example
  Q4 = restated FY - as-filed 9M YTD carries the whole restatement in one quarter.
- **TTM (`TTM_SUM`).** The sum of the four contiguous discrete quarters ending at the latest quarter known at `t`.
  - If a quarter is missing (for example, a late 10-K leaves Q4 unknown when the next Q1 is filed), TTM is null with
    a reason. It is never a shorter or older window.
  - When the window is exactly a fiscal year, TTM is that fiscal year (`TTM_ANNUAL`).
  - The fallback FY_last + YTD_current - YTD_prior (`TTM_ALT`) is flagged.
  - Balances use the latest quarter-end value (`LATEST_INSTANT`).
- **Restatement history.** A reported cell lists the earlier publications for the same period that were available by
  `t`, and sets `restated` when their values differ.

## Ratios, EV bridge and multiples (`fundamentals/metrics.py`)

### Basis

- Flows are TTM at the latest quarter known at `t`.
- Balances are taken at that quarter's end.
- "Four quarters earlier" (YoY growth, average balances, the FCFF working-capital change) is the fiscal quarter that
  ends one fiscal year before the latest quarter, found by date: its end lies 361-374 days earlier (52 or 53 weeks, or
  a calendar year, +/- 3 days). It is never taken by list position, so a quarter missing from the calendar (an unfiled
  10-Q, a late 10-K) cannot turn a YoY comparison into an 18-month one. With no such quarter, growth is `MISSING`.
- Averages use the window-end balance and the balance at that year-ago quarter end. When only the ending balance
  exists it is used, flagged `average_unavailable_ending_balance_used`.

### Not meaningful

A denominator <= 0 gives status `NM`, no value, and exclusion from peer statistics. Examples: P/E with negative
earnings, and growth from a non-positive base.

### EV bridge

- **`ev_standard@1`** = market cap + total debt + preferred + noncontrolling interest - cash and equivalents -
  short-term investments, all at the latest balance-sheet date.
  - Total debt = current + noncurrent long-term debt + commercial paper + short-term borrowings.
  - Operating leases are excluded.
  - Restricted cash is not subtracted.
- **`ev_with_operating_leases@1`** adds operating lease liabilities.
- **`ev_pension_ex_equity_method@1`** subtracts the pension funded status, which adds a net unfunded liability, and
  subtracts equity-method investments.
- **Missing components.** A component the filer did not report for that date is listed as `MISSING` and excluded from
  the sum; it is never treated as a reported zero. The EV status is then `INCOMPLETE`, and multiples built on it
  inherit that status. On live data, both Apple and Microsoft are `INCOMPLETE`, because neither tags preferred stock
  or noncontrolling interest.
- **Financial-sector issuers** (SIC 6000-6799) have every EV-based metric `SUPPRESSED` by default.

### Price inputs

Prices are never fetched. A price input names its value, currency, source and observation time.

- A price observed after `t` is refused as look-ahead.
- A price more than 5 days old at `t` is flagged `stale_price`.
- A gap over 135 days between price and fundamentals is flagged.
- A price in a different currency from the statements gives `MISSING` (no FX is applied to multiples).
- Market cap = price x cover-page shares outstanding (`dei:EntityCommonStockSharesOutstanding`), with balance-sheet
  shares as a flagged fallback.
- P/E (TTM) = price / (net income TTM / diluted weighted-average shares of the period ending at the latest quarter).
  When that count comes from a YTD or annual period, it is flagged.

### Persistence

`persist()` writes results through `ResearchStore.record_metric`, with the formula `id@version` and the input
observation ids.

- An owner price becomes an observation (`owner.price_input`) through `record_price_input()`, so persisted multiples
  cite it.
- `derived_metric` has no flag column, so `INCOMPLETE`, `NM` and `SUPPRESSED` results are stored without a value,
  with a reason.
- "Show calculation" for a stored row is `explain_record()`, served at `/api/company/metric-record/<id>`.

| Formula | Expression | Unit | Basis | Not meaningful when | Needs price |
|---|---|---|---|---|---|
| `revenue_ttm@1` | sum of revenue over the four latest discrete fiscal quarters | money | TTM at latest quarter | — | no |
| `gross_margin@1` | gross_profit_TTM / revenue_TTM | ratio | TTM at latest quarter | NM if revenue <= 0 | no |
| `operating_margin@1` | operating_income_TTM / revenue_TTM | ratio | TTM at latest quarter | NM if revenue <= 0 | no |
| `ebitda_margin@1` | EBITDA_TTM / revenue_TTM; EBITDA = operating income + D&A | ratio | TTM at latest quarter | NM if revenue <= 0 | no |
| `net_margin@1` | net_income_TTM / revenue_TTM | ratio | TTM at latest quarter | NM if revenue <= 0 | no |
| `fcf_ttm@1` | CFO_TTM - capex_TTM | money | TTM at latest quarter | — | no |
| `fcf_margin@1` | FCF_TTM / revenue_TTM | ratio | TTM at latest quarter | NM if revenue <= 0 | no |
| `effective_tax_rate@1` | income_tax_TTM / pretax_income_TTM | ratio | TTM at latest quarter | NM if pretax income <= 0 | no |
| `fcff_ttm@1` | EBIT_TTM x (1 - effective tax rate) + D&A_TTM - capex_TTM - (NWC_end - NWC_4q_earlier) | money | TTM at latest quarter | unavailable if the effective tax rate is NM | no |
| `revenue_growth_yoy@1` | revenue_TTM / revenue_TTM(4 quarters earlier) - 1 | ratio | TTM at latest quarter | NM if prior <= 0 | no |
| `net_income_growth_yoy@1` | net_income_TTM / net_income_TTM(4 quarters earlier) - 1 | ratio | TTM at latest quarter | NM if prior <= 0 | no |
| `revenue_growth_fy@1` | revenue_FY / revenue_FY(prior fiscal year) - 1 | ratio | latest fiscal year | NM if prior <= 0 | no |
| `roe@1` | net_income_TTM / average(equity attributable to parent) | ratio | TTM at latest quarter | NM if average equity <= 0 | no |
| `roa@1` | net_income_TTM / average(total assets) | ratio | TTM at latest quarter | NM if average assets <= 0 | no |
| `roic@1` | EBIT_TTM x (1 - effective tax rate) / average(invested capital); invested capital = total debt + total equity - cash | ratio | TTM at latest quarter | NM if pretax income <= 0 or average invested capital <= 0 | no |
| `debt_to_equity@1` | total_debt / equity attributable to parent | ratio | balance at latest quarter end | NM if equity <= 0 | no |
| `net_debt@1` | total_debt - cash and equivalents - short-term investments | money | balance at latest quarter end | — | no |
| `net_debt_to_ebitda@1` | net_debt / EBITDA_TTM | x | TTM at latest quarter | NM if EBITDA <= 0 | no |
| `interest_coverage@1` | EBIT_TTM / interest_expense_TTM | x | TTM at latest quarter | NM if interest expense <= 0 | no |
| `current_ratio@1` | current_assets / current_liabilities | x | balance at latest quarter end | NM if current liabilities <= 0 | no |
| `quick_ratio@1` | (cash + short-term investments + receivables) / current_liabilities | x | balance at latest quarter end | NM if current liabilities <= 0 | no |
| `market_cap@1` | price x shares outstanding (cover page, latest at the as-of time) | money | price time | — | yes |
| `ev_standard@1` | market cap + total debt + preferred + noncontrolling interest - cash - short-term investments | money | price time / latest balance | — | yes |
| `ev_with_operating_leases@1` | ev_standard + operating lease liabilities | money | price time / latest balance | — | yes |
| `ev_pension_ex_equity_method@1` | ev_standard - pension funded status (adds net unfunded liability) - equity-method investments | money | price time / latest balance | — | yes |
| `pe_ttm@1` | price / (net_income_TTM / diluted weighted-average shares) | x | TTM at latest quarter | NM if net income TTM <= 0 | yes |
| `pe_fy@1` | price / diluted EPS (latest fiscal year, as reported) | x | latest fiscal year | NM if EPS <= 0 | yes |
| `price_to_book@1` | market cap / equity attributable to parent | x | balance at latest quarter end | NM if equity <= 0 | yes |
| `price_to_sales@1` | market cap / revenue_TTM | x | TTM at latest quarter | NM if revenue <= 0 | yes |
| `ev_to_sales@1` | ev_standard / revenue_TTM | x | TTM at latest quarter | NM if revenue <= 0 | yes |
| `ev_to_ebitda@1` | ev_standard / EBITDA_TTM | x | TTM at latest quarter | NM if EBITDA <= 0 | yes |
| `ev_to_ebit@1` | ev_standard / EBIT_TTM | x | TTM at latest quarter | NM if EBIT <= 0 | yes |
| `fcf_yield@1` | FCF_TTM / market cap | ratio | TTM at latest quarter | — | yes |
| `dividend_yield@1` | dividends paid_TTM / market cap (cash basis) | ratio | TTM at latest quarter | — | yes |

## Peers (`fundamentals/peers.py`)

**Rule `peers_sic4@1`.** Peers are issuers in the research store with the same 4-digit SEC SIC as the target, ordered
by CIK.

- With market caps for the target and peers, the 0.25x-4x size band is applied. Otherwise it is reported as not
  applied.
- The liquidity filter is not applied, because there is no traded-value data.
- An explicit owner list (`owner_list@1`) replaces the rule and is echoed back.

**Point-in-time caveats** are always returned:

- SIC is the current classification and has no history.
- Membership is limited to ingested issuers (survivorship).
- The classification snapshot may postdate `t`.

**Comps** use the same metrics as of `t` for every member.

- Every value carries its formula `id@version`, status, flags, input observation ids and source dates.
- A member with no SEC fact available at `t` (for example, an issuer registered later; membership uses today's SIC)
  is shown with `fundamentals.status = NO_FUNDAMENTALS_AT_AS_OF` and contributes nothing to the statistics.
- NM, INCOMPLETE and missing values are excluded and counted.
- Statistics are suppressed below three valid peers.
- Percentiles are PERCENTILE.INC, computed exactly.
- Forward multiples are absent because there is no licensed estimates feed (CAP-CO-08).

## DCF (`fundamentals/dcf.py`, `fundamentals/valuation.py`)

The model implements book eq. 17.1:
EV = sum FCFF_t/(1+WACC)^t + TV_T/(1+WACC)^T, with TV_T = FCFF_T+1/(WACC-g) and FCFF_T+1 = FCFF_T x (1+g) by default.
Its id is `dcf_fcff_gordon@1`.

**WACC (`wacc_capm@1`)** = w_e (r_f + beta x ERP + CRP) + w_d k_d (1 - tax).

- The weights must sum to 1. They come from owner input, or from market values given a price input.
- The tax rate defaults to the effective tax rate TTM, with its source shown.
- CRP defaults to 0 when not supplied, and its source then says so (it is not shown as an owner input).
- r_f can be read from the stored Treasury par curve at a chosen tenor, with its observation id. Its compounding
  convention is unverified (CAP-MAC-07).
- The computed WACC is rounded to 10 dp, and that displayed value is the one used.

**Guards.** The model refuses rather than computes when:

- WACC <= g;
- the discount-rate currency differs from the cash-flow currency, unless the owner overrides;
- the issuer is in the financial sector (SIC 6000-6799), unless the owner overrides.

Overrides are echoed in the output.

**Outputs:**

- **Equity bridge.** EV - total debt - preferred - NCI + cash + short-term investments. Missing components are listed,
  and the status is then `INCOMPLETE`.
- **Per-share value**, using diluted weighted-average shares.
- **Sensitivity surface.** A 5 x 5 grid of WACC +/- 1pp steps by g +/- 0.5pp steps. Cells with WACC <= g are marked
  invalid.
- **Scenario range.** The defaults are low, base and high (+/-1pp WACC, -/+0.5pp g, -/+10% FCFF). These are
  illustrative sensitivities, not forecasts.
- **Football-field data.** The 52-week range is `NOT_AVAILABLE` because no price history is held.

Forecasts come only from typed inputs: either explicit `fcff` values, or `fcff_growth` applied to FCFF TTM and
labelled as an owner assumption.

## Search, identity and filings

**Search** (`fundamentals/search.py`; CAP-INS-01, CAP-CO-01) looks only at local data. It covers:

- the latest SEC `company_tickers_exchange.json` snapshot received by `t`;
- ingested issuers, including name, former names and ticker aliases valid at `t`, which are never backdated;
- stored OpenFIGI outcomes;
- the core instrument master and its versioned provider mappings.

Ranking runs EXACT_ID, EXACT_TICKER, NAME_PREFIX, NAME_CONTAINS, then FORMER_NAME. `selected` is always null, because
the owner chooses. CUSIP- or SEDOL-shaped queries are refused (CAP-INS-03).

**Identity and filings** (`fundamentals/filings.py`) read the SEC submissions payload from the raw layer and verify
its SHA-256.

- Former names give the name in force at `t`.
- Tickers carry no dates in the source.
- Each filing accepted by `t` gets a filing-index link, a primary-document link, and 8-K Item 2.02 / 4.02 flags.
  CAP-CO-06 found that the submissions `acceptanceDateTime` runs later than true UTC on its samples, so this filter is
  conservative.

## Free official sources (`data/sources/`)

| Source | Registry (`config/providers/`) | Dataset | Availability rule | Rate limit |
|---|---|---|---|---|
| US Treasury daily par yield curve XML | `us-treasury.json` | `ust.par_yield_curve` (entity `UST-PAR-NOMINAL`, field `par_yield:<tenor>`, percent) | live capture: first seen; backfill: 23:59:59 America/New_York on the curve date, flagged | self-imposed 1 per 2 s (none published) |
| ECB EXR reference rates | `ecb.json` | `ecb.exr_reference` (entity `ECB-EXR-<CCY>-EUR`, `<CCY>/EUR` = CCY per 1 EUR) | live capture: first seen (never the 14:15 CET fixing time); backfill: 23:59:59 Europe/Berlin, flagged | self-imposed 1 per 2 s (none published) |
| OpenFIGI mapping | `openfigi.json` | `openfigi.mapping` (entity `OPENFIGI-JOB:<idType>:<idValue>`) | receipt time | 25 per 60 s, 10 jobs per request (250 / 100 with a key); strict sliding window; 429 waits retry-after; 413 splits |
| SEC company_tickers_exchange.json | `sec-edgar-reference.json` | `sec.company_tickers_exchange` (tables `ref_sec_ticker_snapshot`, `ref_sec_ticker`) | snapshot receipt time; no history before the first snapshot | SEC 10/s shared with `sec-edgar` |
| Owner price input | `owner-research-input.json` | `owner.price_input` | stated observation time | n/a (no network) |

### Capture semantics

These are sources without upstream vintages (`sources/common.py`).

- A value is written once.
- An identical re-fetch writes nothing.
- A changed value becomes a new publication, available at its own receipt time and flagged
  `revised_after_first_seen`. Earlier as-of reads keep the original.
- Backfilled history uses the end-of-observation-day bound so that backtests can use it. That bound is later than each
  source's published schedule (Treasury "usually ... by 6:00 PM Eastern"; ECB "around 16:00 CET"). This is a
  documented design choice; live captures use the exact first-seen time.

### Rights recorded in the registry entries

- **Treasury.** The feed page states no licence text. Cloud-model use and redistribution are withheld (status
  provisional).
- **ECB.** Cite the ECB, and state modifications: cross rates are labelled derived. The rates are "for information
  purposes only", so they are used for valuation and reporting, never for order pricing or funding. A cross rate lists
  the reference dates and observation ids of its inputs; one built from fixings of different dates is flagged
  `mixed_reference_dates`, and stale or previous-day inputs are flagged `stale_input:<CCY>` /
  `previous_fixing_input:<CCY>`.
- **OpenFIGI.** FIGIs are public domain.
- **SEC.** Public information.

### Identifier checks (`sources/identifiers.py`)

These run before any network call: ISIN (Luhn), CUSIP and SEDOL check digits, and FIGI format. The algorithms are as
recorded in CAP-INS-01 and CAP-INS-02 and still need verification against the standards. CUSIP and SEDOL inputs are
refused for OpenFIGI without a recorded licence.

### Commands

```bash
cd backend
PYTHONPATH=src python3 -m unknown.data.sec ingest --cik 320193
PYTHONPATH=src python3 -m unknown.data.sources.treasury ingest --month 202610
PYTHONPATH=src python3 -m unknown.data.sources.ecb ingest --start 2026-10-01
PYTHONPATH=src python3 -m unknown.data.sources.openfigi map --isin US0378331005
PYTHONPATH=src python3 -m unknown.data.sources.sec_tickers ingest
UNKNOWN_LIVE_SOURCES=1 PYTHONPATH=src python3 -m unittest tests.test_sources_live -v   # opt-in live smoke
```

### Live smoke run, 2026-10-22

Each test made one request with the owner-declared User-Agent and wrote to a temporary store.

- **Treasury:** month 202610 returned 14 curve dates (2026-10-01..2026-10-21), all 14 tenors present; 10Y = 5.13 on
  2026-10-21, matching CAP-MAC-07.
- **ECB:** `lastNObservations=1` returned 44 series. USD = 1.1205 on 2026-10-22. Discontinued series are included,
  so stale fixings are now flagged.
- **OpenFIGI:** one unauthenticated request for `US0378331005` returned 301 candidate listings
  (`MULTIPLE_CANDIDATES`). None was selected.
- **SEC tickers:** 10,443 rows covering 8,029 companies.

## API routes (route module `company`)

All routes are read-only:

- The research store is opened with `mode=ro`.
- No handler makes a network call.
- `as_of` is either an ISO date, meaning the end of that UTC day, or an ISO instant with a zone. It defaults to now.
- Missing data comes back as an explicit `UNAVAILABLE` or `REFUSED` reason.

| Path | Returns |
|---|---|
| `/api/company/search?q=&as_of=&limit=` | ranked candidates (issuers with listings, core instruments, FIGI listings), `selected: null`, per-source status and retrieval time |
| `/api/company/<cik>` or `/tearsheet` | typed blocks: identity, filings, statements (FY, Q, TTM), metrics, peers, description link, quote `NOT_CONNECTED`, events `NOT_LICENSED` |
| `/api/company/<cik>/filings?forms=&limit=` | identity plus filings accepted by `as_of` with SEC links and 8-K flags |
| `/api/company/<cik>/statements?basis=FY\|Q\|TTM&periods=&known_at=&history=` | standardized statements with per-cell lineage and coverage |
| `/api/company/<cik>/metrics?price=&price_currency=&price_source=&price_time=` | all metrics with status, formula and inputs |
| `/api/company/<cik>/metrics/<metric>` | show calculation: formula, expression, input cells with sources, source dates |
| `/api/company/metric-record/<metric_id>` | show calculation of a persisted metric (input observations) |
| `/api/company/<cik>/peers?peers=` | peer group with rule and caveats, comps table and statistics |
| `/api/company/<cik>/dcf?g=&fcff=\|fcff_growth=&years=&wacc=\|rf=\|rf_tenor=&beta=&erp=&kd=&tax=&we=&wd=` | DCF model with sourced inputs, sensitivity, scenarios, or `REFUSED` with the reason |
| `/api/macro?as_of=` | catalog of macro datasets available at `as_of` (counts, first/last date, newest availability time); later data is never counted |
| `/api/macro/yield-curve?date=` | Treasury par curve as of; missing tenors listed; previous-date fallback labelled; 10Y-2Y slope (derived, with its input observation ids) |
| `/api/macro/fx?date=&base=` | ECB reference rates as of, with stale flags; `base` adds derived cross rates via EUR |

## Known gaps and open questions

- **As-reported statements.** Not built (CAP-CO-04). These need each filing's own XBRL instance and presentation
  linkbase, including extension concepts and segments. Only standardized views from companyfacts exist.
- **Full-text search, 8-K alert feed, nightly index reconciliation** (CAP-CO-02). Not built; filings come from the
  stored submissions file only.
- **Financial-sector statements.** v1 does not map bank or insurer statements. Their EV metrics and DCF are suppressed.
- **Multi-class market cap.** Not built; the cover-page total is used when one exists.
- **FX conversion.** Not applied to multiples or statements (CAP-CO-05 FX policy pending).
- **Saved, versioned peer sets.** Not implemented; an explicit list is accepted per request.
- **Store API.** `pit.py` has no accessor for an observation by id, nor for a key's latest publication, so a few reads
  query `observation` directly. `derived_metric` has no quality-flag column. `entity_alias` cannot close an alias
  interval when a ticker moves (CAP-INS-06). These are reported to the store owner.
- **Treasury.** Feed-specific reuse terms are unreviewed (provisional rights). The compounding convention is
  unverified, so bootstrapping is blocked.
- **ECB.** History depth and `includeHistory` vintages are not loaded.
- **FRED/ALFRED, H.15, SOFR.** Not ingested.
