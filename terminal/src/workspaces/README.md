# Workspaces

Each workspace lives in `src/workspaces/<id>/`. Its `index.ts` exports a `WorkspaceDefinition`, and the workspace is registered in `registry.ts`. Navigation, the command palette, the `g <key>` shortcuts and the `?` help are all built from that registry. See `terminal/README.md`, "Adding a workspace", for the exact snippet, the reserved shortcuts and the optional `commands` (palette entries) and `keys` (workspace shortcuts).

Register a workspace only when the backend service behind it exists. Unbuilt capabilities appear only on the Capabilities page.

Registered: `overview` (g o), `sessions` (g s), `orders` (g b), `risk` (g r), `instruments` (g i), `operations` (g p), `audit` (g a), `policy` (g y) and `capabilities` (g c). Each directory has a README covering its routes, commands and the API gaps it found.

Shared code that workspaces use:
- `ui/primitives.tsx`: Money, Qty, StatusPill and badges.
- `ui/DataTable.tsx`.
- `format/decimal.ts` and `format/exact.ts`: exact decimal formatting and arithmetic, with no floats.
- `state/react.tsx`: `useQuery`, `useSendCommand` and `useTrackedCommand`.
- `ui/charKeys.ts`: the single-character shortcut preference.
