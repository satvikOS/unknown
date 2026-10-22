# Instruments, currencies, identifiers and time {#instruments}

Never use a display ticker as the primary key of a financial instrument. A ticker can be reused, shared across exchanges or changed by a corporate action. The canonical instrument record must identify the economic contract and map it to each provider's identifier.

## Instrument master

An instrument record contains an internal immutable identifier, asset class, issuer or underlying, trading currency, settlement currency, venue identifiers, tick schedule, quantity increment, minimum quantity, multiplier, effective dates and status. Derivatives additionally require expiry, strike, option right, exercise style, settlement method and deliverable. Bonds require coupon schedule, day-count convention, maturity, redemption terms and accrued-interest convention.

Store broker mappings as versioned child records. IBKR contract identifiers and an Indian broker's instrument token are provider-specific mappings, not substitutes for the internal key. A mapping change must not rewrite old executions. Historical records retain the mapping version used at the time.

| Field | Representation | Validation |
|---|---|---|
| Instrument ID | Immutable string or UUID | Unique and never reassigned |
| Price | Integer mantissa plus scale | Tick schedule valid at event time |
| Quantity | Integer mantissa plus scale | Positive, increment and lot constraints |
| Currency | Explicit code and precision policy | No implicit conversion |
| Multiplier | Exact decimal/rational | Positive and contract-specific |
| Effective interval | UTC timestamps | Non-overlapping versions |
| Provider mapping | Provider, external ID, version | Verified against contract metadata |

## Monetary arithmetic

Represent accounting money as integer minor units or exact decimals with declared rounding. Price precision and cash precision are different. A stock can quote to a finer scale than the account's cash reporting precision. Fractional shares introduce another quantity scale. Compute products with checked wide intermediates before rounding the cash result.

If $p$ is currency per unit, $q$ is units and $M$ is the contract multiplier, trade value is

$$N=pqM. \tag{3.1}$$

Store the unrounded calculated amount and the broker-reported cash amount when they differ. A rounding difference belongs in an explicit reconciliation adjustment with a reason. Never repeatedly round intermediate research calculations into cash values.

A buy commitment rounds conservatively upward and includes fees. A sellable quantity rounds downward to the allowed increment. Avoid converting a decimal string into binary floating point and then back into money. Python's Decimal documentation is useful for exact decimal operations; C++ reference code uses integer amounts and overflow checks. [S02]

## Currency conversion

Choose an explicit convention. Let $x_{c\to B}$ be units of base currency $B$ per unit of currency $c$. Then base equity is

$$E_B=\sum_c x_{c\to B}E_c. \tag{3.2}$$

Conversion for reporting does not create spendable cash in another currency. A dollar balance valued in rupees remains a dollar balance until a valid conversion transaction settles or the broker's verified capability provides an eligible funding mechanism. The conservative profile disables implicit borrowing caused by a currency shortfall.

Record FX source, timestamp, bid/ask convention and staleness. For a cash outflow, use the adverse conversion side when estimating funding. Account base currency is a reporting preference; it does not mean every trade settles in that currency.

## Time model

Every event should carry event time, provider publication time when available, first observation time, local receive time and ingestion sequence. Local latency measurements use a monotonic clock. Cross-system elapsed times require synchronized wall clocks and an error bound. Do not subtract clocks from unrelated machines and report nanosecond precision without synchronization evidence.

The feature's availability time is the earliest time the system could actually use it. A filing published at 14:00 but received at 14:03 cannot influence a simulated 14:01 decision. A macro series revised in December cannot retroactively change the values used in a September backtest. [S03]

Use timezone-aware calendars for display and market rules, UTC for storage, and immutable calendar versions for replay. Daylight saving transitions create repeated or nonexistent local times. Settlement uses business days according to relevant calendars. Account daily-loss boundaries must use a configured risk calendar rather than the server's accidental timezone.

## Edge cases and tests

Test ticker reuse, different shares with the same short symbol, split adjustments, fractional quantities, expired options, bond prices quoted as a percentage of par, and a currency pair whose quote direction differs from the internal convention. Reject an order if tick metadata is missing. Do not guess a contract from a natural-language company name.

Property tests should confirm that converting to base currency and back with the same exact rate is consistent within declared rounding, that funding commitments never round below required cash, and that no negative quantity can bypass a sign check through integer overflow. Test DST transitions and market holidays with deterministic fixtures.

The instrument master is shared by research, UI, risk and execution. If each subsystem has a different symbol map, a perfectly calculated signal can still trade the wrong contract. Make mapping disagreements visible and stop the affected route until they are resolved.
