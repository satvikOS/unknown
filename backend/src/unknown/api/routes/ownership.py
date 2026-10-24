"""Issuer ownership routes (CAP-CO-09). Route module ``ownership``; read-only (research store ``mode=ro``).

  GET /api/ownership/<cik|ticker|AAPL.XNAS>?as_of=&top=50      insiders (Forms 3/4/5), institutional holders (13F data sets) with
                                              changes and concentration, and 5 % holders (Schedules 13D/13G)

Everything is point in time: rows accepted (insiders, schedules) or filed (13F) after ``as_of`` are not shown.
Each section that has no data says why (reason code from the unavailable vocabulary) instead of showing zeros.
"""
from __future__ import annotations

from ...data.fundamentals.filings import load_submissions
from ...data.fundamentals.readonly import ReadOnlyResearchStore, research_paths
from ...data.sources import edgar_13f, edgar_ownership
from ...domain.clock import ns_to_iso
from .company import as_of, parse_cik

INGEST = ("python -m unknown.data.sources.edgar_ownership ingest --cik <CIK>, then "
          "python -m unknown.data.sources.edgar_13f ingest --cik <CIK>")


def handle(ctx, parts, q, account_key, conn):
    if len(parts) < 2:
        raise ValueError("use /api/ownership/<cik>")
    db, raw = research_paths(ctx.paths.root)
    try:
        store = ReadOnlyResearchStore(db, raw)
    except FileNotFoundError:
        return {"status": "UNAVAILABLE", "reason": "NO_DATA", "detail": f"research store not initialised ({INGEST})"}
    try:
        token = parts[1]
        if token.upper().removeprefix("CIK").isdigit():
            cik = parse_cik(token)
            eid = edgar_ownership.entity_id(cik)
        else:
            eid = edgar_ownership.resolve_issuer(store, token)
            if eid is None:
                raise LookupError(f"{token} is not a ticker of an SEC issuer in the research store (use a CIK, or ingest the issuer)")
            cik = int(eid[8:])
        t = as_of(q)
        top = max(1, min(500, int(q.get("top", 50))))
        subs, meta = load_submissions(store, eid)
        if meta.get("status") == "NOT_INGESTED":
            raise LookupError(f"{eid} is not in the research store ({INGEST})")
        section = q.get("section")
        out = {"cik": cik, "entity_id": eid, "name": (subs or {}).get("name") or meta.get("name"),
               "tickers": (subs or {}).get("tickers") or [], "as_of": ns_to_iso(t),
               "commands": [f"python -m unknown.data.sources.edgar_ownership ingest --cik {cik}",
                            f"python -m unknown.data.sources.edgar_13f ingest --cik {cik}"]}
        if section in (None, "", "insiders"):
            out["insiders"] = edgar_ownership.insiders(store, eid, t)
        if section in (None, "", "institutional"):
            out["institutional"] = edgar_13f.institutional(store, eid, t, top=top)
        if section in (None, "", "holders"):
            out["major_holders"] = edgar_ownership.major_holders(store, eid, t)
            out["cusips"] = edgar_ownership.issuer_cusips(store, eid, t)
        out["non_us"] = "US issuers only: India and EU ownership need a licensed feed or an owner import (not licensed here)"
        return out
    finally:
        store.close()


ROUTES = [(("ownership",), handle)]
