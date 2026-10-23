# Unknown terminal

This is the browser terminal for Project Unknown (ADR-0013, book Chapter 22). It shows the core's authoritative state and sends authenticated owner commands. It is built with Vite, React and TypeScript in strict mode. `unknown-api` serves the built files from `terminal/dist`.

Status (ADR-0001 words): **implemented** and **tested**. The unit tests (vitest) run offline. The end-to-end suite (`e2e/`, Playwright) runs the built terminal against a real simulated core and API started for the run. It covers login, the overview labels, green START with step-up (HTTP 428), session creation, a filled BUY, red STOP with STOP_LATCH, stop persistence through a core restart, start after reconciliation, audit verification and the palette. It also runs an accessibility pass over every workspace in both themes. The terminal is not paper- or live-verified.

Workspaces registered: Overview, Sessions, Orders, Risk, Instruments, Operations, Audit, Policy and Capabilities (see `src/workspaces/*/README.md`).

## Run it

```bash
npm ci                      # exact versions from package-lock.json
npm run typecheck           # tsc --noEmit, strict (app and e2e code)
npm test                    # vitest
npm run build               # typecheck + production build into dist/
npx playwright install chromium   # once per machine
npm run test:e2e            # Playwright against a fresh core + API (see e2e/README.md)
```

To develop against a live backend, use your own runtime directory and port:

```bash
cd backend
export UNKNOWN_HOME=/tmp/unknown-dev            # any private directory
PYTHONPATH=src python3 -m unknown.cli init
PYTHONPATH=src python3 -m unknown.cli set-passphrase
PYTHONPATH=src python3 -m unknown.service.core --account SIM-1 --deployment mac-dev --fixture-market &
PYTHONPATH=src python3 -m unknown.cli bootstrap-sim --account SIM-1 --deployment mac-dev
PYTHONPATH=src python3 -m unknown.api.server --account SIM-1 --port 8765 --static ../terminal/dist &
cd ../terminal
UNKNOWN_API=http://127.0.0.1:8765 TERMINAL_PORT=5173 npm run dev   # http://127.0.0.1:5173
```

Vite proxies `/api` to `UNKNOWN_API`, including the Server-Sent Events stream. In production, `python -m unknown.api.server --static terminal/dist` serves the build. The page loads no third-party origin, so it works under the API's Content-Security-Policy (`default-src 'self'`).

## Bundle

Each workspace is loaded on demand (`src/workspaces/lazy.tsx`): the registry stays static, and the shell shows "Loading the <name> workspace…" while a workspace's chunk arrives. A chunk that fails to load (for example after the API started serving a newer build) is contained in the workspace area with a reload button, so the header and STOP keep working. Hovering or focusing a navigation link starts loading its chunk.

| Build | Initial JavaScript | Initial CSS |
|---|---|---|
| One bundle (before, 2026-10-22) | 938.20 kB (265.71 kB gzip) | 44.55 kB |
| Split by workspace (first split) | 305.28 kB (96.75 kB gzip) | 16.50 kB |
| Split by workspace (2026-10-22, with the command lookups, conditional GETs, exact timestamps) | 310.99 kB (98.58 kB gzip) | 16.50 kB |

The largest workspace chunk is Portfolio, 91.00 kB. `vite.config.ts` fails the build when the initial JavaScript (the entry chunk and everything it imports statically) exceeds 400 kB, instead of printing Vite's 500 kB chunk warning.

## Layout

```
src/
  api/types.ts          wire types mirroring backend/src/unknown/api/queries.py (decimals are strings)
  api/client.ts         typed client: every GET, auth, CSRF, 401 -> login, 428 -> step-up -> same-id retry,
                        GET /api/commands/<id> (stored result), 409 COMMAND_ID_CONFLICT
  format/decimal.ts     exact decimal-string formatting and comparison (no floats)
  format/exact.ts       exact decimal arithmetic on BigInt (add, sub, mul, sums, ratios in bps)
  format/time.ts        ns timestamps (exact from the API's *_ns_str with BigInt), ages, clock
  api/nsStrings.ts      the API's exact *_ns_str siblings: nsStrOf / exactNs, exactJson for recorded payloads
  state/stream.ts       /api/stream subscription: seq, version, gap/reset/reconnect detection
  state/queries.ts      query cache keyed by endpoint+params; refetch on version bump or poll
  state/commands.ts     command tracker: sending -> pending (command id) -> applied on stream seq
  state/auth.ts, stepup.ts, notices.ts, router.ts, runtime.ts, react.tsx (hooks)
  controls/             red STOP, green START, control drawer, pure control model
  ui/                   shell, header, nav, palette, DataTable, primitives, modal, theme, styles.css
  workspaces/registry.ts   the only list of workspaces (nav, palette, shortcuts)
  workspaces/lazy.tsx      on-demand loading of each workspace chunk (loading line, contained failure)
  workspaces/<id>/         one directory per workspace
e2e/                    Playwright suite, backend harness and accessibility checks (e2e/README.md)
```

## Adding a workspace

Register a workspace only when its backend service exists (ADR-0013 "no dead controls"). Capabilities that are not built yet belong on the Capabilities page.

1. Create `src/workspaces/<id>/index.ts`:

```ts
import type { WorkspaceDefinition } from '../types';

export const sessionsWorkspace: WorkspaceDefinition = {
  id: 'sessions',                 // URL segment: #/sessions, #/sessions/<session_id>
  title: 'Sessions',
  icon: 'sessions',               // see src/ui/Icon.tsx IconName
  shortcut: 'g s',                // must be unique; reserved list in registry.ts
  order: 20,                      // nav position
  // A dynamic import: the component (which receives { route, navigate }) is its own chunk.
  load: () => import('./SessionsWorkspace').then((m) => m.SessionsWorkspace),
  description: 'Seed, goal, P&L, protection, settlement, successor',
  keywords: ['mandate', 'successor'],
};
```

`index.ts` is in the initial bundle, so it imports no component (`.tsx`) module: `registry.test.ts` fails if it does. Constants it needs, such as palette paths, go in a small module of their own (for example `portfolio/tabs.ts`).

2. Add one import line and one array entry to `src/workspaces/registry.ts`. Array order does not matter, because `order` sets the navigation order:

```ts
import { sessionsWorkspace } from './sessions';
// ...
export const WORKSPACES: readonly WorkspaceDefinition[] = validateRegistry([
  overviewWorkspace,
  capabilitiesWorkspace,
  sessionsWorkspace,
]);
```

Reserved shortcuts and order: overview `g o` (10), sessions `g s` (20), orders `g b` (30), risk `g r` (40), instruments `g i` (50), operations `g p` (60), audit `g a` (70), policy `g y` (80), capabilities `g c` (900), portfolio `g f` (45), market data `g d` (55), research jobs `g j` (85). `validateRegistry` throws on a duplicate id or shortcut, and `registry.test.ts` fails with it.

Optional fields:
- `commands`: palette entries that open a view of the workspace. They show under "Go to" and are for navigation only: commands with effects run from their forms. An example is `{ id: 'ticket', label: 'New order ticket', path: ['~new'] }`.
- `keys`: workspace-local single-key shortcuts, listed in the `?` help. Their handler must return early when `charKeysEnabled()` is false (WCAG 2.1.4).

Route contract: `route.rest[0]` is the entity id. Once a workspace with id `sessions`, `orders` or `instruments` is registered, the command palette lists those records and jumps to `#/<id>/<entity id>` (session_id, order_id, instrument_id). Cross-workspace links use `<WorkspaceLink to="orders" rest={[orderId]}>`, which renders plain text until the target workspace is registered.

## Component and hook API

```ts
// Data: refetches on every state-stream version bump (new seq, gap, reset, reconnect, manual refresh).
useQuery<E extends Endpoint>(endpoint: E | null, params?: ParamsOf<E>, options?: { pollMs?: number; seqDriven?: boolean })
  : { status: 'idle'|'loading'|'success'|'error'; data?: ResponseOf<E>; error: unknown; fetching: boolean;
      fetchedAtMs: number | null; seqAtFetch: number | null; key: string | null; refetch(): void }
// e.g. useQuery('/api/orders', { state: 'open', limit: 200 });  useQuery('/api/preflight', undefined, { pollMs: 3000 })
// seqDriven: false for data outside the core's event log (research store and registry, valuation snapshots,
// the capability register): a new seq or a gap does not refetch it; opening the view, Refresh, a reconnect,
// a reset and pollMs still do, and seqAtFetch is null. useApi (research-common) defaults to it; the Company
// tearsheet (about 640 kB) is no longer fetched again for every core event.
// Detail endpoints: useQuery(`/api/sessions/${encodePathSegment(id)}`)

// Commands: never optimistic. Resolves when the core's decision is known; the record then moves
// pending -> applied once the stream reports the command's event seq.
const send = useSendCommand();
send<K extends CommandKind>(kind: K, payload: CommandPayload<K>, opts: { label: string; commandId?: string })
  : Promise<TrackedCommand>   // { commandId, status: 'sending'|'pending'|'applied'|'rejected'|'failed'|'cancelled',
                              //   rejection?: {code,message,details}, awaitSeq, error, outcomeUnknown,
                              //   lookup: null | {state: 'querying'|'found'|'not_recorded'|'failed', ...} }
useTrackedCommand(commandId) // live record for one command;  <CommandStatus commandId=... /> renders it
// Step-up kinds (start, publish/activate policy, (de)authorize capital, ownership, resolve_exception)
// prompt for the passphrase automatically on HTTP 428 and retry with the same command id.
// Unknown outcome (network failure, 5xx): the tracker asks GET /api/commands/<id> for the stored result
// before anything offers a retry. A stored result is adopted (pending/applied, or rejected), never resent;
// "not recorded" is definitive (not executed) or provisional (still unknown). commands.retry(id) queries
// again before it resends with the same id and payload; commands.recheck(id) queries without resending.
// HTTP 409 COMMAND_ID_CONFLICT is a definitive refusal: status 'rejected' with the API's code and message.

<DataTable<T>
  caption: string; captionHidden?: boolean;
  columns: Column<T>[];   // { id, header, description?, cell(row), sortValue?(row), sortKind?: 'text'|'number'|'decimal',
                          //   align?, numeric?: boolean (right-aligned monospace), wrap?, width? }
  rows: T[]; rowKey(row): string;
  initialSort?: { columnId; direction: 'ascending'|'descending' };
  onRowActivate?(row); activateLabel?: string;     // Enter/Space/double-click
  emptyText?; virtualizeAbove?: number (500); rowHeight?: number (28); maxHeight?: string ('min(70vh, 640px)') />

<Money amount={decimalString|null} currency="USD" kind?: 'realized'|'unrealized'|'settled'|'unsettled'|'protected'
       |'available'|'held'|'authorized'|'estimated'|'cost'|'marked' showKind? signed? unavailable?="no quote" />
<Qty value={decimalString} unit? />
<Timestamp ns={epochNs} nsStr={nsStrOf(row, 'x_ns')} | ms={epochMs} zone?: 'utc'|'local' withMs? withNs? never?="never" />
      // nsStr: the API's exact *_ns_str. Every <Timestamp ns> passes it (ui/timestamp.test.ts scans the source): the
      // millisecond comes from it, the tooltip shows all nine fraction digits, withNs shows them in the text.
<Age sinceNs? | ageMs + observedAtMs? clockOffsetMs? staleAfterMs? />   // live-ticking; appends "(stale)"
<StatusPill label="RUNNING" tone: 'ok'|'warn'|'bad'|'info'|'neutral' detail? compact? />  // text + glyph + colour
<Badge tone?>FIXTURE</Badge>  <FixtureBadge />  <ModeBadge mode="SIMULATED" />  <ShortId value=… />  <YesNo … />
<WorkspaceFrame title description? queries?={[q1, q2]} actions?>…</WorkspaceFrame>  // provenance: seq, age, refresh
<QueryError what="Orders" error={q.error} />
```

Formatting is exact. `formatDecimal('1234567.8912', { minFraction: 2 })` gives `1,234,567.8912`. Trailing zeros are trimmed down to the currency's minor units, and non-zero digits are never rounded away. `compareDecimal` sorts money without floats.

## Contract checklist (enforced in code)

- Money is a decimal string from the API and is never converted to a JS number. Every amount shows its currency, and amount kinds are labelled in column headers and tooltips.
- Quotes and marks show their source and age. Fixture data carries a `FIXTURE` badge. The header always shows the account mode in text and colour, plus a mode-coloured band.
- Red STOP is always in the header and never shrinks out of view. It needs only a session and sends immediately. The drawer then shows the durable command id and stop progress from `/api/preflight` `stop_progress`: entries blocked, open buys and cancels pending, unknown orders, residual positions, and "reconciled flat" only when the server says so. Liquidation is read from the active policy (`NONE`), and the drawer says it is not offered.
- Green START opens the preflight panel. It lists each blocker with its cause and resolution and offers no bypass. Start is enabled only when a stop or resume hold is active and every blocker is resolvable by step-up.
- There are no optimistic updates. A command shows `sending`, then `pending` with its command id, then `applied` when the stream reaches the command's event seq. On any stream reconnect or reset, every visible query refetches; a new seq or a gap refetches every visible query on core state (data outside the event log, such as research, is `seqDriven: false`).
- No read is served stale. Every GET asks the API (`cache: 'no-cache'`, never `force-cache`). A response with an ETag (today the capability register, about 3 MB) is kept with its tag, and the next GET of that path sends `If-None-Match` itself with `cache: 'no-store'`: the API answers 304 when the register is unchanged and the kept body is used, or 200 with the new one. The client validates because the API speaks HTTP/1.0 and Chromium revalidates only HTTP/1.1 responses with their ETag (checked on 2026-10-22: with `no-cache` alone it never sent `If-None-Match`). Every other JSON route is `no-store` and is fetched in full; validators are dropped at sign-out. POSTs use `no-store`.
- Keyboard: Cmd/Ctrl+K opens the palette, `?` lists shortcuts, `g <key>` switches workspaces, and table rows take arrows, Home/End, PageUp/PageDown and Enter. Dialogs trap focus and restore it on close. The modal scrim starts below the header, so STOP stays clickable. Single-character shortcuts can be turned off from the `?` help or the palette (WCAG 2.1.4, `ui/charKeys.ts`).
- The stream status pill reads STREAMING, never LIVE: in this terminal, LIVE always means the account mode.

## Contrast (WCAG 2.2 AA)

Text tokens on their surfaces, as computed from `styles.css`:

| Pair | Dark | Light |
|---|---|---|
| text / surface | 14.4 | 16.2 |
| muted text / surface-2 | 7.1 | 5.9 |
| accent / surface | 8.9 | 6.3 |
| ok, warn, bad, info / surface | 9.2, 9.7, 7.8, 8.8 | 5.9, 6.0, 6.4, 6.2 |
| mode SIMULATED, PAPER, LIVE / surface | 8.1, 8.6, 6.7 | 6.8, 6.8, 6.8 |
| STOP text / STOP fill | 5.6 | 6.6 |
| START text / START fill | 5.4 | 6.2 |
| control border (inputs, buttons) / surface | 3.6 | 3.8 |

The end-to-end suite measures these on the rendered pages; see `e2e/README.md`. On the last run it checked 5,251 text elements across 29 views per theme. The lowest text contrast was 5.35:1 (dark) and 4.78:1 (light), with no violations.

## API notes for the backend

See the foundation report. In short:
- ns timestamps exceed 2^53, so JSON.parse rounds them. The API now sends an exact `*_ns_str` beside every `*_ns`, and the terminal shows exact values from it: the audit event detail shows event, receipt and recorded times to the nanosecond, recorded payloads (audit, session events, operations and overview detail JSON) and the other record dumps (research-job audit summaries, instrument attributes and details, research values, stress resting orders) print every `*_ns` exactly and leave the siblings out (`api/nsStrings.ts` `exactJson`), the research JSON tree shows them without "≈", and every `<Timestamp ns>` also gets the row's `*_ns_str` (`nsStrOf`), so its millisecond comes from the string (a float can be one millisecond late, or a second late at a boundary) and its tooltip shows all nine fraction digits; session milestones carry theirs (`atNsStr`). Floats remain only where a value is not displayed: sort keys and ages.
- Path segments are percent-decoded by the API after it splits the path, so an id containing `/` is addressed as `%2F` (`encodePathSegment`).
- `/api/overview` does not carry the active policy's name or `on_stop_liquidation`, so the client also fetches `/api/policy/active`.
