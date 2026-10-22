# Plugin route access (not a workspace)

Shared code for workspaces that read or write the API's plugin routes (`backend/src/unknown/api/routes/*.py`), whose shapes are not in `src/api/types.ts`:

- `usePluginQuery<T>(endpoint, params, { pollMs })` runs a GET through the shared query cache. It gets the same refetch on every state-stream version bump, the same `WorkspaceFrame` provenance, and the same 401 handling as `useQuery`. The caller supplies `T` and must parse the body defensively, because the server owns the shape.
- `usePluginPost()` returns `post<T>(path, body)`. It sends the owner session cookie and the CSRF token that the API client already holds, refreshes the session once on a 403 CSRF refusal, and throws the client's error classes, so `describeError` shows the server's own message (for example a 409 refusal).

Used by `portfolio`, `marketdata` and `research-jobs`. This folder has no `index.ts` and is not registered.
