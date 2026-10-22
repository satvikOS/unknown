"""Pluggable read-only route modules for the terminal API.

Each module exposes ``ROUTES``: a list of (path_prefix_parts, handler) where
handler(ctx, parts, query, account_key, conn) returns a JSON-able object or
raises LookupError (404) / ValueError (400). Handlers are read-only: they use
the read-only core connection ``conn`` or their own read-only stores, never the
core writer. Owner commands still go through POST /api/commands only.

To add a domain: create ``routes/<domain>.py`` and append its module name to
MODULES below (one line; keep the list sorted)."""
from __future__ import annotations

import importlib

MODULES: list[str] = []


def load():
    routes = []
    for name in MODULES:
        mod = importlib.import_module(f"{__name__}.{name}")
        routes.extend(mod.ROUTES)
    return routes
