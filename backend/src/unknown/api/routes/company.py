"""Read-only company research and macro routes (CAP-CO-01/02/05/07/10/11/12, CAP-INS-01, CAP-MAC-07, CAP-MAC-12).

Registered as route module ``company`` (``ROUTES`` below). Every handler opens the research store
read-only (``~/.unknown/research/research.sqlite``, ``mode=ro``), computes from point-in-time data
as of the ``as_of`` query parameter (ISO date = end of that UTC day, or an ISO instant with a zone;
default now), and returns real values with lineage or an explicit unavailable reason. No handler
calls the network or writes anything.

  GET /api/company/search?q=&as_of=&limit=
  GET /api/company/metric-record/<metric_id>                  show calculation of a persisted metric
  GET /api/company/<cik>[/tearsheet]?as_of=&price=...
  GET /api/company/<cik>/filings?as_of=&forms=10-K,10-Q&limit=
  GET /api/company/<cik>/statements?as_of=&basis=FY|Q|TTM&periods=&known_at=&history=1
  GET /api/company/<cik>/metrics?as_of=&price=...
  GET /api/company/<cik>/metrics/<metric>?as_of=&price=...     show calculation
  GET /api/company/<cik>/peers?as_of=&peers=<cik>,<cik>&price=...
  GET /api/company/<cik>/dcf?as_of=&g=&fcff=a,b,c | fcff_growth=&years= ; wacc= | rf=|rf_tenor=, beta=, erp=, kd=, ...
  GET /api/macro                                              catalog of macro series held
  GET /api/macro/yield-curve?as_of=&date=
  GET /api/macro/fx?as_of=&date=&base=EUR

Price inputs (multiples, market-value weights) are never fetched: pass ``price``,
``price_currency``, ``price_source`` and ``price_time`` (ISO instant at or before as_of).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from ...data.fundamentals import dcf as dcf_mod
from ...data.fundamentals.filings import filings as filings_list
from ...data.fundamentals.filings import identity, load_submissions
from ...data.fundamentals.metrics import FORMULAS, MetricEngine, PriceInput, explain_record
from ...data.fundamentals.peers import comps, peer_group
from ...data.fundamentals.readonly import ReadOnlyResearchStore, research_paths
from ...data.fundamentals.search import search
from ...data.fundamentals.statements import Fundamentals
from ...data.fundamentals.tearsheet import tearsheet
from ...data.fundamentals.valuation import ModelRefused, dcf_model
from ...data.pit import OPEN_END
from ...data.sources import ecb, treasury
from ...domain.clock import NS_PER_S, ns_to_iso
from ...domain.fixed import Fixed

END_OF_DAY_NS = 86_400 * NS_PER_S - 1


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_instant(text: str, *, name: str) -> int:
    """A plain date means the end of that UTC day; an instant must carry a zone."""
    t = text.strip()
    if len(t) == 10:
        d = date.fromisoformat(t)
        return (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - EPOCH) // timedelta(microseconds=1) * 1000 + END_OF_DAY_NS
    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"{name} must include a time zone (e.g. 2026-10-22T20:00:00Z) or be a plain date")
    return (dt - EPOCH) // timedelta(microseconds=1) * 1000


def as_of(q: dict) -> int:
    if q.get("as_of"):
        return parse_instant(q["as_of"], name="as_of")
    return time.time_ns()


def price_input(q: dict, t_ns: int) -> PriceInput | None:
    if not q.get("price"):
        return None
    missing = [k for k in ("price_currency", "price_source", "price_time") if not q.get(k)]
    if missing:
        raise ValueError(f"a price input needs {', '.join(missing)} (source and observation time are required)")
    observed = parse_instant(q["price_time"], name="price_time")
    if observed > t_ns:
        raise ValueError("price_time is after as_of: look-ahead refused")
    return PriceInput(Fixed.parse(q["price"]), q["price_currency"].upper(), q["price_source"], observed, None, q.get("price_listing", ""))


def parse_cik(text: str) -> int:
    t = text.upper().removeprefix("CIK")
    if not t.isdigit() or len(t) > 10:
        raise ValueError("company id must be a CIK (digits); use /api/company/search to find it")
    return int(t)


def _open(ctx):
    db, raw = research_paths(ctx.paths.root)
    try:
        return ReadOnlyResearchStore(db, raw)
    except FileNotFoundError:
        return None


def _unavailable(reason: str) -> dict:
    return {"status": "UNAVAILABLE", "reason": reason}


NO_STORE = ("research store not initialised: ingest an issuer first "
            "(python -m unknown.data.sec ingest --cik <CIK>)")


def _sic(store, entity_id: str):
    _, meta = load_submissions(store, entity_id)
    if meta.get("status") == "NOT_INGESTED":
        raise LookupError(f"{entity_id} is not ingested; run python -m unknown.data.sec ingest --cik {int(entity_id[8:])}")
    return (meta.get("attributes") or {}).get("sic")


def company(ctx, parts, q, account_key, conn):
    store = _open(ctx)
    if len(parts) >= 2 and parts[1] == "search":
        try:
            return search(q.get("q", ""), as_of_ns=as_of(q), store=store, core_conn=conn, limit=int(q.get("limit", 20)))
        finally:
            if store is not None:
                store.close()
    if store is None:
        return _unavailable(NO_STORE)
    try:
        if len(parts) == 3 and parts[1] == "metric-record":
            return explain_record(store, int(parts[2]))
        if len(parts) < 2:
            raise ValueError("use /api/company/<cik>/... or /api/company/search?q=")
        cik = parse_cik(parts[1])
        entity_id = f"SEC-CIK-{cik:010d}"
        sub = parts[2] if len(parts) > 2 else "tearsheet"
        t = as_of(q)
        if sub == "tearsheet":
            _sic(store, entity_id)
            peers = [f"SEC-CIK-{parse_cik(x):010d}" for x in q["peers"].split(",")] if q.get("peers") else None
            return tearsheet(store, entity_id, t, price=price_input(q, t), peer_list=peers)
        if sub == "filings":
            subs, meta = load_submissions(store, entity_id)
            if subs is None:
                if meta.get("status") == "NOT_INGESTED":
                    raise LookupError(meta["reason"])
                return {**_unavailable(meta.get("reason", "submissions payload unavailable")), "entity_id": entity_id}
            forms = {x.strip() for x in q["forms"].split(",")} if q.get("forms") else None
            return {"identity": identity(subs, meta, t), **filings_list(subs, t, forms=forms, limit=min(int(q.get("limit", 40)), 1000))}
        if sub == "statements":
            _sic(store, entity_id)
            known = parse_instant(q["known_at"], name="known_at") if q.get("known_at") else None
            f = Fundamentals(store, entity_id, t, knowledge_cutoff_ns=known)
            return f.statement(q.get("basis", "FY").upper(), int(q.get("periods", 4)), history=q.get("history", "1") != "0")
        if sub == "metrics":
            sic = _sic(store, entity_id)
            f = Fundamentals(store, entity_id, t)
            eng = MetricEngine(f, price_input(q, t), sic=sic)
            if len(parts) == 4:
                if parts[3] not in FORMULAS:
                    raise LookupError(f"unknown metric {parts[3]}; one of {', '.join(FORMULAS)}")
                return eng.explain(parts[3])
            return {"entity_id": entity_id, "as_of": ns_to_iso(t), "currency": f.currency, "mapping": "std-map@1",
                    "latest_quarter": f.latest_quarter().to_json() if f.latest_quarter() else None,
                    "price_input": eng.price.to_json() if eng.price else None, "metrics": [r.to_json(cik) for r in eng.all()]}
        if sub == "peers":
            _sic(store, entity_id)
            explicit = [f"SEC-CIK-{parse_cik(x):010d}" for x in q["peers"].split(",")] if q.get("peers") else None
            group = peer_group(store, entity_id, t, explicit=explicit)
            p = price_input(q, t)
            return {"group": group, "comps": comps(store, entity_id, group, t, prices={entity_id: p} if p else None)}
        if sub == "dcf":
            sic = _sic(store, entity_id)
            f = Fundamentals(store, entity_id, t)
            rf_point = None
            if q.get("rf_tenor"):
                rf_point = treasury.point(store, q["rf_tenor"], t, q.get("rf_date"))
                if rf_point is None:
                    return _unavailable(f"no Treasury par yield for tenor {q['rf_tenor']} available at the as-of time "
                                        "(ingest with python -m unknown.data.sources.treasury)")
            if not q.get("g"):
                raise ValueError("g (terminal growth, decimal e.g. 0.025) is required")
            try:
                return dcf_model(
                    f, sic=sic, g=q["g"], fcff=[x for x in q["fcff"].split(",") if x.strip()] if q.get("fcff") else None,
                    fcff_growth=q.get("fcff_growth"), years=int(q["years"]) if q.get("years") else None, wacc=q.get("wacc"),
                    rf=q.get("rf"), rf_point=rf_point, beta=q.get("beta"), erp=q.get("erp"), crp=q.get("crp"), kd=q.get("kd"),
                    tax=q.get("tax"), we=q.get("we"), wd=q.get("wd"), rate_currency=q.get("rate_currency"), price=price_input(q, t),
                    step_w=q.get("step_wacc", "0.01"), step_g=q.get("step_g", "0.005"),
                    allow_financial=q.get("allow_financial") == "1", allow_currency_mismatch=q.get("allow_currency_mismatch") == "1")
            except (ModelRefused, dcf_mod.DcfInputError) as exc:
                return {"status": "REFUSED", "reason": str(exc), "model": dcf_mod.FORMULA, "entity_id": entity_id, "as_of": ns_to_iso(t)}
        raise LookupError(f"unknown company endpoint {sub}")
    finally:
        store.close()


def macro(ctx, parts, q, account_key, conn):
    store = _open(ctx)
    if store is None:
        return _unavailable("research store not initialised: ingest macro series first (python -m unknown.data.sources.treasury "
                            "ingest --month YYYYMM; python -m unknown.data.sources.ecb ingest --last 1)")
    try:
        t = as_of(q)
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "yield-curve":
            return treasury.curve_as_of(store, t, q.get("date"))
        if sub == "fx":
            rates = ecb.rates_as_of(store, t, q.get("date"))
            base = (q.get("base") or "EUR").upper()
            if rates["status"] == "OK" and base != "EUR":
                try:
                    rates["normalized"] = {"base": base, "rates": ecb.normalize(rates["rates"], base)}
                except LookupError as exc:
                    rates["normalized"] = {"base": base, "status": "UNAVAILABLE", "reason": str(exc)}
            return rates
        if sub == "":
            # Catalog as of t: only observations available at t (current knowledge), so a past as_of never
            # reveals later dates or counts (CAP-MAC-05, ADR-0015).
            out = []
            for ds, label in ((treasury.DATASET, "US Treasury daily par yield curve (nominal)"),
                              (ecb.DATASET, "ECB euro foreign exchange reference rates")):
                r = store.conn.execute("SELECT COUNT(*) AS n, MIN(period_end) AS first, MAX(period_end) AS last, "
                                       "MAX(available_time_ns) AS newest FROM observation WHERE dataset=? AND available_time_ns<=? "
                                       "AND known_to_ns=?", (ds, t, OPEN_END)).fetchone()
                out.append({"dataset": ds, "title": label, "observations": r["n"], "first_date": r["first"], "last_date": r["last"],
                            "newest_available_time": ns_to_iso(r["newest"]) if r["newest"] is not None else None,
                            "status": "OK" if r["n"] else "NOT_AVAILABLE_AT_AS_OF",
                            **({} if r["n"] else {"reason": "no observation of this dataset available at the as-of time "
                                                            "(not ingested, or ingested data starts later)"})})
            return {"series": out, "as_of": ns_to_iso(t),
                    "not_available": ["FRED/ALFRED (needs an owner-registered API key)", "H.15, SOFR, ECB yield curves, Eurostat "
                                      "(not yet ingested)"]}
        raise LookupError(f"unknown macro endpoint {sub}")
    finally:
        store.close()


ROUTES = [(("company",), company), (("macro",), macro)]
