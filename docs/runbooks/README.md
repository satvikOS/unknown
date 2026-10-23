# Runbooks

Each runbook states the trigger, what the system has already done automatically, what the owner does, and the exit condition. Commands refer to the implemented core (`backend/`). Terminal actions call the same commands.

Conventions:

- `ACCOUNT` is the internal account key, for example `SIM-1`.
- `UNKNOWN_HOME` defaults to `~/.unknown`.
- Owner CLI: `cd backend && PYTHONPATH=src python3 -m unknown.cli ...`.
- A **latch** (red stop) clears only through the owner's green start with preflight. A **condition** clears automatically when the fact changes. Examples of conditions: broker disconnected, reconciliation required, stale market data.

## RB-01 Red stop: always available

- **Trigger:** owner decision, a loss budget breach, foreign activity, a restore from backup, 3/10/100 consecutive core-cycle failures, a ledger invariant failure (`LEDGER_INVARIANT`), an undeclared order transition (`ORDER_TRANSITION_REFUSED`), or a broker-event journal that cannot be written (`EVENT_JOURNAL_UNWRITABLE`, RB-15).
- **Already done by the system:**
  - the latch is durable, so it survives restart. If the database cannot record the stop, the core latches it in memory and in a file next to the database, cancels open buys at the broker directly, and records the stop as soon as the database accepts writes (RB-12);
  - unsent buys are canceled locally;
  - cancels are queued for working buys;
  - sells already working stay working;
  - nothing is sold automatically (`on_stop_liquidation = NONE`).
- **Owner:**
  - Terminal: red STOP. CLI: `unknown.cli stop --account ACCOUNT --reason "..."`.
  - Watch the stop progress: entries blocked, cancels pending, unknown orders, residual positions, reconciled flat.
- **Out-of-band (core down or unreachable):** this system sends nothing while the core is down, but orders already resting at the broker remain working. Use the broker's own interface (TWS / IBKR Client Portal: cancel all orders for the account). After restart, the core reconciles before anything else. A cancel made at the broker shows up as a broker-confirmed CANCELED, and the reservation is released then.
- **Exit:** RB-02.

## RB-02 Green start

- **Preflight blockers:** each one is named. Fix the cause; there is no bypass.

| Blocker | Action |
|---|---|
| `OWNER_REAUTHENTICATION_REQUIRED` | Re-enter the passphrase (step-up). |
| `BROKER_DISCONNECTED` / `BROKER_AUTHENTICATION_REQUIRED` | RB-06. |
| `RECONCILIATION_REQUIRED` / `RECONCILIATION_STALE` | Wait for the next run (the core retries every 5 s while required), or inspect Operations. |
| `BLOCKING_EXCEPTIONS:<kinds>` | RB-03 (`UNKNOWN_ORDER`), RB-04 (`POSITION_MISMATCH`, `CASH_MISMATCH`, `RECONCILIATION_DIFFERENCE`), RB-05 (`EXECUTION_ANOMALY`), RB-14 (`POST_COMPLETION_ADJUSTMENT`). `ORDER_TRANSITION_REFUSED` is a code defect: a handler tried an order state change that the published table (`core/order_machine.py`) does not declare, so nothing of that command was written. Its `OrderTransitionRefused` event keeps the attempted transition and, for a broker observation, the received payload. Handle it as RB-11, then let reconciliation recover the broker's view. |
| `UNKNOWN_ORDERS` | RB-03, then RB-13 if it stays unresolved. |
| `STOP_LATCHED_IN_MEMORY` | RB-12. |
| `LOSS_BUDGET:<kind>` | RB-07. |
| `MARKET_DATA_STALE:<instrument>` | RB-08. |
| `EXECUTION_OWNERSHIP_NOT_HELD` | RB-09. |

- **Exit:** a `StartGranted` event with its preflight evidence.

## RB-03 Unknown order

- **Trigger:**
  - a submission timed out, the transport failed, or the process restarted while a submission was in flight;
  - the broker reports the order **Inactive** (IBKR: held, not working, and not said to be final). The core no longer releases such an order as `REJECTED` (ADR-0009 amendment).
- **Already done by the system:**
  - the order is `UNKNOWN`; an Inactive report is kept on the order as `broker_held`;
  - the reservation is retained;
  - a blocking `UNKNOWN_ORDER` exception blocks new entries;
  - a held buy gets a cancel request, so it cannot start working again unattended. A held sell is not canceled;
  - nothing is resubmitted.
- **Automatic resolution (reconciliation):**
  - if the broker lists or finds the order working, or reports a terminal status, that status and its executions are applied. A broker terminal report releases the reservation;
  - if the broker *conclusively* reports it never received the order, it becomes `REJECTED` and the reservation is released. The IBKR adapter is conclusive only when `placeOrder` was never called;
  - if the lookup cannot decide, the difference stays blocking.
- **Owner (if it stays unresolved):**
  1. Check the broker's order and trade history in its own interface. If the order is still working or held there, cancel it at the broker. The core applies the broker's CANCELED status and releases the reservation; no owner close is needed.
  2. If the broker has no trace of it, contact broker support through authorized channels, then close it on that evidence (RB-13).
  3. Never resend under a new id until the original is resolved.
- **IBKR note:** host-side resubmission can transmit an order after a reconnect (ADR-0014). Keep orders sent around a disconnect UNKNOWN until reconciled.

## RB-04 Position or cash mismatch

- **Trigger:** reconciliation reports `POSITION_MISMATCH`, `CASH_MISMATCH` or `RECONCILIATION_DIFFERENCE`.
- **Already done by the system:** entries are blocked, and both snapshots are in the reconciliation run record.
- **Owner:**
  1. Find the economic cause: a missing fill, a fee, a corporate action, a manual trade, or a deposit or withdrawal.
  2. A deposit or withdrawal is resolved with `resolve_exception` action `record_external_cash_flow`. It needs a note, and the amount is signed and attributed to `outside`.
  3. Never edit balances directly.
- **Exit:** the next reconciliation is CLEAN.

## RB-05 Execution anomaly

- **Trigger:** a fill above the remaining quantity, outside the limit, after a terminal state, a sell without owned quantity, the same execution id arriving with changed content, or a correction after disposal or settlement.
- **Already done by the system:**
  - the economic fact is recorded;
  - any uncovered quantity sits in an `exception:<case>` owner with SUSPENSE;
  - the session moves to `FAILED_REVIEW`;
  - entries are blocked.
- **Same execution id, changed content:** the booked original stands. Nothing of the amendment is booked; the received payload is kept in an `ExecutionContentConflict` event and in the case detail (`stored` and `received`). The case is critical and names the execution's order, session and current owner. Ask the broker which version is right. A genuine amendment reaches the ledger only as a broker correction (a new execution id) or as a reconciliation difference (RB-04). Then resolve the case with a note.
- **After the session ended:** a late fill, a bust or a correction for an ended session also opens a `POST_COMPLETION_ADJUSTMENT` (RB-14).
- **Owner:** compare with the broker's trade confirmation. Resolve the case with a note. If the execution was the broker's error, follow the broker's correction; the correction then arrives as a new execution revision.

## RB-06 Broker authentication or connectivity

- **Trigger:** a `BrokerConnectivityChanged` event (disconnected or authentication required).
- **Already done by the system:** entries are blocked through `needs_reconciliation`. Research and monitoring continue.
- **Owner (IBKR):** log in to TWS or IB Gateway manually with 2FA (weekly, and after a Saturday reset). No login automation is used (book Chapter 12). The core reconnects, reconciles and resumes condition checks. An owner stop, if set, still needs RB-02.

## RB-07 Loss budget breach

- **Trigger:** `RiskStateUpdated` with breach `DAILY_LOSS` or `CUMULATIVE_LOSS_FROM_HIGH_WATER`.
- **Already done by the system:** the stop is latched; budgets are never reset by a new session or a restart.
- **Owner:** review whether the cause was ordinary market outcome, model failure, execution or a defect (book Chapter 26). Widening a budget means publishing and activating a new policy (step-up). A daily breach clears its condition on the next risk day, but the latch still needs RB-02.

## RB-08 Stale or unavailable market data

- **Trigger:** `valuation_ok = false`, or pretrade `QUOTE_FRESH` / `QUOTE_NOT_DELAYED` failures.
- **Already done by the system:** valuation fails closed and buys are refused. Delayed data never validates an order.
- **Owner:** check data entitlements and subscriptions (IBKR market-data sharing to paper, error 10089/10197). Resume when quotes are fresh.

## RB-09 Execution ownership transfer (Mac to AWS or back)

1. On the current owner deployment:
   1. red stop;
   2. resolve or cancel open orders;
   3. a clean reconciliation;
   4. `release_execution_ownership`, which returns a certificate.
2. On the new deployment:
   1. `acquire_execution_ownership` (step-up). Outside SIMULATED, pass the old deployment's certificate as `release_certificate`. For a first deployment with no predecessor, pass `attest_no_other_deployment: true`. Without one of these the command is refused (`RELEASE_CERTIFICATE_REQUIRED`).
   2. reconcile;
   3. RB-02.
3. Never run two deployments with trading credentials for one account. Any order tagged with another deployment, or with no tag, latches the stop (ADR-0011).

## RB-10 Backup and restore drill

- **Backup:** run `python -m unknown.cli backup --account <key> [--keep 96]`. It is safe while the core runs: it opens a separate read-only connection and never takes the writer lease. The command:
  - writes `<UNKNOWN_HOME>/backups/<key>-<UTC time>.sqlite` (mode 600);
  - writes a JSON sidecar with the source epoch, last event sequence, integrity check and SHA-256;
  - keeps the newest `--keep` copies.
  - On the Mac, the `com.unknown.backup` launchd agent written by `scripts/deploy/macos/render_launchd.py` runs it every 15 minutes (docs/deploy/macos.md). Copy backups off-host encrypted, e.g. to an encrypted volume; this tool does not encrypt.
- **Why a backup cannot be mistaken for the live database:** every copy is marked inside itself (`backup_origin`). Opening it anywhere is detected as a restore. A database whose `<db>.epoch` marker is missing or not older is also treated as restored (fail closed).
- **Restore drill:** run `python -m unknown.cli restore-drill --account <key> [--backup FILE]`. It works in a temporary directory, so live state is never touched, and exits non-zero on any problem. It checks:
  - the copy matches its recorded SHA-256;
  - the hash chain verifies;
  - replaying the event log into an empty database reproduces every projection;
  - opening the copy is detected as a restore, with the stop latched (`RESTORED_FROM_BACKUP`), in-flight orders `UNKNOWN` and a blocking exception;
  - the ledger invariants hold.
- **Real restore:**
  1. Stop the core and the API (`launchctl bootout gui/$(id -u)/com.unknown.core`, and the same for `.api`).
  2. Run `python -m unknown.cli restore --account <key> --backup FILE`. It refuses while a core holds the writer lease. It moves the current `<key>.sqlite` together with its `-wal` and `-shm` into `data/replaced-<key>-<time>/`, so a stale write-ahead log can never be replayed onto the restored file. It then copies the backup with the SQLite backup API, checks integrity, fsyncs and renames it into place.
  3. Start the core. It runs `quick_check` on the restored database, detects the restore and latches the stop (`RESTORED_FROM_BACKUP`, in-flight orders `UNKNOWN`).
  4. Reconcile against broker activity that happened after the backup, resolve the `RESTORED_FROM_BACKUP` case, then green start (RB-02).

  Never copy a backup over `<key>.sqlite` by hand while `-wal` or `-shm` files exist beside it.

  **The broker-event journal travels with the database.**
  - `backup` copies `data/<key>.events` right after the database, as `<key>-<time>.events` with its own SHA-256 in the sidecar. Every journal entry the database copy applied is in the journal copy.
  - `restore-drill` checks that copy and applies it to the restored database before verifying the ledger.
  - `restore` keeps the live journal when it belongs to the restored database (same database identity), and the next start applies what the database lacks, in order. Otherwise the backup's journal copy is put in its place.
  - A journal of another database is never applied: it is set aside, the stop latches (`EVENT_JOURNAL_SET_ASIDE`) and reconciliation is required.
  - A backup taken before database identities existed gets a new identity on restore, so the current journal is set aside and reconciliation recovers its events.
  - Do not delete or edit a journal by hand. RB-15 covers a damaged one (ADR-0003 amendment).
- **Record:** the drill date, backup age, problems found and recovery time.
- **Production rule:** a restored ledger trades only after a clean reconciliation and an owner green start.
- **Note:** databases written before commit `c41a505` use the earlier hash-chain formula, and `verify_chain` reports them as mismatched. That is development data only; recreate it.

## RB-11 Repeated core-cycle failures

- **Trigger:** log lines `core cycle failed (N consecutive)`. A stop is latched at 3, 10 and 100.
- **Owner:**
  1. Read the exception in the core log.
  2. Fix or roll back the code. Production code changes are owner decisions.
  3. Restart the core. It comes up with a recovery hold and reconciles.
  4. RB-02.

## RB-12 Red stop latched in memory (the database could not record it)

- **Trigger:** the database could not record a red stop: an owner STOP, a CLI stop or a system stop. Causes include a full disk, an I/O error, a locked or damaged store, or a control row the desk cannot read (BR-SES-13; ADR-0010 amendment).
- **What the owner sees:**
  - The STOP answer says `persisted: false`, with the error, the cancels sent and the note "STOP LATCHED IN MEMORY ONLY: the database did not record it ...". The terminal shows it in the command result; `unknown.cli stop` prints it.
  - Preflight lists `STOP_LATCHED_IN_MEMORY` (control drawer: "Stop latched in memory"). Stop progress reports entries blocked.
  - `unknown.cli status --account ACCOUNT` and `GET /api/health` show `memory_latch`. It holds the reason, the time it engaged, the error, the stop ids it stands in for, the cancels sent, the records still pending and `last_persist_error`.
  - The core log shows `RED STOP NOT RECORDED` at CRITICAL, then `RED STOP STILL NOT RECORDED` every 10 s while the database refuses writes.
- **Already done by the system:**
  - Exposure-increasing commands are refused at the core socket: start, session create, start and resume, request amendments, capital authorization and buy intents.
  - The gateway transmits no buy; a buy it has claimed is recorded `REJECTED` (`NOT_TRANSMITTED`). Sells and cancels still go out.
  - Every open buy gets a cancel sent straight to the broker. The core takes them from the ledger if it can still read it, and from the broker's open orders that carry our client order id. Manual and foreign orders are not touched.
  - The latch is written, fsync'd, to `<UNKNOWN_HOME>/data/<account>.stop-latch.json`, so a restart comes up latched.
- **How it is recorded:** at the first command or loop cycle after the database accepts writes, the core records each stop under its own command id. It then records a SYSTEM stop with reason `MEMORY_LATCH_PERSISTED`, whose `StopReasserted` event carries the evidence: the error, the cancels sent and the ids it stood in for. Only then is the memory latch released and the file removed; the log says "the stop latched in memory since ... is now recorded". A stop that the desk refuses once writes work (for example, an unknown account) is recorded `REJECTED`, and the SYSTEM stop still records the latch.
- **Owner:**
  1. The stop is in force in this process: entries are blocked and buys were canceled at the broker. Confirm at the broker's own interface (RB-01, out-of-band).
  2. Fix the cause. Free disk space (preflight's `DISK_SPACE` uses the policy's `min_free_disk_mb`), check the volume and the file permissions, or follow RB-10 if the database is damaged.
  3. Watch `unknown.cli status --account ACCOUNT` until `memory_latch` is null. In Audit, the `StopLatched` / `StopReasserted` events carry the `memory_latch` evidence.
  4. Never delete `<account>.stop-latch.json` by hand: it is how the next start learns about the stop. If the file cannot be read, or holds anything but stops of this account, the core starts latched with its own SYSTEM stop (`UNREADABLE_STOP_LATCH_FILE`). If the file itself cannot be written, the latch holds in that process only, logged CRITICAL. Then do not restart the core until the database records the stop.
  5. Green start (RB-02) once the stop is recorded and preflight is clear.
- **Exit:** `memory_latch` is null in health, the stop and its evidence are in the event log, and a green start follows.

## RB-13 Close a stuck UNKNOWN order on broker evidence

- **When:** reconciliation leaves an order `UNKNOWN` because the broker cannot report it. It has no trace in open orders, completed orders or executions, and every lookup answers "cannot determine" (for IBKR: an order that was transmitted but never surfaced). This runbook is not for an order the broker still shows as working or held; cancel that one at the broker (RB-03).
- **Already done by the system:** see RB-03. The reservation is kept and entries stay blocked until the order is resolved.
- **Owner:**
  1. Get broker evidence of the outcome: an activity statement, a trade confirm, or a support reference. The kinds accepted are `BROKER_STATEMENT`, `BROKER_CONFIRM` and `SUPPORT_REFERENCE`; the reference is 1 to 200 characters.
  2. Wait for a reconciliation that is complete, run by this core process, younger than the policy's `max_reconciliation_age_s`, and later than the order's last change. Check it in Operations > Reconciliation. The core reconciles every 60 s, and every 5 s while a reconciliation is required. Resolve any other blocking difference first.
  3. Re-enter the passphrase (step-up). Then send `resolve_exception` on the order's `UNKNOWN_ORDER` case, or on the `ORDER_UNRESOLVED` / `ORDER_MISSING_AT_BROKER` reconciliation case that names the order:

     ```json
     {"kind": "resolve_exception",
      "payload": {"case_id": "<case id>", "action": "close_unknown_order",
                  "note": "<what the broker evidence shows>",
                  "evidence": {"kind": "BROKER_STATEMENT", "reference": "<statement, confirm or support reference>"}}}
     ```

     The terminal's Operations > Exceptions form offers only `acknowledge` for these cases. Acknowledging resolves the case but does **not** close the order or release its reservation, and preflight still reports `UNKNOWN_ORDERS`. Send the command through `POST /api/commands` from a logged-in session, with the `X-CSRF-Token` header, after `POST /api/auth/reauth`. There is no CLI subcommand for it.
  4. If the command is refused with `CLOSE_NOT_PROVEN`, nothing changed. The `problems` list names every unmet condition (table below). Other refusals: `OWNER_REAUTHENTICATION_REQUIRED`; `EVIDENCE_REQUIRED`; `NOTE_REQUIRED`; `ORDER_NOT_UNKNOWN` (only an `UNKNOWN` order is closed this way); `INVALID_ACTION` (the case names no order of this account).
- **Effect:**
  - the order ends `REJECTED` (no fills) or `CANCELED` (its fills stay booked);
  - its holds are released;
  - its other cases resolve with the same evidence, and its outbox rows are closed;
  - `UnknownOrderClosedByOwner` records the evidence, the reconciliation run and the lookup.

  The account must reconcile again before a green start.
- **Exit:** the next clean reconciliation, then RB-02.

| `CLOSE_NOT_PROVEN` problem | Meaning and action |
|---|---|
| `NO_RECONCILIATION`, `RECONCILIATION_INCOMPLETE`, `RECONCILIATION_NOT_IN_THIS_PROCESS`, `RECONCILIATION_STALE`, `RECONCILIATION_WITHOUT_ORDER_EVIDENCE` | No usable run. Wait for the next complete run in this process. |
| `RECONCILIATION_PREDATES_THE_ORDERS_LAST_CHANGE` | The broker reported something about the order after the run, such as a repeated Inactive report. Wait for the next run. |
| `ORDER_NOT_LOOKED_UP` | The run did not look the order up. Wait for the next run. |
| `BROKER_LISTS_THE_ORDER_AS_OPEN`, `BROKER_LOOKUP_FINDS_THE_ORDER:<status>` | The broker still knows the order. Cancel it at the broker (RB-03); do not close it. |
| `RECONCILIATION_DIFFERENCES:<kinds>` | Resolve those differences first (RB-04, RB-05). |

## RB-14 Dispose of inventory an ended session left behind

- **Trigger:** a blocking `POST_COMPLETION_ADJUSTMENT` case. The broker reported a late fill, a bust or a correction after the session ended (ADR-0008 amendment). The case detail names the execution or bust, the session's state and `booked_to`: `protected:<id>` after completion, `unallocated` after close.
- **Already done by the system:**
  - the effect is booked to `booked_to`, never to the ended session. The session's state, outcome, protected value and successor do not change;
  - any inventory it left (a late buy fill, or shares that a busted sell had delivered) sits in that owner's lots. Portfolio > Holdings lists it by owner;
  - a cash shortfall moves to `exception:<case>` as an `OVERCOMMITTED` case, which the owner funds with `resolve_exception` action `cover_shortfall`;
  - a fill on an order that was already terminal also opens an `EXECUTION_ANOMALY`;
  - new entries are blocked while the case is open.
- **Owner:**
  1. Confirm the late execution, bust or correction against the broker's trade confirmation.
  2. Resolve any open `EXECUTION_ANOMALY`, `POSITION_MISMATCH`, `FOREIGN_ACTIVITY` or `LEDGER_INVARIANT` case on the account first. Each of them blocks every sell, the disposal included (`OPEN_EXCEPTIONS`).
  3. Sell the residue with an owner `submit_intent` whose purpose is `DISPOSAL`:

     ```json
     {"kind": "submit_intent",
      "payload": {"account_key": "ACCOUNT", "session_id": "<the ended session>", "order_id": "<new order id>",
                  "instrument_id": "<instrument>", "side": "SELL", "quantity": "<at most what booked_to holds unreserved>",
                  "limit_price": "<inside the price collar>", "order_type": "LIMIT", "time_in_force": "DAY",
                  "deadline_ns": 0, "policy_hash": "<the session mandate's policy hash>", "purpose": "DISPOSAL"}}
     ```

     Set `deadline_ns` to a real deadline in nanoseconds; the 0 above is a placeholder. The order ticket has no purpose field: it sends ENTRY or EXIT, and pretrade refuses those for an ended session. So send the intent through `POST /api/commands`. The full pretrade runs, with the owner's disposal standing in for the session-state check, and the recorded decision says so.

     Refusals:
     - `UNAUTHORIZED`: only the owner disposes;
     - `INVALID_FIELD`: a disposal is a SELL;
     - `SESSION_NOT_ENDED`: a running session sells with EXIT;
     - `OWNED_QUANTITY`: more than `booked_to` holds unreserved;
     - `OPEN_EXCEPTIONS`, and the quote, collar and deadline checks.
  4. The fill, fee and settlement book to the same owner. Proceeds in `protected:<id>` stay protected and never fund another session (ADR-0008). The disposal's own fill and first commission open no new case.
  5. After a clean reconciliation, resolve the `POST_COMPLETION_ADJUSTMENT` case with a note.
- **Exit:** that owner holds no lots of the instrument, reconciliation is CLEAN and the case is resolved.

## RB-15 Broker-event journal full, unwritable or corrupt

- **Trigger:** the core log shows `broker events cannot be journaled; not taking events from the broker: ...` at CRITICAL. The stop is latched with reason `EVENT_JOURNAL_UNWRITABLE: <error>`, and an `INCOMPLETE` reconciliation makes preflight report `RECONCILIATION_REQUIRED`. `unknown.cli status --account ACCOUNT` and `GET /api/health` show `event_journal`: its path, journal id, `failure` and the number of `unapplied` entries.
- **Already done by the system** (ADR-0012 amendment):
  - no further events are taken from the broker. At most one batch waits in the gateway's memory, and the rest stays in the adapter;
  - entries already journaled are still applied in order;
  - the stop is latched: buys are canceled and no new buy is transmitted, while sells and cancels still go out. A reconciliation is required before a green start;
  - a failure is reported once per episode, and again if its cause changes. If the owner clears the stop while the journal still fails, the stop and the reconciliation requirement are set again.
- **A. Full or unwritable** (`SQLITE_FULL`, an I/O error, permissions; `failure` names a `JournalUnavailable`):
  1. Free disk space or fix the volume or permissions.
  2. The next successful append ends the failure. The events waiting in memory and in the adapter are then journaled and applied in broker order.
  3. Wait for a clean reconciliation, then RB-02.
- **B. Corrupt, or not this core's history.** The `failure` names one of:
  - `JournalCorrupt`: a checksum mismatch or a broken chain;
  - `JournalConflict`: an entry's command id is held by the core for a different command, for example a journal file rolled back to an older copy;
  - a refusal: the file belongs to another account, or has another format or journal id.

  The verified entries before the broken one have been applied, and nothing is appended behind it.
  1. Stop the core (`launchctl bootout`, as in RB-10).
  2. Move the journal aside together with its `-wal` and `-shm` files, if present, into `data/replaced-events-<time>/`. Keep it for investigation. There is no CLI for it; `unknown.gateway.journal.EventJournal(path, account_key, now_ns)` gives `verify()` and `entries()`.
  3. Start the core. It creates a new journal with a new journal id, and nothing from the old one is applied.
  4. Reconciliation recovers what the broker still reports: executions inside its history window, and statuses through open orders and lookups. A gap beyond the window needs statements and an owner attestation (`unknown.cli broker-admin --account ACCOUNT ack-history-gap <day> --note ...`). Before green start, compare the old journal's unapplied entries with the reconciled ledger.
  5. RB-02.
- **Not covered by the journal:** events the adapter had received but the gateway had not appended when the process died. Reconciliation is their only recovery, inside the broker's history window.
- **Housekeeping:** the journal is not in the online backup and is never pruned. Include its size in disk-space checks.
- **Exit:** `event_journal.failure` is null and `unapplied` is 0 in health, a reconciliation is clean, and a green start follows.
