# Research jobs workspace

Questions for the bounded research agents (ADR-0016). The backend is `api/routes/research_jobs.py`: it writes only the job store and never imports provider code. The model worker (`python -m unknown.agents.worker`) runs the jobs, under the owner's caps in `<UNKNOWN_HOME>/config/agents.json`.

Shortcut `g j`, nav order 85. Palette: "Ask a research question" opens `#/research-jobs/~new`.

## Routes

| Route | View |
|---|---|
| `#/research-jobs` | Every job, newest first. Filter by state. Shows the state, outcome, question, role, proposal (advisory, abstained or none) and delivery. |
| `#/research-jobs/~new` | Ask a question (`POST /api/research/jobs`). |
| `#/research-jobs/<job_id>` | One job: state and reason, request and evidence spans, caps and spend, the advisory proposal, the audit trail and job-store events. Cancel (`POST .../cancel`). |

The job store is outside the core event log, so stream version bumps do not refresh it. The list and the detail poll every 3 s while a job is queued, running or delivering. Otherwise the list polls every 15 s.

## Rules this workspace keeps

- **Spend is stated before submitting.** The form says that a job may spend money on a paid model provider, up to its spend cap. It lists the caps the API has reported: the cap and consumption of the most recent job. It also says that the default cap is the owner's per-job cap, which the API does not expose before submitting. Nothing is sent until the owner ticks the acknowledgement. After submitting, the page shows the queued job's own `spend_cap` from the response.
- **Model output is never shown as advice.** The proposal sits in a dashed "model output" box labelled "Not advice". Its hypothesis, reasoning, cited evidence and assumptions are each labelled as model text. A job's `reason` is labelled with its author. Only a cancel of a queued job stores the owner's own text (the job store records a `cancelled` event). For a job cancelled while running, the API keeps no owner text: the job store or the worker writes the reason, and the cancel form says so. A `schema_invalid` reason lists the schema errors of the model's rejected output, which can name properties the model invented, so it is labelled as possibly quoting model output. Every other reason is written by the worker or the runner. A proposal the core recorded is marked "recorded by the core" and "advisory only: it cannot reserve cash, place an order or change policy". The page checks that the core's stored body is identical to the job's output, comparing both as the API returned them (canonical JSON), so a difference in a field the page does not display still shows as "differs".
- **Proposal schema versions.** A version-1 proposal shows its free-text evidence lines. A version-2 proposal (`contracts/research_proposal.schema.json`, finding BR-DAT-12) shows its verified claims inside the model-output box: the claim text, the quote that was checked verbatim against the span, and the evidence references it cites by index. Its typed fields (as-of instant, instrument and entity ids, observation window, forecast present or not, and each evidence reference with dataset, store reference, observed and available times and span hash) come from the job and the research store, not from the model, so they are shown after the box under their own heading. The core's label (`ABSTENTION`, `QUALITATIVE`, `FORECAST_UNCALIBRATED`, `FORECAST_QUANTIFIED`) is shown with the recorded proposal; the core assigns none to version 1. The request panel lists the `instrument_ids` the owner named. The submit form does not offer `instrument_ids` yet; a request that needs them goes to `POST /api/research/jobs` directly.
- **States are honest.** QUEUED says that nothing has been sent and nothing spent. RUNNING says that the job may be spending now. FAILED shows the worker's reason verbatim. A 409 refusal (for example "paid model calls are disabled: there is no owner policy") is shown as the API's own message.
- **Money is exact.** Caps and consumption are decimal strings. They are compared with `compareDecimal` and `ratioBps`, and never converted between currencies.

## Request fields

These mirror `agents/submission.py` and `agents/pit_evidence.py`. The form checks the same limits before sending, and the server still decides.

- `question`: 1 to 4000 characters.
- `role`: `^[a-z][a-z0-9_]{0,63}$`, default `research`.
- `issuer_ids`: up to 5 entity ids or tickers known to the research store.
- `fields`: up to 40 `taxonomy:Concept` names, and only together with issuers.
- `spend_cap`: a decimal string. It can only lower the owner's per-job cap.
- `max_output_tokens`: 256 to 32,000.
- `deadline_seconds`: 60 to 86,400.
- `horizon_seconds`: 1 to 31,536,000.

Blank options are left out, so the server's defaults apply.

The API takes issuers, not instrument ids. "Add an issuer from an instrument" maps an instrument from the instrument master to an issuer. It uses the instrument's `sec.cik` attribute as `SEC-CIK-##########` when one is recorded, and otherwise its symbol as a ticker. FIXTURE instruments have no issuer and are not mapped.

## API gaps found

- No endpoint reports the owner's caps (per job, per day, lifetime) before a job exists. The form can show only the most recent job's cap and the cap the API returns once a job is queued.
- The job list omits each job's `spend_cap` (the envelope is left out of list rows), so a cap per row would need one detail request per job.
- The submission takes `issuer_ids` only. Instrument ids are mapped in the terminal, as described above.
- `POST .../cancel` drops the owner's `reason` for a RUNNING job (`JobStore.cancel` records only `cancel_requested`), so a running job's final reason is the job store's or worker's text.
