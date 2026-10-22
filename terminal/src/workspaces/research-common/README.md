# research-common

This folder is not a workspace. It holds code shared by the Company, Markets & macro and Research workspaces:

- `api.ts`: `useApi<T>(path, params)`. It is a typed read of the plugin routes (`/api/company`, `/api/macro`, `/api/research`) through the shared query cache (`useQuery`), so caching, refetch on stream version bumps and header provenance stay the same. Empty parameters are dropped. `apiPath(...)` encodes segments like the core client does.
- `format.ts`: exact decimal-point shifting (a ratio shown as a percentage with nothing rounded), API instant parsing, the API's as-of rule (`checkInstant`), and extraction of the populate command named in an unavailable reason. It also formats float statistics, labelled as such.
- `chartMath.ts` and `charts.tsx`: inline SVG line and range charts with no dependency. A chart is an `<svg role="img">` with a title and a text summary. Gaps stay gaps, and markers carry the exact value text.
- `parts.tsx`: panels, the `Unavailable` notice (reason verbatim plus command), status tags, unit-aware values, instants and a JSON tree for nested records.
- `research.css`: local layout only. Colours come from the shared theme tokens.
