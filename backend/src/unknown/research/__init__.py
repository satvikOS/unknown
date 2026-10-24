"""Research engine (increment I5; RES-01..RES-05, AUT-03; CAP-RES-04, -05, -07..-12).

Research code runs without production secrets, network authority or core
write access. It never posts money to the core ledger: the only path into the
core is ``core_replay``, which drives a *temporary* core database with a
simulated broker to validate the fast engine's accounting (RES-05 keeps
deterministic simulation, replay, shadow, paper and live distinct).

Modules
-------
common      errors, formula registry (formula id@version), stable hashing
spec        strict strategy specification schema (RES-01, BR-DAT-08)
bars        bars with availability times, point-in-time views, PIT-store bridge
costs       IBKR-like fee schedules, spread/slippage/impact cost model (CAP-RES-05)
engine      causal event-driven daily-bar backtester (CAP-RES-04)
sizing      algorithm A15 with the fee-inclusive risk term, shared with the strategy runner (BR-DAT-04)
execution   submission window and transmission-time limit, shared with the strategy runner (BR-DAT-19)
features    feature rows: validity window, warm-up state, enumerated missingness (BR-DAT-18)
stats       dependence-aware statistics, bootstrap, DSR/PSR, PBO (CAP-RES-08/09)
folds       walk-forward, purged k-fold and CPCV fold generators (CAP-RES-07)
strategies  TREND_DAILY_V1 and baselines (book ch. 15)
manifest    reproducibility manifests and replay comparison (CAP-RES-11)
registry    pre-registered experiments and the complete trial registry (CAP-RES-10)
promotion   artifact registry, status lifecycle, owner approvals, selection policy (CAP-RES-12, AUT-03)
core_replay second validation path through the real core (RES-05)

Statistical quantities (returns, volatility, Sharpe, VaR/ES, bootstrap) are
Python floats; every function that returns one documents its convention, and
no float is ever used to post money. Cash, positions, prices and fees are
exact ``Fixed`` values (ADR-0004)."""
