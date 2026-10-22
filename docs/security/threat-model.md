# Threat model

Status: written 2026-10-22 against `impl/foundation`. It covers the processes that exist today: core, API, owner CLI, IBKR adapter, agent layer and research data. It is reviewed whenever a process, token, credential or network path is added.

This is a single-owner system on the owner's Mac, with a documented AWS path (docs/deploy/aws.md). The assets that matter are, in order:
1. the authority to place orders and move capital (broker session, execution ownership, the stop latch);
2. the integrity of the economic record (journal, holds, lots, settlements, event log);
3. secrets (broker login, which the system never holds; model-provider keys; the owner passphrase hash; socket tokens);
4. licensed data and the owner's private statements;
5. availability of the red stop.

## Trust boundaries

```
browser ──HTTP 127.0.0.1──▶ API process (read-only DB) ──Unix socket + API token (OWNER)──▶ core (single writer)
owner CLI ─────────────────────────────── Unix socket + API token (OWNER) ───────────────▶ core
agent worker (model key, no broker creds) ── Unix socket + agent token (AGENT only) ─────▶ core
strategy runner (no broker creds) ───────── Unix socket + strategy token (STRATEGY only) ─▶ core
core ──TWS API socket 127.0.0.1:4002──▶ IB Gateway (owner logged in, 2FA) ──▶ IBKR
agent worker ──HTTPS──▶ model provider          research fetchers ──HTTPS──▶ SEC, ECB, OpenFIGI, ...
```

## Threats and controls

| # | Threat | Control | Where enforced or tested |
|---|---|---|---|
| T1 | Another local user or process sends commands to the core | Socket in a `0700` directory, mode `0600`. A per-client token file (`0600`) decides which roles a client may assert. Tokens are compared in constant time. | `service/ipc.py`, `service/paths.py` (refuses group- or world-readable token files) |
| T2 | An agent or strategy escalates its authority | Each token is bound to one role. The core permission table is checked **before** command-id dedup, so a refused command never claims an id. Reserved id prefixes belong to the system. A strategy may act only under its own artifact id and only in sessions whose mandate lists it. | `domain/principal.py`, `core/desk.py`; `test_desk_commands` permission matrix (every role and command kind), `test_strategy_decisions` |
| T3 | A command is replayed or reused with different content | Durable command ids with payload and principal comparison. A conflict is recorded as `CommandIdConflict` and refused. | `core/desk.py`; `test_desk_commands` idempotency tests |
| T4 | The economic record is tampered with on disk | Every event column is hash-chained (`verify_chain`). Replaying the log into an empty database must reproduce every projection. Ledger invariants are verified after every command. *This is a diagnostic, not a security boundary*: anyone who can write the file can rewrite the whole chain. Protection relies on the OS account and off-host backups. | `core/replay.py`, `core/ledger.py`; `test_desk_commands` hash-chain tests, `test_replay_randomized` |
| T5 | A restored or copied database reissues orders or loses the stop | Every backup is marked inside itself. A stale or missing epoch marker is treated as a restore. On restore the stop latches, in-flight orders become UNKNOWN (looked up, never resent) and open buys are cancelled. | `core/desk.py open_desk`, `store/db.py`; `test_control_recovery` restore tests, `test_ops_backup` |
| T6 | Two deployments trade one broker account | The writer lease fences a second process on the same database. Outside SIMULATED, ownership needs a release certificate or an owner attestation. Foreign orders and executions latch the stop. | `store/db.py`, `core/handlers/admin.py`, `core/handlers/broker.py`; `test_control_recovery` |
| T7 | Connecting to the wrong (live) broker account | Only `mode: PAPER` is accepted. The port must match the paper profile. The managed-account list at handshake must equal the configured paper id, otherwise the adapter latches `DISCONNECTED_UNSAFE` and is never retried. Preflight compares the broker-reported mode with the registered mode and fails closed when it is unknown. LIVE is refused in the pretrade MODE check. | `broker/ibkr/config.py`, `adapter.py`, `core/handlers/control.py`; `test_ibkr_adapter`, `test_control_recovery` |
| T8 | Browser attacks on the terminal (CSRF, session theft, clickjacking) | `HttpOnly` / `SameSite=Strict` cookie, a CSRF token on every state change, strict CSP, `frame-ancestors 'none'`, bound to 127.0.0.1. Logins are rate-limited. Sensitive commands need step-up re-authentication, and the core independently checks freshness for `start` and `broker_admin_action`. | `api/server.py`, `api/auth.py`; API tests |
| T9 | The red stop is unavailable | The stop needs no step-up. It goes through the same latch path for every source, survives restarts, and is transmitted even while reconciliation is dirty. If the core is down, the broker's own interface is the out-of-band path (RB-01). | `core/handlers/control.py`, `orders.py outbox_begin`; `test_reconciliation`, `test_control_recovery` |
| T10 | Prompt injection makes an agent act | Agents hold no broker credentials and no write handle. Their only core write is a typed, non-executable `submit_research_proposal`. Tool calls go through a default-deny gateway that offers read-only tools. Document text is rendered as untrusted data behind a random nonce. | `agents/tools.py`, `agents/evidence.py`, `core/handlers/control.py`; `test_agents_*` |
| T11 | Runaway model spend | Every call reserves against the lifetime, daily, per-job and per-provider caps before any request. Unknown usage stays reserved. Tariffs must be owner-approved. The live CI job is capped per run and fails on skip. | `agents/budget.py`, `agents/tariffs.py`; `test_agents_budget`, `.github/workflows/live-anthropic.yml` |
| T12 | Secrets or account ids reach the public repository | Owner configuration lives outside git (`~/.unknown`). A pre-commit hook and a CI guard block account-id patterns and key prefixes. The IBKR config loader refuses a file inside any git work tree. | `scripts/hooks/pre-commit`, `.github/workflows/ci.yml`, `broker/ibkr/config.py` |
| T13 | Supply chain | The core money path is standard library only. The IBKR SDK comes from the official zip with a pinned SHA-256 and a fingerprint check at load. The agent SDK and its transitive dependencies are pinned to versions at least 14 days old. CI actions are pinned by commit SHA. | `scripts/setup/ibkr_venv.sh`, `broker/ibkr/adapter.py load_sdk`, `backend/requirements-agents.txt`, `.github/workflows/*` |
| T14 | Licensed data misuse | The provider registry records the allowed uses per dataset. The data-use gate refuses to send a dataset to a model provider unless `model_processing_cloud` is allowed. | `data/registry.py`, `agents/gate.py`; `test_agents_gate` |
| T15 | Broker login automation weakens 2FA | Not built, by policy. The owner logs in manually, and the service reconnects without credentials. | CLAUDE.md, docs/deploy/macos.md, aws.md |

## Accepted risks and open items

- **Same-user compromise.** Malware running as the owner's macOS user can read tokens and rewrite the database. Mitigations are OS hygiene, FileVault and off-host encrypted backups. A separate service user for the core is a later hardening step.
- **Passkeys.** WebAuthn is deferred (ADR-0013); the passphrase uses scrypt.
- **Encryption at rest.** Backups are not encrypted by this tool; rely on FileVault or an encrypted destination (RB-10).
- **No TLS on localhost.** The API is plain HTTP on 127.0.0.1. Remote access must go through SSH or SSM tunnels, never a public bind.
- **Hash chain without an external anchor.** The chain proves internal consistency only. Periodically publishing the chain head somewhere the host cannot rewrite, for example an append-only S3 object lock, is an option for the AWS path.
