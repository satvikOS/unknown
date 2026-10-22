# Market making and optimal execution {#market-making}

Market making seeks compensation for supplying liquidity while bearing inventory and adverse-selection risk. It is not a guaranteed collection of the spread. A passive order is more likely to execute when another participant finds it attractive, which can mean the market is about to move against it. Direct-access HFT assumptions do not automatically fit a retail broker path.

## Avellaneda-Stoikov model

In a stylized model, the midprice follows a Brownian process with volatility $\sigma$, inventory is $q$, risk aversion is $\gamma$ and remaining horizon is $\tau$. Exponential utility and exponential order-arrival intensity $\lambda(\delta)=A e^{-k\delta}$ lead to a widely used approximate reservation price and spread form:

$$r=s-q\gamma\sigma^2\tau,\qquad \Delta=\gamma\sigma^2\tau+\frac{2}{\gamma}\log(1+\gamma/k). \tag{18.1}$$

Approximate bid and ask are $r-\Delta/2$ and $r+\Delta/2$. This is a public academic model, not a reconstruction of a named firm's strategy. [S22] Price, inventory-unit and time conventions must make the exponents dimensionless. Contract size cannot be changed without adjusting the parameter interpretation.

For synthetic units, let $s=100$, $q=5$, $\gamma=0.1$, $\sigma=0.2$, $\tau=1$, $k=1$. Then $r=99.98$ and $\Delta\approx1.9102$. The large spread relative to many liquid equity spreads illustrates how arbitrary parameter choices can produce unrealistic quotes. Calibration is essential. Round outward to valid ticks and apply inventory, price and market-state restrictions.

The model omits important practical features: discrete ticks, queue priority, clustered order flow, hidden liquidity, fees, asymmetric information and latency. Estimate fill intensity from relevant data and validate out of sample. If fills are conditioned on future price moves, a simple independent Poisson model is misleading. A production research track must model adverse selection and queue position explicitly.

## Inventory control

Long inventory lowers the reservation price in the formula, encouraging sales and discouraging further buys. In practice, add hard inventory limits, concentration limits, quote-size limits and loss triggers. Never allow a soft utility penalty to replace a hard account constraint. If shorting is disabled, ask quantity cannot exceed owned unreserved shares; a symmetric two-sided model is no longer directly applicable.

Quotes must expire or be canceled when data becomes stale, a venue halts, a clock fault occurs or the strategy loses eligibility. Cancellation takes time and may fail. Stress the inventory acquired during that delay. A market-making backtest with instantaneous cancels and guaranteed queue fills is not credible.

## Optimal liquidation

For a parent order of $Q$ shares over $N$ intervals, let remaining inventory be $x_0=Q$, $x_N=0$ and child trades $n_k=x_{k-1}-x_k\ge0$. A simple discrete objective is

$$J=\sum_{k=1}^{N}\eta\frac{n_k^2}{\Delta t}+\lambda\sigma^2\sum_{k=1}^{N}x_k^2\Delta t, \tag{18.2}$$

with consistent units. The first term represents temporary impact; the second penalizes price-risk exposure while waiting. This simplified quadratic form captures a central trade-off in the public Almgren-Chriss framework. [S23] It omits permanent impact, spread, stochastic liquidity and constraints that a practical scheduler must add.

With $\lambda=0$ and constant $\eta$, equal child sizes minimize the convex impact term, yielding a TWAP-like schedule. Positive risk aversion favors earlier execution. Solve the constrained quadratic problem, then round to lots and verify that the rounded schedule still sums to $Q$. Replanning must account for actual fills rather than resetting the parent quantity.

## Participation and execution benchmarks

A participation schedule targets a fraction of observed eligible market volume, subject to price and size caps. It cannot know future volume. A VWAP-targeting schedule uses a forecast intraday volume profile and measures deviation afterward. A liquidity-seeking algorithm needs explicit venue permissions and order semantics. None should chase completion past an approved price bound merely to satisfy a schedule.

Compare schedules on implementation shortfall, residual exposure, completion rate and tail cost under the same simulated market paths. Include missed fills and cancel delay. The best schedule depends on alpha decay, urgency, liquidity and risk tolerance; there is no universally optimal execution algorithm.

## Deployment classification

The first release may use simple bounded execution schedules for approved long-only strategies. Two-sided market making, direct feeds and exchange-specific order types remain a separate advanced track. Their admission requires appropriate market access, data, permissions, capital and latency evidence. Public mathematical elegance does not substitute for those prerequisites.

