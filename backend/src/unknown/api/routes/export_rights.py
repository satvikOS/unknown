"""Data-rights records for exports (CAP-UX-07): GET /api/export/rights.

The terminal's shared export primitive (terminal/src/ui/export.ts) asks this route before it writes a file. It returns
the source registry (the rights records under ``config/``, parsed by ``data.registry.load_registry``) reduced to
what an export needs: each dataset's allowed uses, where the rights were read and when, and the terms URLs; plus the aliases
that map runtime source ids (news sources, market-data bus sources) to a registry dataset.

The export rule (applied in the terminal and stated in every export's provenance):
* owner-generated data (ledger, orders, positions as quantities and cost, notes) is always exportable;
* a column from a registered dataset is exportable when the dataset allows ``storage`` (a file is a stored copy);
* real-time exchange data additionally needs ``redistribution``;
* a column whose source has no record is excluded (fail closed), and so is every licensed column when this route
  fails.
"""
from __future__ import annotations

from ...data.registry import RegistryError, load_registry
from ...news.sources import SOURCES as NEWS_SOURCES

# Market-data bus source id -> (registry record, dataset). A bus source without an entry has no rights record.
MARKET_DATA = {"ALPACA": ("alpaca", "alpaca.stock_stream")}
RULE = ("owner-generated data is always exportable; a licensed column is exportable when its dataset allows "
        "'storage'; real-time exchange data also needs 'redistribution'; a source without a rights record is excluded")


def rights(ctx, parts: list[str], q: dict, account_key: str, conn) -> dict:
    if parts != ["export", "rights"]:
        raise LookupError(f"unknown export endpoint /{'/'.join(parts)}")
    try:
        registry = load_registry()
    except (RegistryError, OSError, ValueError) as exc:
        return {"registry": "UNAVAILABLE", "reason": "ERROR", "detail": f"rights registry unreadable: {exc}",
                "records": {}, "aliases": {}, "rule": RULE}
    records = {}
    for rid, rec in registry.items():
        records[rid] = {"name": rec.name, "verified_on": rec.verified_on, "terms": list(rec.sources),
                        "datasets": {d.dataset_id: {"allowed_uses": sorted(d.allowed_uses),
                                                    "rights_source": d.rights_source,
                                                    "rights_verified_on": d.rights_verified_on}
                                     for d in rec.datasets}}
    aliases = {f"news:{s.source_id}": {"record": s.provider_id, "dataset": s.dataset_id} for s in NEWS_SOURCES}
    aliases.update({f"marketdata:{k}": {"record": r, "dataset": d} for k, (r, d) in MARKET_DATA.items()})
    return {"registry": "AVAILABLE", "records": records, "aliases": aliases, "rule": RULE}


ROUTES = [(("export", "rights"), rights)]
