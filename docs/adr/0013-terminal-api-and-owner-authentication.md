# ADR-0013: Terminal, API process and owner authentication

Status: accepted (2026-10-22)

## Context

Book Chapter 22 specifies the terminal's workspaces, its controls and its client contract: a typed client, sequence-numbered server events, snapshot on gap, and no optimistic updates for money or stops. The book review found that owner identity and step-up authentication are never defined, and that R13's "signed policy" contradicts the unsigned schema. The owner develops on a Mac and may deploy on AWS behind private access.

## Decision

### Processes

1. **`unknown-core`** (`python -m unknown.service.core`) owns the desk (the only database writer), the broker adapter, the gateway loop, reconciliation, valuation and session ticks.
   - It accepts commands only through a **Unix domain socket** with mode `0600`, inside a runtime directory with mode `0700`.
   - Each client presents a token from its own `0600` secret file.
   - The token decides which principal roles the client may assert. The API token may assert `OWNER`; an agent token may assert only `AGENT`.
   - The socket also serves read-only *queries* that need live process state: broker session health, a preflight dry run, and the quote book.
2. **`unknown-api`** (`python -m unknown.api.server`) serves the browser terminal.
   - It opens the database **read-only**, holds no broker credentials and no write handle.
   - It forwards owner commands to the core socket.
   - It binds to `127.0.0.1` by default. On AWS it is reached only through private access such as an SSM port-forward; it is never published to the internet.
3. **Browser terminal** (`terminal/`, TypeScript, React, Vite) is built into static files served by the API.
   - Rendering performance is measured separately from core latency (PRF-01).

### Owner authentication

- **Passphrase.** The owner sets a passphrase with `python -m unknown.cli set-passphrase`. The core stores only an scrypt hash (`hashlib.scrypt`, parameters recorded with the hash) in `~/.unknown/secrets/owner.json` (`0600`), outside the repository.
- **Login.** Login returns an opaque session id in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` when served over TLS), plus a CSRF token. Every state-changing request must echo the token in `X-CSRF-Token`.
- **Rate limiting.** Failed logins are rate-limited with exponential backoff per process.
- **Step-up.** These commands require a passphrase re-entry no older than the active policy's `reauth_max_age_s`:
  - `start` (green);
  - `publish_policy` and `activate_policy`;
  - `authorize_capital` and `deauthorize_capital`;
  - `acquire_execution_ownership` and `release_execution_ownership`;
  - `resolve_exception`.

  The core independently re-checks `authenticated_at_ns` for `start`.
- **Red stop never requires step-up.** It needs only a valid session, so it is always one action away.
- **Passkeys.** WebAuthn passkeys are a later hardening step. The standard library has no CBOR/COSE support, and adding a dependency to the authentication path deserves its own decision.

### Policy integrity (R13)

A policy's authority comes from the authenticated owner command that publishes it (recorded with principal, time and command id in the hash-chained event log). A cryptographic signature is not used: the signing key would live on the same host, so it adds no protection against a host compromise. This resolves the book's "signed policy registry" wording. It is revisited if policies are ever authored on a different machine from the one that executes them.

### Terminal contract

- **State stream.** `GET /api/stream` (Server-Sent Events) carries `{seq}` change notifications. The client refetches the affected snapshots, and refetches everything on reconnect or when it sees a gap.
- **No optimistic updates.** No optimistic updates are made for orders, funds, sessions or control.
- **Labels on every number.**
  - Every amount carries its currency, and money is shown as realized, unrealized, settled, unsettled or protected.
  - Every quote shows its source and age.
  - Fixture data is labelled `FIXTURE`.
  - Simulated mode shows `SIMULATED` permanently in text and colour.
- **No dead controls.** Workspaces appear in navigation only when backed by implemented services. Unbuilt capabilities appear only on the Capabilities page, with their register status.
