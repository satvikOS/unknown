# ADR-0004: Exact numeric representation for money, prices, quantities and rates

Status: accepted (2026-10-22)

## Decision

1. All economic numbers use `Fixed`: an immutable exact decimal made of an unbounded Python integer mantissa and a non-negative integer scale. It never involves a hidden context. Constructing it from a `float` raises `TypeError`. Addition and subtraction align scales exactly. Multiplication adds scales. Division and any reduction of scale require an explicit target scale and an explicit rounding mode: `UP`, `DOWN`, `CEILING`, `FLOOR`, `HALF_EVEN` or `UNNECESSARY`, where `UNNECESSARY` raises if the result would be inexact.
2. Typed wrappers carry units:
   - `Money(currency, amount)` adds and subtracts only within the same currency; anything else raises `CurrencyMismatch`.
   - `Price(currency, amount)` is currency per unit of instrument.
   - `Quantity(amount)` is a count of instrument units, at the instrument's quantity scale.
   - `FxRate(base, quote, amount)` gives units of `quote` per one unit of `base`, with explicit orientation. Inversion requires a scale and a rounding mode.
3. Each currency in the registry (`domain/money.py`) has a **ledger scale**: the precision at which journal postings are stored. It is 6 for every fiat currency registered initially. A posting must be exactly representable at the ledger scale. An adapter that receives a broker amount with finer precision rounds it explicitly and records the residual (ADR-0006), so nothing is silently truncated.
4. Rounding directions are fixed by purpose:
   - Buy reservations and fee envelopes round toward more reserved cash: `CEILING` on positive amounts.
   - Sellable quantity rounds `DOWN` to the lot increment.
   - Display rounding never feeds back into stored values.
5. Broker numbers delivered as binary floats are converted through their decimal string representation (`repr`) at the adapter boundary. The raw value is retained and the result is checked against tick and lot rules.
6. Stored representation is canonical decimal TEXT: no exponent and no trailing zeros beyond the value's scale. Hashing uses the canonical form.

## Reasons

The book (Chapters 3 and 9) and the owner both require integer or decimal arithmetic with explicit currency and scale. `decimal.Decimal` depends on a mutable thread-local context, and its silent rounding at 28 digits is a hidden failure mode. An integer-mantissa type has no context, overflow is impossible in Python, and the type is small enough to test exhaustively.

## Consequences

Cash, price, quantity and FX are different types, so the type mismatch, not a reviewer, catches unit errors such as adding a price to cash.
