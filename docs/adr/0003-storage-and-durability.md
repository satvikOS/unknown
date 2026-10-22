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
