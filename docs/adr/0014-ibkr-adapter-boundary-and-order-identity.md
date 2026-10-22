# ADR-0014: IBKR adapter boundary, SDK licence and order identity

Status: accepted (2026-10-22); the licence question below is an **open owner decision**.

## Verified facts (capability record `ibkr-integration`, official IBKR pages fetched 2026-10-22)

- **Distribution.** The TWS API ships only through IBKR's installers. The Python client exists in the *Latest* package: API 10.51.01, `twsapi_macunix.1051.01.zip`, SHA-256 `50a459ed09f0030df25125a177e57b69bdf576ec3a89fcb1f81fdff95bbf6316`. PyPI `ibapi` is version 9.81.1 from 2020 and IBKR disclaims pip distribution, so it is not used.
- **Licence and dependencies.** The 10.51.01 Python client is licensed **GPL-3.0-or-later**: `IBJts/LICENSE` and `setup.py` both state it. It requires `protobuf==5.29.5`.
- **No headless login.** TWS and IB Gateway need a GUI login with 2FA. Headless operation and login automation are unsupported. Re-authentication is needed at least weekly.
- **Order identity.**
  - `orderId` is client-managed and strictly increasing per clientId.
  - `permId` is unique account-wide and is 0 for trades that originate outside IB.
  - `orderRef` may be honoured only for institutional customers.
- **Execution history.** Corrections replace an earlier execution, and are signalled by a change in the digits after the final period of the `execId`. Execution history can be as short as "since midnight" on IB Gateway.
- **Host-side resubmission.** Its default ("maintain and resubmit orders when connection is restored") means an order the adapter believes failed can still be transmitted later.

## Decision

1. **Process and package boundary.** The adapter lives in `backend/src/unknown/broker/ibkr/` and imports `ibapi` at runtime from the owner's local installation. `ibapi` is never vendored into this repository. The core packages (`domain`, `store`, `core`, `risk`, `policy`) never import `ibapi` or the adapter. The adapter is loaded only by the service entry point, and only when the deployment selects `--broker ibkr`.
2. **Order identity persisted before transmission.** The port gains an optional method, `allocate_broker_order_id(client_order_id)`. The adapter durably reserves the next `orderId` before the core commits `SUBMIT_PENDING`. The core records it on the order, so a crash between transmission and acknowledgment can always be resolved by looking up a known `orderId`. Ownership tagging (ADR-0011) therefore does not depend on `orderRef`. Foreign activity is detected by:
   - an unknown `orderId` for our `clientId`;
   - other `clientId`s seen through `reqAllOpenOrders`;
   - executions with `permId` 0 or no mapping.
3. **Reconnection.** Every 1100 or 2110 disconnect forces a reconciliation before new entries (already true in the core). Any order sent inside the disconnect window is UNKNOWN until looked up. The owner is told to consider disabling host-side resubmission (`docs/integrations/ibkr.md`).
4. **Paper only.** The adapter refuses to connect unless the configured mode is PAPER. The handshake must also report exactly the expected paper account id (kept in a local config file outside git), on the configured port.

## Open owner decision: GPL-3.0

Importing a GPL-3.0 library into this project's adapter may make the *distributed* combination subject to the GPL. Personal use without distribution does not trigger distribution obligations. This repository is public, but it contains only the project's own adapter source, never `ibapi`.

The options:

- **(a)** Accept GPL-3.0-or-later for the adapter module and state it in that directory.
- **(b)** Move the adapter to a private repository or a separately licensed component.
- **(c)** Use IBKR's Web API through the Client Portal Gateway with the project's own HTTP client. This avoids the GPL SDK, but the gateway requires daily browser re-authentication.

The implementation follows (a)'s technical boundary while you decide. No licence file is added without your choice (CLAUDE.md). This is not legal advice.
