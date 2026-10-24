"""Read-only platform data behind the analyst's tools (the only analyst module that reads platform stores).

Every store is opened read-only (SQLite ``mode=ro``) and nothing here calls the network or writes anything. The
modules used are the same ones the terminal API's company, news, macro and market-data routes use, so a tool result
is what the terminal itself shows. The static test (``tests/test_agents_static.py``) allows this one file, and only
it, to import those read-only data modules; it still may not import broker, core, gateway, risk, policy, store, API
or market code.

Data rights come first (ADR-0015, CAP-AGT-08): before a tool reads anything it checks that every dataset the result
would come from permits cloud-model processing (``rights_ok``, the data-use gate's registry check). If one does not,
the tool reads nothing and returns the reason. Items from several sources (a news search) are filtered one by one and
the withheld ones are counted, never shown. The tool gateway checks the same rights again on what a tool returns.

Real data only (ADR-0020): a missing store, issuer, series or row returns ``{"status": "UNAVAILABLE", "reason_code",
"reason"}`` (codes from ``domain/unavailable.py``) that the model must report; nothing is estimated or filled in.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...data.fundamentals.filings import filings as filings_list
from ...data.fundamentals.filings import identity, load_submissions
from ...data.fundamentals.readonly import ReadOnlyResearchStore, research_paths
from ...data.fundamentals.search import search as search_issuers
from ...data.fundamentals.statements import Fundamentals
from ...data.pit import OPEN_END
from ...data.sources import macro_global, treasury
from ...domain import unavailable
from ...domain.clock import ns_to_iso
from ...domain.fixed import Fixed
from ...news import sources as news_sources
from ...news.store import HL_END, HL_START, NewsStore
from ...news.store import store_path as news_store_path
from ...service.paths import Paths
from ..common import REPO
from ..tools import ToolOutput, ToolRegistry, ToolSpec
from .toolspec import PLATFORM_DATASET, TOOL_DEFS

MAX_RESULT_CHARS = 60_000
PORTFOLIO_DATASET = "owner.portfolio"
SUMMARY_CHARS = 600
_CIK_RE = re.compile(r"^(?:CIK)?0*([0-9]{1,10})$")
_FORM_RE = re.compile(r"^[0-9A-Z/-]{1,12}$")
_INSTRUMENT_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,40}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class ToolInputError(ValueError):
    """Arguments that pass the schema but not the tool's own checks (reported to the model as an error result)."""


def _json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def unavailable_output(code: str, reason: str, **extra: Any) -> ToolOutput:
    return ToolOutput(_json({"status": "UNAVAILABLE", "reason_code": code, "reason": reason, **extra}), PLATFORM_DATASET)


def _cik(raw: Any) -> int:
    m = _CIK_RE.match(str(raw).strip().upper())
    if not m or int(m.group(1)) == 0:
        raise ToolInputError("cik must be the issuer's SEC CIK in digits (use company_search to find it)")
    return int(m.group(1))


def _forms(raw: Any) -> list[str] | None:
    if not raw:
        return None
    out = [str(f).strip().upper() for f in raw]
    bad = [f for f in out if not _FORM_RE.match(f)]
    if bad:
        raise ToolInputError(f"unknown form names {bad}; use SEC form types such as 10-K, 10-Q, 8-K")
    return out


@dataclass
class PlatformData:
    """Tool handlers over one ``UNKNOWN_HOME``. ``rights_ok(dataset_id)`` is the data-use gate's registry check."""
    root: Path
    rights_ok: Callable[[str], bool]
    clock_ns: Callable[[], int]
    account: str | None = None
    repo: Path = REPO
    withheld: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ plumbing
    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        for d in TOOL_DEFS:
            reg.register(ToolSpec(d["name"], d["description"], d["input_schema"], self._guarded(getattr(self, d["name"]))))
        return reg

    def _guarded(self, fn: Callable[[dict], ToolOutput]) -> Callable[[dict], ToolOutput]:
        def run(args: dict) -> ToolOutput:
            try:
                out = fn(args)
            except ToolInputError as exc:
                return ToolOutput(_json({"status": "ERROR", "reason_code": "INVALID_ARGUMENTS", "reason": str(exc)}),
                                  PLATFORM_DATASET)
            except (sqlite3.Error, OSError) as exc:
                return unavailable_output(unavailable.ERROR, f"store read failed: {type(exc).__name__}: {exc}")
            if len(out.text) > MAX_RESULT_CHARS:
                return unavailable_output(unavailable.ERROR, f"too large: the result is {len(out.text)} characters, above the "
                                          f"{MAX_RESULT_CHARS} limit; ask for fewer periods or items")
            return out
        return run

    def _denied(self, datasets: list[str] | tuple[str, ...]) -> list[str]:
        return [d for d in datasets if not self.rights_ok(d)]

    def _not_permitted(self, denied: list[str], what: str) -> ToolOutput:
        self.withheld.extend(denied)
        return unavailable_output(
            unavailable.NOT_ENTITLED,
            f"{what} cannot be given to the model: the data registry does not grant cloud-model processing for "
            f"{', '.join(sorted(set(denied)))} (ADR-0015 data rights). The terminal still shows it to the owner.",
            datasets=sorted(set(denied)))

    def _research(self) -> ReadOnlyResearchStore | None:
        db, raw = research_paths(self.root)
        try:
            return ReadOnlyResearchStore(db, raw)
        except FileNotFoundError:
            return None

    @staticmethod
    def _output(payload: dict, datasets: list[str] | tuple[str, ...]) -> ToolOutput:
        ds = sorted(set(datasets))
        return ToolOutput(_json(payload), ds[0], tuple(ds[1:])) if ds else ToolOutput(_json(payload), PLATFORM_DATASET)

    # ------------------------------------------------------------------ company
    def company_search(self, args: dict) -> ToolOutput:
        store = self._research()
        try:
            res = search_issuers(args["query"], as_of_ns=self.clock_ns(), store=store, limit=int(args.get("limit", 5)))
        except ValueError as exc:
            raise ToolInputError(str(exc)) from None
        finally:
            if store is not None:
                store.close()
        shown, datasets, held = [], [], []
        for c in res.get("candidates", []):
            ds = sorted({x.get("source") for x in c.get("listings", []) if x.get("source")}
                        or ({"sec.submissions"} if c.get("cik") else {f"unregistered.{c.get('kind')}"}))
            denied = self._denied(ds)
            if denied:
                held.extend(denied)
                continue
            datasets.extend(ds)
            shown.append({"kind": c.get("kind"), "cik": c.get("cik"), "entity_id": c.get("entity_id"), "name": c.get("name"),
                          "match": c.get("match"), "ingested": c.get("ingested"), "sic": c.get("sic"),
                          "listings": [{"ticker": x.get("ticker"), "exchange": x.get("exchange"), "source": x.get("source")}
                                       for x in c.get("listings", [])]})
        self.withheld.extend(held)
        payload = {"query": res.get("query"), "as_of": res.get("as_of"), "candidates": shown,
                   "withheld_candidates": len(res.get("candidates", [])) - len(shown),
                   "withheld_datasets": sorted(set(held))}
        if not shown:
            payload.update(status="UNAVAILABLE", reason_code=unavailable.NOT_FOUND,
                           reason="no issuer matching the query is held with model-processing rights")
        return self._output(payload, datasets)

    def company_filings(self, args: dict) -> ToolOutput:
        cik = _cik(args["cik"])
        denied = self._denied(["sec.submissions"])
        if denied:
            return self._not_permitted(denied, "The filings list")
        store = self._research()
        if store is None:
            return unavailable_output(unavailable.NO_SOURCE, "the research store is not initialised (no SEC data ingested)")
        try:
            subs, meta = load_submissions(store, f"SEC-CIK-{cik:010d}")
        finally:
            store.close()
        if subs is None:
            code = unavailable.NOT_FOUND if meta.get("status") == "NOT_INGESTED" else unavailable.NO_DATA
            return unavailable_output(code, meta.get("reason", "no stored SEC submissions payload"), cik=cik)
        now = self.clock_ns()
        forms = _forms(args.get("forms"))
        listed = filings_list(subs, now, forms=set(forms) if forms else None, limit=int(args.get("limit", 20)))
        ident = identity(subs, meta, now)
        payload = {"issuer": {k: ident.get(k) for k in ("cik", "entity_id", "name", "fiscal_year_end", "sic_description")},
                   "tickers": [x["ticker"] for x in ident.get("listings", [])], "as_of": listed["as_of"],
                   "filings": [{k: f.get(k) for k in ("form", "accession", "filing_date", "report_date", "acceptance_time_raw",
                                                      "items", "flags", "primary_document_description", "links")}
                               for f in listed["filings"]],
                   "count_matching": listed["count"], "coverage_note": listed["coverage_note"],
                   "source": {"dataset": "sec.submissions", "retrieved_at": meta.get("retrieved_at"), "url": meta.get("source_url")},
                   "full_text": "not stored: the platform holds the filing index and XBRL facts, not the documents' text"}
        return self._output(payload, ["sec.submissions"])

    def company_financials(self, args: dict) -> ToolOutput:
        cik = _cik(args["cik"])
        denied = self._denied(["sec.companyfacts"])
        if denied:
            return self._not_permitted(denied, "The financial statements")
        store = self._research()
        if store is None:
            return unavailable_output(unavailable.NO_SOURCE, "the research store is not initialised (no SEC data ingested)")
        entity = f"SEC-CIK-{cik:010d}"
        try:
            if store.conn.execute("SELECT 1 FROM entity WHERE entity_id=?", (entity,)).fetchone() is None:
                return unavailable_output(unavailable.NOT_FOUND, f"{entity} is not in the research store", cik=cik)
            st = Fundamentals(store, entity, self.clock_ns()).statement(args.get("basis", "Q"), int(args.get("periods", 2)),
                                                                        history=False)
        finally:
            store.close()
        lines = []
        for section, rows in (st.get("statements") or {}).items():
            for line in rows:
                values = []
                for cell in line.get("cells", []):
                    src = (cell.get("sources") or [{}])[0]
                    v = {"period": (cell.get("period") or {}).get("label")}
                    if cell.get("value") is None:
                        v["missing"] = cell.get("missing_reason") or "NOT_REPORTED"
                    else:
                        v.update(value=cell["value"], method=cell.get("method"), concept=cell.get("concept"),
                                 form=src.get("form"), accession=src.get("accession"), available=src.get("available_time"))
                    values.append(v)
                lines.append({"section": section, "label": line.get("label"), "unit": line.get("unit"), "values": values})
        payload = {"entity_id": entity, "as_of": st.get("as_of"), "basis": st.get("basis"), "currency": st.get("currency"),
                   "mapping": st.get("mapping"),
                   "periods": [{"label": c.get("label"), "start": c.get("start"), "end": c.get("end"), "flags": c.get("flags")}
                               for c in st.get("columns", [])],
                   "lines": lines, "source": {"dataset": "sec.companyfacts", "note": "values are as reported in XBRL, unscaled"}}
        if not payload["periods"]:
            payload.update(status="UNAVAILABLE", reason_code=unavailable.NO_DATA,
                           reason="no reported periods for this basis in the research store")
        return self._output(payload, ["sec.companyfacts"])

    # ------------------------------------------------------------------ news and filings feed
    def _news(self) -> NewsStore | None:
        path = news_store_path(self.root)
        if not path.exists():
            return None
        return NewsStore(path, readonly=True)

    @staticmethod
    def _dataset_of(source_id: str) -> str:
        try:
            return news_sources.by_id(source_id).dataset_id
        except (KeyError, LookupError, ValueError):
            return f"news.unregistered.{source_id}"

    @staticmethod
    def _clean(text: str | None, n: int = SUMMARY_CHARS) -> str | None:
        if text is None:
            return None
        t = text.replace(HL_START, "").replace(HL_END, "")
        return t if len(t) <= n else t[:n] + "…"

    def _item_view(self, it: dict, dataset: str) -> dict:
        return {"id": it["id"], "source_id": it["source_id"], "dataset": dataset, "kind": it["kind"], "title": it["title"],
                "form": it.get("form"), "accession": it.get("accession"), "items": it.get("items"),
                "filed_date": it.get("filed_date"), "published": it.get("published"), "first_seen": it.get("first_seen"),
                "link": it.get("link"), "snippet": self._clean(it.get("snippet")), "summary": self._clean(it.get("summary"))}

    def news_search(self, args: dict) -> ToolOutput:
        store = self._news()
        if store is None:
            return unavailable_output(unavailable.NO_SOURCE, "no news store yet: the news poller has not run")
        try:
            rows = store.query(q=args.get("query"), ciks={_cik(args["cik"])} if args.get("cik") else None,
                               form=_forms(args.get("forms")), limit=int(args.get("limit", 10)), include_duplicates=False)
        except ValueError as exc:
            raise ToolInputError(str(exc)) from None
        finally:
            store.close()
        shown, datasets, held = [], [], []
        for it in rows:
            ds = self._dataset_of(it["source_id"])
            if self._denied([ds]):
                held.append(ds)
                continue
            datasets.append(ds)
            shown.append(self._item_view(it, ds))
        self.withheld.extend(held)
        payload = {"as_of": ns_to_iso(self.clock_ns()), "items": shown, "withheld_items": len(held),
                   "withheld_datasets": sorted(set(held))}
        if not shown:
            payload.update(status="UNAVAILABLE", reason_code=unavailable.NO_DATA,
                           reason="no stored item matches" + (" with model-processing rights" if held else ""))
        return self._output(payload, datasets)

    def news_item(self, args: dict) -> ToolOutput:
        store = self._news()
        if store is None:
            return unavailable_output(unavailable.NO_SOURCE, "no news store yet: the news poller has not run")
        try:
            it = store.item(int(args["item_id"]))
        finally:
            store.close()
        if it is None:
            return unavailable_output(unavailable.NOT_FOUND, f"no news item {args['item_id']}")
        ds = self._dataset_of(it["source_id"])
        denied = self._denied([ds])
        if denied:
            return self._not_permitted(denied, f"News item {it['id']}")
        view = self._item_view(it, ds)
        view["summary"] = self._clean(it.get("summary"), 4000)
        view["summary_truncated_by_source"] = it.get("summary_truncated")
        view["copies"] = [{"id": c["id"], "source_id": c["source_id"], "title": c["title"]} for c in it.get("copies", [])]
        view["fetch"] = it.get("fetch")
        view["issuers"] = [{"cik": e.get("value"), "name": e.get("name"), "role": e.get("role"), "tickers": e.get("tickers")}
                           for e in it.get("entities", []) if e.get("scheme") == "SEC_CIK"]
        return self._output(view, [ds])

    # ------------------------------------------------------------------ market data
    def market_snapshot(self, args: dict) -> ToolOutput:
        if not _INSTRUMENT_RE.match(args["instrument"]):
            raise ToolInputError("instrument must be a symbol key such as AAPL.XNAS or a core instrument id")
        return unavailable_output(
            unavailable.NOT_CONNECTED,
            "live quotes are held in memory by the core and API processes' market-data bus; the analyst worker has no "
            "market-data connection and no quote store it may read, so no quote can be given",
            instrument=args["instrument"])

    def market_bars(self, args: dict) -> ToolOutput:
        instrument = args["instrument"]
        if not _INSTRUMENT_RE.match(instrument):
            raise ToolInputError("instrument must be a symbol key such as AAPL.XNAS or a core instrument id")
        db = research_paths(self.root)[0]
        if not db.exists():
            return unavailable_output(unavailable.NO_SOURCE, "the research store is not initialised")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mkt_bar'").fetchone() is None:
                return unavailable_output(unavailable.NO_DATA, "no historical bars are stored on this machine (the bars "
                                          "table does not exist; nothing has been ingested)", instrument=instrument)
            sql = ("SELECT dataset_id FROM mkt_bar WHERE instrument_id=? AND fixture=0 AND known_to_ns=? "
                   "AND available_time_ns<=?")
            params: list = [instrument, OPEN_END, self.clock_ns()]
            if args.get("bar_size"):
                sql += " AND bar_size=?"
                params.append(args["bar_size"])
            datasets = sorted({r[0] for r in conn.execute(sql + " GROUP BY dataset_id", params)})
            if not datasets:
                return unavailable_output(unavailable.NO_DATA, f"no stored bars for {instrument}", instrument=instrument)
            denied = self._denied(datasets)
            if denied:
                return self._not_permitted(denied, f"Bars for {instrument}")
            rows = conn.execute(sql.replace("SELECT dataset_id", "SELECT dataset_id, bar_size, what_to_show, bar_start_ns, "
                                            "bar_date, open, high, low, close, volume, currency, adjustment")
                                + " ORDER BY bar_start_ns DESC LIMIT ?", [*params, int(args.get("limit", 60))]).fetchall()
        finally:
            conn.close()
        bars = [{"start": ns_to_iso(r["bar_start_ns"]), "date": r["bar_date"], "bar_size": r["bar_size"],
                 "what": r["what_to_show"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                 "volume": r["volume"], "currency": r["currency"], "adjustment": r["adjustment"], "dataset": r["dataset_id"]}
                for r in reversed(rows)]
        return self._output({"instrument": instrument, "bars": bars}, datasets)

    # ------------------------------------------------------------------ macro
    def macro_overview(self, args: dict) -> ToolOutput:
        datasets = sorted(macro_global.INGEST)
        denied = self._denied(datasets)
        if denied:
            return self._not_permitted(denied, "The macro comparison")
        store = self._research()
        if store is None:
            return unavailable_output(unavailable.NO_SOURCE, "the research store is not initialised (no macro series ingested)")
        try:
            out = macro_global.comparison(store, self.clock_ns(), country_codes=args.get("countries"))
        finally:
            store.close()
        return self._output(out, datasets)

    def us_yield_curve(self, args: dict) -> ToolOutput:
        if args.get("date") and not _DATE_RE.match(args["date"]):
            raise ToolInputError("date must be YYYY-MM-DD")
        denied = self._denied([treasury.DATASET])
        if denied:
            return self._not_permitted(denied, "The Treasury yield curve")
        store = self._research()
        if store is None:
            return unavailable_output(unavailable.NO_SOURCE, "the research store is not initialised")
        try:
            out = treasury.curve_as_of(store, self.clock_ns(), args.get("date"))
        finally:
            store.close()
        return self._output(out, [treasury.DATASET])

    # ------------------------------------------------------------------ portfolio (read-only)
    def portfolio_positions(self, args: dict) -> ToolOutput:
        denied = self._denied([PORTFOLIO_DATASET])
        if denied:
            return self._not_permitted(denied, "The owner's positions and balances")
        if not self.account:
            return unavailable_output(unavailable.NOT_APPLICABLE, "the request named no account")
        db = Paths(self.root).db(self.account)
        if not db.exists():
            return unavailable_output(unavailable.NO_SOURCE, "no core database for the account")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            agg: dict[tuple[str, str], dict] = {}
            for r in conn.execute("SELECT instrument_id, owner, row FROM lots WHERE account_key=?", (self.account,)):
                lot = json.loads(r["row"])
                key = (r["owner"], r["instrument_id"])
                a = agg.setdefault(key, {"owner": r["owner"], "instrument_id": r["instrument_id"],
                                         "currency": lot.get("currency"), "open_lots": []})
                if not Fixed.parse(str(lot.get("open_qty", "0"))).is_zero():
                    a["open_lots"].append({"quantity": lot.get("open_qty"), "cost": lot.get("open_cost")})
            cash = [dict(r) for r in conn.execute("SELECT account_code, owner, currency, amount FROM balances "
                                                  "WHERE account_key=? AND instrument_id=''", (self.account,))]
        finally:
            conn.close()
        positions = [a for a in agg.values() if a["open_lots"]]
        return self._output({"as_of": ns_to_iso(self.clock_ns()), "positions": positions, "balances": cash,
                             "note": "quantities and costs as booked by the core ledger; no marks (quotes are not available "
                                     "to the analyst)"}, [PORTFOLIO_DATASET])

    # ------------------------------------------------------------------ platform metadata
    def data_capabilities(self, args: dict) -> ToolOutput:
        reg = json.loads((self.repo / "docs" / "capabilities" / "register.json").read_text())
        q = (args.get("search") or "").lower()
        hits = []
        for r in reg.get("capabilities", []):
            text = f"{r.get('id')} {r.get('title')} {r.get('area')}".lower()
            if q and q not in text:
                continue
            impl = r.get("implementation") or {}
            hits.append({"id": r.get("id"), "title": r.get("title"), "status": impl.get("status"),
                         "gaps": (impl.get("gaps") or [])[:3]})
            if len(hits) >= 15:
                break
        return ToolOutput(_json({"capabilities": hits, "reason_codes": unavailable.REASONS,
                                 "source": "docs/capabilities/register.json"}), PLATFORM_DATASET)
