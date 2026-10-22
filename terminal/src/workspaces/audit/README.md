# Audit workspace

Registered as `audit` (`g a`). Read-only.

| Route | Shows |
|---|---|
| `#/audit/events[/type=T][/session=S][/order=O][/after=N]` | The event log in seq order. Without `after` it opens on the latest events and follows the live tail; `after=N` starts at seq N+1. "Load earlier" walks back in fixed seq windows; scrolling down loads forward pages of 200. Rows expand to the info JSON, identifiers, times and hashes. |
| `#/audit/journal[/owner=X]` | Journal entries, newest first, with their postings. Each entry's per-currency balance is re-added exactly (BigInt) as a visible check. |
| `#/audit/verify` | Runs `GET /api/audit/verify` on request and lists `chain_problems` and `ledger_problems`. |

Other workspaces can link to a filtered log, for example `<WorkspaceLink to="audit" rest={['events', 'session=' + id]}>`.

`eventLog.ts` keeps the coverage invariant (every matching event with lo < seq <= hi is loaded) through forward, backward, refresh and trimming; `journal.ts` pages journal entries without splitting an event's entries; both are unit tested in `audit.test.ts`.
