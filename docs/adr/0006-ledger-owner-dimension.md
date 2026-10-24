# ADR-0006: Economic journal with an owner dimension; holds for reservations

Status: accepted (2026-10-22)

## Context

The book separates economic assets (settled cash, receivables, inventory, fee liabilities) from policy buckets (unallocated capital, session allocations, protected proceeds, reservations, buffers). It warns that adding the buckets to broker equity double counts money (Chapter 9). The reference engine still sums `unallocated + protected + session cash + inventory cost + receivables` as its conservation identity, so in that code the buckets are the assets. That conflation does not survive real broker cash, external trades or multiple currencies.

## Decision

### Economic journal

Postings are double-entry. Each posting carries `(entry_id, line, account_code, owner, currency, amount)`. Amounts are signed, with debit positive and credit negative. Every journal entry sums to zero **per currency**. Accounts are scoped to one broker account (`account_key`). Inventory postings also carry `instrument_id`.

| Code | Class | Meaning |
|---|---|---|
| `CASH` | asset | Broker-confirmed settled cash |
| `RECEIVABLE` | asset | Sale proceeds and other inflows awaiting settlement |
| `PAYABLE` | liability | Purchase cost and charges awaiting settlement |
| `FEES_ACCRUED` | liability | Estimated fees not yet finalized by the broker |
| `INVENTORY` | asset | Cost basis of open lots (per instrument) |
| `OPENING` | equity | Value present when the account was first imported |
| `CONTRIBUTED` | equity | External deposits minus withdrawals after import |
| `REALIZED` | income | Realized trading gain (credit) or loss (debit) on disposal |
| `COMMISSION` | expense | Broker commissions (final) |
| `FEE_ESTIMATE` | expense | Estimated fees pending finalization |
| `OTHER_FEES` | expense | Exchange/regulatory/other charges |
| `DIVIDEND` | income | Cash distributions |
| `WITHHOLDING` | expense | Tax withheld at source |
| `INTEREST` | income/expense | Credit or debit interest |
| `ROUNDING` | expense | Residuals from explicit broker-amount rounding |
| `SUSPENSE` | asset/liability | Unexplained differences under an open exception |

### Owner dimension (policy buckets)

Every posting names exactly one **owner**:

- `outside`: present in the account but not authorized to the system.
- `unallocated`: authorized capital not assigned to any session.
- `buffer`: operational fee buffer.
- `session:<id>`: a session's working capital.
- `protected:<id>`: a completed session's protected proceeds.
- `external`: unattributed external activity, such as manual trades.
- `exception:<case>`: quarantined value.

Moving value between owners is an **owner transfer**: two postings with the same account code and currency, opposite signs and different owners. Owner transfers balance per account code as well as per currency. Policy buckets are therefore a dimension *of* real economic balances, not additional assets, so double counting is impossible by construction: the account's economic total is the same however it is partitioned.

### Holds (reservations)

Order reservations are rows in `holds`, written in the same transaction as the order and its outbox entry; they are not journal postings.

- Cash hold for a buy: `ceil(leaves_qty × limit × multiplier) + remaining fee envelope`.
- Quantity hold for a sell: `leaves_qty` of the session's lots.

Holds are recomputed from order state on every order transition and released only on a terminal broker state or conclusive reconciliation (ADR-0009).

**Available cash** for owner `o` in currency `c` is `CASH + PAYABLE + FEES_ACCRUED − Σ cash holds`. `PAYABLE` and `FEES_ACCRUED` carry credit (negative) balances. Receivables are never available.

At account level, new allocations are further bounded by `min(broker settled cash, ledger CASH total)` minus every non-`outside` claim, which is book Equation 9.1 with disjoint terms.

### Positions

`lots` holds `(lot_id, account_key, instrument_id, owner, open_qty, open_cost, opened_by_execution, opened_at)`. Disposal is FIFO within the selling owner by default. The lot method is a policy field. Session attribution is not tax-lot attribution.

### Settlement and fees

- **Buy fill:** debit `INVENTORY` and credit `PAYABLE` for `qty × price × multiplier`. Debit `FEE_ESTIMATE` and credit `FEES_ACCRUED` for the estimated fee.
- **Sell fill:** credit `INVENTORY` for lot cost, debit `RECEIVABLE` for proceeds, and post the difference to `REALIZED`. Estimated fees are accrued as for a buy.
- **Commission report (final):** reverse the estimate and post the final amount against `PAYABLE` (buy) or `RECEIVABLE` (sell). The execution is marked fee-final.
- **Settlement confirmation:** debit `PAYABLE`/credit `CASH`, or debit `CASH`/credit `RECEIVABLE`. It is confirmed only when broker settled cash agrees (ADR-0012, reconciliation). Settlement changes availability, never P&L.

### Observation path vs command path

The command path refuses actions that would make an owner's available cash or quantity negative. The observation path (real broker executions, corrections, external flows) **always records the economic fact**. If the fact breaks an expected invariant, for example an execution beyond the reservation, an unknown order, or an unexpected fee, the core opens an `exception` case that blocks new exposure and attributes the shortfall to `exception:<case>`. A genuine execution is never discarded.

### External activity

- **Unexplained deposit:** posted to `CONTRIBUTED`, owner `outside`.
- **Withdrawal:** taken from `outside` first. If `outside` is insufficient, an exception blocks new exposure.
- **Manual trade:** owner `external`. Its cash need is covered by an owner transfer from `outside`; if that is impossible, an exception is opened.

## Amendment (2026-10-24): the buy fee envelope comes from the broker's fee schedule

Status: accepted (2026-10-24). Tested offline only, not paper-verified. It supersedes the policy's `fees` block as the source of the buy fee envelope in the cash hold above.

- **Source.** A BUY's fee envelope is `pretrade_bound` of the fee schedule in `config/fees` whose `broker` is the account's provider and whose `venues`, `products` and `currency` cover the order's instrument (`risk/fee_schedule.py`, `FeeScheduleBook`). Verified items are taken as stated. Unverified items take their conservative bound: no tier discount, no cap, the per-order minimum, the remove-liquidity exchange fee on every share, and the file's `upper_bound`. The bound is taken over the three dates around the UTC date, so every venue's local trade date is covered. Amounts are exact `Fixed` at ledger scale, rounded up (ADR-0004 #4).
- **Refusal.** If no schedule covers the order, or two do, or a file fails to load, the new pretrade check `FEE_SCHEDULE` refuses the entry. Its detail is the reason: `FEE_SCHEDULE_MISSING`, `FEE_SCHEDULE_AMBIGUOUS` or `FEE_SCHEDULE_UNPRICEABLE`, with the broker, venue, product and currency, and the name of any file that failed to load. Nothing is reserved. Sells reserve no envelope and are never refused for a missing schedule, so risk can still be reduced.
- **Record.** The `OrderAccepted` event carries `fee_envelope` (amount, basis, `schedule_id` and the sha256 of the canonical file). The order row stores the amount. Replay rebuilds projections from these events, so it never re-reads the file.
- **Fill estimates.** A fill's provisional fee is that schedule's `charge` (first tier, remove liquidity). For one whole fill this is never above the envelope. Several partial fills each pay the per-order minimum in the estimate, and can exceed the envelope until the commission report arrives, as they could before. If no schedule covers the fill, the estimate uses the policy model, because a fill is a fact and is never refused.
- **Simulator.** Provider `simulated` keeps the policy's fee model (`risk/fees.envelope`), because the simulated broker charges whatever its fixture states. Research replays depend on this.
- **Example.** 300 shares at USD 1.00 on IBKR Pro Tiered US: the envelope is USD 2.011672. The old fixed policy envelope was USD 1.80 and under-reserved this order. Test: `test_fee_envelope.py`.
- **Residuals.** The strategy runtime (`strategy/coreview.py`), research sizing and the what-if and allocation analytics still estimate fees with the policy model. Against an IBKR account they can size a buy the core then refuses with `CASH`. That is a refusal, never an under-reservation. `plan_effective` remains owner-reported until a commission report confirms the Tiered plan.

## Rejected alternative

Separate bucket balances that must be kept "consistent" with economic balances. That is two sources of truth, and it is the double-counting risk the book warns about.
