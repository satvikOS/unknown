"""Company tearsheet: a read model composed of typed blocks (CAP-CO-12).

Each block is ``{status, as_of, newest_input, age_seconds, source, content | reason}`` and fails
independently (an exception in one block becomes that block's ERROR status while the others
render). Status vocabulary (CAP-CO-12): OK, STALE, NOT_LICENSED, NOT_REPORTED, NOT_AVAILABLE,
NOT_CONNECTED, PENDING, ERROR. The tearsheet computes nothing new: identity and filings come from
``filings``, statements from ``statements``, ratios from ``metrics``, peers from ``peers``.
"""
from __future__ import annotations

import time

from ...domain.clock import NS_PER_S, ns_to_iso
from .filings import filings as filings_list
from .filings import identity, load_submissions
from .metrics import MetricEngine, PriceInput
from .peers import comps, peer_group
from .statements import Fundamentals

ANNUAL_FORMS = ("10-K", "20-F", "40-F")


def _block(fn, *, source: str, as_of_ns: int, now_ns: int) -> dict:
    try:
        content, newest_ns, status = fn()
    except LookupError as exc:
        return {"status": "NOT_AVAILABLE", "source": source, "as_of": ns_to_iso(as_of_ns), "reason": str(exc)}
    except Exception as exc:  # each block fails independently (CAP-CO-12)
        return {"status": "ERROR", "source": source, "as_of": ns_to_iso(as_of_ns), "reason": f"{type(exc).__name__}: {exc}"}
    out = {"status": status, "source": source, "as_of": ns_to_iso(as_of_ns), "content": content}
    if newest_ns is not None:
        out["newest_input"] = ns_to_iso(newest_ns)
        out["age_seconds"] = max(0, (now_ns - newest_ns) // NS_PER_S)
    return out


def tearsheet(store, entity_id: str, as_of_ns: int, *, price: PriceInput | None = None, now_ns: int | None = None,
              filings_limit: int = 20, fy_periods: int = 3, q_periods: int = 4, peer_list: list[str] | None = None) -> dict:
    now_ns = now_ns or time.time_ns()
    subs, meta = load_submissions(store, entity_id)
    if meta.get("status") == "NOT_INGESTED":
        raise LookupError(meta["reason"])
    sic = (meta.get("attributes") or {}).get("sic")
    state: dict = {}

    def b_identity():
        if subs is None:
            raise LookupError(meta.get("reason", "submissions payload unavailable"))
        return identity(subs, meta, as_of_ns), meta.get("retrieved_at_ns"), "OK"

    def b_filings():
        if subs is None:
            raise LookupError(meta.get("reason", "submissions payload unavailable"))
        fl = filings_list(subs, as_of_ns, limit=filings_limit)
        # The latest annual report is searched over every filing accepted by t, not only the displayed page: insider
        # forms (3/4/5, 144) can fill the first page for months (seen on live SEC data).
        annual = filings_list(subs, as_of_ns, forms=set(ANNUAL_FORMS), limit=1)["filings"]
        state["latest_10k"] = annual[0] if annual else None
        return fl, meta.get("retrieved_at_ns"), "OK"

    def fundamentals() -> Fundamentals:
        if "f" not in state:
            state["f"] = Fundamentals(store, entity_id, as_of_ns)
        return state["f"]

    def b_statements():
        f = fundamentals()
        if f.fact_count == 0:
            return {"reason": "no standardized facts available at the as-of time"}, None, "NOT_REPORTED"
        content = {"annual": f.statement("FY", fy_periods, history=False), "quarterly": f.statement("Q", q_periods, history=False),
                   "ttm": f.statement("TTM", 1, history=False)}
        newest = max((x.available_ns for v in f.index.values() for d in v.values() for x in d.values()), default=None)
        return content, newest, "OK"

    def b_metrics():
        f = fundamentals()
        eng = MetricEngine(f, price, sic=sic)
        results = eng.all()
        newest = max((r.inputs[0].cell.available_ns() or 0 for r in results if r.inputs and r.inputs[0].cell is not None), default=None)
        return {"price_input": price.to_json() if price else None,
                "metrics": [r.to_json(f.cik) for r in results],
                "show_calculation": f"/api/company/{f.cik}/metrics/<metric>?as_of=...",
                "note": "Price-based metrics need a price input with source and time; none is fetched implicitly."}, newest or None, "OK"

    def b_peers():
        group = peer_group(store, entity_id, as_of_ns, explicit=peer_list)
        prices = {entity_id: price} if price else None
        table = comps(store, entity_id, group, as_of_ns, prices=prices)
        return {"group": group, "comps": table}, None, "OK" if group["members"] else "NOT_AVAILABLE"

    def b_description():
        k = state.get("latest_10k")
        if k is None:
            raise LookupError("no annual report (10-K, 20-F or 40-F) accepted by the as-of time in the stored submissions "
                              "(main file only)")
        return {"reason": "business description text is not stored; open Item 1 of the latest annual report",
                "link": k["links"]["primary_document"], "accession": k["accession"]}, None, "NOT_AVAILABLE"

    blocks = {
        "identity": _block(b_identity, source="sec.submissions", as_of_ns=as_of_ns, now_ns=now_ns),
        "filings": _block(b_filings, source="sec.submissions", as_of_ns=as_of_ns, now_ns=now_ns),
        "statements": _block(b_statements, source="sec.companyfacts via std-map@1", as_of_ns=as_of_ns, now_ns=now_ns),
        "metrics": _block(b_metrics, source="sec.companyfacts via std-map@1; price input as supplied", as_of_ns=as_of_ns, now_ns=now_ns),
        "peers": _block(b_peers, source="research store issuers (SIC from sec.submissions)", as_of_ns=as_of_ns, now_ns=now_ns),
        "description": _block(b_description, source="sec.submissions (link only)", as_of_ns=as_of_ns, now_ns=now_ns),
        "quote": {"status": "NOT_CONNECTED", "source": "market-data area", "as_of": ns_to_iso(as_of_ns),
                  "reason": "the research tearsheet has no quote feed; supply price, price_currency, price_source and price_time "
                            "to compute multiples"},
        "events": {"status": "NOT_LICENSED", "source": "corporate event calendar", "as_of": ns_to_iso(as_of_ns),
                   "reason": "no licensed event calendar (e.g. Wall Street Horizon via IBKR) is subscribed; see filings for dates"},
    }
    return {"entity_id": entity_id, "as_of": ns_to_iso(as_of_ns), "generated_at": ns_to_iso(now_ns), "blocks": blocks,
            "status_vocabulary": ["OK", "STALE", "NOT_LICENSED", "NOT_REPORTED", "NOT_AVAILABLE", "NOT_CONNECTED", "PENDING", "ERROR"]}
