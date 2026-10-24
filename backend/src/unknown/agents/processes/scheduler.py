"""The AI process scheduler, stepped by the model worker after its research jobs (one worker per home).

Each ``step``:
  1. reads ``ai_processes.json`` (an invalid file turns every process off and is audited once per change);
  2. queues succeeded, non-abstained research proposals in the approval queue (no model call) and expires
     overdue proposals;
  3. queues the morning brief when due: a trading day of the configured calendar, at or after the configured
     local time and before the session closes, once per day (trigger key ``brief:<date>``);
  4. queues one summary run per new 8-K/10-Q/10-K or regulator item tagged with a watched issuer, as the news
     poller stores it (from the first item seen after the process was first switched on);
  5. runs at most one queued run (owner requests first, in order), so research jobs are never starved.

A run of a process that is switched off ends FAILED ``process_disabled`` without any call. A RUNNING run left by
a dead worker is ended FAILED ``worker_restarted`` at start, and its possibly-sent reservations stay UNCONFIRMED
(the research-job recovery rule).
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ...data.registry import load_registry
from ...domain.calendar import BusinessCalendar, CalendarHorizonError, load_calendar
from .config import CALENDARS, ProcessConfigError, ProcessesConfig, default_config_path, load_processes_config
from .engine import Evidence, add_items, add_moves, cloud_permission, note_body, note_status, run_model
from .sources import (NewsItem, SourceUnavailable, daily_moves, iso, max_item_id, news_items, news_path, research_path,
                      resolve_tickers)
from .store import ProcessStore, ProcessStoreError, default_store_path
from .tasks import brief_task, explain_task, summary_task

log = logging.getLogger("unknown.agents.processes")

NS = 1_000_000_000
AUDIT_SCOPE = "ai-processes"
SUMMARY_CURSOR = "summaries:last_item_id"
MACRO_DATASETS = ("fred.series", "ecb.exr_reference", "oecd.sdmx_macro", "ust.par_yield_curve")


def _ns(dt: datetime) -> int:
    return int(dt.timestamp()) * NS


class ProcessScheduler:
    def __init__(self, worker, *, calendars_dir: Path = CALENDARS):
        self.w = worker
        root = worker.paths.root
        self.config_path = default_config_path(root)
        self.store = ProcessStore(default_store_path(root), clock_ns=worker.clock.now_ns)
        self.news_db = news_path(root)
        self.research_db = research_path(root)
        self.calendars_dir = Path(calendars_dir)
        self._calendars: dict[str, BusinessCalendar] = {}
        self._seen_problem: dict[str, str] = {}
        self._unfinished: dict[str, dict] = {}

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ helpers
    def _now(self) -> int:
        return self.w.clock.now_ns()

    def _once(self, key: str, message: str, record: Mapping[str, Any]) -> None:
        """Audit and log a recurring problem once per change of its message."""
        if self._seen_problem.get(key) == message:
            return
        self._seen_problem[key] = message
        log.warning("%s", message)
        self.w._audit(AUDIT_SCOPE, dict(record))

    def _registry(self):
        return self.w._registry if self.w._registry is not None else load_registry()

    def config(self) -> ProcessesConfig | None:
        try:
            cfg = load_processes_config(self.config_path)
        except ProcessConfigError as exc:
            self._once("config", f"AI processes are off: {exc}", {"kind": "processes_config_invalid", "error": str(exc)})
            return None
        self._seen_problem.pop("config", None)
        return cfg

    def _calendar(self, cal_id: str) -> BusinessCalendar:
        if cal_id not in self._calendars:
            self._calendars[cal_id] = load_calendar(self.calendars_dir / f"{cal_id}.json")
        return self._calendars[cal_id]

    # ------------------------------------------------------------------ recovery
    def recover(self) -> list[dict]:
        policy, _ = self.w._load_policy()
        ledger = self.w._ledger_for(policy)
        out = []
        for run in self.store.running():
            rid = run["run_id"]
            marked, released = self.w._close_open_rows(
                ledger, ledger.reservations(rid), "worker restarted while this process call may have been in flight; "
                                                  "outcome unknown, kept reserved until reconciled")
            reason = "worker restarted during the run" + ("; possibly-sent spend kept as UNCONFIRMED" if marked else "")
            self.store.finish(rid, state="FAILED", outcome="worker_restarted", reason=reason)
            self.w._audit(rid, {"kind": "recovered", "action": "failed", "reason": reason, "marked_unconfirmed": marked,
                                "released_probes": released, "previous_worker": run.get("worker_id")})
            out.append({"run_id": rid, "action": "failed", "marked_unconfirmed": marked})
        return out

    # ------------------------------------------------------------------ one cycle
    def step(self) -> bool:
        for rid, end in list(self._unfinished.items()):
            self._store_end(rid, end)
        cfg = self.config()
        if cfg is not None:
            try:
                self.queue_research_proposals(cfg)
            except Exception:   # bookkeeping never blocks the processes or the research jobs
                log.exception("queueing research proposals for approval failed")
        try:
            self.store.expire_due()
        except ProcessStoreError:
            log.exception("expiring approvals failed")
        if cfg is None:
            return False
        now = self._now()
        self.schedule_brief(cfg, now)
        self.collect_summaries(cfg)
        run = self._next_run(cfg)
        if run is None:
            return False
        self.execute(run, cfg)
        return True

    def _next_run(self, cfg: ProcessesConfig) -> dict | None:
        for run in self.store.queued():
            if cfg.get(run["process"]).enabled:
                return run
            self.store.finish(run["run_id"], state="FAILED", outcome="process_disabled",
                              reason=f"the {run['process']} process is switched off in {self.config_path}")
            self.w._audit(run["run_id"], {"kind": "process_refused", "reason": "process_disabled"})
        return None

    # ------------------------------------------------------------------ triggers
    def schedule_brief(self, cfg: ProcessesConfig, now: int) -> dict | None:
        s = cfg.get("brief")
        if not s.enabled:
            return None
        cal_id = s.options["calendar"]
        try:
            cal = self._calendar(cal_id)
            tz = ZoneInfo(cal.timezone or "UTC")
            local = datetime.fromtimestamp(now // NS, tz=timezone.utc).astimezone(tz)
            day = local.date()
            if not cal.is_trading_day(day):
                return None
            hh, mm = (int(x) for x in s.options["time"].split(":"))
            if local.time() < dtime(hh, mm):
                return None
            session = cal.session_utc(day)
            if session is not None and now >= _ns(session[1]):
                return None             # the session has closed: a morning brief is no longer useful today
            if self.store.run_for(f"brief:{day.isoformat()}") is not None:
                return None
            prev = day - timedelta(days=1)
            for _ in range(10):
                if cal.is_trading_day(prev):
                    break
                prev -= timedelta(days=1)
            prev_session = cal.session_utc(prev)
            from_ns = _ns(prev_session[1]) if prev_session is not None else now - 24 * 3600 * NS
        except (CalendarHorizonError, OSError, ValueError) as exc:
            self._once("brief-calendar", f"morning brief not scheduled: calendar {cal_id}: {exc}",
                       {"kind": "brief_not_scheduled", "calendar": cal_id, "error": str(exc)})
            return None
        run, created = self.store.enqueue("brief", trigger="schedule", trigger_key=f"brief:{day.isoformat()}",
                                          request={"date": day.isoformat(), "calendar": cal_id, "from_ns": from_ns})
        if created:
            self.w._audit(run["run_id"], {"kind": "process_queued", "process": "brief", "trigger": "schedule",
                                          "date": day.isoformat()})
        return run

    def request_brief(self, *, requested_by: str = "owner") -> dict:
        """An on-demand brief (worker CLI): the window is the last 24 hours."""
        now = self._now()
        day = datetime.fromtimestamp(now // NS, tz=timezone.utc).date().isoformat()
        run, _ = self.store.enqueue("brief", trigger="request", trigger_key=f"brief:request:{now}",
                                    request={"date": day, "calendar": None, "from_ns": now - 24 * 3600 * NS},
                                    requested_by=requested_by)
        return run

    def _watched(self, cfg: ProcessesConfig) -> tuple[dict[str, dict], list[dict]]:
        """symbol -> {cik, name, instrument_id, held} from the watchlist and the latest holdings snapshot."""
        snap = self.store.latest_subjects() if cfg.include_holdings else None
        entries = {e["symbol"]: e for e in (snap or {}).get("entries", []) if e.get("symbol")}
        symbols = list(cfg.watchlist) + [s for s, e in entries.items() if e.get("held") and s not in cfg.watchlist]
        resolved, problem = resolve_tickers(self.research_db, symbols)
        unavailable = []
        if cfg.include_holdings and snap is None:
            unavailable.append({"what": "holdings", "reason": "no holdings snapshot yet: the terminal API records one when the "
                                                               "owner opens the AI workspace"})
        out = {}
        for sym in symbols:
            ref = resolved.get(sym)
            if ref is None:
                unavailable.append({"what": f"issuer for {sym}", "reason": problem or "the SEC ticker reference does not list "
                                                                                   "this ticker"})
            e = entries.get(sym, {})
            out[sym] = {"cik": ref["cik"] if ref else None, "name": ref["name"] if ref else None,
                        "instrument_id": e.get("instrument_id"), "held": bool(e.get("held"))}
        return out, unavailable

    def collect_summaries(self, cfg: ProcessesConfig) -> int:
        s = cfg.get("summaries")
        if not s.enabled:
            return 0
        cursor = self.store.cursor(SUMMARY_CURSOR)
        if cursor is None:
            top = max_item_id(self.news_db)
            if top is None:
                return 0
            self.store.set_cursor(SUMMARY_CURSOR, str(top))    # only items stored after the process was switched on
            self.w._audit(AUDIT_SCOPE, {"kind": "summaries_cursor_started", "after_item_id": top})
            return 0
        watched, _ = self._watched(cfg)
        by_cik = {v["cik"]: sym for sym, v in watched.items() if v["cik"]}
        try:
            items = news_items(self.news_db, after_id=int(cursor), limit=500)
        except SourceUnavailable as exc:
            self._once("summaries-news", f"summaries paused: {exc}", {"kind": "summaries_paused", "error": str(exc)})
            return 0
        forms = set(s.options["forms"])
        queued, last = 0, int(cursor)
        for it in items:
            if queued >= s.options["max_per_cycle"]:
                break
            last = it.item_id
            hit = [c for c in it.ciks if c in by_cik]
            relevant = hit and ((it.kind == "filing" and (it.form or "").removesuffix("/A") in forms)
                                or (it.kind == "regulator" and s.options["regulator"]))
            if not relevant:
                continue
            run, created = self.store.enqueue("summaries", trigger="event", trigger_key=f"summary:news:{it.item_id}",
                                              request={"item_id": it.item_id, "symbol": by_cik[hit[0]], "cik": hit[0]})
            if created:
                queued += 1
                self.w._audit(run["run_id"], {"kind": "process_queued", "process": "summaries", "trigger": "event",
                                              "item_id": it.item_id})
        if last != int(cursor):
            self.store.set_cursor(SUMMARY_CURSOR, str(last))
        return queued

    def queue_research_proposals(self, cfg: ProcessesConfig) -> int:
        """A research job's non-abstained proposal needs the owner's review: queue it with its content hash."""
        if not cfg.queue_research_proposals:
            return 0
        n = 0
        now = self._now()
        for j in self.w.jobs.list(state="SUCCEEDED", limit=50):
            if j.get("abstain") not in (0, False) or self.store.has_source("research_job", j["job_id"]):
                continue
            job = self.w.jobs.get(j["job_id"])
            proposal = (job or {}).get("proposal")
            if not proposal or proposal.get("abstain"):
                continue
            question = (job["inputs"].get("request") or {}).get("question") or ""
            content = {"kind": "research_proposal", "job_id": job["job_id"], "question": question, "proposal": proposal,
                       "core_proposal_id": ((job.get("core_result") or {}).get("body") or {}).get("proposal_id"),
                       "effect_of_approval": "records the owner's review of this exact content only; nothing is executed"}
            row, how = self.store.propose(kind="research_proposal", source="research_job", source_id=job["job_id"],
                                          title=f"Research proposal: {question[:200]}", content=content,
                                          expires_ns=now + cfg.approval_expiry_hours * 3600 * NS)
            self.w._audit(row["approval_id"], {"kind": "approval_queued", "how": how, "source": "research_job",
                                               "source_id": job["job_id"], "content_hash": row["content_hash"]})
            n += how == "queued"
        return n

    # ------------------------------------------------------------------ one run
    def execute(self, run: dict, cfg: ProcessesConfig) -> dict:
        rid = run["run_id"]
        run = self.store.claim(rid, self.w.worker_id)
        self.w._audit(rid, {"kind": "process_run_started", "process": run["process"], "trigger": run["trigger"],
                            "trigger_key": run["trigger_key"], "request": run["request"]})
        handler: Callable[[dict, ProcessesConfig], dict] = {"brief": self._brief, "summaries": self._summary,
                                                             "explainer": self._explain}[run["process"]]
        try:
            end = handler(run, cfg)
        except Exception as exc:
            log.exception("process run %s failed inside the worker", rid)
            marked = self.w._unconfirm_open(rid, f"worker error during a process run ({type(exc).__name__}); outcome unknown")
            end = {"state": "FAILED", "outcome": "worker_error", "reason": f"{type(exc).__name__}: {exc}"[:2000], "note": None,
                   "stats": {}, "marked_unconfirmed": marked}
        self._store_end(rid, end)
        return end

    def _store_end(self, rid: str, end: dict) -> None:
        try:
            done = self.store.finish(rid, state=end["state"], outcome=end["outcome"], reason=end["reason"], note=end["note"],
                                     stats=end["stats"])
        except ProcessStoreError as exc:
            log.error("process run %s could not be ended: %s", rid, exc)
            self._unfinished.pop(rid, None)
            return
        except Exception:
            log.exception("could not record the end of process run %s; kept in memory and retried", rid)
            self._unfinished[rid] = end
            return
        self._unfinished.pop(rid, None)
        self.w._audit(rid, {"kind": "process_run_finished", "state": done["state"], "outcome": done["outcome"],
                            "reason": done["reason"], "note_id": done["note_id"], "cost": done["cost"],
                            "prompt_tokens": done["prompt_tokens"], "output_tokens": done["output_tokens"]})
        log.info("process run %s (%s) finished: %s %s", rid, done["process"], done["outcome"], done["reason"])

    @staticmethod
    def _end(title: str, process: str, ev: Evidence, mo, window, now: int, extra: Mapping[str, Any] | None = None) -> dict:
        status = note_status(ev, mo)
        body = note_body(process=process, title=title, ev=ev, mo=mo, window=window, now_ns=now, extra=extra)
        reason = mo.reason if mo is not None else ("no items in the window" if not ev.items else "no model call needed")
        stats = {"model_id": ",".join(mo.model_ids) if mo and mo.model_ids else None,
                 "prompt_tokens": mo.prompt_tokens if mo else None, "output_tokens": mo.output_tokens if mo else None,
                 "cost": mo.cost if mo else None, "currency": mo.currency if mo else None}
        state = "DONE" if status != "ai_unavailable" else "FAILED"
        return {"state": state, "outcome": status, "reason": reason,
                "note": {"title": title, "status": status, "symbols": sorted(ev.symbols), "ciks": sorted(ev.ciks), "body": body},
                "stats": stats}

    def _macro_unavailable(self, ev: Evidence, registry) -> None:
        refusals = [cloud_permission(registry, d) for d in MACRO_DATASETS]
        if all(r is not None for r in refusals):
            ev.unavailable.append({"what": "economic data releases (FRED, ECB, OECD and Treasury series)",
                                   "reason": "the data registry permits these datasets for local processing only, so they are "
                                             "not sent to a cloud model; central-bank releases are included where permitted"})

    def _brief(self, run: dict, cfg: ProcessesConfig) -> dict:
        s = cfg.get("brief")
        now = self._now()
        req = run["request"]
        window = (int(req["from_ns"]), now)
        registry = self._registry()
        watched, unavailable = self._watched(cfg)
        ev = Evidence(unavailable=list(unavailable), symbols=set(watched))
        ciks = {v["cik"] for v in watched.values() if v["cik"]}
        cap = s.options["max_items"]
        try:
            filings = news_items(self.news_db, since_ns=window[0], until_ns=now, kinds=["filing"], ciks=ciks, limit=1000)
            regulator = news_items(self.news_db, since_ns=window[0], until_ns=now, kinds=["regulator"], ciks=ciks, limit=1000)
            macro = news_items(self.news_db, since_ns=window[0], until_ns=now, kinds=["central_bank"], limit=1000)
        except SourceUnavailable as exc:
            filings = regulator = macro = []
            ev.unavailable.append({"what": "filings and releases", "reason": str(exc)})
        for items, why in ((filings, "filing by a watched issuer"), (regulator, "regulator release naming a watched issuer"),
                           (macro, "central-bank release")):
            room = cap - len(ev.items)
            if room <= 0:
                if items:
                    ev.unavailable.append({"what": f"{len(items)} more items ({why})",
                                           "reason": f"over the brief's max_items {cap}"})
                continue
            if len(items) > room:
                ev.unavailable.append({"what": f"{len(items) - room} older items ({why})",
                                       "reason": f"over the brief's max_items {cap}"})
            add_items(ev, items[-room:], registry, why=why)
        if not ev.items:
            ev.unavailable.append({"what": "overnight items", "reason": "no filing or regulator release for the watched issuers "
                                                                        "and no central-bank release in the window"})
        moves, reasons = daily_moves(self.research_db, {sym: v["instrument_id"] for sym, v in watched.items()}, until_ns=now)
        add_moves(ev, moves, reasons)
        self._macro_unavailable(ev, registry)
        mo = None
        if ev.spans:
            subjects = [f"{sym} ({v['name']}, CIK {v['cik']})" if v["cik"] else f"{sym} (issuer not resolved)"
                        for sym, v in watched.items()]
            task = brief_task(date=req["date"], calendar=req.get("calendar") or "on request",
                              window=(iso(window[0]), iso(now)), subjects=subjects, max_claims=min(20, cap))
            mo = run_model(self.w, run_id=run["run_id"], settings=s, task=task, spans=ev.spans, now_ns=now)
        return self._end(f"Morning brief {req['date']}", "brief", ev, mo, window, now,
                         {"subjects": [{"symbol": k, **v} for k, v in watched.items()]})

    def _summary(self, run: dict, cfg: ProcessesConfig) -> dict:
        s = cfg.get("summaries")
        now = self._now()
        req = run["request"]
        registry = self._registry()
        ev = Evidence(symbols={req["symbol"]} if req.get("symbol") else set())
        try:
            items = news_items(self.news_db, item_ids=[int(req["item_id"])])
        except SourceUnavailable as exc:
            items = []
            ev.unavailable.append({"what": f"news item {req['item_id']}", "reason": str(exc)})
        if not items:
            ev.unavailable.append({"what": f"news item {req['item_id']}", "reason": "not in the news store"})
            return self._end(f"Item {req['item_id']}: unavailable", "summaries", ev, None, None, now)
        it: NewsItem = items[0]
        add_items(ev, [it], registry, why="new item for a watched issuer")
        issuer = next((e for e in it.entities if e["cik"] == req.get("cik")), it.entities[0] if it.entities else {})
        subject = f"{issuer.get('name') or req.get('symbol')} ({req.get('symbol')}, CIK {req.get('cik')})"
        title = f"{it.form or it.kind} {issuer.get('name') or req.get('symbol')}: summary"
        mo = None
        if ev.spans:
            mo = run_model(self.w, run_id=run["run_id"], settings=s, task=summary_task(subject=subject, item_title=it.title),
                           spans=ev.spans, now_ns=now)
        else:
            ev.unavailable.append({"what": "AI summary", "reason": ev.items[0]["not_sent_reason"]})
        return self._end(title, "summaries", ev, mo, (it.first_seen_ns, now), now, {"item_id": it.item_id})

    def _explain(self, run: dict, cfg: ProcessesConfig) -> dict:
        s = cfg.get("explainer")
        now = self._now()
        req = run["request"]
        symbol = req["symbol"]
        days = int(req.get("window_days") or s.options["window_days"])
        window = (now - days * 86_400 * NS, now)
        registry = self._registry()
        ev = Evidence(symbols={symbol})
        resolved, problem = resolve_tickers(self.research_db, [symbol])
        ref = resolved.get(symbol)
        moves, reasons = daily_moves(self.research_db, {symbol: req.get("instrument_id")}, until_ns=now, since_ns=window[0])
        add_moves(ev, moves, reasons)
        if ref is None:
            ev.unavailable.append({"what": f"issuer for {symbol}", "reason": problem or "the SEC ticker reference does not list "
                                                                                     "this ticker"})
        else:
            try:
                items = news_items(self.news_db, since_ns=window[0], until_ns=now, kinds=["filing", "regulator"],
                                   ciks=[ref["cik"]], limit=1000)
            except SourceUnavailable as exc:
                items = []
                ev.unavailable.append({"what": "filings and releases", "reason": str(exc)})
            add_items(ev, items[-s.options["max_items"]:], registry, why=f"filing or release naming {symbol} in the window")
        title = f"Move explainer {symbol} ({iso(window[0])[:10]} to {iso(now)[:10]})"
        extra = {"symbol": symbol, "window_days": days}
        if not moves:
            extra["explanation"] = "Price move unavailable, so no explanation was attempted: " + "; ".join(reasons.values())
            return self._end(title, "explainer", ev, None, window, now, extra)
        if not ev.spans:
            extra["explanation"] = ("No filing or release in the window explains the move."
                                    if not ev.items else "No item in the window may be sent to a model; see the list below.")
            return self._end(title, "explainer", ev, None, window, now, extra)
        m = moves[0]
        direction = "rise" if not m.change.startswith("-") else "fall"
        subject = f"{ref['name']}, CIK {ref['cik']}" if ref else symbol
        mo = run_model(self.w, run_id=run["run_id"], settings=s,
                       task=explain_task(symbol=symbol, subject=subject, window=(iso(window[0]), iso(now)), direction=direction),
                       spans=ev.spans, now_ns=now)
        if mo.status == "abstained":
            extra["explanation"] = "No filing or release in the window explains the move."
        return self._end(title, "explainer", ev, mo, window, now, extra)

