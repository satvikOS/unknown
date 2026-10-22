# Data acquisition, provenance and analytics {#data-platform}

More sources improve decisions only when their incremental information survives costs, delays, duplication and licensing constraints. Build a source registry before adding feeds. Each entry records provider, dataset, transport, entitlement, allowed uses, redistribution restrictions, retention, model-processing permission, update frequency, availability clock, quality checks and estimated cost. A source without resolved rights stays outside production ingestion.

## Coverage map

The target catalog includes trades and quotes, depth where justified, reference data, corporate actions, filings and financial statements, analyst estimates, earnings calendars, macro releases and vintages, rates and yield curves, FX, options surfaces, short availability, institutional ownership, news, transcripts and permitted alternative data. Social posts, satellite or shipping data, app usage and web traffic are optional research datasets, not automatic requirements. Each addition must identify a specific hypothesis or operational need.

SEC EDGAR provides official filing APIs; implement fair-access behavior and current provider limits rather than scraping aggressively. [S36] ALFRED supports historical macro vintages useful for causal research. [S03] Exchange-grade real-time data, depth, estimates and news usually require separate entitlements. A public website view does not establish rights to machine extraction, storage, redistribution or cloud-model submission.

## Raw, normalized and derived layers

The raw layer preserves permitted original payloads with source identifiers and receipt timestamps. The normalized layer resolves instruments, units, calendars, event types and correction links. The derived layer contains bars, features, embeddings, analytics and research tables. Every derived row traces to raw identifiers or a dataset version. Corrections create new versions; they do not silently alter a historical experiment.

Partition large historical files by dataset, venue and date; use a columnar format for scans. Keep schema evolution explicit. Avoid one tiny file per tick. Use a compact database for metadata and an object store for larger immutable datasets. A separate columnar query engine may later improve analytics, but only after the dataset size and workload justify it.

The feature contract includes entity, event time, availability time, feature version, value, unit, missingness reason and provenance. A missing value is not zero. For example, no reported earnings figure, a delayed provider response and an actual zero earnings figure are different states.

## Quality gates

Validate sequence continuity, timestamp plausibility, crossed or locked quotes, price scale, nonnegative size, instrument status and corporate-action consistency. A crossed quote may be a transient valid market condition or a feed problem; classify rather than automatically delete it. Reject impossible scaling errors, quarantine uncertain events and track their effect on coverage.

Measure completeness by expected event type and session, not simply row count. A quiet instrument may correctly have no trades. Compare independent reference values when available, but do not majority-vote three feeds that share one upstream source. Record provider lineage to understand correlated failures.

Freshness budgets depend on strategy horizon. A daily fundamental feature may be valid for weeks; a quote used for immediate execution may be invalid after milliseconds or seconds depending on the strategy and venue. Freshness is checked both at signal creation and before order transmission. A fast network cannot make a stale filing fresh.

## News and document pipeline

Ingest the earliest licensed timestamp, publication timestamp, revisions and provider identifier. Deduplicate syndicated copies using canonical links, content hashes and similarity clusters. Extract entities, event type, quantities, time references and quoted evidence. Cloud models may classify or summarize, but numerical claims used in calculations should be validated against source spans and typed parsers.

Document text is untrusted data. Instructions embedded in a filing or news page cannot change tool permissions, order limits or model routing. Retrieval results carry provenance into the model output; unsupported claims are marked uncertain. An analyst agent produces an event hypothesis, not an executable brokerage command.

## Financial analytics

Normalize accounting periods, currencies, share counts and restatements before calculating ratios. Trailing figures must use reports available at the decision time. Compute enterprise value using a documented treatment of debt, cash, minority interests and preferred equity. A price-to-earnings ratio with negative earnings should not be treated as an ordinary ranking number. Estimate revisions require point-in-time vendor snapshots.

Keep formulas visible in the terminal. A user should be able to inspect inputs, units, source dates and adjustments behind a valuation. This auditability is more valuable than a dense screen of apparently precise but untraceable values.

## Acceptance and deletion

A dataset becomes eligible when a replay can reconstruct its availability, quality reports are within approved bounds, rights are recorded and retention is enforceable. Test symbol changes, restated statements, duplicate news, a missing trading day and a provider correction. Deleting expired raw data may affect reproducibility; preserve allowed hashes, manifests and derived evidence where the license permits. Rights enforcement is part of the pipeline, not a note added after ingestion.

