"""Point-in-time equity screen runs with a reproducible manifest (CAP-MKT-10; book ch. 11).

A screen definition (validated by :func:`compile_definition`)::

    {"universe": {"sources": ["sec", "esef"], "sic_from": 3500, "sic_to": 3599, "exchanges": ["Nasdaq"],
                  "countries": ["FR"], "entities": ["SEC-CIK-0000320193"]},
     "criteria": [{"field": "gross_margin", "op": "gte", "value": "0.4"},
                  {"field": "revenue_fy", "op": "between", "value": "1000000000", "value2": "5000000000", "currency": "USD",
                   "missing": "include", "nm": "exclude"}],
     "sort": {"field": "revenue_ttm", "direction": "desc"}, "limit": 200}

Values are exact decimal strings (ratios as fractions: 0.4 = 40 %). A money criterion names its currency;
a value in another currency is NOT_APPLICABLE (no FX conversion is applied).

Missing values never pass or fail silently. Per criterion, ``missing`` is ``exclude`` (default: the row is
excluded and counted with the reason) or ``include`` (the row is kept as UNKNOWN for that criterion,
flagged); ``nm`` (not meaningful, e.g. P/E with negative earnings) is ``exclude`` by default.

Reproducibility. A run is computed in audit mode: facts are those available at ``as_of`` *and* known to
this system at ``knowledge_cutoff`` (default: the run time), so a later ingestion cannot change it. The
manifest records the definition hash, as-of and cutoff, the universe (entity ids), every price input
(source, value, observation time) and per-dataset data versions; ``rerun`` replays exactly those inputs
and compares the result hash. The SEC frames API is not point-in-time and is refused as a source.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..data.fundamentals.metrics import PRICE_DATASET, PriceInput
from ..data.fundamentals.peers import issuers as sec_issuers
from ..data.pit import OPEN_END
from ..data.sources.esef import DATASET as ESEF_DATASET
from ..domain.clock import NS_PER_S, ns_to_iso
from ..domain.fixed import Fixed
from .fields import ESEF, FIELDS, SEC, IssuerView

ENGINE = "screener@1"
OPS = {"gt", "gte", "lt", "lte", "between"}
TREATMENTS = {"exclude", "include"}
MAX_CRITERIA = 20
STALE_PRICE_DAYS = 5
MIC = {"NASDAQ": "XNAS", "NYSE": "XNYS"}       # SEC submissions exchange names -> ISO 10383 MIC for live keys
DATASETS = ("sec.companyfacts", ESEF_DATASET, PRICE_DATASET)


class ScreenRefused(ValueError):
    """The definition cannot be run as written (HTTP 400 with the reason)."""


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def _dec(v, where: str) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise ScreenRefused(f"{where}: value must be a decimal string (got {type(v).__name__}; floats are refused)")
    try:
        return Fixed.parse(str(v)).to_str()
    except ValueError as exc:
        raise ScreenRefused(f"{where}: {exc}") from None


def compile_definition(d: dict) -> dict:
    """Validate and normalise a definition; raises ScreenRefused with an explicit reason."""
    if not isinstance(d, dict):
        raise ScreenRefused("definition must be an object")
    text = canonical(d).lower()
    if "frames" in text and ("/frames/" in text or "sec.frames" in text or "frames:" in text):
        raise ScreenRefused("the SEC frames API is refused as a screen source: it returns one fact per entity that was last filed for a "
                            "calendar period, so it is not point-in-time and would leak later filings into an as-of run")
    u = d.get("universe") or {}
    sources = u.get("sources") or [SEC, ESEF]
    if not set(sources) <= {SEC, ESEF}:
        raise ScreenRefused(f"universe.sources must be within ['sec', 'esef'] (got {sources})")
    uni = {"sources": sorted(set(sources))}
    for k in ("sic_from", "sic_to"):
        if u.get(k) not in (None, ""):
            try:
                uni[k] = int(u[k])
            except (TypeError, ValueError):
                raise ScreenRefused(f"universe.{k} must be an integer SIC code") from None
    for k in ("exchanges", "countries", "entities"):
        if u.get(k):
            if not isinstance(u[k], list) or not all(isinstance(x, str) for x in u[k]):
                raise ScreenRefused(f"universe.{k} must be a list of strings")
            uni[k] = sorted({x.strip() for x in u[k] if x.strip()})
    crit = []
    raw = d.get("criteria") or []
    if not isinstance(raw, list) or len(raw) > MAX_CRITERIA:
        raise ScreenRefused(f"criteria must be a list of at most {MAX_CRITERIA}")
    for i, c in enumerate(raw):
        where = f"criteria[{i}]"
        if not isinstance(c, dict):
            raise ScreenRefused(f"{where} must be an object")
        f = c.get("field")
        if f not in FIELDS:
            raise ScreenRefused(f"{where}: unknown field {f!r} (GET /api/screener/fields lists them)")
        op = c.get("op")
        if op not in OPS:
            raise ScreenRefused(f"{where}: op must be one of {sorted(OPS)}")
        out = {"field": f, "op": op, "value": _dec(c.get("value"), where), "missing": c.get("missing") or "exclude",
               "nm": c.get("nm") or "exclude"}
        if op == "between":
            out["value2"] = _dec(c.get("value2"), where)
            if Fixed.parse(out["value2"]) < Fixed.parse(out["value"]):
                raise ScreenRefused(f"{where}: value2 must be >= value")
        if out["missing"] not in TREATMENTS or out["nm"] not in TREATMENTS:
            raise ScreenRefused(f"{where}: missing and nm must be 'exclude' or 'include'")
        if FIELDS[f].unit in ("money", "price"):
            cur = (c.get("currency") or "").strip().upper()
            if len(cur) != 3 or not cur.isalpha():
                raise ScreenRefused(f"{where}: a {FIELDS[f].unit} criterion needs a 3-letter currency (no FX conversion is applied)")
            out["currency"] = cur
        crit.append(out)
    s = d.get("sort") or {}
    sort = None
    if s.get("field"):
        if s["field"] not in FIELDS:
            raise ScreenRefused(f"sort: unknown field {s['field']!r}")
        sort = {"field": s["field"], "direction": "asc" if s.get("direction") == "asc" else "desc"}
    limit = d.get("limit", 200)
    if not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ScreenRefused("limit must be an integer between 1 and 2000")
    columns = [c for c in (d.get("columns") or []) if c in FIELDS][:12]
    return {"universe": uni, "criteria": crit, "sort": sort, "limit": limit, "columns": columns}


# ------------------------------------------------------------------ universe
def universe(store, uni: dict) -> list[dict]:
    """Issuers held in the research store that match the universe filters (identity attributes are current values)."""
    out = []
    if SEC in uni["sources"]:
        for iss in sec_issuers(store):
            row = store.conn.execute("SELECT attributes FROM entity WHERE entity_id=?", (iss["entity_id"],)).fetchone()
            attrs = json.loads(row["attributes"] or "{}") if row else {}
            exch, ticker = _listing(store, iss["entity_id"])
            out.append({"entity_id": iss["entity_id"], "name": iss["name"], "source": SEC, "cik": iss["cik"], "sic": iss["sic"],
                        "exchange": exch, "ticker": ticker, "country": attrs.get("country"), "lei": None})
    if ESEF in uni["sources"]:
        for r in store.conn.execute("SELECT entity_id, name, attributes FROM entity WHERE kind='eu_issuer' ORDER BY entity_id"):
            attrs = json.loads(r["attributes"] or "{}")
            countries = attrs.get("countries") or []
            out.append({"entity_id": r["entity_id"], "name": r["name"], "source": ESEF, "cik": None, "sic": None, "exchange": None,
                        "ticker": None, "country": countries[0] if countries else None, "lei": attrs.get("lei") or r["entity_id"].split("-")[-1]})
    keep = []
    for iss in out:
        if uni.get("entities") and iss["entity_id"] not in uni["entities"]:
            continue
        if ("sic_from" in uni or "sic_to" in uni):
            try:
                sic = int(iss["sic"])
            except (TypeError, ValueError):
                continue
            if sic < uni.get("sic_from", 0) or sic > uni.get("sic_to", 9999):
                continue
        if uni.get("exchanges") and (iss["exchange"] or "") not in uni["exchanges"]:
            continue
        if uni.get("countries") and (iss["country"] or "") not in uni["countries"]:
            continue
        keep.append(iss)
    return keep


def _listing(store, eid: str) -> tuple[str | None, str | None]:
    r = store.conn.execute("SELECT scheme, value FROM entity_alias WHERE entity_id=? AND scheme LIKE 'ticker:%' AND valid_to_ns=? "
                           "ORDER BY valid_from_ns, scheme, value LIMIT 1", (eid, OPEN_END)).fetchone()
    return (r["scheme"].split(":", 1)[1], r["value"]) if r else (None, None)


def live_key(iss: dict) -> str | None:
    mic = MIC.get((iss.get("exchange") or "").upper())
    return f"{iss['ticker']}.{mic}" if mic and iss.get("ticker") else None


# ------------------------------------------------------------------ prices
def owner_price(store, eid: str, as_of_ns: int, cutoff: int | None) -> PriceInput | None:
    sql = "SELECT * FROM observation WHERE dataset=? AND entity_id=? AND available_time_ns<=?"
    params: list = [PRICE_DATASET, eid, as_of_ns]
    if cutoff is None:
        sql += " AND known_to_ns=?"
        params.append(OPEN_END)
    else:
        sql += " AND known_from_ns<=? AND known_to_ns>?"
        params += [cutoff, cutoff]
    r = store.conn.execute(sql + " ORDER BY available_time_ns DESC, obs_id DESC LIMIT 1", tuple(params)).fetchone()
    if r is None or r["value"] is None:
        return None
    src = r["source_ref"].removeprefix("owner:")
    return PriceInput(Fixed.parse(r["value"]), r["currency"] or "USD", src, r["available_time_ns"], r["obs_id"], r["field"].split(":", 1)[-1])


def live_price(quote: dict | None, as_of_ns: int) -> PriceInput | None:
    """A live quote event from the market-data bus: last trade price (never a mid or a bid/ask guess)."""
    if not quote or not quote.get("last"):
        return None
    t = int(quote.get("ts_source_ns") or quote.get("ts_recv_ns") or 0)
    if t <= 0 or t > as_of_ns:
        return None
    src = f"live:{quote.get('source')}:{quote.get('data_type')}:{quote.get('key')}"
    return PriceInput(Fixed.parse(str(quote["last"])), (quote.get("currency") or "USD").upper(), src, t, None, quote.get("key") or "")


def _price_json(p: PriceInput) -> dict:
    return {"value": p.value.to_str(), "currency": p.currency, "source": p.source, "observed_at_ns": str(p.observed_at_ns),
            "observed_at": ns_to_iso(p.observed_at_ns), "obs_id": p.obs_id, "listing": p.listing}


def _price_from_json(d: dict) -> PriceInput:
    return PriceInput(Fixed.parse(d["value"]), d["currency"], d["source"], int(d["observed_at_ns"]), d.get("obs_id"), d.get("listing", ""))


def resolve_prices(store, issuers: list[dict], as_of_ns: int, cutoff: int | None, live_quotes: dict[str, dict] | None,
                   live_status: dict[str, str] | None) -> tuple[dict[str, PriceInput], dict[str, tuple[str, str]]]:
    prices, reasons = {}, {}
    stale_ns = STALE_PRICE_DAYS * 86_400 * NS_PER_S
    for iss in issuers:
        eid = iss["entity_id"]
        p = owner_price(store, eid, as_of_ns, cutoff)
        key = live_key(iss)
        lp = live_price((live_quotes or {}).get(key), as_of_ns) if key else None
        if lp is not None and (p is None or lp.observed_at_ns >= p.observed_at_ns):
            p = lp
        if p is not None and as_of_ns - p.observed_at_ns > stale_ns:
            reasons[eid] = ("STALE", f"latest price {p.source} observed {ns_to_iso(p.observed_at_ns)} is older than {STALE_PRICE_DAYS} days")
            continue
        if p is not None:
            prices[eid] = p
            continue
        if key is None:
            reasons[eid] = ("NO_SOURCE", "no owner price input at or before the as-of time and no live-quote key for this listing")
        elif live_status is None:
            reasons[eid] = ("NO_DATA", "no owner price input at or before the as-of time (live quotes are used only for runs as of now)")
        elif live_quotes is None:
            reasons[eid] = ("NOT_CONNECTED", f"no owner price input, and no market-data session in this API process to quote {key}")
        else:
            reasons[eid] = ("PENDING" if key in live_status else "NOT_CONNECTED",
                            live_status.get(key) or f"no live quote for {key}: no market-data session is streaming it")
    return prices, reasons


# ------------------------------------------------------------------ evaluation
def _test(c: dict, v: Fixed) -> bool:
    a = Fixed.parse(c["value"])
    return {"gt": v > a, "gte": v >= a, "lt": v < a, "lte": v <= a,
            "between": a <= v <= Fixed.parse(c.get("value2") or c["value"])}[c["op"]]


def evaluate(view: IssuerView, criteria: list[dict]) -> tuple[str, list[dict]]:
    """(outcome, per-criterion checks). Outcome: PASS | UNKNOWN | FAIL | EXCLUDED."""
    checks, failed, excluded, unknown = [], False, False, False
    for c in criteria:
        val = view.get(c["field"])
        check = {"field": c["field"], "value": val["value"], "status": val["status"], "unit": val["unit"], "currency": val["currency"]}
        if val["value"] is not None and c.get("currency") and val["currency"] and val["currency"].upper() != c["currency"]:
            check.update(result="UNKNOWN" if c["missing"] == "include" else "EXCLUDED", reason="NOT_APPLICABLE",
                         detail=f"value in {val['currency']}, criterion in {c['currency']} (no FX conversion)")
        elif val["value"] is not None:
            check["result"] = "PASS" if _test(c, Fixed.parse(val["value"])) else "FAIL"
        else:
            treat = c["nm"] if val["status"] == "NM" else c["missing"]
            check.update(result="UNKNOWN" if treat == "include" else "EXCLUDED", reason=val.get("reason"), detail=val.get("detail"))
            check["treatment"] = f"{'nm' if val['status'] == 'NM' else 'missing'}={treat}"
        failed |= check["result"] == "FAIL"
        excluded |= check["result"] == "EXCLUDED"
        unknown |= check["result"] == "UNKNOWN"
        checks.append(check)
    outcome = "FAIL" if failed else "EXCLUDED" if excluded else "UNKNOWN" if unknown else "PASS"
    return outcome, checks


def data_versions(store, as_of_ns: int, cutoff: int) -> dict:
    out = {}
    for ds in DATASETS:
        r = store.conn.execute("SELECT COUNT(*) AS n, MAX(obs_id) AS max_id, MAX(available_time_ns) AS latest FROM observation WHERE dataset=? "
                               "AND available_time_ns<=? AND known_from_ns<=? AND known_to_ns>?", (ds, as_of_ns, cutoff, cutoff)).fetchone()
        out[ds] = {"observations": r["n"], "max_obs_id": r["max_id"], "latest_available": ns_to_iso(r["latest"]) if r["latest"] else None}
    return out


def run(store, definition: dict, as_of_ns: int, *, now_ns: int, knowledge_cutoff_ns: int | None = None,
        live_quotes: dict[str, dict] | None = None, live_status: dict[str, str] | None = None, replay: dict | None = None) -> dict:
    """Run a screen. ``replay`` (a stored manifest) fixes the universe and price inputs to those of an earlier run."""
    spec = compile_definition(definition)
    cutoff = knowledge_cutoff_ns if knowledge_cutoff_ns is not None else now_ns
    if cutoff < as_of_ns:
        cutoff = as_of_ns if knowledge_cutoff_ns is None else cutoff
    if replay is not None:
        ids = set(replay["universe"]["entity_ids"])
        issuers = [i for i in universe(store, {**spec["universe"], "entities": sorted(ids)})]
        prices = {eid: _price_from_json(p) for eid, p in replay["price_inputs"].items()}
        price_reasons = {eid: tuple(r) for eid, r in replay.get("price_reasons", {}).items()}
    else:
        issuers = universe(store, spec["universe"])
        needs_price = any(FIELDS[c["field"]].kind == "quote" or (FIELDS[c["field"]].kind == "metric" and _needs_price(c["field"]))
                          for c in spec["criteria"]) or (spec["sort"] and _needs_price(spec["sort"]["field"])) or \
            any(_needs_price(c) for c in spec["columns"])
        prices, price_reasons = resolve_prices(store, issuers, as_of_ns, cutoff, live_quotes, live_status) if needs_price else ({}, {})
    rows, counts, reason_counts, excluded_rows, no_data = [], {"PASS": 0, "UNKNOWN": 0, "FAIL": 0, "EXCLUDED": 0}, {}, [], 0
    shown = list(dict.fromkeys([c["field"] for c in spec["criteria"]] + spec["columns"] + ([spec["sort"]["field"]] if spec["sort"] else [])))
    for iss in issuers:
        view = IssuerView(store, iss, as_of_ns, cutoff, prices.get(iss["entity_id"]), price_reasons.get(iss["entity_id"]))
        has = view.has_data()
        no_data += not has
        outcome, checks = evaluate(view, spec["criteria"])
        counts[outcome] += 1
        ident = {k: iss[k] for k in ("entity_id", "name", "source", "cik", "lei", "ticker", "exchange", "sic", "country")}
        if outcome == "EXCLUDED":
            for ch in checks:
                if ch["result"] == "EXCLUDED":
                    k = f"{ch['field']}: {ch.get('reason')}"
                    reason_counts[k] = reason_counts.get(k, 0) + 1
            excluded_rows.append({**ident, "has_data_at_as_of": has,
                                  "reasons": [{"field": ch["field"], "reason": ch.get("reason"), "detail": ch.get("detail")}
                                              for ch in checks if ch["result"] == "EXCLUDED"]})
        if outcome not in ("PASS", "UNKNOWN"):
            continue
        rows.append({**ident, "outcome": outcome, "has_data_at_as_of": has, "checks": checks,
                     "values": {f: view.get(f) for f in shown},
                     "price": _price_json(prices[iss["entity_id"]]) if iss["entity_id"] in prices else None})
    if spec["sort"]:
        f, desc = spec["sort"]["field"], spec["sort"]["direction"] == "desc"
        have = [r for r in rows if r["values"][f]["value"] is not None]
        lack = [r for r in rows if r["values"][f]["value"] is None]
        have.sort(key=lambda r: Fixed.parse(r["values"][f]["value"]), reverse=desc)
        rows = have + sorted(lack, key=lambda r: r["entity_id"])
    else:
        rows.sort(key=lambda r: (r["name"] or "", r["entity_id"]))
    truncated = len(rows) > spec["limit"]
    rows = rows[:spec["limit"]]
    body = {"results": rows, "excluded": excluded_rows, "counts": counts}
    manifest = {"engine": ENGINE, "definition": spec, "definition_hash": sha(spec), "as_of_ns": str(as_of_ns), "as_of": ns_to_iso(as_of_ns),
                "knowledge_cutoff_ns": str(cutoff), "knowledge_cutoff": ns_to_iso(cutoff), "mode": "audit (known at the cutoff)",
                "universe": {"entity_ids": [i["entity_id"] for i in issuers], "count": len(issuers),
                             "by_source": {s: sum(1 for i in issuers if i["source"] == s) for s in (SEC, ESEF)}},
                "price_inputs": {eid: _price_json(p) for eid, p in sorted(prices.items())},
                "price_reasons": {eid: list(r) for eid, r in sorted(price_reasons.items())},
                "data_versions": data_versions(store, as_of_ns, cutoff),
                "formulas": sorted({FIELDS[f].formula for f in shown})}
    return {"as_of": manifest["as_of"], "knowledge_cutoff": manifest["knowledge_cutoff"], "results": rows, "excluded": excluded_rows,
            "counts": counts, "excluded_by_reason": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])),
            "truncated": truncated, "columns": shown, "manifest": manifest, "result_hash": sha(body),
            "coverage": {"universe": len(issuers), "with_data_at_as_of": len(issuers) - no_data, "without_data_at_as_of": no_data,
                         "note": "The universe is the issuers held in this research store (ingested), not an exchange listing; issuers "
                                 "that delisted are included only if they were ingested, so survivorship coverage is not measured."},
            "computed_at": ns_to_iso(now_ns)}


def _needs_price(field: str) -> bool:
    from ..data.fundamentals.metrics import FORMULAS
    return field == "price" or (field in FORMULAS and FORMULAS[field].needs_price)


def rerun(store, stored: dict, *, now_ns: int) -> dict:
    """Recompute a stored run from its manifest (same definition, as-of, cutoff, universe and price inputs) and compare."""
    m = stored["manifest"]
    again = run(store, m["definition"], int(m["as_of_ns"]), now_ns=now_ns, knowledge_cutoff_ns=int(m["knowledge_cutoff_ns"]), replay=m)
    return {"reproducible": again["result_hash"] == stored["result_hash"], "stored_hash": stored["result_hash"],
            "recomputed_hash": again["result_hash"], "as_of": m["as_of"], "knowledge_cutoff": m["knowledge_cutoff"],
            "data_versions_then": m["data_versions"], "data_versions_now": again["manifest"]["data_versions"],
            "counts_then": stored["counts"], "counts_now": again["counts"]}


def iso_day_end_ns(day: str) -> int:
    d = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(d.timestamp()) * NS_PER_S + 86_400 * NS_PER_S - 1
