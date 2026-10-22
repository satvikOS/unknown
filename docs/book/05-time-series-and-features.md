# Time series, features and regime estimation {#time-series}

The feature pipeline transforms available events into a stable, versioned representation of market state. Every feature needs a formula, units, sampling rule, warm-up condition, missing-data policy and availability timestamp. A feature that cannot be reproduced from raw inputs cannot support an auditable decision.

## Returns and aggregation

Simple return is $r_t=P_t/P_{t-1}-1$; log return is $g_t=\log(P_t/P_{t-1})$. Log returns add over time, while portfolio cash PnL must still use actual quantities and prices. Use total-return prices when the research question includes distributions. Use unadjusted execution prices for order and ledger reconstruction.

Calendar bars, volume bars and event-time samples answer different questions. Calendar bars ease alignment but mix active and inactive periods. Volume bars adapt sampling to activity but can create irregular time intervals. Record the bar construction version and how incomplete bars are treated. A strategy cannot use the final high or close of a bar before the bar is complete.

## Online moments

Welford's update avoids subtracting two large nearly equal sums. For a new observation $x_n$:

$$d=x_n-\mu_{n-1},\quad \mu_n=\mu_{n-1}+d/n,\quad
M_{2,n}=M_{2,n-1}+d(x_n-\mu_n). \tag{5.1}$$

Sample variance is $M_{2,n}/(n-1)$ for $n>1$. This formula describes an expanding sample, not a rolling window. A rolling implementation needs removals or an appropriate data structure and its own stability tests. Reject non-finite inputs and define behavior during warm-up.

For exponentially weighted estimates,

$$\hat\mu_t=(1-\alpha)\hat\mu_{t-1}+\alpha x_t,\qquad
\hat v_t=\lambda\hat v_{t-1}+(1-\lambda)r_t^2. \tag{5.2}$$

The second formula is an EWMA of squared returns with a zero-mean approximation. Its sampling interval and initialization must be explicit. With half-life $H$ observations, use $\lambda=2^{-1/H}$. Irregularly timed observations can use elapsed-time decay. Do not interpret a volatility estimate from one-second samples as an annual value without the correct time scaling and assumptions.

## Standardization and leakage

Define a causal standardized feature using estimates available before the prediction:

$$z_t=\frac{x_t-\hat\mu_{t-1}}{\max(\hat\sigma_{t-1},\epsilon)}. \tag{5.3}$$

The positive floor prevents division by a nearly zero scale. Fit normalization on training data only, then carry the fitted transformation into validation and test. Winsorization thresholds also belong to training. A global scaler fitted across the entire dataset leaks future distributional information.

## Mean reversion and state estimation

An Ornstein-Uhlenbeck research model is $dX_t=\kappa(\theta-X_t)dt+\sigma dW_t$. For interval $\Delta$, its conditional mean is $\theta+(X_t-\theta)e^{-\kappa\Delta}$, and variance is $\sigma^2(1-e^{-2\kappa\Delta})/(2\kappa)$ for $\kappa>0$. Its half-life is $\log 2/\kappa$. This is a model for a suitable stationary quantity, not a blanket model of stock prices.

Discretize to $X_{t+1}=a+bX_t+\varepsilon_t$. If $0<b<1$, estimate $\kappa=-\log b/\Delta$ and $\theta=a/(1-b)$. Values outside that interval do not justify pretending a mean-reverting fit exists. Structural breaks and residual diagnostics matter. Pair-trading residuals need separate checks for stable relationships and executable hedges.

A linear state-space model separates hidden state from noisy observation:

$$x_t=Fx_{t-1}+w_t,\qquad y_t=Hx_t+v_t. \tag{5.4}$$

The Kalman prediction is $\hat x^-_t=F\hat x_{t-1}$ and $P^-_t=FP_{t-1}F^T+Q$. Update with $K_t=P^-_tH^T(HP^-_tH^T+R)^{-1}$, $\hat x_t=\hat x^-_t+K_t(y_t-H\hat x^-_t)$. Use a numerically stable covariance update such as the Joseph form. Covariance matrices must remain symmetric and positive semidefinite within numerical tolerance. The linear-Gaussian assumptions are explicit limitations.

## Regimes and drift

Begin with interpretable regime features: spread percentile, realized volatility, trading activity, price trend, market session and data quality. An HMM is an optional research extension with transition probabilities and state-conditional observations. A hidden state label is not a guaranteed economic interpretation. Refit only under the authorized research process; do not change a live strategy's parameters silently.

Drift monitoring compares current feature distributions, forecast residuals and realized costs with training/evaluation references. A distribution change can be harmless, while a small change in a critical feature can be damaging. Combine statistical alerts with strategy-specific operating envelopes. Drift can disable entries or move a candidate to research; it cannot automatically enlarge risk.

## Practical verification

Feed a known sequence into batch and streaming implementations and compare outputs. Shift publication times to confirm features move only when information becomes available. Replay a split, an empty interval and a stale quote. Record warm-up status explicitly. The strategy must reject an input vector whose feature version or validity window differs from the approved model contract.
