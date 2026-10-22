# Multiasset mathematics and separate eligibility gates {#multiasset}

The instrument model anticipates many markets, but each asset class introduces distinct economic obligations. Stocks-first implementation is a sequencing choice consistent with the owner's priority. Enabling every instrument immediately would conflict with the requested debt avoidance and with unresolved account permissions.

## Options

For a European call under the Black-Scholes assumptions of lognormal diffusion, constant volatility/rates and continuous dividend yield $q_d$,

$$C=Se^{-q_dT}N(d_1)-Ke^{-rT}N(d_2),\quad d_1=\frac{\log(S/K)+(r-q_d+\sigma^2/2)T}{\sigma\sqrt T},\quad d_2=d_1-\sigma\sqrt T. \tag{21.1}$$

Put-call parity is $C-P=Se^{-q_dT}-Ke^{-rT}$ under compatible European assumptions. American exercise, discrete dividends, borrow constraints and market frictions require additional treatment. Implied volatility solves the pricing equation numerically; verify price bounds, monotonicity and convergence rather than returning a number for an impossible quote.

Delta, gamma and vega are derivatives with respect to underlying price and volatility. For a small move, $\Delta V\approx\Delta\Delta S+\tfrac12\Gamma(\Delta S)^2+Vega\Delta\sigma+Theta\Delta t$. Unit conventions matter: vega per volatility unit differs from vega per percentage point. Large moves need full revaluation, not a truncated Greek approximation.

A purchased option can expire worthless; exercise may create an underlying position and funding obligation. Short options can create much larger losses and assignment exposure. Even defined-risk spreads have execution, assignment and timing complications. The initial policy disables all options; later admission needs exercise/expiry controls, contract multipliers, settlement style and broker permissions.

## Futures

Futures P&L is quantity times contract multiplier times price change, with currency and quote conventions. Daily variation margin creates cash-flow obligations independent of a session's preferred horizon. A stop cannot guarantee that an adverse gap stays within posted margin. Expiry, first-notice date, delivery and roll rules are mandatory instrument metadata.

A continuous futures series is a research construction, not a tradable contract. Back-adjustment can distort absolute price levels. Store the actual contract and roll transaction costs. Initial futures trading is disabled because margin obligations conflict with the conservative starting mandate.

## Foreign exchange

An exchange rate is a price of one currency in another; record base/quote orientation and invert with appropriate bid/ask treatment. For a simple no-arbitrage forward relation under compatible funding assumptions, $F=S(1+r_dT)/(1+r_fT)$. Actual forwards incorporate market conventions and funding/basis effects. Spot conversion for funded equity purchases is different from leveraged speculative FX.

Eligibility depends on residence, broker and instrument. Indian resident foreign-exchange and overseas-margin restrictions require specific review. [S18] The system must not route through another jurisdiction to evade a restriction. Automatic currency borrowing remains disabled.

## Fixed income

For cash flows $CF_t$ and periodic yield $y$, $P=\sum_t CF_t/(1+y)^t$ in the simplified convention. Macaulay duration is the present-value-weighted average time; modified duration is $D_{Mac}/(1+y)$ for that periodic convention. Small yield changes satisfy

$$\Delta P/P\approx-D_{mod}\Delta y+\tfrac12 Convexity(\Delta y)^2. \tag{21.2}$$

Real instruments require day-count basis, compounding, accrued interest, clean/dirty price, calendars, calls, credit and liquidity. A single yield shift does not represent curve twists or credit-spread risk. Use key-rate and spread sensitivities where appropriate. Fixed-income execution may be quote-driven and unsuitable for equity-style order assumptions.

## Crypto and other products

Continuous venue hours do not imply continuous liquidity or safety. Crypto introduces venue custody, funding, stablecoin, chain, withdrawal and jurisdiction risks. Perpetual derivatives add liquidation and leverage mechanics. No crypto custody keys or withdrawal authority belong to research agents. Initial enablement is off until a separate account and risk contract is approved.

Leveraged/inverse ETFs, CFDs, structured products and prediction contracts cannot be treated as ordinary fully paid stocks. Product documentation, legal eligibility, path dependence and counterparty exposure must be reviewed individually. A unified symbol search should display unsupported products as research-only.

## Admission template

For every new class, complete pricing conventions, maximum-loss pathways, cash obligations, corporate/contract events, settlement, calendars, data rights, account permissions, tax records, execution semantics and stress tests. Extend the ledger and simulator before enabling orders. Multiasset breadth is earned by these capabilities, not by adding another broker API endpoint.

