# Sessions workspace

`#/sessions` lists sessions and `#/sessions/<session_id>` shows one session. The shortcut is `g s`.

## Data

| Source | Used for |
|---|---|
| `GET /api/sessions` | List rows, successor links (a session's successor is the session whose `predecessor_id` names it) |
| `GET /api/sessions/{id}` | Detail: row, mandate, balances by account code, orders, executions, settlements, timeline |
| `GET /api/events?session=&after=&limit=` | Timeline events after the first 500 that the detail returns |
| `GET /api/buckets` | Server-computed liquid value, available cash and holds for `session:<id>`, `protected:<id>` and `unallocated` |
| `GET /api/positions` (polled every 5 s) | The session's holdings marked to bid, with source, age and the FIXTURE label |
| `GET /api/policies`, `GET /api/policy/active` | Template defaults, loss triggers, fee envelope, wind-down and successor rules |
| `GET /api/overview` | Control state for previews: stop, reconciliation, exceptions, ownership, active sessions |

## Commands

| Command | Offered in | Confirmation |
|---|---|---|
| `create_session` | always (form) | form with validation and the implied required return |
| `amend_session_request` | `REQUESTED`, `WAITING_FUNDS` | form; only changed fields are sent |
| `cancel_session` | `REQUESTED`, `WAITING_FUNDS`, `READY` | yes |
| `start_session` | `READY` | yes, with a preview of the blockers the core is expected to report |
| `suspend_session` | `RUNNING` | no (the safe direction) |
| `resume_session` | `SUSPENDED` | yes, with a preview of entry blockers |
| `end_session` | `RUNNING`, `SUSPENDED` | yes, with the wind-down rules and the stop warning |

Every command goes through the shared command tracker with a pre-generated durable id (`commandLog.ts` remembers which id belongs to which session). Nothing is optimistic. The page changes only when the stream reaches the command's event seq and the queries refetch. While a command is `sending` or `pending`, the session's other commands are paused.

## Derived figures

`decimalMath.ts` does exact BigInt decimal arithmetic. It never uses floats.

- **Implied required net return.** This uses the core's rule: `(target - seed) / seed`, rounded half-even to 4 places, then times 100. The detail page also shows whether this matches the figure the core recorded on `SessionRequested`.
- **Live result.** This is computed from the session bucket and its marked holdings:
  - marked equity is liquid value plus market value at bid;
  - realized net of fees is liquid value plus inventory cost minus the seed;
  - unrealized is the sum of the holdings' unrealized amounts.

  If any holding has no mark, or the bucket's inventory cost differs from the holdings' cost basis, equity is not shown. The second case means the two reads straddle a fill.
- **Fee feasibility.** Every buy reserves at least the policy minimum fee plus the estimate margin. A seed at or below that amount cannot place any order. The form says so, and the core still accepts the mandate.
