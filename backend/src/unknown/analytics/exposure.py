"""Exposure and concentration (CAP-PORT-03; book Ch. 6).

Groupings: instrument, owner (bucket or session), sector and currency. Every grouping
names its classification scheme; a security without a classification falls into the
explicit bucket ``unknown`` and is never assigned a default sector. Exposure uses
positions with a mark (fresh or stale, flagged); unpriced positions are listed apart
with their cost basis and lower the coverage figure instead of counting as zero.

Sector scheme: SEC EDGAR Standard Industrial Classification. The issuer is resolved
through a ticker alias in the point-in-time research store (``data.pit``) that was valid
at the decision instant, so an alias first recorded later cannot classify an earlier
portfolio. The SIC code itself is the issuer's code as last recorded from the EDGAR
submissions feed (current SIC, not a historical one; stated in every output)."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from ..domain.fixed import Fixed
from ..domain.money import currency
from .common import frac, lineage, weight

SIC_SCHEME = "SEC EDGAR SIC (issuer 'sic' from the submissions feed), divisions by 1987 SIC Manual major-group ranges"
SIC_DIVISIONS = (  # (first major group, last major group, division)
    (1, 9, "A Agriculture, Forestry and Fishing"),
    (10, 14, "B Mining"),
    (15, 17, "C Construction"),
    (20, 39, "D Manufacturing"),
    (40, 49, "E Transportation, Communications, Electric, Gas and Sanitary Services"),
    (50, 51, "F Wholesale Trade"),
    (52, 59, "G Retail Trade"),
    (60, 67, "H Finance, Insurance and Real Estate"),
    (70, 89, "I Services"),
    (91, 97, "J Public Administration"),
    (99, 99, "K Nonclassifiable Establishments"),
)
UNKNOWN = "unknown"


def sic_division(sic: str | int | None) -> str | None:
    try:
        code = int(str(sic))
    except (TypeError, ValueError):
        return None
    major = code // 100
    for lo, hi, name in SIC_DIVISIONS:
        if lo <= major <= hi:
            return name
    return None


class SicClassifier:
    """Read-only sector lookup against the research store; absent store -> every name is 'unknown' with a reason."""

    def __init__(self, research_db: str | Path | None):
        self.path = Path(research_db) if research_db else None
        self.conn = None
        self.unavailable = None
        if self.path is None:
            self.unavailable = "no research store configured"
        elif not self.path.exists():
            self.unavailable = f"research store not found at {self.path}"
        else:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
            self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()

    def classify(self, instrument_id: str, symbol: str | None, attributes: dict, at_ns: int) -> dict:
        try:
            return self._classify(instrument_id, symbol, attributes, at_ns)
        except sqlite3.DatabaseError as exc:
            # A file at the research-store path without the entity tables (or corrupt) is a reason, not a 500.
            return {"instrument_id": instrument_id, "scheme": SIC_SCHEME, "sector": UNKNOWN, "status": "unclassified",
                    "reason": f"research store unreadable: {exc}"}

    def _classify(self, instrument_id: str, symbol: str | None, attributes: dict, at_ns: int) -> dict:
        base = {"instrument_id": instrument_id, "scheme": SIC_SCHEME}
        if attributes.get("fixture"):
            return {**base, "sector": UNKNOWN, "status": "unclassified", "reason": "fixture instrument: no issuer"}
        if self.conn is None:
            return {**base, "sector": UNKNOWN, "status": "unclassified", "reason": self.unavailable}
        entity_ids: set[str] = set()
        cik = attributes.get("sec.cik")
        if cik is not None:
            try:
                entity_ids.add(f"SEC-CIK-{int(str(cik)):010d}")
            except ValueError:
                return {**base, "sector": UNKNOWN, "status": "unclassified",
                        "reason": f"instrument attribute sec.cik {cik!r} is not a CIK number"}
        elif symbol:
            for r in self.conn.execute("SELECT entity_id FROM entity_alias WHERE scheme LIKE 'ticker:%' AND value=? "
                                       "AND valid_from_ns<=? AND valid_to_ns>?", (symbol, at_ns, at_ns)):
                entity_ids.add(r["entity_id"])
        if not entity_ids:
            return {**base, "sector": UNKNOWN, "status": "unclassified",
                    "reason": f"no issuer alias for {symbol!r} valid at the decision time"}
        if len(entity_ids) > 1:
            return {**base, "sector": UNKNOWN, "status": "unclassified",
                    "reason": f"ambiguous issuer for {symbol!r}: {sorted(entity_ids)}"}
        eid = entity_ids.pop()
        row = self.conn.execute("SELECT attributes FROM entity WHERE entity_id=?", (eid,)).fetchone()
        sic = json.loads(row["attributes"]).get("sic") if row else None
        division = sic_division(sic)
        if division is None:
            return {**base, "sector": UNKNOWN, "status": "unclassified", "entity_id": eid,
                    "reason": "issuer has no recognisable SIC code in the research store"}
        return {**base, "sector": division, "sic": str(sic), "status": "classified", "entity_id": eid,
                "reason": None, "as_of_note": "current SIC as recorded from the EDGAR submissions feed"}


def _group(rows: list[dict], key: str, denominator: Fixed | None, gross: Fixed, scale: int) -> list[dict]:
    acc: dict[str, dict] = defaultdict(lambda: {"long": Fixed(0, scale), "short": Fixed(0, scale), "count": 0})
    for r in rows:
        g = acc[r[key]]
        mv = Fixed.parse(r["market_value"])
        if mv.is_negative() or Fixed.parse(r["quantity"]).is_negative():
            g["short"] += mv
        else:
            g["long"] += mv
        g["count"] += 1
    out = []
    for k, g in sorted(acc.items(), key=lambda kv: (-frac(kv[1]["long"] + abs(kv[1]["short"])), kv[0])):
        gross_g = g["long"] + abs(g["short"])
        net_g = g["long"] + g["short"]
        out.append({key: k, "positions": g["count"], "long": g["long"].to_str(), "short": g["short"].to_str(),
                    "gross": gross_g.to_str(), "net": net_g.to_str(),
                    "weight_of_denominator": weight(net_g, denominator) if denominator is not None else None,
                    "share_of_gross": weight(gross_g, gross)})
    return out


def exposure_report(holdings_report: dict, *, classifier: SicClassifier | None = None, at_ns: int | None = None,
                    top_k: int = 5, denominator: str = "scope_nav") -> dict:
    """Long/short/gross/net, bucket weights, HHI, effective N and top-k share per currency (CAP-PORT-03)."""
    at_ns = holdings_report["as_of_ns"] if at_ns is None else at_ns
    classifier = classifier or SicClassifier(None)
    by_ccy: dict[str, list[dict]] = defaultdict(list)
    unpriced: dict[str, list[dict]] = defaultdict(list)
    classes = {}
    for p in holdings_report["positions"]:
        if p["instrument_id"] not in classes:
            attrs = {"fixture": p.get("instrument_fixture", p.get("fixture"))}
            if p.get("sec_cik") is not None:
                attrs["sec.cik"] = p["sec_cik"]
            classes[p["instrument_id"]] = classifier.classify(p["instrument_id"], p.get("symbol"), attrs, at_ns)
        cls = classes[p["instrument_id"]]
        row = {"instrument_id": p["instrument_id"], "owner": p["owner"], "currency": p["currency"],
               "sector": cls["sector"], "quantity": p["quantity"], "market_value": p["market_value"],
               "mark_status": p["mark"]["status"]}
        (by_ccy if p["market_value"] is not None else unpriced)[p["currency"]].append(row)
    currencies = sorted(set(by_ccy) | set(unpriced))
    per_ccy = {}
    for ccy in currencies:
        scale = currency(ccy).ledger_scale
        rows = by_ccy.get(ccy, [])
        long_ = sum((Fixed.parse(r["market_value"]) for r in rows if not Fixed.parse(r["quantity"]).is_negative()), Fixed(0, scale))
        short = sum((Fixed.parse(r["market_value"]) for r in rows if Fixed.parse(r["quantity"]).is_negative()), Fixed(0, scale))
        gross = long_ + abs(short)
        total = holdings_report["totals"].get(ccy, {})
        den_value = total.get("nav_marked") if denominator == "scope_nav" else None
        den = Fixed.parse(den_value) if den_value is not None else None
        # instrument-level concentration over the scope (owners aggregated)
        inst_mv: dict[str, Fixed] = defaultdict(lambda: Fixed(0, scale))
        for r in rows:
            inst_mv[r["instrument_id"]] += Fixed.parse(r["market_value"])
        shares = []
        if gross.is_positive():
            shares = sorted((float(frac(abs(v)) / frac(gross)) for v in inst_mv.values()), reverse=True)
        hhi = sum(s * s for s in shares) if shares else None
        unp = unpriced.get(ccy, [])
        priced_n = len(rows)
        per_ccy[ccy] = {
            "currency": ccy, "long": long_.to_str(), "short": short.to_str(), "gross": gross.to_str(), "net": (long_ + short).to_str(),
            "denominator": {"name": "scope NAV (nav_marked)" if den is not None else None,
                            "value": den.to_str() if den is not None else None,
                            "reason": None if den is not None else "scope NAV unavailable (unpriced positions)"},
            "gross_of_denominator": weight(gross, den) if den is not None else None,
            "net_of_denominator": weight(long_ + short, den) if den is not None else None,
            "ibkr_form": {"long_pct": weight(long_, long_ + abs(short)) if gross.is_positive() else None,
                          "short_pct": weight(abs(short), long_ + abs(short)) if gross.is_positive() else None,
                          "note": "IBKR white-paper form with an absolute short value in the denominator (the paper "
                                  "does not state the sign; CAP-PORT-03 open question)"},
            "by_instrument": _group(rows, "instrument_id", den, gross, scale),
            "by_owner": _group(rows, "owner", den, gross, scale),
            "by_sector": _group(rows, "sector", den, gross, scale),
            "concentration": {"hhi": hhi, "effective_n": (1.0 / hhi) if hhi else None,
                              f"top_{top_k}_share": sum(shares[:top_k]) if shares else None,
                              "basis": "instrument shares of gross exposure (owners aggregated)"},
            "coverage": {"priced_positions": priced_n, "unpriced_positions": len(unp),
                         "sector_classified_share_of_gross": weight(
                             sum((abs(Fixed.parse(r["market_value"])) for r in rows if r["sector"] != UNKNOWN), Fixed(0, scale)),
                             gross) if gross.is_positive() else None,
                         "stale_marked_positions": sum(1 for r in rows if r["mark_status"] == "stale")},
            "unpriced": [{"instrument_id": r["instrument_id"], "owner": r["owner"], "quantity": r["quantity"]} for r in unp],
        }
    by_currency = [{"currency": c, "gross": per_ccy[c]["gross"], "net": per_ccy[c]["net"],
                    "note": "exposure in its own currency; no FX conversion applied"} for c in currencies]
    return {"kind": "exposure", "account_key": holdings_report["account_key"], "scope": holdings_report["scope"],
            "as_of_ns": holdings_report["as_of_ns"], "core_seq": holdings_report["core_seq"],
            "classification": {"scheme": SIC_SCHEME, "unclassified_bucket": UNKNOWN,
                               "instruments": list(classes.values())},
            "by_currency": by_currency, "currencies": per_ccy,
            "fixture_data": holdings_report.get("fixture_data", False),
            "fixture_note": holdings_report.get("fixture_note"),
            "lineage": lineage(["expo.long_short", "expo.bucket_weight", "expo.hhi", "expo.sic_division", "port.market_value"],
                               inputs={"holdings_core_seq": holdings_report["core_seq"],
                                       "mark_policy": holdings_report["mark_policy"]},
                               units="currency amounts as decimal strings; weights and shares dimensionless",
                               source_dates={"valuation_ns": holdings_report["as_of_ns"], "classification_at_ns": at_ns},
                               missing=[{"instrument_id": c["instrument_id"], "reason": c["reason"]}
                                        for c in classes.values() if c["status"] != "classified"])}
