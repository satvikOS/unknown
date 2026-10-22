# ADR-0016: Bounded agents, model-provider adapters and spend control

Status: accepted (2026-10-22)

## Context

The owner wants specialised AI roles: data quality, document and news analysis, quantitative research, experiment execution, evaluation, portfolio analysis, reconciliation support and incident diagnosis. These roles route across OpenAI, xAI and other frontier providers, with no hard-coded model names or prices (AI-01..AI-06, AUT-04). The verified capability records (`model-providers`, `agent-operations`, 2026-10-22) establish the following:

- Model ids exist as lowercase API ids, for example `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra` and `grok-4.7`. Providers publish list endpoints, lifecycle fields and scheduled price changes with effective dates.
- Long-context bands change the rate. On xAI, every token of a request at or above 200k prompt tokens is billed at the higher rate.
- Retention, zero-data-retention and residency differ by provider and by model.

## Decision

1. **Agents are research-side processes.** They hold no broker credentials and no ledger write handle. They authenticate to the core socket with the *agent* token, which may assert only `AGENT`. Their single core write is a typed `submit_research_proposal`, which can never reserve, order or change policy (ADR-0007, ADR-0013).
2. **Job envelope.** Every job carries:
   - `job_id`, role and schema version;
   - input artifact hashes;
   - the allowed-tool list;
   - deadline, maximum turns and maximum tool calls;
   - retry budget, token cap and spend cap (exact decimal, currency stated);
   - the expected output schema;
   - a parent job id, with a depth limit of 1, so there is no unbounded spawning.

   The scheduler deduplicates equivalent jobs by input hash, caps concurrency and discards expired outputs.
3. **Provider adapters.** OpenAI, xAI, Anthropic, Google and Mistral sit behind one interface: `list_models`, `count_tokens`, `generate(structured)`, `batch` and `usage`. Model ids are discovered at runtime and diffed against the previous snapshot. New ids are `unreviewed` and cannot be routed until the owner approves them in the registry. Production roles pin specific ids, never moving aliases.
4. **Tariffs are effective-dated data.** Each tariff row records provider, model, tier, region, context band, input, output and cached rates, `effective_from`, source URL and retrieval date. Cost is computed exactly from the response's usage object. A request whose band rule is unrecorded is blocked rather than guessed.
5. **Spend control.**
   - Paid calls are disabled until the owner records a budget and enables a provider (AI-05).
   - Each call reserves its worst-case cost (prompt tokens times input rate, plus max output tokens times output rate) against role, job and daily caps *before* the network call.
   - Actual usage is reconciled afterwards, and the provider's own spend limits are set as a second barrier.
   - No agent can raise any cap; caps are owner policy.
6. **Data-use gate.** Content enters a model context only if the source dataset's registry entry grants `model_processing_cloud` (ADR-0015), and only if the provider's recorded retention and residency are acceptable under the owner's data policy.
7. **Untrusted input.** Retrieved documents are data. The model's output is validated against the schema locally. Cited spans must exist verbatim in the supplied evidence. Numbers are checked against typed facts where available, and claims without verified evidence are labelled unsupported. Injection, fabricated-evidence, malformed-output, outage and contradictory-recommendation cases are part of the evaluation suite.
8. **Routing.** Routing is by *measured* task quality, latency and cost on held-out task sets per role. Routing tables are owner-approved artifacts with a version. A model upgrade requires a regression run before it can replace a pinned id.
9. **No anthropomorphic stand-ins.** "Intuition" is implemented as calibrated learned estimators with uncertainty records (book Chapter 4). "Pressure" is implemented as stress scenarios and deadlines in evaluation (book Chapter 19).
