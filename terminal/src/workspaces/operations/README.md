# Operations workspace

Book Chapter 22, "Operations: reconciliation, alerts, service health". Registered as `operations` (`g p`).

| Route | Shows | Reads | Commands |
|---|---|---|---|
| `#/operations` | Service health, live-update link, control state, execution ownership, start preflight, attention list | `/api/health` and `/api/preflight` (polled every 5 s), `/api/overview`, `/api/policy/active` | none (the control drawer handles STOP/START) |
| `#/operations/exceptions[/<case_id>]` | Open or all cases; one case with its detail and the resolve form | `/api/exceptions?status=OPEN\|ALL` | `resolve_exception` (step-up; note required): `acknowledge`, `record_external_cash_flow` (CASH_MISMATCH: currency + signed amount), `cover_shortfall` (OVERCOMMITTED: `from_owner`) |
| `#/operations/reconciliation[/<run_id>]` | Latest 50 runs; one run's differences, settlements confirmed, watermark | `/api/reconciliations?limit=50` | none |
| `#/operations/fees` | Executions whose fee is still `ESTIMATED` (or all recent) | `/api/executions?limit=500` | `accept_fee_estimate` (confirmation dialog) |

Money-moving resolutions (`record_external_cash_flow`, `cover_shortfall`) have a review step before the command is sent. Every command shows its durable id and moves sending, then pending, then applied on the stream. Nothing is updated locally.

`parts.tsx` holds pieces the Audit and Instruments workspaces reuse: route-backed tabs (`RouteTabs`, `TabPanel`), `Segmented`, `InlineCommandStatus`, and `DetailFields`/`InlineFields` for exception and difference details. `model.ts` holds the pure logic and is unit tested in `model.test.ts`.
