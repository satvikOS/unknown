# Fundamental, event and macro research {#fundamental-research}

Longer-horizon analysis is a natural place for cloud models because documents, economic context and company disclosures do not require a model call on every tick. The model's role is to extract evidence, propose hypotheses and identify missing information. Valuation, position sizing and execution still use explicit formulas and policies.

## Fundamental valuation

An enterprise discounted-cash-flow model uses

$$EV=\sum_{t=1}^{T}\frac{FCFF_t}{(1+WACC)^t}+\frac{TV_T}{(1+WACC)^T},\quad TV_T=\frac{FCFF_{T+1}}{WACC-g}. \tag{17.1}$$

The terminal-growth expression requires $WACC>g$ and compatible nominal/currency assumptions. Equity value then adjusts for net debt and other claims using a documented bridge. Per-share value uses a defensible diluted share count. Small changes in discount rate or terminal growth can dominate the result; show a sensitivity surface and scenario range rather than one apparently exact fair value.

A weighted cost of capital model may use $WACC=w_e k_e+w_d k_d(1-\tau)$ under its tax and capital-structure assumptions. A CAPM-style $k_e=r_f+\beta ERP$ is a modeling convention, not an observable truth. Currency, inflation and country-risk assumptions must be internally consistent. Distressed, financial and early-stage companies often need different valuation structures.

Ratios such as free-cash-flow yield, return on invested capital and leverage require consistent accounting definitions. Compare like-for-like periods and sectors. Separate reported values, normalized adjustments and model forecasts. An agent may suggest an adjustment, but it must attach source evidence and preserve the original figure.

## Event surprise

An earnings surprise can be standardized as $(actual-consensus)/scale$, where scale might be historical surprise volatility or another predeclared positive denominator. The consensus snapshot must precede the announcement and its definition must match the actual measure. GAAP versus adjusted EPS and fiscal-period mismatches can create false signals.

The event pipeline detects publication, resolves issuer and instrument, extracts typed facts, validates units, compares against a point-in-time baseline and publishes a versioned event feature. A model-generated summary alone does not trigger an order. Event freshness, source confidence, expected holding horizon and cost-adjusted evidence enter the approved strategy rule.

## Event study

Estimate expected return from a pre-event model, for example $R_{i,t}=\alpha_i+\beta_iR_{m,t}+\epsilon_{i,t}$. Abnormal return is actual minus model expectation; cumulative abnormal return sums over a defined event window. Avoid overlapping events, contaminated estimation periods and selection of only famous successes. Market-adjusted event studies are descriptive unless the trading rule and data availability are specified.

The opening gap after an overnight announcement is already part of the observed price. A strategy cannot capture it by buying at the previous close after reading the news. Model the first eligible execution after the actual information delay.

## Macro and cross-asset context

Track release calendars, surprise relative to contemporaneous expectations, revisions and policy regimes. Use historical vintages for revised series. [S03] A simple factor model relates returns to market, rates, credit, currency and commodity factors; estimated sensitivity is conditional on the sample. Economic narratives can change without a new causal mechanism, so evaluate forecast calibration and incremental predictive value.

A macro regime classifier may reduce exposure or choose among approved strategies. It cannot raise global risk limits. Require a stable definition of each regime, an abstention state and hysteresis to avoid rapid switching. The model should output probabilities and supporting observations rather than a categorical claim of certainty.

## Evidence graph

Represent claims as nodes linked to source passages, reported numbers, transformations and dependent forecasts. For example, a margin-improvement hypothesis links to a filing's segment table, a calculated historical margin series and a scenario assumption. The terminal lets the owner inspect each dependency. Contradictory sources are retained with timestamps and confidence, not resolved by whichever agent speaks last.

Cloud-provider processing rights must be checked for licensed news, transcripts and estimates. Some entitlements permit viewing but restrict bulk extraction or external-model use. Cache derived summaries only where allowed and preserve deletion obligations in the source registry.

## Acceptance criteria

Test decimal/unit extraction, negative values in parentheses, amended filings, fiscal-year shifts, duplicated news and delayed feeds. Evaluate factual extraction separately from return prediction. A model can accurately summarize a report while having no trading edge. Deploy only the part whose evidence supports its claimed capability.

