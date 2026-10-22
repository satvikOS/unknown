# Bounded agent layer (increment I6)

Status (ADR-0001 vocabulary): **implemented and tested offline**. No paid call has been made. Paid calls stay disabled until the owner writes `~/.unknown/config/agents.json` and approves the tariff rows and models involved. Decision record: [ADR-0016](../adr/0016-bounded-agents-and-model-providers.md). Evidence: the `model-providers` (CAP-MODEL-01..12) and `agent-operations` (CAP-AGT-01..14) records in [`docs/capabilities/register.json`](../capabilities/register.json), verified 2026-10-22.

## Architecture

Agents are research-side code under `backend/src/unknown/agents/`. They hold no broker credential and no ledger write handle. Their only write to the core is `submit_research_proposal`, sent with the agent token (role `AGENT`, ADR-0007/0013). A static test fails the build if the package imports broker, core, gateway, risk, policy, store, API or market code.

One job goes through the runner in this order (`runner.py`):

1. **Routability.** For each candidate `(provider, model)`:
   - the adapter has no unverified protocol facts for generation;
   - the catalog lists the model as approved for the role, present, active and fresh;
   - an owner-approved, verified tariff row exists.
2. **Data-use gate, counting call** (`gate.py`). Token counting sends the same content as generation, so it gets the same gate (CAP-MODEL-04).
3. **Token count.** From the provider endpoint when one is usable; otherwise a conservative local estimate, `ceil(utf8_bytes x factor) + overhead`.
4. **Context fit and token cap** (from the envelope).
5. **Worst case** (`tariffs.py`):
   - prompt tokens at the highest applicable input rate (including any cache-write multiplier the request can trigger), plus max output tokens at the output rate;
   - plus the costliest server-side fallback model when fallbacks are on.
6. **Data-use gate, generation call.** The decision is bound to the request hash.
7. **Reservation** (`budget.py`). An atomic check against every cap, then a durable insert, all before any network call.
8. **Provider call** through the adapter.
9. **Settlement.**
   - Usage is complete and every token class has a recorded rate: the row is settled at the exact cost.
   - Otherwise the full reservation stays consumed as `UNCONFIRMED` until reconciled.
10. **Output handling.**
    - Output is parsed with exact decimals and validated against the job's output schema (`schema.py`).
    - Any `tool_requests` go through the default-deny gateway (`tools.py`), and the job takes another turn.
    - Claims are verified against the supplied spans and typed facts (`evidence.py`).
11. **Proposal.** A `ResearchProposal` matching `contracts/research_proposal.schema.json` (or an abstention) is submitted. Outputs that arrive after the deadline or after cancellation are discarded, not submitted.

| Module | What it enforces |
|---|---|
| `config.py` | Owner policy file. Absent means every provider is disabled and every paid call refused. Unknown keys, floats and group/world-accessible files are refused. There is no write path. |
| `envelope.py` | `JobEnvelope` with strict validation. `JobRegistry` handles dedupe (CAP-AGT-01 key), per-role and global concurrency, expiry with recorded discards, cancellation, and parent depth <= 1 (a child cannot widen its parent's tools, deadline, token cap or spend cap). |
| `budget.py` | SQLite `SpendLedger` (`~/.unknown/agents/ledger.sqlite`, path injectable). It covers reserve, settle, release, mark_unconfirmed and reconcile. The caps are lifetime, month, day, job, role/day, and provider lifetime/day. The envelope cap can only lower the owner's per-job cap. |
| `tariffs.py` | Effective-dated rows, band selection by prompt tokens with the all-tokens rule, exact `cost()`, and `worst_case()`, which needs an owner-approved row. Unverified bands and unrecorded rates are refused. |
| `schema.py` | Local JSON-Schema subset validator. Any keyword it cannot enforce fails closed. The per-provider compatibility checker classifies each keyword as enforced, best_effort, not_enforced, unverified or rejected (CAP-MODEL-02). |
| `evidence.py` | Span-id existence, verbatim quotes after whitespace normalisation, exact numeric comparison with typed facts, and labelled `unsupported`/`uncertain` claims. It also renders untrusted data blocks behind a random nonce. Instruction screening is informational only. |
| `tools.py` | The registry accepts read-only tools only; order, policy, secret and spawning names are refused. The gateway checks the envelope allowlist, the tool-call budget, the arguments and the dataset rights of what a tool returns. |
| `gate.py` | `Dataset.permits("model_processing_cloud")` combined with the provider data policy (`config/models/provider_policies.json`) and the owner's data policy. It covers retention, training, residency, paid tier, staleness and covered models. Unknown facts block. |
| `catalog.py` | Snapshots and diff. New ids are `unreviewed`. Approved ids missing from a successful refresh become non-routable and raise an alert. A 401/403 marks the provider `credentials_invalid`. Moving aliases are refused. Providers without lifecycle data need an owner lifecycle review date. |
| `providers/` | `base.py` holds the interface, error taxonomy, retry policy, circuit breaker, urllib transport and protocol-fact tables. Adapters: `openai.py`, `xai.py` and `gemini.py` (urllib, verified fragments only) and `anthropic.py` (official SDK, lazily imported). |

Tool use is provider-neutral: the model asks for a tool through the `tool_requests` field of its structured output (`config/models/schemas/research_answer.v1.json`). It names the tool and gives its arguments as a JSON string. The gateway validates the request and executes it or refuses it; results come back as untrusted data blocks on the next turn. The model never gets authority from document text.

## Enabling a provider (owner)

1. **Write the policy file.** It goes at `~/.unknown/config/agents.json` (or `$UNKNOWN_HOME/config/agents.json`), mode `0600`, owned by you. Money values are decimal strings; JSON numbers are refused for money.
2. **Provide the API key.** Use the environment variable named in `api_key_env`. For Anthropic you can instead use a file under `~/.unknown/secrets/` named by `api_key_file`; it must be mode `0600` or it is refused. Keys are never stored in the policy file and never logged.
3. **Approve the tariff rows.** In `config/models/tariffs.json`, re-verify each price against the provider's pricing page and set `approved_by` on the rows you rely on. Shipped rows are all unapproved, so admission refuses until you do this. With `refusal_fallback: "default"`, also approve the rows of the fallback targets (`claude-opus-4-8`, `claude-opus-5`), because the worst case must bound a fallback attempt.
4. **Approve models per role.** Add an entry to `config/models/approved.json` (shipped empty). In code, the catalog comes from a live list call; approval entries name the exact id from that snapshot.

### `agents.json` schema (schema_version 1)

```json
{
  "schema_version": 1,
  "comment": "free text",
  "currency": "USD",
  "caps": {
    "per_day": "15.00",
    "per_job": "2.00",
    "lifetime": "200.00",
    "per_month": "60.00",
    "per_role_per_day": {"research": "10.00"},
    "per_provider": {"anthropic": {"lifetime": "200.00", "per_day": "15.00"}}
  },
  "providers": {
    "anthropic": {
      "enabled": true,
      "api_key_env": "ANTHROPIC_API_KEY",
      "api_key_file": "anthropic_api_key",
      "region": "global",
      "default_model": "claude-opus-5-5",
      "effort": {"default": "medium", "by_role": {"research": "medium"}},
      "refusal_fallback": "default",
      "account": {"training_permission_granted": false, "zdr_enabled": false}
    }
  },
  "data_policy": {
    "max_policy_age_days": 30,
    "default": {"max_provider_retention_days": 30, "allowed_processing_regions": ["any"], "allowed_storage_regions": ["any"]},
    "datasets": {"sec.companyfacts": {"max_provider_retention_days": 0, "allowed_processing_regions": ["us"], "allowed_storage_regions": ["us"]}}
  },
  "retry": {"max_attempts": 2, "base_ms": 1000, "max_ms": 30000, "jitter_ms": 250},
  "circuit_breaker": {"failures": 3, "window_s": 60, "cooldown_s": 300},
  "token_estimate": {"bytes_per_token_factor": "1", "overhead_tokens": 1024},
  "catalog": {"max_age_hours": 24},
  "concurrency": {"global": 1, "per_role": {"research": 1}}
}
```

Required keys: `schema_version`, `currency`, `caps.per_day`, `caps.per_job`, `providers` and, for each provider, `enabled`, `api_key_env`, `region` and `account`. Everything else is optional. A missing optional section never widens anything:

- **Caps.** Every listed cap applies. Days and months are UTC, and `lifetime` counts all rows ever recorded.
  - When `per_role_per_day` is present, any role it does not list has no budget.
  - The envelope's `spend_cap` can only lower `per_job`.
  - A zero `per_day` or `per_job` disables paid calls.
- **`providers`.** Known ids: `openai`, `xai`, `anthropic`, `gemini`.
  - `region` must exist in `provider_policies.json`. Anthropic uses `global` or `us`; the region is sent explicitly as `inference_geo`, and only `global` tariff rows ship.
  - `account` facts: `training_opt_in` (OpenAI), `training_permission_granted` (Anthropic, xAI), `zdr_enabled`, `paid_tier` (Gemini). A fact the gate needs but you have not recorded blocks.
  - `effort` (Anthropic) is required. The adapter refuses to send without an explicit level for the role, because Claude Opus 5.5 defaults to `medium`.
  - `refusal_fallback`: `"off"` (default) or `"default"`. `"default"` uses `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`.
- **`data_policy`.** Without it the gate blocks every call. `max_provider_retention_days` is an integer (0 means no third-party retention) or `"unlimited"`. Regions are codes such as `us`, `eu` or `india`, or `["any"]`; an endpoint whose processing region is `unspecified` passes only `["any"]`.
- **`retry`.** Absent means no retries. Retries apply only to transient classes, never earlier than `retry-after`, never past the deadline, and within the envelope's `retry_budget`.
- **`circuit_breaker`.** Absent means it opens after 1 overload or server error and stays open for 300 s.
- **`token_estimate`.** Absent means factor 1 on UTF-8 bytes plus 1024 tokens.
- **`concurrency`.** Absent means 1 job at a time. A role without a limit never runs. Pass it to `JobRegistry(max_global=..., max_per_role=...)`.

### Environment variable names

- `api_key_env` is whatever the owner names; it must match `^[A-Z][A-Z0-9_]{1,63}$`. Suggested: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`.
- `UNKNOWN_HOME` relocates `~/.unknown`.
- `UNKNOWN_LIVE_ANTHROPIC=1` opts into the live smoke test.

### Research dependency (Anthropic SDK)

The core stays standard-library only. The Anthropic adapter uses the official SDK, pinned in [`backend/requirements-agents.txt`](../../backend/requirements-agents.txt): `anthropic==1.12.1`, the newest release uploaded at least 14 days before 2026-10-22, with all transitive dependencies resolved under the same rule. The SDK is imported only inside the default client factory, so every other module and test runs without it.

```bash
python3 -m venv ~/.unknown/venvs/agents && ~/.unknown/venvs/agents/bin/pip install -r backend/requirements-agents.txt
cd backend && PYTHONPATH=src ~/.unknown/venvs/agents/bin/python -m unittest discover -s tests -p 'test_agents_*.py' -v
```

The client is built with `anthropic.Anthropic(api_key=<key>, max_retries=0)`, so every attempt goes through our retry policy and our ledger.

Never sent:
- `thinking`, because Opus 5.5 always thinks and the disabled and budget forms return 400;
- `temperature`, `top_p` and `top_k`, which return 400 since 2026-10-21;
- forced `tool_choice`;
- `output_format`, which is deprecated (structured output goes through `output_config.format`);
- `cache_control`, so no cache writes are billed;
- an assistant prefill.

`stop_reason` is checked before content: `refusal`, with `stop_details.category`, becomes a typed abstention. `usage.iterations` prices each fallback attempt at its own model's rate. A mismatch between the requested and reported `inference_geo`, or a returned model that is not an expected fallback, is quarantined.

### Live smoke test

`backend/tests/test_agents_live_anthropic.py` sends one tiny structured-output request: `max_output_tokens` 150, at most one truncation retry at 300, and effort `low`. It runs through the full runner and reserves and settles in the real ledger, so the spend counts against the owner's caps. It sends the proposal to an in-memory fake core.

It skips unless all of these hold:
- `UNKNOWN_LIVE_ANTHROPIC=1` is set;
- the SDK is importable;
- the owner policy enables Anthropic and has a `data_policy`;
- a key resolves;
- the default model's tariff row is approved.

```bash
cd backend && UNKNOWN_LIVE_ANTHROPIC=1 PYTHONPATH=src ~/.unknown/venvs/agents/bin/python -m unittest tests.test_agents_live_anthropic -v
```

## Verified versus open

### Adapters: what is implemented from the register and where they refuse to guess

Every HTTP adapter declares a `FACTS` table; reading an `Unverified` fact raises `UnverifiedFact`, a `NotImplementedError` that names the record. Send preconditions are checked first: (a) the provider is enabled, (b) a key is present, (c) an open reservation exists, and (d) a matching gate decision exists. Only then does an unverified fact stop the call, so nothing is ever sent from a guessed field. The runner skips such adapters (`unverified_protocol_facts:...`).

**Anthropic.** This is the only adapter that can send. Its source is the official SDK plus the Claude API reference supplied by the coordinator. Register facts used:
- `output_config.format` (CAP-MODEL-02);
- the `refusal` and `max_tokens` stop reasons;
- the usage fields `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `inference_geo` (CAP-MODEL-04/08/10);
- the error taxonomy, including spend-limit detection by `error.details.error_code`, retry-after presence and the 400 message prefix (CAP-MODEL-06);
- no forced `tool_choice` (CAP-MODEL-03/11);
- the 32 MB request limit.

**OpenAI.**

| Verified (record) | Refused, `UnverifiedFact` (record) |
|---|---|
| Base `https://api.openai.com/v1` and `GET /models` with model fields `id`, `object`, `created`, `owned_by`, `shutdown_date` (CAP-MODEL-01) | Auth header (CAP-MODEL-01) |
| `POST /responses/input_tokens` returning `input_tokens` and `object: "response.input_tokens"` (CAP-MODEL-04) | List items field (CAP-MODEL-01) |
| `/responses` (CAP-MODEL-04/09) | Request model field (CAP-MODEL-02) and input shape (CAP-MODEL-04) |
| `text.format {type: json_schema, strict}`, `max_output_tokens`, `store: false` | Schema field in `text.format` (CAP-MODEL-02) |
| Error codes and types; `x-ratelimit-*` and `Retry-After` headers (CAP-MODEL-06) | Response text, refusal and truncation locations (CAP-MODEL-02) |
| | Response model field (CAP-MODEL-11) |
| | Usage field names, and whether cached tokens are a subset of input (CAP-MODEL-04/08) |
| | Regional path prefixes (CAP-MODEL-10) |
| | Tool-call items (CAP-MODEL-03) |

**xAI.**

| Verified (record) | Refused, `UnverifiedFact` (record) |
|---|---|
| Global `https://api.x.ai` and US `https://us.api.x.ai`; the US endpoint's model list comes from `provider_policies.json` and other models are rejected locally (CAP-MODEL-10) | Auth header (CAP-MODEL-01) |
| `/v1/models`, `/v1/language-models`, `/v1/language-models/{id}` and their fields; prices stored unchanged in `usd_cents_per_100m_tokens` and never billed (CAP-MODEL-01) | List items field (CAP-MODEL-01) |
| `POST /v1/tokenize-text` (CAP-MODEL-04) | `capabilities` structure (CAP-MODEL-01) |
| Cached-token usage locations (CAP-MODEL-08) | Generation endpoint (CAP-MODEL-02/03) |
| 429, and 5xx treated as overload (CAP-MODEL-06) | `response_format` inner shape (CAP-MODEL-02) |
| | Response text location (CAP-MODEL-02) |
| | Usage fields (CAP-MODEL-08) |

**Gemini.**

| Verified (record) | Refused, `UnverifiedFact` (record) |
|---|---|
| `GET https://generativelanguage.googleapis.com/v1beta/models` with `pageSize` (max 1000), `pageToken` and `nextPageToken`; model limit fields (CAP-MODEL-01) | Auth header (CAP-MODEL-01) |
| `response_format {mime_type: application/json}` and `store: false` (CAP-MODEL-02/09) | List items field and model id field (CAP-MODEL-01) |
| `usage.total_cached_tokens` (CAP-MODEL-08) | `countTokens` path prefix and response field (CAP-MODEL-04) |
| Error labels, including `quota_exceeded`, which is not retried before the next midnight Pacific (CAP-MODEL-06) | Interactions endpoint, schema field and response text (CAP-MODEL-02) |
| | Usage fields (CAP-MODEL-08) |

Error labels whose carrying field is not recorded are matched against any string in the error body.

### Tariffs (`config/models/tariffs.json`)

Only numbers present in the register, or in the coordinator-supplied Claude API reference table (cached 2026-10-06; re-verify before relying), are recorded.

- **OpenAI.** Short-context Standard rows for `gpt-6.1-sol` (2.00/10.00, cached 0.10), `gpt-6-luna` (0.10/0.50) and `gpt-6-astra` (10.00/50.00).
  - Long context (more than 272K) is `unverified`. CAP-MODEL-05's open question is whether the long-context rate applies to every token of the request.
  - Cached rates for luna and astra are null: the guide says "0.1x on most of these models", which names no id.
  - Cache-write multipliers (1.25x/2.5x) are recorded as `unverified`. `cost()` refuses write tokens; `worst_case()` uses the multiplier as an upper bound.
- **xAI.** `grok-4.7` is 2.00/6.00 (cached 0.50) below 200k and 4.00/12.00 (cached 1.00) at 204,800 and above, with every token billed at the band rate.
  - Prompts from 200,000 to 204,799 are `unverified`: the register writes "200k" and does not say whether that means 200,000 or 204,800. The live `long_context_threshold` catalog field can settle it.
- **Anthropic.**
  - `claude-opus-5-5`: 4.00/20.00, cache read 0.20.
  - `claude-sonnet-5-5`: 2.00/10.00. The cache-read rate is null because the sources conflict: the API reference says 0.20, CAP-MODEL-05 says 0.05x, which is 0.10.
  - `claude-haiku-5-5`: 0.10/0.50 up to 100,000 prompt tokens, and 0.50/2.50 above, per request.
  - `claude-opus-5` and `claude-opus-4-8` (5.00/25.00) are recorded only to bound and price refusal fallbacks.
  - Cache-write multipliers (5m 1.25x, 1h 2x) are `unverified`, per the coordinator's instruction.
- **Not recorded.** Regional uplifts (OpenAI +10%, Anthropic `us` 1.1x, xAI US 1.1x) have no rows, so regional calls cannot be routed. Gemini and Mistral prices have no model id or output rate in the register.

### Open questions the implementation relies on (from the records)

- **CAP-MODEL-01.**
  - The xAI catalog price unit (cents per 100M) needs a live fixture check.
  - There is no machine-readable Gemini lifecycle, so Gemini and OpenAI models need an owner `lifecycle_reviewed_until`.
  - Does OpenAI's list hide models that project allowlists exclude?
- **CAP-MODEL-02.**
  - Per-model OpenAI strict support.
  - Whether xAI's `response_format` is strict.
  - Gemini property ordering.
- **CAP-MODEL-04.** Billing and rate limits of the OpenAI and xAI counting endpoints. Counts use a zero-amount admission row and are treated as provider-default retention.
- **CAP-MODEL-05.**
  - The OpenAI long-context rule (band refused).
  - Reconciliation APIs are unverified, so `reconcile()` takes an owner evidence string.
  - Mistral prices.
- **CAP-MODEL-06.**
  - xAI and Gemini retry metadata.
  - Whether errors are billed. They are treated as unconfirmed, so the reservation stays consumed until reconciliation.
- **CAP-MODEL-09.**
  - Master API terms were not fetched.
  - Gemini paid-tier abuse-log retention is unquantified, so finite ceilings block Gemini.
  - Mistral default retention.
- **CAP-MODEL-10.** Mistral regions, Vertex regional processing, and owner residency law.
- **CAP-MODEL-11.** OpenAI deprecations beyond `shutdown_date`.
- **CAP-AGT-01.**
  - Default deadlines, `kill_grace_s` and `max_turns` per role are owner settings; the envelope carries them.
  - Whether any role may request sub-jobs: depth <= 1 is enforced, and children cannot widen.
- **CAP-AGT-02.**
  - Owner budgets (now expressible).
  - Anthropic spend-limit latency.
  - Reconciliation tolerance.
- **CAP-AGT-06.** The per-role threshold for detecting typed-output corruption.

### Not built in this increment

- Mistral adapter.
- Batch (CAP-MODEL-07).
- Prompt caching (CAP-MODEL-08). The xAI `x-grok-conv-id` header is recorded but unused.
- Native per-provider tool-call translation (CAP-MODEL-03). Tools use `tool_requests` in structured output.
- Streaming and SSE mid-stream error mapping.
- Case-insensitive enum comparison for Anthropic. No current schema has an enum.
- The licensed or personal enum/const literal check (CAP-MODEL-02).
- Calibration tables for token estimates.
- The approval queue (CAP-AGT-04).
- The OpenTelemetry traces (CAP-AGT-13).
- The evaluation release gate (CAP-AGT-14).
- A tamper-evident audit chain (CAP-AGT-09). The runner returns its records in memory.
- A durable job store (the registry is in memory).
- A model-worker process that wires `JobRegistry`, `JobRunner` and the `CoreClient` with the agent token.
