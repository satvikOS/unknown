# Research design and backtesting {#research-validation}

A backtest is an experiment on a historical information set. Its purpose is to reject weak ideas and estimate uncertainty under explicit assumptions. It is not a certificate of future profitability. Start every experiment with a hypothesis, universe, information clock, execution rule, cost model, benchmark, parameter-search budget and rejection criterion. Record failed experiments as well as successful ones so that selection bias remains visible.

## Causal event simulation

The simulator processes ordered events: instrument changes, market data, strategy decisions, order transmission, acknowledgments, fills, fees and settlement. The strategy receives only data whose availability time precedes its decision. A bar's close cannot both generate a signal and supply a guaranteed fill at the same close. A market-on-close order requires the venue's submission deadline and a model of the auction; it is a separate strategy contract.

Each event has an exchange time if available, local receipt time, sequence identifier and stable replay order. Equal timestamps are not enough to establish causality. A simulation seed controls random latency and fill choices. The output includes a manifest hashing code, data, configuration, calendars and simulator version. Replaying the same manifest must reproduce the same economic events.

The execution model must match data granularity. Daily bars cannot justify millisecond fill precision. With only top-of-book data, queue position is unobserved; use pessimistic assumptions and sensitivity ranges. With order-book events, queue modeling still requires assumptions about hidden liquidity, priority rules and cancellations ahead. Treat a price touch as an opportunity, not an automatic full fill.

## Cost and capacity

Net P&L includes commissions, venue fees, spread, slippage, market impact, borrow where permitted, financing, currency conversion and applicable transaction taxes. Model research/cloud costs separately and then report an all-in economic result as well. A strategy earning $2 daily with $10 daily infrastructure cost is not economically successful merely because the broker statement is positive.

A simple impact sensitivity model is $I=Y\sigma\sqrt{Q/V}$, where $Q$ is trade size and $V$ relevant market volume. This empirical functional form is a scenario model, not a universal law. Estimate $Y$ using suitable execution data, constrain the regime where it is used, and test several values. Participation limits should use executable venue liquidity rather than misleading aggregated volume.

Compute implementation shortfall relative to a documented decision benchmark:

$$IS=s\left(\sum_j q_jp_j-Qp_0\right)+fees+opportunity\ cost, \tag{7.1}$$

where $s=+1$ for a buy and $-1$ for a sell. If some quantity is unfilled, define its opportunity-cost mark and report it separately. A broker's average fill price alone cannot measure an execution algorithm's total quality.

## Validation splits

Use chronological training, validation and untouched test periods. In walk-forward evaluation, train only on past data, choose parameters on an inner validation period, and evaluate on the next unseen segment. Purge overlapping label windows at boundaries; add an embargo when adjacent examples share future outcomes. Fit scaling, feature selection, imputation and universe selection only within the training fold.

Point-in-time constituent membership prevents survivorship bias. Corporate-action-adjusted histories must not leak future membership or unavailable fundamentals. Include delisted instruments and actual filing availability. A revised macro series should use its historical vintage. Historical data acquired today can still contain future knowledge relative to a historical decision.

## Metrics and uncertainty

Report net return, turnover, exposure, maximum drawdown, expected shortfall, hit rate, average win/loss, capacity, fill ratio, time in market and performance by regime. The sample Sharpe statistic is $\bar r/s_r$ per observation. Multiplication by $\sqrt{K}$ to annualize assumes an appropriate sampling structure; serial dependence changes the variance of aggregated returns. Show the convention and use block resampling when dependence matters.

The information ratio compares active returns with a benchmark. A long-only equity strategy should be evaluated against relevant passive exposure and a cash alternative, not only against zero. A high win rate can conceal rare severe losses. A high return can be explained by leverage or market beta. Regression attribution should include an intercept, relevant factors, robust uncertainty and an honest statement about sample size.

For candidate versus incumbent, resample paired return blocks and estimate the distribution of the difference in the chosen utility. Do not compare independent confidence intervals when both strategies traded the same dates. Selection across many candidates inflates apparent success; retain the number and dependence of trials. Deflated Sharpe and probability-of-overfitting methods can be additional diagnostics, but require their own assumptions and enough observations. [S24]

## Rejection protocol

Reject on causal leakage, missing costs, unresolved data rights, invalid instruments, unreconciled positions or insufficient evaluation history before discussing return. Then apply predeclared economic and risk thresholds. Stress fees, delay and fill assumptions. Check neighboring parameters: an isolated sharp optimum is suspicious. Compare against a simple baseline using the same data and execution model.

Paper trading evaluates integration, not historical edge alone. Its fills can differ materially from live execution; IBKR itself documents differences between paper and live simulation. [S07] Require clean event reconciliation, no duplicate submissions and controlled fault recovery before considering limited live exposure. A calendar deadline never substitutes for this evidence.

The experiment artifact contains hypothesis, manifest, search history, fold results, uncertainty, stress results, rejected alternatives and a signed promotion recommendation. If any component is missing, the terminal labels the candidate incomplete. A compelling AI narrative does not repair an incomplete experiment.

