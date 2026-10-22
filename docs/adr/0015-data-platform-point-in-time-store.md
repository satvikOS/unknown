# ADR-0015: Data platform — provider registry, point-in-time store and lineage

Status: accepted (2026-10-22)

## Context

The owner requires the following (DAT-01..DAT-06):

- a provider registry covering capabilities, entitlements, usage rights, costs, limits, freshness and failure behaviour;
- source identity, event, receipt and availability times, correction history and dataset versions, with point-in-time reconstruction;
- protection against look-ahead, survivorship, restatement leakage, unit, currency and symbol-history errors;
- formula, inputs, units, source dates, missingness and provenance for every displayed or model-consumed metric;
- honest representation of missing or unauthorized data.

The book review found that the point-in-time store had no knowledge-time or supersedes fields, and that a dataset's "allowed uses" were never enumerated.

## Decision

### Separation from the money path

Research data lives in its own store under `~/.unknown/research/`. It is written by data workers, never by the core, and the core never reads it to make money decisions. The core receives market quotes only through the quote book (ADR-0002). Every decision that uses a quote records that quote.

### Layers

1. **Raw.** Immutable, content-addressed payloads (`raw/<sha256>`), each with a manifest: provider, dataset, request, receipt time, licence profile and retention deadline. A file is deleted only when its retention deadline requires it. The hash and manifest are kept where the licence permits.
2. **Observations (bitemporal).** One table, `observation`, with these fields:
   - `dataset_id` and `entity_id`: an internal instrument, issuer, series or country key, never a display ticker;
   - `field`, `period_start`, `period_end`, `value` (exact decimal text) or `value_text`, `unit`, `currency`, `scale_note`;
   - `event_time`, `published_time`, `receipt_time` and `available_time`, the earliest legitimate decision-use time;
   - `known_from` and `known_to`: knowledge-time validity, with `known_to` NULL while current;
   - `supersedes` (observation id), `source_ref` (raw hash plus locator) and `quality` flags;
   - `missing_reason`, one of `NOT_REPORTED`, `NOT_YET_PUBLISHED`, `PROVIDER_DELAYED`, `UNAUTHORIZED`, `NOT_APPLICABLE`, `ERROR`. A missing value is never zero.
3. **Point-in-time read.** `as_of(t)` returns observations with `available_time <= t` and `known_from <= t < known_to`. Restatements and revisions therefore never leak backwards. Macro series use provider vintages (ALFRED) where they exist. A dataset without vintages is marked `vintage_support = false`, and its earlier values are reconstructed only from what this system itself captured, with that capture time recorded.
4. **Derived metrics.** A derived metric row carries `formula_id@version`, its input observation ids, units and the as-of time. The terminal's "show calculation" path uses exactly these fields.

### Provider registry

`provider` and `dataset` records hold the following:

- capabilities;
- coverage (markets and history depth);
- update frequency and availability clock;
- rate limits and the pacing implementation;
- cost model and the budget it is charged to;
- entitlement evidence;
- allowed uses, as an enumerated set: `display`, `non_display`, `storage`, `derived_storage`, `redistribution`, `model_processing_cloud`, `model_processing_local`;
- retention days, failure behaviour and the source URL with its verification date.

A dataset without recorded rights stays out of production ingestion. The agent layer refuses to send any content to a cloud model unless its dataset grants `model_processing_cloud`.

### Storage engine

SQLite (catalog and observations) plus content-addressed files are used for v1. Columnar storage (Parquet with DuckDB) is adopted when measured scan sizes justify it (book Chapter 11). Bars and ticks get their own tables keyed by instrument, bar specification version and time.

### First lawful sources

These are free official sources that need no purchase:

- SEC EDGAR submissions, companyfacts and frames, using the fair-access rate limit and a declared `User-Agent` from local configuration;
- US Treasury yield curve data;
- ECB Data Portal reference rates and series;
- OpenFIGI mapping;
- FRED/ALFRED, which needs a free API key the owner must register;
- exchange holiday calendars from official exchange pages.

Real-time and depth market data come from IBKR subscriptions on the owner's account, and only where entitled. Each source's exact terms come from the `data-providers` capability record and are re-verified before ingestion.
