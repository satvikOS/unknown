# Broker adapters and account capabilities {#broker-adapters}

An adapter converts a broker's specific semantics into the system's explicit capability contract. It must not pretend that all brokers offer identical orders, sessions, fractional quantities or settlement fields. Keep raw messages and normalized events side by side where retention rights permit. Broker-specific extensions remain visible rather than being silently discarded by a lowest-common-denominator interface.

## Required interface

Provide methods to establish an authenticated session, inspect account capabilities, resolve instruments, subscribe to required data, retrieve balances/positions/orders/executions, submit a price-bounded order, request cancellation and query uncertain outcomes. Each result includes broker time if present, receipt time, request identity, scope and pagination/completeness status. A successful HTTP response is not necessarily an accepted order.

Capabilities include legal entity, permitted markets, cash/margin status, shorting, fractional support, currency behavior, time-in-force, outside-hours eligibility, supported identifiers, pacing, session-expiry behavior and cancellation semantics. Unknown capability values default to disabled. Paper and live accounts have separate identifiers and credentials; a boolean scattered through code is insufficient isolation.

## IBKR implementation path

Use the official supported API and pin the SDK and TWS/IB Gateway versions together after compatibility testing. [S04] Establish connectivity, obtain valid order identifiers, subscribe to account/order state, reconcile, then enable entry submission. Retain broker permanent identifiers and execution identifiers as well as local command IDs. Do not assume callback order is identical to network transmission order.

IBKR documents TWS request pacing as a function of market-data-line allowance; its default example is 100 lines and 50 requests per second. A streaming subscription's initial request counts differently from its subsequent updates. [S05] Implement a configurable broker-aware scheduler with reserved cancellation/reconciliation capacity. This documentation is not a promise of HFT execution latency or unlimited order throughput.

Authentication and scheduled restarts constrain unattended operation. IBKR documents daily/weekly reauthentication behavior. [S06] The service must enter an authentication-required state, notify the owner and resume only after reconciliation. Do not bypass two-factor authentication or automate credential challenges outside supported mechanisms. Research and monitoring can remain alive while order access is unavailable.

Paper trading is useful for API integration but has execution-model differences. [S07] Record which tests used local simulation, broker paper or live data; these are distinct evidence levels. Avoid presenting a simulated fill as an actual exchange execution.

## Additional brokers

Kite Connect is an example of an India-focused adapter with documented user-session and API behavior. [S08, S09] Current exchange/broker algorithmic requirements must be verified before enabling it. Alpaca's order documentation supplies another potential U.S. API integration contract. [S10] These examples are not a conclusion that the owner is eligible for either service or every instrument it offers.

The broker selection scorecard evaluates account eligibility, asset coverage, order semantics, execution quality, fees, data entitlements, authentication burden, historical replay, operational support and rights. Do not choose solely by an advertised commission. Add a broker only when it creates a concrete capability or resilience benefit that exceeds integration complexity.

## Pacing and retries

Use separate bounded queues for entries, cancels, queries and subscriptions, with a scheduler that understands shared upstream limits. Token buckets can shape average rates and bursts, but the configured contract must reflect the provider's actual counting rules. A reserved emergency capacity is useful only if the broker's own limits allow it; local priority cannot force the broker to accept requests.

Retry read-only queries with bounded exponential backoff and jitter when appropriate. Order submission is different: after a timeout, query by durable identity before resubmitting. If the broker cannot reliably correlate a request, keep the result unknown and require reconciliation. Never turn every network timeout into a second order.

## Cross-broker aggregation

Maintain separate cash, settlement and positions per legal account. Aggregate exposure for risk reporting but do not net funding across brokers. A sale at broker A does not fund a purchase at broker B until a permitted transfer actually completes. Transfers and withdrawals remain outside agent authority by default.

If two brokers hold the same stock, portfolio risk sees the combined exposure. An execution router chooses only among eligible funded accounts and includes transfer friction, commissions and data quality. Cross-broker arbitrage introduces leg risk and should not be enabled merely because two quotes differ.

## Adapter acceptance suite

Test authentication expiry, stale account snapshots, duplicate callbacks, order rejection, partial fills, cancel/fill races, reconnect, missing execution history, rate limiting and manual external trades. Compare normalized state with broker statements. Release an adapter only after every enabled capability has a verified test and unsupported fields cause explicit rejection. The companion repository currently contains contracts and simulation, not tested live broker adapters.

