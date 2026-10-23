# Markets & macro workspace

Registered as `macro` (`g m`, nav order 120). It reads `GET /api/macro`, `/api/macro/yield-curve` and `/api/macro/fx` (backend `api/routes/company.py`, `data/sources/treasury.py`, `data/sources/ecb.py`).

Route: `#/macro/as_of=…/date=…/compare=…/fx_date=…/base=…`.

- **Knowledge as of**: every panel shows only what was available at that time. The series catalog counts observations available then, so a past as-of never reveals later dates or counts.
- **Series held**: each dataset with its status, observation count, date range and newest availability time. It also lists the sources that are not available, as the API states them.
- **US Treasury par yield curve**: the curve for a date (or the latest available), with an optional comparison date.
  - The chart has one point per tenor, evenly spaced and not to scale. A missing tenor is a gap in the line and a row in the table; it is never interpolated (CAP-MAC-07).
  - The table gives each tenor's years, par yield (percent), availability time, publication and observation id, and flags such as `availability_estimated_backfill`.
  - The 10Y − 2Y slope is shown with its formula and inputs, labelled derived.
  - When the requested date has no curve at the as-of time, the API's fallback message is shown.
- **ECB euro reference rates**: each rate as published (units of the currency per 1 EUR), with its reference date, availability time, observation and flags.
  - The API's use note is shown above the table: information only, not for transactions.
  - With another base currency, the derived cross rates are listed with their formula, rounding, input observations and reference dates. A cross that cannot be built shows the API's reason.
- Without a research store, the API's reason is shown with both ingest commands (`python -m unknown.data.sources.treasury ingest --month YYYYMM`, `python -m unknown.data.sources.ecb ingest --last 1`).
- With a research store but no observation of a dataset at the as-of time, the catalog row, the curve and the rates each show the API's reason verbatim. Those reasons name no command, so the dataset's ingest command is shown with them.
- **Cross-country comparison** (`GlobalMacro.tsx`, `global.ts`; `GET /api/macro/global`): one row per venue country (US, IN and the EU countries), one column per source and definition (World Bank annual GDP growth, CPI inflation and unemployment; OECD monthly CPI and unemployment; BIS end-of-month policy rate; FRED series when the owner's key is present). A cell shows the exact value and its period; hover for dataset, availability time and observation id. Euro-area countries show the ECB rate (BIS area XM), labelled. NOT_COVERED, NOT_INGESTED and NOT_AVAILABLE_AT_AS_OF are shown as words, never zero.
- **Series**: one column for chosen countries (`col=…/countries=US,DE`), drawn with the shared SVG line chart (gaps for missing periods), with each series' status and ingest command.
- **Sources**: each dataset's count, period range and newest availability; FRED's state (inactive without `~/.unknown/secrets/fred_api_key`); and the sources not used after the rights review (IMF, RBI website, FBIL, NSE, BSE, SEBI) with their recorded reasons.
- Command line: `ECST` opens this workspace.
