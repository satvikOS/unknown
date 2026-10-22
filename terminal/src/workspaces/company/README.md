# Company workspace

Registered as `company` (`g e`, nav order 110). It reads `GET /api/company` (backend `api/routes/company.py`), which opens the research store read-only and computes from point-in-time data as of the `as_of` parameter. Nothing here writes or fetches from the network.

## Routes

- `#/company` and `#/company/search/q=…/as_of=…`: issuer search. Candidates are ranked and never selected for you. Each source (SEC ticker snapshot, ingested issuers, OpenFIGI outcomes, core instrument master) shows its status and reason. The title says "50 of 73" when the API returns fewer candidates than it matched. An issuer opens its tearsheet at the search's as-of time; a core instrument opens in Instruments. When the API searched no SEC source (no research store), the view says so (NOT_SEARCHED) with the ingest command, rather than just "no candidates".
- `#/company/<cik>/<tab>/key=value…` with tabs:
  - `tearsheet`: the typed blocks of CAP-CO-12. Each block shows its status, source, as-of time, newest input and age, and fails on its own. A block without content (quote NOT_CONNECTED, events NOT_LICENSED, a missing annual report) shows the API's reason verbatim.
  - `statements`: standardized FY, Q or TTM statements, with `periods` and an optional knowledge cutoff (`known_at`). Each cell shows its value with its currency or unit, or its missing reason verbatim (NOT_REPORTED, NOT_DERIVABLE, MISSING_INPUT, NOT_APPLICABLE). Select a cell to see its method, rule, concept or formula, components, source filings (SEC links) and restatement history. The coverage panel shows the mapping, reported, used and unmapped concepts, and a census of cells by method and missing reason.
  - `metrics`: every formula with its status (OK, NM, MISSING, INCOMPLETE, SUPPRESSED, NO_PRICE) and the reason verbatim. Activating a row loads the metric's calculation (`/metrics/<metric>`): the expression, inputs down to source filings, and the EV bridge.
  - `dcf`: owner assumptions form, then the model as its recipe: inputs with sources (Treasury observation id and availability), the WACC build-up, projections, terminal value, equity bridge, per-share value, the WACC x g grid, scenarios and the football field, with the price input drawn as a marker when it is in the reporting currency. When the equity bridge is INCOMPLETE, the equity value and per-share tiles carry that status, and the grid, scenarios and football field name the missing components. REFUSED and UNAVAILABLE answers are shown with their reason; the form checks syntax only.
  - `peers`: the peer rule, members, exclusions and caveats, the comps table and peer statistics (peers only). A member opens at the same as-of time.
  - `filings`: filings accepted by the as-of time, with a form filter. The acceptance time is `acceptanceDateTime` exactly as the SEC file publishes it: its "Z" is not true UTC (CAP-CO-06), so it is never converted or labelled UTC.
- Route options: `as_of`, `known_at`, `basis`, `periods`, `peers`, `forms` and the price input (`price`, `price_currency`, `price_source`, `price_time`). Every option carries across the tabs of one issuer. Opening another issuer (a search result or a peer) carries only `as_of`: a price is an observation of one listing, and carried to another issuer it would compute that issuer's market cap, EV and multiples from the wrong price. No price is ever fetched: price-based metrics stay NO_PRICE until the owner supplies all four price fields.

## Display rules

- Decimal strings stay strings, and the browser never rounds them. A currency unit renders as `Money`. Other units are shown as the API writes them (`x`, `USD per share`, `shares`).
- A `ratio` shows as an exact percentage: the decimal point is moved. In detail views the ratio is shown next to it; in tables it is in the tooltip.
- DCF rates (WACC, g, cost of equity, after-tax cost of debt, terminal share of EV) are decimals as the API writes them, with the exact percentage beside them.
- Unavailable sources show the API's reason and the command it names (for example `python -m unknown.data.sec ingest --cik <CIK>`), never placeholder data. An unavailable DCF risk-free rate names only the Treasury module, so the complete command (`python -m unknown.data.sources.treasury ingest --month YYYYMM`) is shown in its place.
- A filing without a primary document (the submissions file names none) shows its description without a link.

## API notes found while building this

- Without a research store, `/api/company/search` answers with the core instrument source only. It lists no `research.issuers` entry with a reason, so the view infers it from the missing SEC sources (`searchedIssuers` in `model.ts`) and says NOT_SEARCHED in its own words. The issuer routes say it themselves (`UNAVAILABLE` with the ingest command).
- Ticker aliases are valid from their retrieval time. An issuer ingested "later" than the as-of time is found by name or CIK but not by ticker.
- The tearsheet response is large (about 640 KB for the fixture issuer), because it embeds three full statements with every source. Like every terminal query, it is refetched on each state-stream version bump, although research data does not change with core events.
