"""Read-only global macro routes (CAP-MAC-05, CAP-MAC-06, CAP-MAC-09): World Bank, OECD, BIS and (with an
owner key) FRED series for US, IN and the EU venue countries, from the point-in-time research store.

  GET /api/macro/global?as_of=&countries=US,IN,DE           cross-country comparison table
  GET /api/macro/global/series?column=&countries=&as_of=&start=YYYY-MM-DD

No handler calls the network or writes anything; the store is opened read-only. Without a store the
response is UNAVAILABLE with the ingest commands."""
from __future__ import annotations

from ...data.sources import macro_global
from .company import _open, _unavailable, as_of


def _codes(q: dict) -> list[str] | None:
    return [c.strip().upper() for c in q["countries"].split(",") if c.strip()] if q.get("countries") else None


def handler(ctx, parts, q, account_key, conn):
    store = _open(ctx)
    if store is None:
        return {**_unavailable("research store not initialised: ingest macro series first"),
                "commands": list(macro_global.INGEST.values())}
    try:
        t = as_of(q)
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "":
            return macro_global.comparison(store, t, country_codes=_codes(q))
        if sub == "series":
            if not q.get("column"):
                raise ValueError("column is required (e.g. column=oecd_cpi_yoy)")
            start = q.get("start")
            if start and len(start) != 10:
                raise ValueError("start must be a date (YYYY-MM-DD)")
            return macro_global.series(store, q["column"], t, country_codes=_codes(q), start=start)
        raise LookupError(f"unknown global macro endpoint {sub}")
    finally:
        store.close()


ROUTES = [(("macro", "global"), handler)]
