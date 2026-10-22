# Project Unknown — implementation status

Last updated: 2026-10-22 (increment I1 in progress). Branch: `impl/foundation` (from `docs/technical-book-v1` @ 83df445). Nothing is merged to `main`. No live trading, paid services, data purchases or cloud resources have been used or enabled.

Status words follow ADR-0001: `implemented` means code exists; `tested` means deterministic offline tests pass; `paper-verified` needs an identified IBKR paper account (none yet). No capability is paper- or live-verified.

## Repository housekeeping (done)

- Remote branch `feat/paper-desk` deleted on 2026-10-22 after verifying its tip was `83df44519bdb9107f3276d4e83ffa906ad9779e2`, with 0 commits not on `docs/technical-book-v1`, no PR, and created by the owner account. The book branch and PR #1 are untouched.
- Implementation branch `impl/foundation` has no upstream tracking to the book branch; push only with `git push -u origin impl/foundation`.

## Increment I1 — core foundation (offline, deterministic)

| Area | Status | Where |
|---|---|---|
| Exact arithmetic, money/price/quantity/FX types | implemented, tested | `backend/src/unknown/domain/` |
| Event store, idempotent commands, stored rejections, hash chain, writer lease/fencing | implemented, smoke-tested | `store/`, `core/desk.py` |
| Journal with owner dimension, holds, lots, invariant verification | implemented, smoke-tested | `core/ledger.py`, `core/projector.py` |
| Policy documents, mandates | implemented | `policy/model.py`, `config/policies/*.json` |
| Session state machine (fund, start, wind-down, complete/close, single successor, settlement) | implemented, smoke-tested end to end | `core/handlers/sessions.py` |
| Order lifecycle, outbox, UNKNOWN outcomes, cancel races, corrections, external/foreign activity | implemented, smoke-tested | `core/handlers/orders.py`, `broker.py` |
| Pretrade risk (deterministic, full diagnostics) | implemented | `risk/pretrade.py` |
| Red stop / green preflight / restart hold / restore detection | implemented | `core/handlers/control.py`, `admin.py`, `core/desk.py` |
| Reconciliation (opening import, orders, positions, cash, evidence-based settlement) | implemented, smoke-tested | `core/handlers/recon.py`, `reconcile/reconciler.py` |
| Valuation: target observation, drawdown, loss budgets from high-water mark | implemented | `core/handlers/valuation.py` |
| Replay equivalence (events -> projections byte-for-byte) | implemented, smoke-tested | `core/replay.py` |
| Deterministic simulated broker with fault injection | implemented, tested (20 tests) | `broker/simulated.py` |

In progress (background workflows on 2026-10-22): a fault-matrix test suite (8 test files) and an adversarial code review. Their findings are fixed in the next commits; this table moves to `tested` only when those suites pass on Python 3.12 and 3.14.

Known open items in I1:
- The stop command and broker observations must not be blocked by a ledger-invariant failure. They should commit, open a blocking exception and latch. Today every command aborts on an invariant failure. Fix pending.
- Self-cross (wash-trade) prevention in pretrade is not yet implemented.
- The XNYS holiday calendar is not loaded: settlement *expectations* fall back to weekdays-only, and the calendar id is recorded on each settlement row. Settlement *confirmation* is evidence-based and unaffected.
- The fee model in `config/policies/*.json` is labelled UNVERIFIED pending the IBKR capability record.

## Parallel tracks

- **Capability register (PAR-01..03):** a primary-source research workflow over Bloomberg Terminal, LSEG Workspace, S&P Capital IQ Pro, IBKR, data providers, model providers, regulation and AWS is running. Output lands in `docs/capabilities/`.
- **Book review:** 134 verified contradictions and omissions (11 refuted). Raw files live in the session scratchpad; they will be published as `docs/design/book-resolutions.md` with a disposition per item.

## External blockers (cannot be resolved by implementation)

- **IBKR paper integration (BRK-02):** needs an explicitly identified IBKR paper account, its credentials entered by the owner locally (never in the repository), and TWS or IB Gateway installed on the Mac. None of these exist on this machine (checked 2026-10-22: no TWS or Gateway in /Applications, no Java runtime).
- **Live trading:** account entity and type, residence and eligibility, authorized capital, entitlements and paid budgets are all unset (`config/deployment_record.example.json`). Live mode is refused in code.
- **Paid model and data providers:** disabled until the owner configures budgets and keys.

## How to run

```bash
cd backend
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src /opt/homebrew/bin/python3.12 -m unittest discover -s tests -v
```

## Next concrete steps

1. Fold in the test-suite and review findings; fix the core; commit with green suites on 3.12 and 3.14.
2. Publish the capability register and the book-resolution register with dispositions.
3. I3: core service loop, authenticated API process and browser terminal over the existing commands and projections.
4. I2: IBKR adapter built from the verified IBKR integration record, with recorded-fixture tests. Paper verification waits on the owner's paper account.
