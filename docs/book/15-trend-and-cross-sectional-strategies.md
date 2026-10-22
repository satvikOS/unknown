# Trend, momentum and cross-sectional strategies {#trend-strategies}

This and the following strategy chapters define research candidates, not private firm algorithms or recommendations to trade them. Each candidate needs an information set, signal equation, position rule, execution rule, cost model, invalidation condition and evidence manifest. A formula without those surrounding contracts is not an implementable strategy.

## Time-series trend

Let $r_t=\log(P_t/P_{t-1})$ on causally adjusted observations. A simple trend score is the difference between fast and slow exponentially weighted log-price means, normalized by a volatility estimate:

$$z_t=\frac{EMA_f(\log P)_t-EMA_s(\log P)_t}{\hat\sigma_t\sqrt{h}+\epsilon}. \tag{15.1}$$

The denominator is a chosen normalization over horizon $h$, not a claim that price differences are normally distributed. Parameters $f<s$, $h$ and $\epsilon$ are fixed within a research trial. A long-only mapping uses $w_t=\min(w_{max},k\max(0,z_t))$, with a no-trade band around current weight to reduce turnover. Execute after the observation becomes available.

An alternative breakout uses the maximum high over the preceding $n$ completed bars, explicitly excluding the current bar. Enter only when a valid subsequent price exceeds that level and expected costs fit the edge model. Exit on a lower trailing threshold, time limit or risk policy. A gap through the entry level is filled at a modeled executable price, not the historical threshold.

Trend strategies can benefit from persistent moves and suffer repeated reversals in choppy markets. Test behavior by volatility, market direction and liquidity. Do not disable a difficult historical regime after observing losses unless that regime rule is itself trained and evaluated causally.

## Cross-sectional momentum

At rebalance date $t$, define a past-return score over a documented interval, optionally skipping the most recent period:

$$m_{i,t}=\log P_{i,t-a}-\log P_{i,t-b},\qquad b>a\ge0. \tag{15.2}$$

Rank only instruments eligible at that date. Winsorize using training-defined rules and neutralize against approved sector or size exposures if the hypothesis requires it. A long-only portfolio can select the highest-ranked eligible names and hold cash when risk limits bind. A long-short academic factor is not directly deployable when shorting is disabled.

Normalize weights with explicit liquidity and concentration caps. For inverse-volatility weights, $\tilde w_i=1/\max(\hat\sigma_i,\sigma_{floor})$ and then project onto the feasible allocation set. Missing or suspiciously tiny volatility must not produce an enormous weight. Include delistings, membership changes, rebalancing costs and taxes in the appropriate evaluation layer.

## Supervised return prediction

For feature vector $x_t$, a linear baseline minimizes

$$\sum_t(y_t-\beta_0-x_t^\top\beta)^2+\lambda\lVert\beta\rVert_2^2. \tag{15.3}$$

The label $y_t$ should correspond to an executable future return horizon after observation delay. Fit standardization only on training data. Ridge regression provides a stable baseline; tree ensembles or neural models must improve out-of-sample utility under identical costs, not merely training accuracy.

Map forecasts into decisions using uncertainty and costs. One rule enters only when a lower confidence bound on expected net return exceeds a predeclared hurdle. Forecast intervals need calibration on unseen data. A model's self-reported verbal confidence is not that interval.

## Worked candidate specification

Candidate `TREND_DAILY_V1` uses a point-in-time universe of liquid unleveraged equities/ETFs, completed daily bars, a fixed fast/slow EMA pair and a long-only capped allocation. It rebalances no more than once per eligible session, uses price-bounded orders and leaves unallocated cash idle. The trial manifest records parameter candidates before evaluation. It compares against buy-and-hold and a volatility-capped passive baseline.

Reject the candidate if it relies on same-close fills, disappears after modest fee increases, concentrates performance in a handful of unreproducible events or breaches approved drawdown stress limits. A daily strategy needs enough history across regimes; two weeks of paper operation validates plumbing, not its long-term economics.

## Implementation algorithm A15

On each eligible completed observation: validate source freshness and corporate-action version; update causal features; compute the approved model score; map score to desired portfolio exposure; subtract current and pending exposure; apply no-trade and minimum-size rules; create a typed intent with expiry; submit to the common risk pipeline. The strategy never sends a broker command directly. Record a decision event even when the result is no trade so that missed opportunities and restraint can be audited.

