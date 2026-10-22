# Probability, estimation and calibrated uncertainty {#probability}

Trading decisions concern distributions of future outcomes conditional on information currently available. A narrative that sounds likely is not a probability estimate. Define the random variable, horizon, information set and loss function before fitting a model.

Let $\mathcal F_t$ denote available information at time $t$, and $R_{t,h}$ a return over horizon $h$. The forecasting target is a distribution $P(R_{t,h}\mid\mathcal F_t)$ or a specified functional such as its mean, quantile or sign probability. A prediction of the next trade direction is not automatically a prediction of a profitable round trip after costs.

## Expectation and variance

For outcomes $r_i$ with probabilities $p_i$,

$$\mu=\sum_i p_i r_i,\qquad \sigma^2=\sum_i p_i(r_i-\mu)^2. \tag{4.1}$$

Suppose a trade gains 2 dollars with probability 0.55 and loses 2 dollars otherwise. Gross expected PnL is 0.20 dollars. If round-trip costs are 0.25, expected net PnL is negative 0.05. A win rate above one half is therefore insufficient. The payoff distribution and costs matter together.

Sample mean uncertainty is often approximated by $s/\sqrt n$ for independent observations. Financial observations can be autocorrelated and overlapping, so the raw count of rows overstates independent evidence. A useful diagnostic approximation is

$$n_{\mathrm{eff}}\approx\frac{n}{1+2\sum_{k=1}^{K}\rho_k}. \tag{4.2}$$

This approximation needs a sensible truncation and stationarity assumptions. It is not a replacement for block-bootstrap analysis or a robust covariance estimator. A million overlapping one-minute labels can contain much less evidence than a million independent outcomes.

## Bayesian updating

For a Bernoulli event, a Beta prior with parameters $\alpha,\beta$ and $s$ successes in $n$ observations yields

$$p\mid D\sim\mathrm{Beta}(\alpha+s,\beta+n-s). \tag{4.3}$$

The posterior mean is $(\alpha+s)/(\alpha+\beta+n)$. A Beta(2,2) prior and six successes in ten observations yield Beta(8,6), with mean about 0.571. The prior represents assumptions and effective prior evidence; it is not free predictive accuracy. Trades selected adaptively and outcomes with dependence require more careful models.

For a normal observation with known noise variance $\sigma^2$ and a normal prior $\mu\sim N(\mu_0,\tau_0^2)$, independent observations with mean $\bar x$ give

$$\tau_n^{-2}=\tau_0^{-2}+n\sigma^{-2},\quad
\mu_n=\tau_n^2(\mu_0\tau_0^{-2}+n\bar x\sigma^{-2}). \tag{4.4}$$

The equation combines prior and observed information by precision. Known Gaussian noise and independence are strong assumptions. For market data, heavy tails, regime changes and selection can dominate the apparent precision. Cap stale evidence and validate whether the observation model is useful.

## Calibration and discrimination

A classifier is calibrated if events assigned probability 0.7 occur about 70% of the time in comparable evaluation groups. It can be calibrated yet uninformative by always predicting the unconditional frequency. Discrimination measures ranking ability; calibration measures probabilistic accuracy. Both matter, and neither alone establishes profitability.

For binary outcomes $y_i$ and predictions $p_i$, Brier score is

$$\mathrm{BS}=\frac1n\sum_i(p_i-y_i)^2. \tag{4.5}$$

Log loss is $-n^{-1}\sum_i[y_i\log p_i+(1-y_i)\log(1-p_i)]$. Use numerically safe clipping only as an explicitly documented implementation detail. Compare against a frequency baseline. Plot reliability by time, volatility regime and instrument liquidity, not only one aggregate chart.

## Hypothesis testing and selection

A p-value is not the probability that a strategy is true. It describes a statistic under a specified null and sampling procedure. Testing many strategies and reporting only the best changes the interpretation. Record every attempted feature set, parameter grid and selection decision. An untouched holdout must remain untouched after a disappointing result.

Bootstrap whole blocks or sessions when serial dependence matters. For comparing a candidate to a champion, resample their paired net outcomes on the same periods. Estimate a distribution of the difference, not two unrelated confidence intervals. Include fees and capacity constraints in both. The project promotion protocol uses these principles without claiming a universal sample-count threshold.

## Implementation contract

An uncertainty record includes target definition, horizon, observation count, effective-sample diagnostic, training interval, evaluation interval, calibration method, confidence interval method and data version. Model output that lacks these fields cannot be treated as quantified trading confidence.

Store research decimals at sufficient precision, but round order prices and cash only at execution/accounting boundaries. Test known synthetic distributions to verify the estimator. Then deliberately violate independence and introduce a regime shift to see whether the monitoring detects degraded calibration. Statistical competence includes knowing when the assumptions are no longer credible.
