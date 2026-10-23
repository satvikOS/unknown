# ADR-0003: Storage engine and durability settings

Status: accepted (2026-10-22)

## Decision

1. The core persists to **one SQLite database file on local storage** of the execution host. It is never on a network filesystem. Only the core process opens it read-write. Read-only consumers (API, terminal projections, reports) open it with `mode=ro` or read published snapshots.
2. Settings: `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON` and `busy_timeout`. On macOS also `fullfsync=ON` and `checkpoint_fullfsync=ON`, because macOS `fsync` does not flush the drive cache. The SQLite version in use is recorded at startup and checked against the advisories listed on sqlite.org/wal.html (book S35) before any release.
3. Every command runs in one `BEGIN IMMEDIATE` transaction. It performs the dedup check, authorization, the decision, appending events, updating projections and storing the result. Either all of that commits or none of it does.
4. Money values are stored as canonical decimal TEXT and parsed back exactly (ADR-0004). SQL arithmetic or aggregation over amount columns is forbidden, because SQLite would convert to binary floating point. Aggregation happens in the core with exact arithmetic.
5. Schema changes are numbered migrations that run inside a transaction and are recorded in `schema_migrations`.
6. Backups use the SQLite online backup API to a separate file, then encryption and an off-host copy. Restore drills are scripted in `docs/runbooks`. A restored ledger stays in recovery mode until it is reconciled against broker activity that occurred after the backup.

## Reasons

Single host, single writer and modest volume, with no operational burden from a separate database server. The book (Chapter 8) allows SQLite where single-host durability suffices. WAL lets read-only consumers proceed without blocking the writer.

## Reopen when

Multiple hosts need concurrent writers, which the design forbids for execution anyway (ADR-0011); measured write latency or volume exceeds SQLite's comfortable range; or a managed service is needed for regulated retention.

## Amendment (2026-10-22): the broker-event journal and the stop-latch file

Implemented on `impl/foundation` at 8b98d00 (book-review findings BR-ORD-20 and BR-SES-13). The core database stays the single source of economic truth. Two more files now sit next to it in `<UNKNOWN_HOME>/data/`, and only the core service writes them:

- **`<account>.events`: the per-account broker-event journal** (`gateway/journal.py`; ADR-0012 amendment). It is a separate SQLite file opened with the same settings as the core database (item 2 above: WAL, `synchronous=FULL`, `fullfsync` on macOS). Every broker event the gateway takes from an adapter is appended to it in one fsync'd transaction before it is applied. The `entries` and `applied` tables are append-only (triggers refuse UPDATE and DELETE). Each entry carries a SHA-256 hash chained to the previous one and is re-verified before it is applied. The file records its account, journal id and format; a journal of another account or format is refused. Marks are committed with `synchronous=NORMAL`: a mark lost to a power failure only causes a duplicate re-application, which the core answers with the stored result. The journal is **not** part of the online backup (item 6) and is not pruned. A restore of the core database leaves it in place, and the next process re-applies what the restored core is missing, provided the restored core holds at least one of the journal's entries.
- **`<account>.stop-latch.json`: the memory stop latch** (ADR-0010 amendment). It is written, fsync'd and renamed into place only while a red stop is latched in memory because the database could not record it, and removed once the database has recorded it.

A disk-full or I/O failure on the journal fails closed: the gateway stops taking broker events and the stop latches (`EVENT_JOURNAL_UNWRITABLE`). A latch file that cannot be written leaves the latch in process memory only, logged CRITICAL. The runbooks are RB-12 and RB-15.
