# Ledger, reservations and settlement {#ledger}

Money accounting is the foundation of the system. Use an append-only economic journal and transactional projections. Every amount has a currency and scale. A single-currency educational implementation may use integer cents; a production implementation must handle currencies and products with different precision. Never store dollars as binary floating-point balances.

## Economic accounts

Track settled cash, unsettled receivables, fee liabilities, position inventory and external capital flows. Separately track policy buckets: unallocated capital, session allocations, protected proceeds, order reservations and operational buffers. Policy buckets are claims on economic assets, not additional assets. Adding them to the broker's equity would double count money.

A purchase transfers value from cash to inventory plus expense. A sale removes inventory cost, creates a cash receivable and recognizes realized P&L. For a $40 cost-basis position sold for $50 with $1 fee, the journal debits receivable $49 and fee expense $1, credits inventory $40 and realized gain $10. Debits and credits balance. On settlement, debit settled cash $49 and credit receivable $49. Settlement changes availability, not profit.

The simplified reference uses a conserved policy-cash ledger and explicit positions/receivables rather than a complete general ledger with tax lots. The production schema in the appendix specifies journal entries, lots and fees. Its implementation remains a required gate; do not claim tax reporting from the simplified example.

## Spendable amount

Define broker-eligible settled cash $C_b$, ledger settled cash $C_l$, reconciled restrictions $H$, already committed cash $R$, protected proceeds $P$ and operational buffer $B$. A conservative availability bound is

$$A=\max\{0,\min(C_b,C_l)-H-R-P-B\}. \tag{9.1}$$

These terms must be disjoint. If a broker field already deducts an order hold, subtracting the same hold again understates availability; failing to deduct it anywhere overstates availability. Each adapter documents the semantics of every balance field. An opaque field named buying power is not interchangeable with settled unleveraged cash.

Apply the same reasoning per currency and account. A positive consolidated USD-equivalent balance cannot fund a EUR order unless actual permitted EUR cash or an explicit completed conversion exists. Do not use an automatic FX loan to bridge the difference.

## Reservation lifecycle

Before a buy, reserve limit price times remaining quantity plus a conservative fee envelope. A market order has no finite price cap; the first implementation uses price-bounded orders for new entries. A buy limit does not guarantee a fill, and unexpected fees still require reconciliation. A sell reserves owned quantity so that two strategies cannot simultaneously sell the same shares.

Partial fills consume the corresponding reservation and add the position or receivable. Remaining reservations are released only after terminal broker confirmation or conclusive reconciliation. A cancel request is not terminal confirmation. A rejected order releases its reservation only when rejection is unambiguous and no contradictory fills exist.

If an execution violates an expected limit, exceeds reserved quantity or has unexpected fees, ingest the economic fact into an exception workflow and freeze additional exposure. Production must not discard a real fill merely because it fails an invariant designed for commands. The reference simulator rejects impossible synthetic fills to expose incorrect test input; the real adapter needs the richer exception journal.

## Fees, lots and corrections

Commissions can arrive after fills. Maintain estimated and final amounts separately. A session cannot be considered fully reconciled merely because its positions are flat. If the broker later changes a fee, record an adjustment linked to the original execution and update performance without rewriting history. Retain a fee buffer before making funds reusable.

Tax-lot selection is broker- and jurisdiction-dependent. Store acquisition identifiers, dates, quantities, costs, adjustments and disposal links. Session attribution is not necessarily tax-lot attribution. A new session does not reset wash-sale analysis or tax holding periods. Corporate actions can alter quantities and basis without an ordinary trade.

Execution busts and corrections require reversal entries plus replacement events. Event identifiers alone are insufficient when a provider sends amended content under a related identifier. Keep provider version/correction fields and preserve raw messages for investigation according to data rights.

## Settlement calendar

Calculate expected settlement using instrument, venue, trade date and applicable holiday calendar; then reconcile actual broker availability. U.S. most covered securities moved to T+1 in May 2024. [S01] Do not generalize that rule to every instrument or market. Weekends and holidays make elapsed time different from business-day settlement. A timestamp timer cannot manufacture settled cash.

The state machine is expected, pending, confirmed or exception. Only confirmation from an eligible account balance and reconciliation makes proceeds spendable under the conservative policy. A delayed settlement keeps research alive and sessions waiting for funding.

## Reconciliation algorithm

At startup and periodically: fetch a complete broker snapshot with its scope; ingest executions since the last durable watermark with overlap; deduplicate; compare open orders, positions, cash and fees; classify differences; persist a reconciliation report; advance the watermark only after durable completion. Snapshot pagination and concurrent fills require a consistent cut or an explicit convergence loop.

Tolerances are typed: price-mark differences differ from missing shares. Never use a broad monetary tolerance to hide one missing order. Unknown external trades are assigned to an external-owner bucket until attributed; they still reduce available capital. Reconciliation failure blocks new entries but permits appropriately controlled risk reduction.

Acceptance tests include duplicate fills, delayed commissions, partial cancel races, restart during reservation, settlement on a holiday, an external manual trade and a broker correction. Every test ends with both journal balance and inventory conservation checks.

