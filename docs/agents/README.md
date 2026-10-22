# Bounded agent layer (increment I6)

Status (ADR-0001 vocabulary): **implemented and tested offline**, including the model worker, the durable job store, the audit trail and the terminal API routes (see [Running research jobs on this Mac](#running-research-jobs-on-this-mac)). The only paid call so far is the CI live smoke test (`live-anthropic.yml`). Paid calls stay disabled until the owner writes `~/.unknown/config/agents.json` and approves the tariff rows and models involved; `approved.json` still ships empty. Decision record: [ADR-0016](../adr/0016-bounded-agents-and-model-providers.md). Evidence: the `model-providers` (CAP-MODEL-01..12) and `agent-operations` (CAP-AGT-01..14) records in [`docs/capabilities/register.json`](../capabilities/register.json), verified 2026-10-22.

## Architecture

Agents are research-side code under `backend/src/unknown/agents/`. They hold no broker credential and no ledger write handle. Their only write to the core is `submit_research_proposal`, sent with the agent token (role `AGENT`, ADR-0007/0013). A static test fails the build if the package imports broker, core, gateway, risk, policy, store, API or market code. Only `worker.py` may import the core socket client (`service/ipc.py`), and it may use only the agent token. Only the evidence builders and the worker may read the point-in-time research store.

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
4. **Approve models per role.** Add an entry to `config/models/approved.json` (shipped empty). In code, the catalog comes from a live list call; approval entries name the exact id from that snapshot. The exact entry for Claude Opus 5.5 and the research role is in [step 3 below](#owner-steps-for-a-live-job).

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
- **`concurrency`.** Absent means 1 job at a time. A role without a limit never runs: the model worker fails its queued jobs with that reason. The worker runs one job at a time and one worker runs per `UNKNOWN_HOME`, so `global` above 1 has no effect yet.

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

## Running research jobs on this Mac

The owner asks a question through the terminal API. The API writes a job to the durable job store. The model worker runs it under the spend caps and delivers the proposal to the core, where it is recorded as a research proposal. The terminal reads the job, its audit trail, its spend and the recorded proposal back.

```
terminal API ──writes──> agents/jobs.sqlite <──claims/finishes── model worker ──AGENT token──> core socket
 (no key, no provider code)        │                                   │  provider call (key, ledger, gate)
                                   └── GET /api/research/jobs/<id> <── agents/audit.sqlite, agents/ledger.sqlite, core proposals
```

### Budget source (one owner source)

The only source of model budgets is the owner policy `<UNKNOWN_HOME>/config/agents.json`. Its `caps` are lifetime, month, UTC day, job, role per day and provider limits, all exact decimal strings in one currency. An absent file, or a zero `per_day` or `per_job`, disables paid model calls (AI-05). `config/paper_defaults.json` used to carry `paid_model_daily_budget_cents: 0`, which no code read; that field has been removed and the file's note points here. The API reads `agents.json`, read-only, for two things: to refuse a submission (HTTP 409) while paid calls are disabled, and to default a job's envelope spend cap to `per_job`. A request can lower that cap but never raise it. The spend ledger enforces every cap at reservation time. Nothing in the agent layer writes `agents.json`.

### Components

| Piece | Where | What it guarantees |
|---|---|---|
| Job store (`jobstore.py`) | `<UNKNOWN_HOME>/agents/jobs.sqlite` | WAL, `synchronous=FULL`, `fullfsync` on macOS, mode 0600. States are QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED and EXPIRED; `outcome` keeps the runner's fine status (`budget_exhausted`, `schema_invalid`, ...). Submission is idempotent: the same job id with the same content returns the job, and an equivalent job (CAP-AGT-01 key: role, schema, prompt template, input hashes) that is queued, running or succeeded within 24 h, and is not being cancelled, is returned instead of a new one. The key leaves out caps and the deadline. Claims are atomic and respect the concurrency limits. A finished job's proposal is stored in the same transaction as its terminal state. |
| Audit trail (`audit.py`) | `<UNKNOWN_HOME>/agents/audit.sqlite` | Append-only (UPDATE and DELETE refused by triggers). It holds every runner record (reservations, settlements, gate decisions, generations, tool calls, schema failures, discards) and every worker action (start, candidates, budget admission, catalog refresh, recovery, delivery), written as they happen and hash-chained like the core event log. `verify()` reports edits, deletions and re-attribution; truncating the newest rows is detectable only against a head kept elsewhere. |
| Evidence (`pit_evidence.py`) | `<UNKNOWN_HOME>/research/research.sqlite` (read-only) | Issuers (PIT entity ids such as `SEC-CIK-0000320193`, or tickers such as `AAPL`) and XBRL concepts become spans pinned to the submission instant with `as_of(t, knowledge_cutoff_ns=t)`. Each span states concept, period, unit and exact value, and carries a typed fact for exact numeric checks. The worker re-derives the spans and refuses the job if they differ from what was submitted, so only store-derived text, labelled with the store's dataset id, reaches the data-use gate and the provider. |
| Model worker (`worker.py`) | `python -m unknown.agents.worker` | See below. |
| API routes (`api/routes/research_jobs.py`) | `GET/POST /api/research/jobs`, `GET /api/research/jobs/<id>`, `POST /api/research/jobs/<id>/cancel` | Owner session required; POSTs also require the CSRF token. The API writes only the job store. It holds no key and loads no provider, runner, worker or tool module; a test checks every module the API process loads. |

What the worker does with each job, in order:

1. **Owner policy.** It re-reads the owner policy, so an edit applies without a restart. An absent or invalid policy fails the job closed, with the reason and no network call.
2. **Evidence.** It re-derives the evidence and checks the task hash against the envelope.
3. **Candidates.** Candidates are enabled providers crossed with the models approved for the job's role in `config/models/approved.json`, with the provider's `default_model` first. An unapproved model is never a candidate, and the runner re-checks catalog approval, lifecycle and freshness.
4. **Local admission (no network).** The adapter must be enabled, hold a key and have its SDK installed, and an owner-approved tariff row must exist. A **budget probe** then reserves, and immediately releases, the cheapest call the job could make: `max_output_tokens` at the output rate with zero prompt tokens, checked against every cap. If even that does not fit, the job fails `budget_exhausted` before any provider call.
5. **Catalog.** The model catalog is refreshed from the list endpoint only when the last snapshot is older than `catalog.max_age_hours`. Snapshots persist in `agents/catalog/` across restarts. A failed refresh is retried no sooner than 5 minutes later.
6. **Run.** The runner runs the job: count, worst case, gate, reservation, call, settlement, validation and claim verification.
7. **Delivery.** The proposal, or the runner's abstention, goes to the core as `submit_research_proposal` with command id `agent.proposal.<job_id>`, sent as principal `agent.<role>` with the AGENT token.
   - **Core unreachable.** The proposal stays PENDING and is retried with backoff, starting at 1 s and doubling to 5 min. It is never dropped.
   - **Resend after a crash.** The resend is byte-identical, so the core's command-id deduplication records it once.
   - **Rejected or conflicting.** A proposal the core rejects, or one that conflicts with a different payload under the same command id, is kept and marked REJECTED or CONFLICT.
   - **Refused before the runner.** A job refused before the runner starts (no policy, provider disabled, model not approved, no key, budget probe refused, evidence changed) records no proposal; the reason is on the job.

**Crash recovery.** At start the worker takes an exclusive lock on `agents/worker.lock`, so only one worker runs per home. It then checks every RUNNING job left by a dead worker against the spend ledger:
- **Admission probes.** An OPEN probe is released, because a probe is never sent.
- **Possibly sent.** Any other OPEN, SETTLED or UNCONFIRMED reservation means a request may have been sent. The job becomes FAILED with reason `worker restarted`, and its OPEN reservations become UNCONFIRMED: the outcome is unknown, so the money stays reserved until reconciled.
- **Cancel requested.** A job the owner asked to cancel becomes CANCELLED instead, and is never queued again.
- **Nothing sent.** If every reservation was released, the job is queued again, at most 3 times.

**Other failures.**
- **The job's end cannot be stored** (for example SQLite busy). The outcome, which may hold a paid proposal, stays in memory and is retried at the start of every cycle.
- **One delivery fails unexpectedly.** It never blocks other deliveries, expiry or new jobs.
- **A cancel accepted while a job runs** always discards the output, even when it lands after the runner's last check. The store re-checks the cancel in the same transaction as the terminal state.

### Owner steps for a live job

Everything here stays on this Mac; nothing below is committed except the approval in step 3.

1. **Policy.** `~/.unknown/config/agents.json` already exists (USD 200 lifetime, 15.00 per day, 2.00 per job; Anthropic enabled with `api_key_env: ANTHROPIC_API_KEY` and `api_key_file: anthropic_api_key`). No change is needed.
2. **API key.** It goes in `~/.unknown/secrets/anthropic_api_key` with mode 600 (the directory is mode 700); the worker refuses a file readable by others. Alternatively, export `ANTHROPIC_API_KEY` in the worker's environment only, never in the API's. For example:

   ```bash
   mkdir -p ~/.unknown/secrets && chmod 700 ~/.unknown/secrets
   ( umask 077 && read -rs KEY && printf '%s' "$KEY" > ~/.unknown/secrets/anthropic_api_key ); chmod 600 ~/.unknown/secrets/anthropic_api_key
   ```

3. **Approve `claude-opus-5-5` for the research role.** Add exactly this entry to the `approvals` list in `config/models/approved.json`. Use today's date, and set `policy_version` to the owner policy hash printed by the command below; any version label is accepted.

   ```json
   {"provider": "anthropic", "model_id": "claude-opus-5-5", "roles": ["research"], "approved_by": "owner",
    "approved_at": "2026-10-22", "policy_version": "<owner policy hash>",
    "note": "Owner approval for research jobs under the USD 200 authorization"}
   ```

   ```bash
   cd backend && PYTHONPATH=src python3 -c "from unknown.agents.config import load_owner_policy; print(load_owner_policy().policy_hash)"
   ```

   Notes:
   - The tariff row for `claude-opus-5-5` is already owner-approved in `config/models/tariffs.json`.
   - `lifecycle_reviewed_until` should not be needed: the CI smoke run routed this model, which requires the list endpoint to report it as active. If a job ever records `catalog:lifecycle_unknown_without_owner_review`, add one.
   - Approving a different model, or another role, follows the same shape.
4. **SDK.** The worker runs in the agents venv, because the core stays standard-library only:

   ```bash
   python3 -m venv ~/.unknown/venvs/agents && ~/.unknown/venvs/agents/bin/pip install -r backend/requirements-agents.txt
   ```

5. **Start the core, the API and the worker.** The core creates `secrets/agent_token` at start. `--check` lists what is still missing and makes no network call.

   ```bash
   cd backend
   PYTHONPATH=src python3 -m unknown.service.core --account SIM-1 --deployment mac-dev --fixture-market &   # or the IBKR PAPER service
   PYTHONPATH=src python3 -m unknown.api.server --account SIM-1 --port 8765 &
   PYTHONPATH=src ~/.unknown/venvs/agents/bin/python -m unknown.agents.worker --check
   PYTHONPATH=src ~/.unknown/venvs/agents/bin/python -m unknown.agents.worker &      # --once exits when idle
   ```

6. **Ask a question.** The terminal has no research panel yet, so use the API directly. Log in with the owner passphrase, then submit with the returned CSRF token:

   ```bash
   read -rs P; printf '{"passphrase": "%s"}' "$P" | curl -s -c ~/.unknown/run/api.cookies -H 'Content-Type: application/json' \
     --data @- http://127.0.0.1:8765/api/auth/login; unset P            # prints {"csrf_token": ...}
   CSRF=...   # the csrf_token value
   curl -s -b ~/.unknown/run/api.cookies -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
     -d '{"question": "Did Apple grow revenue in its latest fiscal year?", "issuer_ids": ["AAPL"], "max_output_tokens": 4000}' \
     http://127.0.0.1:8765/api/research/jobs
   curl -s -b ~/.unknown/run/api.cookies http://127.0.0.1:8765/api/research/jobs/<job_id>
   ```

   Request fields:
   - `question` is required.
   - `role` defaults to `research`.
   - `issuer_ids` takes up to 5 PIT entity ids or tickers.
   - `fields` takes XBRL concepts (taxonomy:Concept); the default is 12 common us-gaap/dei concepts.
   - `max_output_tokens` defaults to 8000 (range 256..32000).
   - `deadline_seconds` defaults to 3600.
   - `horizon_seconds` defaults to 90 days.
   - `spend_cap` must be at or below the owner's `per_job`.

   The detail response shows:
   - the job and its events;
   - the evidence spans;
   - the audit trail with its chain status;
   - the ledger rows for the job;
   - the proposal, and the proposal as the core recorded it.

**Cost of one job** at the approved rates of 4.00 USD input and 20.00 USD output per million tokens:
- **Evidence size.** AAPL and MSFT with the default concepts gave 138 spans, about 35 KB (measured on this Mac's store, 2026-10-22). That is roughly 10k prompt tokens.
- **First reservation.** With the provider's token count, the reservation is about 0.05 USD of prompt plus 0.16 USD for 8000 output tokens. If counting fails, the local estimate (1 token per byte plus 1024) reserves about 0.15 USD of prompt instead.
- **Settlement** is the actual usage.
- **Truncation.** A truncated answer is retried once with twice the output tokens.
- **Caps.** Every call is checked against 2.00 USD per job and 15.00 USD per day.

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
- A terminal panel for research jobs. The API routes exist; the browser terminal does not call them yet.
- `kill_grace_s`. A provider call in flight at the deadline is not interrupted; its output is discarded.
- Child jobs in the durable store. The envelope supports depth 1, but the store accepts root jobs only.
- More than one job at a time. One worker runs per `UNKNOWN_HOME`, one job at a time.
- Reconciliation of UNCONFIRMED spend against a provider cost report. `SpendLedger.reconcile()` takes owner evidence; no cost API is integrated.
- Proposal-to-order lineage (CAP-AGT-09). No order intent consumes research proposals yet.
