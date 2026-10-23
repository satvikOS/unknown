"""Company and instrument search (CAP-INS-01, CAP-CO-01).

Local sources only (an API read path never calls the network):

1. SEC ``company_tickers_exchange.json`` snapshot received at or before the as-of time
   (``sources.sec_tickers``);
2. issuers ingested into the research store (SEC submissions: name, former names, SIC, tickers) and EU
   issuers from ESEF filings (``sources.esef``: LEI, name, filing countries, reporting currency) with their
   ESMA FIRDS listings (ISIN, MIC, CFI, currency; ``sources.firds``);
3. OpenFIGI mapping outcomes stored at or before the as-of time (``sources.openfigi``);
4. the core instrument master (``instruments`` and versioned ``provider_mappings``), read through
   the API's read-only core connection.

Ranking: EXACT_ID (CIK, ISIN, FIGI, instrument id or provider id) < EXACT_TICKER < NAME_PREFIX
< NAME_CONTAINS < FORMER_NAME (fuzzy name matches never auto-resolve, CAP-CO-01). Results are
grouped: one issuer per CIK with its listings; core instruments and FIGI listings separately. The
search never selects: ``selected`` is always null and the owner must choose explicitly. Queries
shaped like CUSIP or SEDOL are not looked up (licensed identifiers, CAP-INS-03). Every result
carries its source and retrieval time.
"""
from __future__ import annotations

import json
import sqlite3

from ...domain.clock import ns_to_iso
from ..sources import esef as esef_src
from ..sources import firds as firds_src
from ..sources import openfigi as openfigi_src
from ..sources import sec_tickers
from ..sources.identifiers import classify

RANK = {"EXACT_ID": 0, "EXACT_TICKER": 1, "NAME_PREFIX": 2, "NAME_CONTAINS": 3, "FORMER_NAME": 4}


def _like(text: str) -> str:
    return "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _name_match(q: str, name: str | None) -> str | None:
    if not name:
        return None
    ql, nl = q.lower(), name.lower()
    if nl.startswith(ql) or any(tok.startswith(ql) for tok in nl.replace(",", " ").split()):
        return "NAME_PREFIX"
    if ql in nl:
        return "NAME_CONTAINS"
    return None


def _better(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if RANK[a] <= RANK[b] else b


def search(query: str, *, as_of_ns: int, store=None, core_conn: sqlite3.Connection | None = None, limit: int = 20) -> dict:
    q = (query or "").strip()
    if not q or len(q) > 100:
        raise ValueError("query must be 1-100 characters")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    qtype = "LEI" if esef_src.lei_valid(q) else classify(q)
    qu = q.upper()
    cik_q = int(qu.removeprefix("CIK")) if qtype == "CIK" else None
    issuers: dict[int, dict] = {}
    others: list[dict] = []
    sources: list[dict] = []

    def issuer(cik: int, name: str) -> dict:
        return issuers.setdefault(cik, {"kind": "issuer", "cik": cik, "entity_id": f"SEC-CIK-{cik:010d}", "name": name,
                                        "listings": [], "match": None, "ingested": False, "sources": [],
                                        "source_label": "SEC EDGAR (US filer)"})

    if qtype == "LICENSED_ID":
        sources.append({"source": "query", "status": "REFUSED", "reason": "CUSIP/SEDOL-shaped query: licensed identifier, not "
                        "looked up without a recorded licence (CAP-INS-03)"})
        return _result(q, qtype, as_of_ns, [], sources)

    if store is not None:
        # 1. SEC tickers snapshot
        snap = sec_tickers.snapshot_at(store.conn, as_of_ns)
        if snap is None:
            first = sec_tickers.first_snapshot_time(store.conn)
            sources.append({"source": "sec.company_tickers_exchange", "status": "NOT_AVAILABLE",
                            "reason": "no snapshot received at or before the as-of time" +
                                      (f" (first snapshot {ns_to_iso(first)})" if first else " (never ingested)")})
        else:
            rows = store.conn.execute(
                "SELECT cik, name, ticker, exchange FROM ref_sec_ticker WHERE snapshot_id=? AND (UPPER(ticker)=? OR cik=? "
                "OR LOWER(name) LIKE ? ESCAPE '\\') LIMIT 500", (snap["rows_snapshot_id"], qu, cik_q or -1, _like(q))).fetchall()
            for r in rows:
                it = issuer(r["cik"], r["name"])
                m = "EXACT_ID" if cik_q == r["cik"] else "EXACT_TICKER" if r["ticker"].upper() == qu else _name_match(q, r["name"])
                it["match"] = _better(it["match"], m)
                it["listings"].append({"ticker": r["ticker"], "exchange": r["exchange"], "source": "sec.company_tickers_exchange",
                                       "retrieved_at": snap["retrieved_at"], "validity": "as of snapshot (no dates in source)"})
            sources.append({"source": "sec.company_tickers_exchange", "status": "OK", "retrieved_at": snap["retrieved_at"],
                            "snapshot_id": snap["snapshot_id"], "rows_matched": len(rows)})
        # 2. ingested issuers
        n_ing = 0
        for r in store.conn.execute("SELECT entity_id, name, attributes FROM entity WHERE kind='issuer'"):
            attrs = json.loads(r["attributes"] or "{}")
            cik = attrs.get("cik")
            if cik is None:
                continue
            m = "EXACT_ID" if cik_q == cik else _name_match(q, r["name"])
            aliases = store.conn.execute("SELECT scheme, value, valid_from_ns, valid_to_ns, source_ref FROM entity_alias WHERE entity_id=? "
                                         "AND valid_from_ns<=? AND valid_to_ns>?", (r["entity_id"], as_of_ns, as_of_ns)).fetchall()
            if any(a["value"].upper() == qu and a["scheme"].startswith("ticker:") for a in aliases):
                m = _better(m, "EXACT_TICKER")
            former = _former_names(store, attrs)
            if m is None and any(_name_match(q, n) for n in former):
                m = "FORMER_NAME"
            if m is None:
                continue
            n_ing += 1
            it = issuer(cik, r["name"])
            it.update({"ingested": True, "sic": attrs.get("sic")})
            it["match"] = _better(it["match"], m)
            if m == "FORMER_NAME":
                it["former_name_matches"] = [n for n in former if _name_match(q, n)]
            for a in aliases:
                if a["scheme"].startswith("ticker:"):
                    it["listings"].append({"ticker": a["value"], "exchange": a["scheme"].split(":", 1)[1], "source": "sec.submissions",
                                           "valid_from": ns_to_iso(a["valid_from_ns"]), "validity": "recorded from retrieval; not backdated"})
        sources.append({"source": "research.issuers", "status": "OK", "matched": n_ing})
        ingested = {json.loads(r["attributes"] or "{}").get("cik") for r in
                    store.conn.execute("SELECT attributes FROM entity WHERE kind='issuer'")}
        for cik, it in issuers.items():
            it["ingested"] = it["ingested"] or cik in ingested
        # 2b. EU issuers (ESEF) and their FIRDS listings
        n_eu = 0
        isin_leis = {i.get("issuer_lei") for i in firds_src.instruments_by_isin(store, qu)} if qtype == "ISIN" else set()
        for r in store.conn.execute("SELECT entity_id, name, attributes FROM entity WHERE kind='eu_issuer'"):
            attrs = json.loads(r["attributes"] or "{}")
            lei = attrs.get("lei")
            m = "EXACT_ID" if lei and (lei == qu or lei in isin_leis) else _name_match(q, r["name"])
            if m is None:
                continue
            listings = firds_src.instruments_for_issuer(store, lei)
            if m != "EXACT_ID" and any((i.get("short_name") or "").upper() == qu for i in listings):
                m = "EXACT_TICKER"
            n_eu += 1
            currency = _esef_currency(store, r["entity_id"])
            others.append({"kind": "eu_issuer", "lei": lei, "entity_id": r["entity_id"], "name": r["name"], "match": m,
                           "countries": attrs.get("countries", []), "esef_status": attrs.get("esef_status"), "reason": attrs.get("reason"),
                           "reporting_currency": currency, "filings": len(attrs.get("filings", [])),
                           "source": "filings.xbrl.org (ESEF)", "source_label": "ESEF annual reports (EU, IFRS)",
                           "retrieved_at": attrs.get("index_checked_at"), "ingested": True,
                           "listings": [{"isin": i.get("isin"), "mic": i.get("mic"), "cfi": i.get("cfi"), "currency": i.get("currency"),
                                         "name": i.get("full_name"), "source": "esma.firds"} for i in listings]})
        for i in (firds_src.instruments_by_isin(store, qu) if qtype == "ISIN" else []):
            others.append({"kind": "eu_instrument", "isin": i.get("isin"), "mic": i.get("mic"), "cfi": i.get("cfi"), "name": i.get("full_name"),
                           "currency": i.get("currency"), "issuer_lei": i.get("issuer_lei"), "match": "EXACT_ID", "source": "esma.firds",
                           "file_name": i.get("file_name")})
        sources.append({"source": "research.eu_issuers", "status": "OK", "matched": n_eu})
        # 3. OpenFIGI outcomes
        n_figi = 0
        for m in openfigi_src.mappings_as_of(store, as_of_ns):
            job_value = m["job"].split(":")[2] if m["job"].count(":") >= 2 else ""
            for c in m["outcome"].get("candidates", []):
                match = None
                if job_value.upper() == qu or (c.get("figi") or "").upper() == qu or (c.get("compositeFIGI") or "") == qu:
                    match = "EXACT_ID" if qtype in ("ISIN", "FIGI") else "EXACT_TICKER"
                elif (c.get("ticker") or "").upper() == qu:
                    match = "EXACT_TICKER"
                else:
                    match = _name_match(q, c.get("name"))
                if match is None:
                    continue
                n_figi += 1
                others.append({"kind": "figi_listing", "figi": c.get("figi"), "composite_figi": c.get("compositeFIGI"),
                               "share_class_figi": c.get("shareClassFIGI"), "name": c.get("name"), "ticker": c.get("ticker"),
                               "exch_code": c.get("exchCode"), "security_type": c.get("securityType"), "match": match,
                               "mapping_status": m["outcome"]["status"], "source": "openfigi.mapping", "retrieved_at": m["retrieved_at"],
                               "obs_id": m["obs_id"], "status_note": "unverified external mapping until a second source agrees (CAP-INS-02)"})
        sources.append({"source": "openfigi.mapping", "status": "OK", "matched": n_figi})

    if core_conn is not None:
        try:
            n_core = 0
            for r in core_conn.execute("SELECT instrument_id, symbol, body FROM instruments"):
                body = json.loads(r["body"])
                maps = core_conn.execute("SELECT provider, external_id, version, row FROM provider_mappings WHERE instrument_id=? "
                                         "ORDER BY provider, version", (r["instrument_id"],)).fetchall()
                m = None
                if r["instrument_id"].upper() == qu or any(x["external_id"].upper() == qu for x in maps):
                    m = "EXACT_ID"
                elif r["symbol"].upper() == qu:
                    m = "EXACT_TICKER"
                else:
                    m = _name_match(q, body.get("name"))
                if m is None:
                    continue
                n_core += 1
                others.append({"kind": "instrument", "instrument_id": r["instrument_id"], "symbol": r["symbol"], "name": body.get("name"),
                               "asset_class": body.get("asset_class"), "venue_mic": body.get("venue_mic"), "currency": body.get("currency"),
                               "status": body.get("status"), "match": m, "source": "core instrument master",
                               "mappings": [{"provider": x["provider"], "external_id": x["external_id"], "version": x["version"]} for x in maps]})
            sources.append({"source": "core.instruments", "status": "OK", "matched": n_core})
        except sqlite3.OperationalError as exc:
            sources.append({"source": "core.instruments", "status": "NOT_AVAILABLE", "reason": f"core database: {exc}"})
    else:
        sources.append({"source": "core.instruments", "status": "NOT_AVAILABLE", "reason": "no core connection"})

    cands = [c for c in issuers.values() if c["match"] is not None] + others
    for c in cands:
        if c["kind"] == "issuer":
            seen, uniq = set(), []
            for li in c["listings"]:
                k = (li["ticker"], li["exchange"], li["source"])
                if k not in seen:
                    seen.add(k)
                    uniq.append(li)
            c["listings"] = uniq
    cands.sort(key=lambda c: (RANK[c["match"]], 0 if c["kind"] == "instrument" else 1 if c["kind"] in ("issuer", "eu_issuer") else 2,
                              c.get("name") or "", str(c.get("cik") or c.get("instrument_id") or c.get("figi") or c.get("lei") or c.get("isin"))))
    return _result(q, qtype, as_of_ns, cands[:limit], sources, total=len(cands))


def _esef_currency(store, entity_id: str) -> str | None:
    """Reporting currency of the newest ESEF money fact held (label only; statements recompute it as of a time)."""
    r = store.conn.execute("SELECT unit FROM observation WHERE dataset=? AND entity_id=? AND field IN ('ifrs-full:Assets', "
                           "'ifrs-full:Revenue', 'ifrs-full:ProfitLoss') AND length(unit)=3 ORDER BY period_end DESC LIMIT 1",
                           (esef_src.DATASET, entity_id)).fetchone()
    return r["unit"] if r else None


def _former_names(store, attrs: dict) -> list[str]:
    sha = attrs.get("submissions_ref")
    if not sha or store.raw_dir is None:
        return []
    path = store.raw_dir / sha
    if not path.exists():
        return []
    try:
        return [f.get("name") for f in json.loads(path.read_bytes()).get("formerNames", []) if f.get("name")]
    except (ValueError, OSError):
        return []


def _result(q, qtype, as_of_ns, cands, sources, total=0) -> dict:
    return {"query": q, "query_type": qtype, "as_of": ns_to_iso(as_of_ns), "candidates": cands, "total_matches": total,
            "selected": None, "sources": sources,
            "note": "Candidates are ranked, never selected: the owner chooses explicitly, and nothing becomes tradeable without "
                    "broker contract verification (CAP-INS-01). Name and former-name matches never auto-resolve (CAP-CO-01)."}
