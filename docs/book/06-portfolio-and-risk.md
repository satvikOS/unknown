# Portfolio construction and risk mathematics {#portfolio-risk}

Risk is a distribution of possible outcomes, not a single stop price. Separate the owner's protected wealth, the brokerage account, the amount authorized for this system, the amount assigned to a session, and the amount currently exposed. A session ledger is bookkeeping inside an account; it is not a legally isolated subaccount. The most reliable protection against an algorithm spending unrelated assets is to keep them outside its authorized account and remove borrowing permissions where the broker supports that restriction.

## Returns, exposures and concentration

For cash equity holdings with dollar weights $w$, expected period return vector $\mu$ and covariance matrix $\Sigma$, portfolio mean and variance are

$$\mu_p=w^\top\mu,\qquad \sigma_p^2=w^\top\Sigma w. \tag{6.1}$$

Weights use a consistent denominator: account equity or authorized trading capital, explicitly identified. A 10% position relative to a small session can still be negligible at account level; many concurrent sessions can reverse that relationship. Risk checks therefore apply at order, instrument, strategy, session, broker and consolidated-account levels. Correlated ETFs must not pass as independent exposures merely because their symbols differ.

A simple constrained allocation minimizes

$$\tfrac12 w^\top\Sigma w-\lambda\mu^\top w+\eta\lVert w-w_{old}\rVert_1 \tag{6.2}$$

subject to long-only weights, a cash reserve, position limits and turnover limits. The last term approximates proportional trading costs. A covariance estimate must be symmetric and positive semidefinite; shrink an unstable sample estimate toward a diagonal or factor model and disclose the shrinkage. Do not invert a nearly singular matrix without condition checks. A deterministic equal-weight or capped volatility allocation is a useful baseline against which a complicated optimizer must earn its place.

For marginal volatility contribution, $\partial\sigma_p/\partial w_i=(\Sigma w)_i/\sigma_p$. Component contribution is $w_i(\Sigma w)_i/\sigma_p$; these sum to $\sigma_p$ when it is nonzero. This decomposition exposes a portfolio dominated by a common factor. A negative contribution is possible for a hedge; it is not evidence that the instrument has no independent loss potential.

## Loss tails and path dependence

Define loss $L=-\Delta V$. Value-at-risk at confidence $\alpha$ is the loss quantile $\inf\{l:P(L\le l)\ge\alpha\}$. For continuous distributions, expected shortfall is the mean loss beyond that quantile. For empirical samples with atoms, use the quantile-integral definition

$$ES_\alpha=\frac{1}{1-\alpha}\int_\alpha^1 VaR_u\,du. \tag{6.3}$$

Neither statistic bounds the worst possible loss. Stress scenarios should include a large overnight equity gap, correlated selloff, stale quotes, venue halt, failed cancel, fee revision and currency move. Stress must revalue the actual instruments, including nonlinear options where relevant, rather than merely multiply normal volatility by an arbitrary number.

Let $H_t=\max_{u\le t}V_u$ after appropriate cash-flow adjustment. Drawdown is $D_t=1-V_t/H_t$. A drawdown trigger requests exposure reduction; it cannot guarantee execution at the threshold. Deposits must not reset the high-water mark in a way that hides losses. Use unitized equity or maintain an explicit external-flow-adjusted series.

## Position sizing

An educational stop-distance sizing rule is

$$q=\left\lfloor\min\left(\frac{B_{risk}}{d+c},\frac{C_{available}-f}{p_{limit}},q_{concentration},q_{liquidity}\right)\right\rfloor_{lot}. \tag{6.4}$$

Here $d$ is an adverse price distance per share, $c$ a conservative per-share cost allowance, $f$ a fixed fee allowance and $B_{risk}$ the planned loss budget. The loss budget is not a guaranteed maximum: a gap can exceed $d$. Quantity is rounded down to a valid lot. Fractional trading requires a separate quantity scale and verified broker support. If rounding yields zero, the correct output is no order.

Example: a $100 session with a $0.50 planned-risk allowance, a $0.20 distance and $0.05 cost allowance permits two shares by the risk formula. A $60 share price permits only one by cash. A $0.75 minimum commission might make the entire strategy economically unsuitable. The engine should reject infeasible small-capital requests; it must not add leverage to make an aspirational target possible.

## Growth objectives and ruin

For a binary independent bet with win probability $p$, loss probability $1-p$ and net win multiple $b$, maximizing expected log growth gives $f^*=p-(1-p)/b$. This Kelly expression assumes known probabilities, repeatable bets and the specified loss structure. Estimation error, correlated positions and unbounded losses invalidate a naive application. The initial system does not use full Kelly sizing. Fractional Kelly remains a research candidate subject to hard exposure ceilings.

Turning $100 into $5,000 requires a fiftyfold terminal value, or 4,900% net return. A target does not create a positive expected return. Repeatedly restarting losing sessions can consume the whole authorized account even if each session has a small stop. Aggregate daily and lifetime authorized-loss budgets must survive restarts, new sessions, software releases and calendar boundaries. A human may replenish or revise a budget; an agent may not replenish its own loss allowance.

## Proposed defaults and their status

The shipped configuration is simulation-only. Paper examples use a $100 seed, a $105 illustrative goal, a 2% session drawdown trigger and a 1% daily authorized-capital loss trigger. These are initial test settings, not calibrated investment recommendations. Live capital remains unset; no program should interpret an unset cap as infinity. Live settings require an explicit owner-approved account budget, fee schedule and instrument universe.

The initial live-eligible design excludes borrowing, short sales, derivatives, leveraged or inverse products, automatic currency borrowing and automatic transfers. It reserves fees, keeps a separate operational buffer and limits total session allocations. These restrictions reduce specific debt pathways; they do not establish zero risk or protect against every broker error, external charge or legal liability. The system's enforceable claim is narrower: under tested assumptions, it rejects its own unsupported exposure-increasing commands.

