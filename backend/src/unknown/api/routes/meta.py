"""Platform metadata routes. GET only, owner session required by the server; nothing here writes.

* ``/api/meta/unavailable-reasons``: the one reason vocabulary for unavailable values (ADR-0020,
  ``unknown.domain.unavailable``; docs/design/unavailable-values.md). The terminal renders an unavailable value as an
  em dash carrying one of these codes.
"""
from __future__ import annotations

from ...domain import unavailable


def handle(ctx, parts, q, account_key, conn):
    sub = list(parts[1:])
    if sub == ["unavailable-reasons"]:
        return {"reasons": unavailable.vocabulary(), "glyph": "—",
                "rule": "an unavailable value is null with one of these codes; never zero, a placeholder or an example"}
    raise LookupError("unknown meta endpoint")


ROUTES = [(("meta",), handle)]
