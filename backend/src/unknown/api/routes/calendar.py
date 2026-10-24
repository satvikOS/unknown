"""Corporate events calendar routes (CAP-NEWS-10, partial). Route module ``calendar``; read-only.

  GET /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&issuers=AAPL,789019,NVDA.XNAS&as_of=&status=occurred,announced,projected
                   &types=results_release,periodic_report,earnings_call

Entries come from SEC filings only: occurred (8-K Item 2.02, 10-Q/10-K accepted), announced (a date the issuer
stated in an 8-K) and projected (estimated internally from the prior year, labelled so). ``issuers`` takes CIKs,
tickers or live keys (``AAPL.XNAS``); empty means every SEC issuer in the research store. Default range: four
weeks back to thirteen weeks ahead of the as-of date, grouped by ISO week (Monday).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ...data.fundamentals.peers import issuers as sec_issuers
from ...data.fundamentals.readonly import ReadOnlyResearchStore, research_paths
from ...data.pit import OPEN_END
from ...data.sources.edgar_events import EVENT_TYPES, calendar, week_start
from ...domain.clock import NS_PER_S
from .company import as_of

STATUSES = {"occurred", "announced", "projected"}
MAX_DAYS = 400


def _resolve(store, token: str) -> str | None:
    t = token.strip().upper()
    if t.removeprefix("CIK").isdigit():
        eid = f"SEC-CIK-{int(t.removeprefix('CIK')):010d}"
        return eid if store.conn.execute("SELECT 1 FROM entity WHERE entity_id=?", (eid,)).fetchone() else None
    sym = t.rsplit(".", 1)[0] if "." in t and len(t.rsplit(".", 1)[1]) == 4 else t
    rows = {r[0] for r in store.conn.execute("SELECT entity_id FROM entity_alias WHERE scheme LIKE 'ticker:%' AND value=? AND valid_to_ns=?",
                                             (sym, OPEN_END))}
    return rows.pop() if len(rows) == 1 else None


def _set(q: dict, key: str, allowed: set[str]) -> set[str] | None:
    if not q.get(key):
        return None
    vals = {v.strip() for v in q[key].split(",") if v.strip()}
    bad = vals - allowed
    if bad:
        raise ValueError(f"{key}: unknown {sorted(bad)}; use {sorted(allowed)}")
    return vals


def handle(ctx, parts, q, account_key, conn):
    db, raw = research_paths(ctx.paths.root)
    try:
        store = ReadOnlyResearchStore(db, raw)
    except FileNotFoundError:
        return {"status": "UNAVAILABLE", "reason": "NO_DATA", "detail": "research store not initialised: ingest issuers first "
                "(python -m unknown.data.sec ingest --cik <CIK>)", "weeks": [], "issuers": []}
    try:
        t = as_of(q)
        today = datetime.fromtimestamp(t // NS_PER_S, tz=timezone.utc).date()
        start = date.fromisoformat(q["from"]) if q.get("from") else week_start(today) - timedelta(days=28)
        end = date.fromisoformat(q["to"]) if q.get("to") else week_start(today) + timedelta(days=13 * 7 - 1)
        if end < start or (end - start).days > MAX_DAYS:
            raise ValueError(f"from..to must be a forward range of at most {MAX_DAYS} days")
        statuses, types = _set(q, "status", STATUSES), _set(q, "types", set(EVENT_TYPES))
        unresolved, chosen = [], []
        if q.get("issuers"):
            for tok in q["issuers"].split(","):
                if not tok.strip():
                    continue
                eid = _resolve(store, tok)
                (chosen.append(eid) if eid else unresolved.append(tok.strip()))
        else:
            chosen = [i["entity_id"] for i in sec_issuers(store)]
        out = calendar(store, [{"entity_id": e} for e in dict.fromkeys(chosen)], start, end, t, statuses=statuses, types=types)
        out["unresolved"] = unresolved
        out["filter"] = {"issuers": q.get("issuers") or None, "status": sorted(statuses) if statuses else None,
                         "types": sorted(types) if types else None}
        return out
    finally:
        store.close()


ROUTES = [(("calendar",), handle)]
