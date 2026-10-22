# Project Unknown implementation instructions

Read `docs/book/README.md`, Chapters 1, 8-14, 20 and 25 before changing trading behavior. This repository currently contains a technical specification and OFFLINE reference code, not a production trading system.

## Authority

- Keep the default mode simulation. Never connect to a live account, place a real order, provision paid resources or enable paid model calls without the owner's explicit authorization and resolved deployment gates.
- The owner controls capital, risk parameters and production code. Agents can research and recommend changes. Automatic selection is limited to approved artifacts inside a fixed owner-approved policy.
- Never put secrets, account IDs, private statements or licensed market datasets in this public repository.
- Do not add an open-source license for original project code without the owner's choice. Review third-party licenses and data rights.

## Invariants

- Integer/decimal monetary arithmetic with explicit currency and scale; never floating-point cash balances.
- Durable command IDs and payload comparison; duplicate fills have one economic effect.
- Reserve before submission. Cancel requests do not release reservations until confirmed or conclusively reconciled.
- Unknown external outcomes remain unknown and reserved. Query before retrying an order.
- Protect completed proceeds. New sessions use independently eligible unallocated funds.
- Stop persists through restart. Start requires owner authority and fresh preflight/reconciliation.
- No borrowing, shorting, derivatives or automatic transfers in the initial cash-equity eligibility policy.
- Never discard a real broker execution to keep a local invariant looking healthy; use exception accounting and halt new exposure.

## Working method

Inspect existing code and the relevant requirement before editing. Make small reviewable changes. Preserve tests and documented limitations. Use official current API documentation and pin actual dependencies when introduced. Do not invent SDK methods or version numbers. Research code runs without production secrets or network authority.

Where things live (implementation branch `impl/foundation`, created from `docs/technical-book-v1`):
- `docs/status/STATUS.md` — durable hand-off: what is implemented, verified, blocked, and the next step. Read it first.
- `docs/requirements/register.md` — requirement IDs and status; `docs/requirements/dependency-graph.md` — build order.
- `docs/adr/` — architecture decisions (supersede book text where they say so); `docs/design/book-resolutions.md` — verified book contradictions/omissions and how each is resolved.
- `docs/capabilities/register.json` — parity capability register (vendor-referenced, dated).
- `backend/` — the Python core (standard library only on the money path). `reference/` and root `tests/` remain the book's offline companion and are not the product.

Run both suites before committing:

```bash
python3 -m unittest discover -s tests -v                                   # book companion
cd backend && PYTHONPATH=src python3 -m unittest discover -s tests -v      # core (also run with python3.12)
```

Add meaningful tests for monetary, authority or state-machine changes; every money-changing test asserts the ledger invariants (`Harness.verify() == []`). Document what is specified versus implemented versus externally verified. Passing offline tests is not evidence of profitability, live compatibility or legal compliance. Status words follow ADR-0001.

## Scope and sequencing

The objective is the full platform in the owner brief (research terminal parity work, data platform, quantitative research, bounded agents, portfolio, execution, reconciliation, monitoring) delivered through verified increments in `docs/requirements/dependency-graph.md`. The "minimal paper desk" in earlier text is an implementation checkpoint, not the product scope. Do not broaden *trading eligibility* (asset classes, shorting, margin, live routing) before the corresponding release gates and owner authorization; research and analytics breadth is not gated by that.
