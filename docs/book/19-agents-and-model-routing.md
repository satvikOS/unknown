# Cloud agents, learned intuition and model routing {#agents}

Logic and intuition become concrete engineering roles. Logic consists of explicit constraints, arithmetic, state machines and verified permissions. Intuition means learned statistical patterns, contextual inference and uncertain forecasts. Learned outputs may inform an approved strategy, but deterministic controls decide whether its proposed action is admissible. Neither component can erase the other's evidence requirements.

## Job ownership

Use specialized workers with bounded inputs, outputs and deadlines. A source worker acquires licensed data. An extraction worker produces evidence-linked facts. A research worker proposes experiments. A quantitative evaluator runs fixed tests. A critic searches for leakage and failure cases. A portfolio planner proposes allocations. An execution service, not a language model, controls broker commands. A reconciliation service accounts for economic events. An operator assistant explains state and suggests owner actions.

Each job contains `job_id`, role, schema version, input artifact hashes, allowed tools, deadline, token/cost allowance, retry budget and expected output type. Workers cannot recursively create unbounded workers. The scheduler deduplicates equivalent jobs, caps concurrency and discards expired outputs. A news extraction finishing after its trading relevance window may remain useful for research but cannot revive an expired order intent.

## Typed output and permissions

Use provider-supported structured output where available, then validate locally against a strict schema. OpenAI and xAI document structured-output capabilities. [S27, S28] Schema compliance ensures shape, not factual accuracy or permission. Validate cited evidence, numeric ranges, instrument identity, timestamps and policy scope separately.

A useful forecast output includes horizon, distribution or quantiles, confidence-calibration version, evidence references, assumptions, invalidation conditions and an abstention reason. Do not accept a free-text instruction such as buy aggressively as an executable command. The planner can emit only a proposal type; the operating-system identity lacks broker credentials and order-service administrative access.

## Model choice and cost

Route by measured task performance. Cheap capable models can classify or extract well-structured text; stronger models can analyze conflicting filings or critique research. Escalate only when uncertainty or expected value justifies cost. Provider diversity can improve resilience, but several models agreeing on the same mistaken source is not independent evidence.

At the October 22, 2026 documentation snapshot, listed standard input/output prices per million tokens include GPT-6.1-sol at $2/$10, GPT-6-luna at $0.10/$0.50, GPT-6-astra at $10/$50 and Grok 4.7 at $2/$6. These are examples, not frozen procurement quotes; caching, service tiers, context length, tools and regional options can change the bill. [S25, S26] Keep model identifiers and tariffs in a versioned provider registry and recheck before enabling paid calls.

For $N$ jobs, average input $I$ and output $O$ tokens, approximate text cost is

$$C=N(Ip_{in}+Op_{out})/10^6+C_{tools}+C_{retries}. \tag{19.1}$$

At 1,000 jobs/day with 2,000 input and 200 output tokens each, the listed rates imply $6/day for the sol example and $5.20/day for the Grok example before extras. The application starts with paid calls disabled because the owner has not specified a service budget. A desire for best performance is not a numerical spending authorization.

## Learning and ensembles

A mixture combines calibrated predictors, for example $\hat\mu=\sum_i a_i\hat\mu_i$ with nonnegative weights summing to one. Estimate weights on past validation data and account for correlated errors. A gating model chooses weights from observable regime features; its own complexity consumes validation capacity. Compare against a single simple predictor and a fixed average.

For a Markov decision process, the Bellman optimality relation is $Q^*(s,a)=E[r+\gamma\max_{a'}Q^*(s',a')]$. In trading, the environment is partially observed and nonstationary, actions affect costs, and historical data do not reveal all counterfactual fills. Offline reinforcement learning therefore needs conservative evaluation and a realistic simulator. No live exploratory orders are allowed merely to train an agent. Production learning is limited to approved estimators and bounded parameter rules; generated code remains review-only.

## Stress without anthropomorphism

Bots do not need to feel fear or tension. Convert the request for pressure into adversarial evaluation: deadline misses, contradictory news, missing data, a crash during a fill, extreme volatility and model outages. Reward calibrated abstention, accurate accounting and robust net utility. A reward based only on reaching the session target encourages hidden risk and must not govern production authority.

## Acceptance criteria

Evaluate extraction accuracy, unsupported-claim rate, calibration, task completion, latency, cost and downstream incremental utility on a held-out corpus. Include prompt-injection documents and malformed tool outputs. A model upgrade requires regression evaluation and a recorded artifact version. If all model providers fail, the deterministic system still reconciles, stops and manages already-approved risk according to policy.

