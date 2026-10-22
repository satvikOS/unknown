"""Pluggable route modules for the terminal API.

Each module exposes ``ROUTES``: a list of (path_prefix_parts, handler) where
handler(ctx, parts, query, account_key, conn) returns a JSON-able object or
raises LookupError (404) / ValueError (400). GET handlers are read-only: they use
the read-only core connection ``conn`` or their own read-only stores, never the
core writer. Owner commands still go through POST /api/commands only.

A module may also expose ``POST_ROUTES``: a list of (path_prefix_parts, handler)
where handler(ctx, parts, body, session) returns a JSON-able object. The server
dispatches a plugin POST only after it has authenticated the owner session and
checked the session's CSRF token (``X-CSRF-Token``), exactly as for
/api/commands. A POST handler writes only its own domain's store (for example
the research job store), never the core database, and raises LookupError (404),
ValueError (400), PermissionError (403) or ``RouteConflict`` (409).

To add a domain: create ``routes/<domain>.py`` and append its module name to
MODULES below (one line; keep the list sorted)."""
from __future__ import annotations

import importlib

MODULES: list[str] = [
    "research_jobs",
]


class RouteConflict(RuntimeError):
    """The request is well formed but conflicts with the current state (HTTP 409)."""


def _modules():
    return [importlib.import_module(f"{__name__}.{name}") for name in MODULES]


def load():
    routes = []
    for mod in _modules():
        routes.extend(mod.ROUTES)
    return routes


def load_post():
    routes = []
    for mod in _modules():
        routes.extend(getattr(mod, "POST_ROUTES", []))
    return routes
