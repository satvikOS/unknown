# ADR-0020: Real data only; unavailable values are an em dash with a reason

Status: accepted (2026-10-23, owner rule)

## Context

The owner's rule (2026-10-23): the platform shows only real data and real streams. No mock, fake, sample, example,
placeholder, demo, synthetic or fixture data or information may be produced, served or shown by product code. When a
value is not available the platform shows an em dash that carries a machine-readable reason, visible on hover and
focus and to assistive technology, and an error is shown as an error, as Bloomberg, Capital IQ and LSEG do. Never
zero, never a made-up value, never an "example" security shown as if it were data.

Before this decision product code generated data in five places: a SYNTHETIC market-data source (the default when no
`config/marketdata.json` existed), SYNTHETIC chart bars (used when no real source covered a security), the core's
`--fixture-market` random-walk quotes that also filled simulated orders, the `bootstrap-sim` fixture instruments,
and the research fixture study (`python -m unknown.research.study`) on generated series. API routes could serve
FIXTURE rows (`include_fixture=1`), several unavailable values were sent as `0` or an empty result, and an IBKR
subscription was described as `IBKR (REALTIME): requested` before IBKR had reported the delivered type.

## Decision

1. **No generator in product code.** `marketdata/synthetic.py`, `synthetic_factory.py`, `charts.sources.SyntheticBarsSource`,
   `service/fixture_market.py`, `research/fixtures.py` and `research/study.py` are removed from `backend/src`. Market-data
   configuration refuses the ids `SYNTHETIC`, `TEST`, `FIXTURE`, `DEMO`, `MOCK`, `FAKE` and `SAMPLE`. Without
   `config/marketdata.json` there is no source and every key is `UNAVAILABLE` with code `NO_SOURCE`. A chart for a
   security no configured source covers has no bars and `reason: NO_SOURCE`.
2. **Test doubles live only under `backend/tests/` and `terminal/e2e/support/`.** They are injected in code (a bus built
   with the double, a `CoreService` built with a TEST quote feed), never reached through configuration, environment
   variables or flags. Their source id is `TEST`. `tests/test_unavailable_reasons.py` fails if a product module names
   a removed generator or if a product entry point imports a test module.
3. **The simulated broker prices only from a configured real quote source** (`service/sim_execution.py`). A working
   order fills when a live, fresh (5 s) quote from the configured source crosses its limit. With no source nothing
   fills and the core health reports `sim_execution: {pricing: UNAVAILABLE, reason: NO_SOURCE}`; per order the reason is
   `NO_DATA` (no quote), `STALE` (delayed, frozen, stale-labelled or old) or `NO_SOURCE` (another source or test data).
   `python -m unknown.service.core` configures no source. Paper trading priced by real quotes is the IBKR PAPER service.
4. **One reason vocabulary** (`domain/unavailable.py`, `docs/design/unavailable-values.md`, served by
   `GET /api/meta/unavailable-reasons`): `NO_SOURCE`, `NOT_CONNECTED`, `NOT_ENTITLED`, `PENDING`, `NO_DATA`,
   `NOT_REPORTED`, `NOT_APPLICABLE`, `NOT_FOUND`, `STALE`, `ERROR`. An unavailable value is `null` with a code; existing
   specific codes stay in detail fields and map to the vocabulary with `classify()`. Market-data status events carry an
   additive `code`.
5. **FIXTURE rows are never served.** There is no `include_fixture` option. The research routes omit fixture-flagged
   experiments, trials, reports and artifacts and answer 404 for a direct read.
6. **Delivered type, not requested type.** A source whose provider reports the type per subscription (IBKR) declares no
   type; its new subscription reads `IBKR (delivered type not yet reported): requested`, and each quote carries the type
   IBKR delivered.

## Consequences

- A fresh install shows em dashes with `NO_SOURCE` everywhere until the owner configures a source (IBKR paper:
  `docs/OWNER_NEXT_STEPS.md`). That is the intended first experience.
- The terminal end-to-end suite runs the real core and API with test doubles injected by its harness; its data is
  test data in a temporary home, never visible to a product configuration.
- The `fixture` columns and flags in stores (bars, contract resolutions, quotes, research registry) remain as
  defensive labels: product code never sets them, and routes never serve flagged rows. Analytics still propagate a
  `fixture_data` flag if a test double sets it.
- Still open: a simulated core priced by real IBKR quotes (the simulator reads the desk's quote book; the IBKR quote
  session runs only inside the IBKR PAPER service today); a sweep of every analytics field for the `<field>_reason`
  convention beyond positions, market data, charts, research, quotes and the risk-free rate.
