"""Equity screener routes (CAP-MKT-10). Route module ``screener``.

GET (read-only: the research store and the screen store are opened ``mode=ro``):

  GET /api/screener/fields                         field catalog (unit, sources, formula id@version, basis)
  GET /api/screener/screens                        saved screens (latest version each)
  GET /api/screener/screens/<screen_id>            versions and runs of one screen
  GET /api/screener/run?definition=<json>|screen=<id>&as_of=&known_at=   ad hoc run, not stored
  GET /api/screener/runs/<run_id>                  a stored run (results, excluded rows, manifest)
  GET /api/screener/runs/<run_id>/verify           recompute from the manifest and compare result hashes

POST (owner session and CSRF checked by the server; writes only the screen store):

  POST /api/screener/screens   {"name", "definition", "screen_id"?}           saves a new version
  POST /api/screener/runs      {"screen_id"?, "version"?, "definition"?, "as_of"?, "known_at"?}   runs and stores the run

``known_at`` is the knowledge cutoff (default: the run time): only versions this system held then are used.

A run without ``as_of`` is "as of now": live quotes from the market-data bus are used only then, and only
for listings the bus is already streaming (no subscription is opened here). Every price used is recorded
in the run's manifest, so the run can be re-verified.
"""
from __future__ import annotations

import json
import time

from ...data.fundamentals.readonly import ReadOnlyResearchStore, research_paths
from ...screener import engine
from ...screener.fields import catalog
from ...screener.store import ScreenStore, path_for
from .company import parse_instant

NO_STORE = "research store not initialised: ingest issuers first (python -m unknown.data.sec ingest --cik <CIK>)"


def _research(ctx):
    db, raw = research_paths(ctx.paths.root)
    try:
        return ReadOnlyResearchStore(db, raw)
    except FileNotFoundError:
        return None


def _screens(ctx, *, write: bool = False):
    p = path_for(ctx.paths.root)
    if write:
        return ScreenStore(p)
    try:
        return ScreenStore(p, readonly=True)
    except FileNotFoundError:
        return None


def _as_of(text: str | None) -> tuple[int, bool]:
    if text:
        return parse_instant(text, name="as_of"), False
    return time.time_ns(), True


def _live(ctx, store, definition: dict, is_now: bool):
    """Quotes for the universe's listing keys from the bus, if this API process already runs one; None otherwise."""
    bus = getattr(ctx, "marketdata", None)
    if not is_now:
        return None, None
    if bus is None:
        return None, {}
    keys = [k for k in (engine.live_key(i) for i in engine.universe(store, engine.compile_definition(definition)["universe"])) if k]
    if not keys:
        return {}, {}
    snap = bus.snapshot(keys)
    quotes = {q["key"]: q for q in snap.get("quotes", [])}
    status = {s["key"]: f"{s.get('state')}: {s.get('reason')}" for s in snap.get("status", [])}
    return quotes, status


def _execute(ctx, definition: dict, as_of_text: str | None, known_at_text: str | None = None) -> dict:
    store = _research(ctx)
    if store is None:
        return {"status": "UNAVAILABLE", "reason": "NO_DATA", "detail": NO_STORE}
    try:
        t, is_now = _as_of(as_of_text)
        quotes, status = _live(ctx, store, definition, is_now)
        cutoff = parse_instant(known_at_text, name="known_at") if known_at_text else None
        out = engine.run(store, definition, t, now_ns=time.time_ns(), knowledge_cutoff_ns=cutoff, live_quotes=quotes, live_status=status)
        out["live_quotes"] = ("used where streaming" if quotes is not None else
                              "not used: run with an as-of time" if not is_now else "not used: no market-data session in this API process")
        out["manifest"]["live_quotes"] = out["live_quotes"]
        return out
    finally:
        store.close()


def _definition_param(ctx, q: dict) -> tuple[dict, str | None, int | None]:
    if q.get("screen"):
        ss = _screens(ctx)
        if ss is None:
            raise LookupError("no saved screens yet")
        try:
            s = ss.get(q["screen"], int(q["version"]) if q.get("version") else None)
        finally:
            ss.close()
        return s["definition"], s["screen_id"], s["version"]
    if not q.get("definition"):
        raise ValueError("pass definition=<json> or screen=<screen_id>")
    try:
        return json.loads(q["definition"]), None, None
    except json.JSONDecodeError as exc:
        raise ValueError(f"definition is not valid JSON: {exc}") from None


def handle(ctx, parts, q, account_key, conn):
    sub = parts[1] if len(parts) > 1 else "fields"
    try:
        if sub == "fields":
            return {"fields": catalog(), "operators": sorted(engine.OPS), "treatments": sorted(engine.TREATMENTS),
                    "units": {"ratio": "fraction (0.25 = 25 %)", "x": "multiple", "money": "reporting currency (criterion names it)",
                              "price": "price currency"}}
        if sub == "run" and len(parts) == 2:
            definition, _, _ = _definition_param(ctx, q)
            return _execute(ctx, definition, q.get("as_of"), q.get("known_at"))
        if sub == "screens":
            ss = _screens(ctx)
            if ss is None:
                return {"screens": [], "count": 0}
            try:
                if len(parts) == 2:
                    rows = ss.screens()
                    return {"screens": rows, "count": len(rows)}
                return ss.get(parts[2], int(q["version"]) if q.get("version") else None)
            finally:
                ss.close()
        if sub == "runs" and len(parts) >= 3:
            ss = _screens(ctx)
            if ss is None:
                raise LookupError(f"run {parts[2]} not found")
            try:
                stored = ss.run(parts[2])
            finally:
                ss.close()
            if len(parts) == 4 and parts[3] == "verify":
                store = _research(ctx)
                if store is None:
                    return {"status": "UNAVAILABLE", "reason": "NO_DATA", "detail": NO_STORE}
                try:
                    return {"run_id": parts[2], **engine.rerun(store, stored, now_ns=time.time_ns())}
                finally:
                    store.close()
            return stored
    except engine.ScreenRefused as exc:
        raise ValueError(str(exc)) from None
    raise LookupError("unknown screener path")


def post_screens(ctx, parts, body: dict, session) -> dict:
    if len(parts) != 2:
        raise LookupError("POST /api/screener/screens")
    ss = _screens(ctx, write=True)
    try:
        return ss.save(body.get("name", ""), body.get("definition") or {}, time.time_ns(), body.get("screen_id") or None)
    except engine.ScreenRefused as exc:
        raise ValueError(str(exc)) from None
    finally:
        ss.close()


def post_runs(ctx, parts, body: dict, session) -> dict:
    if len(parts) != 2:
        raise LookupError("POST /api/screener/runs")
    screen_id, version = body.get("screen_id"), body.get("version")
    if screen_id:
        ss = _screens(ctx)
        if ss is None:
            raise LookupError(f"screen {screen_id} not found")
        try:
            s = ss.get(screen_id, int(version) if version else None)
        finally:
            ss.close()
        definition, version = s["definition"], s["version"]
    else:
        definition = body.get("definition")
        if not isinstance(definition, dict):
            raise ValueError("pass screen_id or definition")
    try:
        result = _execute(ctx, definition, body.get("as_of"), body.get("known_at"))
    except engine.ScreenRefused as exc:
        raise ValueError(str(exc)) from None
    if result.get("status") == "UNAVAILABLE":
        return result
    ss = _screens(ctx, write=True)
    try:
        run_id = ss.record_run(result, time.time_ns(), screen_id, version)
    finally:
        ss.close()
    return {"run_id": run_id, "screen_id": screen_id, "version": version, **result}


ROUTES = [(("screener",), handle)]
POST_ROUTES = [(("screener", "screens"), post_screens), (("screener", "runs"), post_runs)]
