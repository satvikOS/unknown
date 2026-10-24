# Research workspace

Registered as `research` (`g q`, nav order 130). It reads `GET /api/research` (backend `api/routes/research.py`), which opens the research registry read-only. Nothing here can change a status, approve anything or start a run.

## Routes

- `#/research` or `#/research/experiments`: pre-registered experiments (a FIXTURE experiment is marked as an error and its results are refused), trial counts (every status counts toward M), the development budget, the holdout window and whether the holdout was consumed.
- `#/research/experiments/<id>[/segment=…]`: one experiment.
  - The pre-registration, with its hash re-verified by the API, the hypothesis, selection metric, rejection criteria and holdout plan, and the full record.
  - Trial counts by segment, the budget, the search space and amendments.
  - The holdout access log: every attempt, granted or refused.
  - Every trial, optionally filtered by segment.
- `#/research/trials/<trial_id>`: one trial.
  - The cumulative return chart, compounded from the stored simple returns (`return.simple@1`, float statistics).
  - The statistics. A HOLDOUT trial shows its `holdout` and `full_run` blocks, and a WALK_FORWARD trial its `oos` block.
  - Money in the manifest's currency.
  - Parameters, hashes and the full reproducibility manifest.
- `#/research/artifacts[/<hash>]`: strategy artifacts with their authoritative status (the last record of the transition log). One artifact shows:
  - its status history, with principal, reason, evidence and record hashes;
  - the evidence, with gate verdicts (PASS/FAIL) and the reasons, verbatim;
  - the API's re-verification of the whole transition chain (a problem it reports may concern another artifact's records, and the view says so);
  - the artifact body.
- `#/research/strategies[/<spec_hash>]`: registered specifications, with the hash re-verified. "FIXTURE DATA ALLOWED" is shown as an error-toned label when the spec's data requirements allow fixtures.
- `#/research/reports[/<id>]`: reports. A report derived from a fixture study is refused (an error, not its figures); otherwise the per-kind status, failing gates, conclusion, DSR, PBO, n_eff, the paired bootstrap and the gate verdicts. A statistic the engine could not compute (too few observations, zero variance) shows the engine's reason, and the bootstrap then says NOT COMPUTABLE, not FAIL.
- `#/research/formulas`: the formula registry (`formula_id@version`), which is served from code and is available without a registry.

## Labels and missing data

- Data whose provenance is FIXTURE (`provenance.label`, `fixture`) is refused: the view shows an error, never the data. UNKNOWN ORIGIN is labelled where the API says so, and each panel shows its provenance: source, path, schema version, read time and formulas.
- Without a registry, the API's reason is shown verbatim, with no command: no synthetic study exists to populate it (ADR-0020); experiments come from real research runs.
- Integers beyond 2^53 in nested records (nanosecond timestamps such as `registered_at_ns`) have already been rounded by the browser's JSON parser. A `*_ns` value is shown exactly from the API's `*_ns_str` sibling; any other such integer is marked with "≈" and the view says so, instead of showing it as exact.

## API notes found while building this

- Trial `metrics` have different shapes by segment: a flat block, `{holdout, full_run}`, `{oos, folds}` or `{holdout}`. The list endpoint omits the manifest, so money currency is only known on a trial's own page (`manifest.config.currency`).
- `chain_problems` on an artifact is the verification of the global transition log, not of that artifact's own records. The per-artifact `seq` values are global log positions.
- `reports[].fixture` is an integer (0/1), while experiment `fixture` is a boolean.
