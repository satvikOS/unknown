"""Chart routes (CAP-MKT-07). GET only, owner session required by the server; nothing here writes.

* ``/api/charts/bars?key=&interval=(1m|5m|15m|30m|1h|1d|1w|1mo)&from_ns=&to_ns=[&source=]``: bars aggregated with
  exact decimals on the venue's sessions, labelled with source and data type (``unknown.charts.service``).
* ``/api/charts/sources?key=``: which bar sources are configured and cover the security.

This module never imports a broker adapter package or SDK.
"""
from __future__ import annotations

from ...charts import service


def handle(ctx, parts, q, account_key, conn):
    sub = list(parts[1:])
    if sub == ["bars"]:
        return service.bars(ctx, q, conn)
    if sub == ["sources"]:
        return service.sources(ctx, q, conn)
    raise LookupError("unknown charts endpoint")


ROUTES = [(("charts",), handle)]
