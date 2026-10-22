# Market mechanics and the lifecycle of a trade {#market-mechanics}

A market is a collection of rules for matching interests, establishing ownership and settling obligations. The API is only one interface to those rules. A robust desk therefore models an instrument, a venue, an order, an execution, a position, a cash obligation and settlement as separate objects.

## Quotes, trades and price conventions

Let the best bid be $b_t$, the best ask $a_t$, and the corresponding displayed sizes $B_t$ and $A_t$. The arithmetic midpoint and quoted spread are

$$m_t=(a_t+b_t)/2,\qquad s_t=a_t-b_t. \tag{2.1}$$

The relative spread in basis points is $10^4s_t/m_t$. With bid 99.98 and ask 100.02, the spread is 0.04 dollars, or 4 basis points. Buying at the ask and immediately selling at the bid loses 0.04 per share before fees, even though the midpoint never changes. A backtest that buys and sells at the midpoint has ignored an economically important cost.

Last trade, midpoint, bid, ask, indicative auction price and broker mark are different observations. Use a valuation policy appropriate to the purpose. A conservative liquidation estimate for a small long position may begin at the executable bid, then account for size and costs. A midpoint can be useful for measuring gross forecasting skill, but is not a promise of a fill.

A size-weighted quote proxy is

$$w_t=\frac{a_t B_t+b_t A_t}{A_t+B_t}. \tag{2.2}$$

It moves toward the ask when displayed bid size is larger. This is a simple imbalance proxy, not a universally correct efficient price or a complete microprice model. Handle zero total size, crossed quotes, stale sides and venue-specific semantics. Hidden liquidity and cancellations can invalidate the visible signal.

## Order books and execution priority

A limit order specifies a price boundary. A marketable limit can execute immediately against available liquidity while constraining price. A resting limit may wait, partially fill, lose queue priority on amendment or never execute. Matching rules vary by venue and product; price-time priority is not a universal assumption.

An order acknowledgment means the broker or venue accepted a message at a particular stage. It is not a fill. A cancellation acknowledgment ends the remaining working quantity, but executions that occurred before cancellation can arrive later. An order status callback is not always the only evidence of an execution. Persist execution identifiers separately.

The minimum lifecycle is intent, local validation, reservation, submission, acknowledgment, zero or more executions, cancellation or completion, reconciliation and settlement. Never turn network timeout into a terminal order state. The sender may have transmitted successfully even if the response was lost.

## Settlement and cash

Economic exposure begins when a trade executes. Settlement transfers the relevant cash and securities under the instrument's rules. In a cash-only policy, proceeds can be recognized economically while remaining unavailable for new allocation. Many US securities transactions use T+1; use a business calendar and product-specific rule, not an unconditional 24-hour timer. [S01]

A Friday execution can settle after the weekend and an intervening holiday. Cross-currency obligations can have different calendars. A trade correction can change cash or quantity after the initial report. Record settlement status from authoritative broker evidence and preserve the original execution history.

## Trading sessions and market events

Model regular trading, extended hours, auctions, holidays, half-days, suspensions and maintenance separately. The platform can operate continuously while a particular market is closed. A strategy approved for continuous matching must not automatically participate in an auction. An overnight order may have a different liquidity and protection profile from a regular-session order.

Corporate actions change the relationship between historical prices, quantities and cash. A two-for-one split doubles shares and halves comparable per-share prices; it does not double wealth. Dividends create cashflows and affect total-return series. Mergers and delistings can terminate symbols and create successor instruments. Keep raw execution prices for accounting and adjusted series for the research purpose that calls for them.

## Worked lifecycle

Suppose a paper account has 1,000 settled dollars. Reserve 100.50 for an order to buy ten shares at a 10.00 limit plus a 0.50 cost buffer. Four shares fill at 9.99 with a reported fee of 0.10. The remaining reservation covers six shares and unresolved charges. A cancel is sent. Two more shares fill before cancellation takes effect. The final position is six shares, not four. Only after final remaining quantity is confirmed zero may unused commitment be released.

If those shares are sold, the sale proceeds become a settlement receivable under the cash policy. The system records realized trading PnL using actual fills and fees, while separately tracking when the proceeds become eligible for another session. The distinction is the basis of Chapter 9's ledger.

## Implementation exercise

Represent the example as an event fixture with shuffled delivery of the cancel acknowledgment and final execution. The resulting holdings, costs and unfilled quantity must be identical after reconciliation. Duplicate the execution event and confirm it is not applied twice. Replay the same market messages under different UI refresh rates; order and accounting outcomes must remain unchanged.
