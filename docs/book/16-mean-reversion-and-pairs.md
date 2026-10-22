# Mean reversion, pairs and statistical arbitrage {#mean-reversion}

Mean reversion requires a reason and evidence for a restoring relationship. A price that has fallen is not automatically cheap, and correlation does not imply a stationary spread. The initial cash-only product can research pairs without deploying a short leg. Tradable long-short versions require separate borrow, margin and jurisdiction gates.

## Spread construction

For log prices $x_t$ and $y_t$, estimate a hedge relationship on a training window,

$$y_t=a+\beta x_t+\epsilon_t. \tag{16.1}$$

Then define $s_t=y_t-\hat a-\hat\beta x_t$ using only past estimates. Ordinary regression on nonstationary prices can be spurious. A cointegration hypothesis requires appropriate residual testing with the correct critical values and specification, stability checks and out-of-sample economic validation. A high regression $R^2$ alone is irrelevant to tradability.

The hedge ratio's units matter. A coefficient between log prices is an elasticity; it is not automatically the number of shares to short. To offset small log-price movements, dollar exposures should reflect $\beta$, and share quantities depend on current prices and contract multipliers. Discrete lots leave residual exposure that must be included in risk.

## Entry, exit and hysteresis

Normalize the spread against a causal mean and standard deviation, $z_t=(s_t-\mu_{t-1})/\sigma_{t-1}$. A research rule enters when $|z_t|>z_{entry}$ and exits when $|z_t|<z_{exit}$ with $0\le z_{exit}<z_{entry}$. Hysteresis reduces churn. Add a maximum holding time, structural-break condition and portfolio risk limit. A larger deviation can indicate a broken relationship rather than a better opportunity.

For an OU model $ds=\kappa(\theta-s)dt+\sigma dW$, half-life is $\log 2/\kappa$ only when $\kappa>0$. Estimate on a fixed training policy, inspect uncertainty and compare estimated reversion time with transaction costs and borrow duration. If the fitted discrete AR coefficient lies outside the valid mean-reverting range, do not force it into a positive half-life.

## Neutrality and residual risk

Dollar neutrality, beta neutrality, sector neutrality and factor neutrality are different constraints. A portfolio can be dollar neutral and still carry market beta. With factor loadings matrix $B$, factor-neutral construction imposes $B^\top w\approx0$ subject to allowed positions and turnover. Estimated loadings are uncertain and change over time.

Borrow availability, recall, financing and corporate actions can dominate a statistically attractive spread. A pair's legs may trade on different calendars or currencies. A closed second venue can leave the first leg exposed. Model joint execution and failure, including one-leg fills and rejected hedges; do not assume simultaneous fills at two historical midpoints.

## Intraday reversion

A short-horizon candidate might use deviation from a causal volume-weighted reference, order-flow imbalance and a liquidity filter. Cumulative session VWAP at time $t$ is $\sum_{j\le t}p_jq_j/\sum_{j\le t}q_j$ for the defined eligible trades. Full-day VWAP is unavailable before the day ends. A backtest that trades against the final daily VWAP leaks future information.

Order-flow imbalance requires an explicit event definition. Changes at the best bid/ask can reflect new orders, executions or cancellations; a snapshot feed may not reveal the cause. Train on the exact representation available live. A model trained on full-depth event messages cannot be honestly deployed using intermittent top-of-book snapshots without reevaluation.

## Worked failure analysis

Suppose a pair enters at a two-standard-deviation spread and historical simulations show a $0.30 expected gross convergence per unit. Two-leg spread costs total $0.12, fees $0.06 and estimated impact $0.05, leaving $0.07 before borrow and model error. If one leg gaps $0.50 during a failed hedge, several average wins can disappear. The deployment criterion must evaluate tail exposure and execution reliability, not just the positive mean.

## Algorithm A16 and rejection criteria

At scheduled recalibration, estimate the relationship on past data, validate stationarity/stability diagnostics, freeze parameters and create a candidate artifact. During execution, calculate the spread from synchronized eligible observations, determine desired joint exposure, verify both legs' permissions and liquidity, reserve all required resources, and submit through an approved multi-leg coordinator. If a leg fails, invoke a predefined hedge-or-unwind policy with bounded authority.

Reject on unstable hedge coefficients, unexplained structural breaks, unavailable borrow, one-leg risk beyond limits or performance that vanishes under realistic latency. For the first release, retain this strategy family in research and use long-only candidates for any later eligible cash deployment.

