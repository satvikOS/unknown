# ADR-0001: Record decisions, evidence classes and implementation status

Status: accepted (2026-10-22)

## Context

The engineering book (`docs/book`, branch `docs/technical-book-v1`) is an initial design draft. It is not exhaustive and was not independently validated. Implementation will find omissions and contradictions in it, and the owner requires that parity, completeness and readiness claims be backed by evidence.

## Decision

1. Material architecture changes and every resolution of a book contradiction or omission get an ADR in `docs/adr`. Smaller resolutions go in `docs/design/book-resolutions.md` with the ADR or requirement that adopts them.
2. Every requirement has a stable identifier in `docs/requirements/register.md`. Every capability has a stable identifier in `docs/capabilities/register.json`. Code and tests cite these identifiers.
3. Implementation status uses only these values, in order of evidence strength:
   `proposed` → `specified` → `implemented` → `tested` (offline deterministic tests) → `paper-verified` (against a broker paper account) → `live-verified`.
   A status never advances without an evidence reference (test name, report file, run log). `paper-verified` and `live-verified` require an external system and an identified account; offline tests cannot produce them.
4. The words "complete", "production ready", "institutional grade" and "parity" are used only with the evidence that defines them, as the capability register records it.
5. `docs/status/STATUS.md` is the durable hand-off file. Each increment updates it with: the requirements implemented, files changed, verification performed with actual results, remaining failures and external dependencies, commands to reproduce, and the next concrete step.

## Consequences

Status and claims can be audited mechanically. A screen, a passing unit test or a profitable simulation cannot be presented as broker or market evidence.
