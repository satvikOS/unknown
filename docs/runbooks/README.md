# Runbooks

Each runbook states the trigger, what the system has already done automatically, what the owner does, and the exit condition. Commands refer to the implemented core (`backend/`). Terminal actions call the same commands.

Conventions:

- `ACCOUNT` is the internal account key, for example `SIM-1`.
- `UNKNOWN_HOME` defaults to `~/.unknown`.
- Owner CLI: `cd backend && PYTHONPATH=src python3 -m unknown.cli ...`.
- A **latch** (red stop) clears only through the owner's green start with preflight. A **condition** clears automatically when the fact changes. Examples of conditions: broker disconnected, reconciliation required, stale market data.

## RB-01 Red stop: always available

- **Trigger:** owner decision, a loss budget breach, foreign activity, a restore from backup, or 3/10/100 consecutive core-cycle failures.
- **Already done by the system:**
  - the latch is durable, so it survives restart;
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
| `BLOCKING_EXCEPTIONS:<kinds>` | RB-03, RB-04 or RB-05 by kind. |
| `UNKNOWN_ORDERS` | RB-03. |
| `LOSS_BUDGET:<kind>` | RB-07. |
| `MARKET_DATA_STALE:<instrument>` | RB-08. |
| `EXECUTION_OWNERSHIP_NOT_HELD` | RB-09. |

- **Exit:** a `StartGranted` event with its preflight evidence.

## RB-03 Unknown order

- **Trigger:** a submission timed out, the transport failed, or the process restarted while a submission was in flight.
- **Already done by the system:**
  - the order is `UNKNOWN`;
  - the reservation is retained;
  - a blocking `UNKNOWN_ORDER` exception blocks new entries;
  - nothing is resubmitted.
- **Automatic resolution (reconciliation):**
  - if the broker lists or finds the order, its status and executions are applied;
  - if the broker *conclusively* reports it never received the order, it becomes `REJECTED` and the reservation is released;
  - if the lookup cannot decide, the difference stays blocking.
- **Owner (if it stays unresolved):** check the broker's order and trade history in its own interface, then contact broker support through authorized channels. Never resend under a new id until the original is resolved.
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
  - On the Mac, `scripts/deploy/macos/com.unknown.backup.plist.template` runs it every 15 minutes (docs/deploy/macos.md). Copy backups off-host encrypted, e.g. to an encrypted volume; this tool does not encrypt.
- **Why a backup cannot be mistaken for the live database:** every copy is marked inside itself (`backup_origin`). Opening it anywhere is detected as a restore. A database whose `<db>.epoch` marker is missing or not older is also treated as restored (fail closed).
- **Restore drill:** run `python -m unknown.cli restore-drill --account <key> [--backup FILE]`. It works in a temporary directory, so live state is never touched, and exits non-zero on any problem. It checks:
  - the copy matches its recorded SHA-256;
  - the hash chain verifies;
  - replaying the event log into an empty database reproduces every projection;
  - opening the copy is detected as a restore, with the stop latched (`RESTORED_FROM_BACKUP`), in-flight orders `UNKNOWN` and a blocking exception;
  - the ledger invariants hold.
- **Real restore:** put the backup at `<UNKNOWN_HOME>/data/<key>.sqlite` and start the core. Then:
  1. Reconcile against broker activity that happened after the backup.
  2. Resolve the `RESTORED_FROM_BACKUP` case.
  3. Green start (RB-02).
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
