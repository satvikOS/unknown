# Portfolio and risk analytics

Package `backend/src/unknown/analytics/` and route module `backend/src/unknown/api/routes/portfolio.py`. Capability records CAP-PORT-01 to CAP-PORT-13 (`docs/capabilities/register.json`). Book Chapters 6, 9, 10 and 26. ADR-0006.

Status words follow ADR-0001. Everything here is `tested` with offline deterministic tests, at most. Nothing is `paper-verified`. Every test value is a synthetic fixture produced through the real core (`tests/support.Harness`); none of it is market data.

## What it does

| Capability | What is built | Status | Evidence |
|---|---|---|---|
| CAP-PORT-01 holdings, cash ladder, NAV | `holdings.py`: positions by owner (policy bucket) and instrument, settled and unsettled quantity, reserved quantity, marks with basis, source and age; per-owner and per-scope NAV (`nav_fresh`, `nav_marked`); per-currency cash ladder; weights against named denominators; optional reporting FX | tested | `tests/test_analytics_holdings.py` (14), `tests/test_analytics_review.py` |
| CAP-PORT-01/04 valuation history | `snapshots.py`: end-of-day snapshotter into `<UNKNOWN_HOME>/research/portfolio.sqlite`, with the quotes and marks used, the positions valued and the external flows known | tested | `tests/test_analytics_snapshots.py` (6) |
| CAP-PORT-02 daily P&L (partial) | `pnl.py`: realized, estimated and final fees, other income and expense codes, change in unrealized, with an exact identity against NAV change minus flows | tested | `tests/test_analytics_pnl.py` (2) |
| CAP-PORT-03 exposure, concentration | `exposure.py`: long, short, gross and net by instrument, owner, SEC SIC division and currency; HHI, effective N, top-k; explicit `unknown` bucket; coverage | tested | `tests/test_analytics_exposure.py` (7) |
| CAP-PORT-04 TWR and MWR | `performance.py`, `flows.py`: daily-linked TWR with start-of-day and end-of-day flow conventions, Modified Dietz, IRR, unit value; flows are external only | tested | `tests/test_analytics_performance.py` (17), `tests/test_analytics_review.py` |
| CAP-PORT-05 ex-post statistics (partial) | volatility, Sharpe, Sortino (two forms), beta, correlation, tracking error, information ratio, alpha, drawdown with recovery, n_eff and Sharpe standard error | tested | same file |
| CAP-PORT-10 VaR and ES (historical only) | `var.py`: full-revaluation historical simulation, empirical VaR, quantile-integral ES (eq. 6.3) and the IBKR tail-mean form, overlapping multi-day horizons, Kupiec backtest | tested | `tests/test_analytics_var.py` (8), `tests/test_analytics_review.py` |
| CAP-PORT-11 stress (operational subset) | `stress.py`: overnight gap, correlated selloff, stale quote, fee revision, each followed by the core's own reaction on a private copy | tested | `tests/test_analytics_stress.py` (6), `tests/test_analytics_review.py` |
| CAP-PORT-12 what-if | `whatif.py`: dry run of an intent through the core command path on an in-memory copy; cash, quantity, exposure and concentration impact | tested | `tests/test_analytics_whatif.py` (9) |
| CAP-PORT-13 allocation (subset) | `allocation.py`: equal weight, inverse volatility with floors and caps, Ledoit-Wolf shrinkage, eq. 6.2 solver with turnover limit, lot rounding, trade-list proposals | tested | `tests/test_analytics_allocation.py` (11), `tests/test_analytics_review.py` |
| Read-only API | `api/routes/portfolio.py` | tested at handler level | `tests/test_analytics_routes.py` (5) |

The route module is not yet listed in `api/routes/__init__.py`. Adding `"portfolio"` to `MODULES` turns the routes on.

## Not built

- CAP-PORT-06 (benchmark registry), CAP-PORT-07 (Brinson attribution), CAP-PORT-08 (factor risk model and ex-ante decomposition) and CAP-PORT-09 (returns-based factor attribution). Each needs a licensed or registered data source first.
- CAP-PORT-02: FX translation P&L, corporate actions (LED-09 is still `proposed` in the core), FIFO versus specific-ID comparison, and broker-versus-ledger P&L reason codes.
- CAP-PORT-05: benchmark-relative statistics need a benchmark series. The functions exist and are tested on published examples, but no route feeds them. No risk-free series is configured, so Sharpe is reported as excess over zero and says so.
- CAP-PORT-10: parametric and Monte Carlo VaR, and component VaR.
- CAP-PORT-11: historical replays, regulator scenarios (Fed, EBA), venue halt, failed cancel and currency move.
- CAP-PORT-13: sector and turnover-band caps per group, tracking-error minimisation, and allocation-goal drift.

## Data flow and authority

```
core SQLite (read-only: store.db.connect(readonly=True))  ──►  analytics  ──►  JSON (routes)
core quote book (IPC 'quotes', supplied snapshot)       ──►      │
                                                                   └──► research SQLite portfolio.sqlite (snapshots only)
research store research.sqlite (read-only, PIT aliases)  ──►  sector classification, optional prices
```

- The analytics never write the core database. Every handler uses the request's read-only connection. Tests hash every core table before and after each what-if, stress scenario, snapshot and proposal.
- The what-if and stress scenarios run core commands (`submit_intent`, `valuation`, `broker_commission`) on a private in-memory copy, made with SQLite's online backup from the read-only connection. The decision and the reservation therefore come from the code the live gate runs. The copy keeps the source's writer epoch and lease holder. A shared-code test takes 160 randomized steps through real ledger states (intents, partial fills, quote changes, clock moves). It compares 80 dry runs with the core's actual decision, check by check and hold by hold. Those decisions cover APPROVED, CASH, OWNED_QUANTITY, QUOTE_FRESH, PRICE_COLLAR and MAX_ORDER_NOTIONAL.
- A proposal or what-if result can never become an order. A real order is a new intent through the core, and it passes the risk gate again.

## Conventions

**Money is exact.** Balances, market values, NAV, flows, P&L, notionals and fees are `Fixed` decimals with explicit currency and ledger scale. Market value is `q × mark × multiplier`, rounded toward minus infinity at the ledger scale, which is the same rounding the core valuation uses. Returns, volatilities, VaR and weights are Python floats. They are computed from exact decimals and never post money.

**Marks.** The mark is the bid by default. Mid and last are also available, and the basis is recorded.
- A mark is fresh when its age is within the freshness budget and the data is not delayed.
- For live read models the budget is the active policy's `quote_max_age_ms`.
- The end-of-day snapshotter uses 900,000 ms (15 minutes). This is an analytics convention and is recorded with every run; it is not a trading limit.
- A position without a usable quote is unpriced. It is never valued at zero, and every NAV that needs it is null.
- A stale mark appears in `nav_marked` (flagged) and never in `nav_fresh`.

**Point in time.**
- A quote received after the valuation instant is ignored.
- A flow is known from its event's `recorded_time_ns`.
- A snapshot run is visible at decision time t only if it was created at or before t. For each date, the latest visible run wins, so a later revision never leaks backwards.
- A back-dated valuation is refused if core events exist after the requested instant, because projections cannot reconstruct earlier balances.
- Sector aliases must be valid at the decision instant.
- VaR forecasts in the backtest use only the positions and marks visible at the prior valuation.

**Scopes.**
- `account`: every owner.
- `system`: `unallocated`, `buffer`, `session:*` and `protected:*`.
- `owner:<owner>`: one owner. `session:<id>` and the other owner names are accepted as shorthands.

**External flows (GIPS separation of capital from performance).** A flow is the net posting to value codes on in-scope owners from `OWNER_TRANSFER`, `EXTERNAL_FLOW` and `OPENING` entries. What counts as a flow therefore depends on the scope:

| Scope | Counts as a flow | Does not count |
|---|---|---|
| `system` | Capital authorization and deauthorization | Session funding, protection |
| a session | Funding, protection, closure | — |
| `account` | Deposits, withdrawals, the opening import | — |

Trading P&L, fees, income and settlement are never flows.

**TWR.**
- Sub-period returns link consecutive complete snapshots.
- Start-of-day: `r = EMV/(BMV+CF) − 1`. This is the only convention that reproduces IBKR's worked example. End-of-day: `r = (EMV−CF)/BMV − 1`.
- Flows between snapshots come from cumulative flow differences, so a flow is counted once even across a gap or a revision.
- An undefined sub-period before the first defined one is treated as pre-inception, and one after the last as post-termination.
- Inside the period, a sub-period with no capital at risk (the bucket was emptied by an outflow and refunded later) links as a 0 return and is labelled. Any other undefined sub-period breaks the chain, and the result is null with a reason.
- Statistics count the sub-periods that span skipped incomplete valuations (`subperiods_spanning_gaps`), because each counts as one period while carrying several days of variance.

**MWR.**
- Modified Dietz uses day weights.
- The IRR is found by bisection after a sign-change scan. Multiple roots are reported, not guessed. A root lying exactly on the scan grid (r = 0 for a period with no gain or loss) counts once.
- Periods shorter than one calendar year (end before start + 1 year, so 365 days inside a leap year is not a year) are never annualized (GIPS 2.A.12). The period MWR is shown instead.

**Drawdown.** Drawdown is measured on the flow-adjusted unit-value series (U₀ = 1000), so a deposit cannot hide a loss (book Ch. 6).

**Statistics.**
- Standard deviations use n−1.
- Annualization uses n = 252 by default, or 360 in the IBKR daily preset.
- Ratios need at least `min_obs` (default 20) sub-period returns. Below that they report insufficient data.
- `n_eff = n / (1 + 2 Σ (1−k/n) ρ_k)` over positive autocorrelations, up to min(20, n/4) lags. Sharpe standard errors (Lo 2002, iid) are given with n and with n_eff.

**VaR and ES.**
- VaR is `L_(⌈αN⌉)` on ascending losses.
- ES is `(1/(1−α))[(k/N − α)L_(k) + (1/N)Σ_{j>k} L_(j)]`, which is eq. 6.3 evaluated exactly for an empirical distribution with ties.
- The IBKR "average of losses ≥ VaR" form is reported separately. It differs when the distribution has atoms.
- Kupiec: `LR = −2 ln[(1−p)^{T−x} p^x] + 2 ln[(1−x/T)^{T−x}(x/T)^x]`, with p-value `erfc(√(LR/2))`.
- Scenario returns are fresh snapshot marks. Stale marks are excluded, because repeating a stale price would manufacture zero returns.
- A VaR computed on only the positions that have history says so (`coverage_complete`, `partial_note`) besides listing the uncovered ones.
- Backtest forecasts are ex ante: the forecast for day t uses the latest run of day t−1 that existed before day t was valued. A revision of t−1 recorded later never moves its cutoff forward; a prior day recorded only afterwards gives no forecast and is listed as skipped.

**Stress.**
- Shocked prices are exact, `p(1+s)`, with no tick rounding. The analytic P&L and the core's reaction therefore use the same prices.
- Thresholds (session drawdown trigger, session max loss, daily loss trigger, cumulative loss allowance) are reported as crossed, with the loss beyond them. The loss is never capped, because a trigger only requests reduction.
- Account loss budgets are measured as the core measures them: system P&L at the current marks (every system bucket minus authorized capital) plus the scenario P&L, a high-water mark of max(stored, after), and the core's daily window. If the core has not valued yet on the scenario's risk day, the shocked valuation is the first of that day and the daily loss is 0. The loss since the last core valuation is shown separately as what a window starting there would measure.
- In the gap scenario, resting buy limits fill at their limit through the gap. Resting sell limits fill at their limit when the gapped bid reaches them (the sold shares earn the limit, not the gapped bid) and do not fill when it is below.

**Allocation.**
- `0.5 w'Σw − λμ'w + η‖w − w_old‖₁` subject to 0 ≤ w ≤ u and Σw = 1 − c.
- The solver is pairwise coordinate descent with maximal-violating-pair selection and exact piecewise-quadratic steps. It stops when the KKT violation is at most 1e-12.
- A turnover limit τ is enforced by raising η, found by bisection. This is the Lagrange multiplier of the L1 constraint.
- Without an approved, named expected-return artifact, λμ'w is zero and the result is labelled minimum variance.
- Inverse volatility is `w_i = clip(k/σ_i, floor, cap)` with k solved exactly so the weights fill the budget.
- The universe is the base-currency instruments; others are listed under `excluded_from_universe`. Current weights add up every owner in the scope that holds an instrument.
- Trade lists round down to whole lots and re-verify the cash reserve, caps, long-only and funding after fees. Unsettled sale proceeds fund nothing. A holding the method did not weight (no return history, another currency, no mark) is left unchanged and listed under `left_unchanged`; missing data never becomes a proposal to sell. Sizing at a stale mark is listed under `stale_sizing_marks`.

## API (read-only)

All endpoints are `GET /api/portfolio/...` with `scope=account|system|owner:<owner>|session:<id>`. Each response carries `lineage` (formula ids such as `port.nav@1`, inputs, units, source dates and missing reasons) or an explicit `available: false` with a reason. Fixture data is flagged (`fixture_data`, `fixture_note`).

`holdings`, `exposure`, `snapshots`, `performance`, `pnl`, `var`, `var/backtest`, `stress/<overnight_gap|correlated_selloff|stale_quote|fee_revision>`, `whatif?session=&instrument=&side=&quantity=&limit=`, `allocation`, `formulas`.

## Running

```bash
cd backend
PYTHONPATH=src python3 -m unittest tests.test_analytics_fixtures tests.test_analytics_holdings tests.test_analytics_snapshots \
  tests.test_analytics_pnl tests.test_analytics_performance tests.test_analytics_exposure tests.test_analytics_var \
  tests.test_analytics_whatif tests.test_analytics_stress tests.test_analytics_allocation tests.test_analytics_routes \
  tests.test_analytics_review -v
# end-of-day snapshot of a running simulated desk (FIXTURE market):
PYTHONPATH=src python3 -m unknown.analytics.snapshots record --account SIM-1 --home ~/.unknown-sim
```

Nothing schedules the snapshotter yet. Until it runs daily after the close, performance, P&L, VaR and allocation report "no valuation snapshots yet".

## Known limits and open questions

- **Multi-currency.** Each currency is valued and risk-measured separately. Base-currency totals appear only when the caller supplies FX rates. There is no FX scenario set for an aggregate VaR.
- **Sector.** The SIC code comes from the EDGAR submissions feed and is the issuer's current code, not a point-in-time history. Fixture instruments are always `unknown`.
- **Cost.** The what-if copies the whole core database (online backup) per call, and the core verifies the full ledger per command. Both grow with history. CAP-PORT-12's 250 ms p95 target is not yet benchmarked.
- **Store internals.** The sandbox builds a `Store` facade over the in-memory copy by setting the store's attributes. A small core API such as `Store.attach(conn, epoch, holder)` would make this explicit.
- **ES default.** IBKR wording or eq. 6.3 for display? Both are shown (CAP-PORT-10 open question).
- **Flow convention.** Start-of-day or end-of-day for comparison with live PortfolioAnalyst? Both are implemented; start-of-day is the default (CAP-PORT-04 open question).
- **Stale quotes block exits.** The stale-quote scenario shows that a stale quote fails `QUOTE_FRESH` for sells as well as buys, so risk-reducing exits are refused until a fresh quote arrives. This is a property of the current pretrade gate that the owner may want to review against ADR-0010.
- **Overnight gaps skip the daily loss trigger.** On a new risk day the core's valuation takes the first valuation's P&L as the day's baseline (`core/handlers/valuation.py`), so a loss that is already in the marks at that first valuation never counts against the daily loss trigger, only against the cumulative allowance.
  - Reproduced with the Harness: 8 FIXA bought at 10.00, valued on day 1, then bid 9.40 on day 2. The 4.80 loss exceeds the 3.00 trigger, yet `breach` stays null; only the session drawdown suspends the session.
  - The stress output carries this caveat on the daily-loss threshold. Whether the window should start at the previous close is an owner decision.

## Adversarial review (2026-10-22)

An independent review re-derived the formulas with known-answer cases and hunted for look-ahead, silent defaults and missing lineage. Each confirmed defect was fixed in this package and has a regression test in `tests/test_analytics_review.py`; every regression test fails on the pre-review code except the guard that two genuine IRR roots are still refused.

| Area | Defect | Fix |
|---|---|---|
| MWR | A flat period (IRR exactly 0, a grid point) was reported as "not unique" | Exact grid roots count once (`perf.irr@2`) |
| Annualization | 365 days inside a leap year counted as a full year | Calendar-year test `at_least_one_year` |
| TWR | An emptied and refunded bucket nulled the whole history | No-capital sub-periods link as 0 (`perf.twr@2`) |
| Allocation | Inverse volatility could return weights summing to less than the budget with status ok | Exact clip-and-scale solution (`alloc.inverse_vol@2`) |
| Allocation | Current weights kept only the last owner's value of an instrument; foreign-currency instruments shared the base-currency NAV; a holding without history got a sell proposal | Sum across owners; base-currency universe; `left_unchanged` |
| Allocation | Negative or non-finite η, λ, τ reached the solver | Refused (`ValueError`, 400 at the route) |
| VaR backtest | A late revision of day t−1 moved the forecast cutoff past day t (look-ahead) | Latest run that existed before day t was valued |
| Stress | Daily-loss threshold used an older risk day's start and the last core valuation's P&L | The core's window and current marks; checked against the core's own measurement on the copy |
| Stress | A resting sell filling through a gap up was marked at the gapped bid | Sold quantity earns its limit (`stress.gap_fills@2`) |
| Snapshots | Back-dating check, projections and flows were three separate reads of the live core | One read transaction |
| Marks | Rebuilding a quote dropped its `consolidation` label (wrong feed in snapshots and in the what-if decision record) | Label preserved |
| Holdings | A lot without a settlement record counted as settled | `settlement_unknown_quantity` |
| What-if | The sandbox took the first account's execution owner; a 5,000 ms freshness default was used without a policy | Owner of the evaluated account; impact refused with a reason |
| Exposure | A malformed `sec.cik` or an unreadable research store raised | Unclassified with a reason |
| Routes | `max_age_ms=0` was ignored; non-finite floats accepted; performance lineage always said `nav_fresh` | Honoured; refused; names the field used |
| Lineage | A caller-supplied risk-free rate was labelled "Rf = 0"; partial VaR coverage and gap-spanning returns were not stated | Labelled |

Still open after the review:
- VaR scenario returns use the sequence of snapshot dates. A day on which the snapshotter did not run makes the next "1-day" return span two days, and the backtest compares a 1-day VaR with a 2-day realized loss. A trading calendar check is needed.
- In-kind flows (the `OPENING` import of positions) are measured at cost, not market value. This matters only if an import happens after snapshots began.
- The `last` mark basis has no last-trade timestamp in the quote model, so its age is the quote's age.
- `var.research_price_history` reads `observation` rows; the IBKR bar store (`mkt_bar` in the same research database) is not read yet, so VaR history still comes only from the snapshots.
