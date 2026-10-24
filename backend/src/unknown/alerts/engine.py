"""The server-side alert engine: evaluates stored rules against the live market-data bus, the clock and the news store.

It runs in the API process on its own thread (started at server start and on first use of the alerts API), so alerts
fire whether or not a terminal is open. It is an ordinary market-data bus client for the keys its active rules need.
Each step:

1. on the first step, a gap since the last heartbeat is recorded in the audit as a window that was NOT evaluated;
2. reloads rules when the store's rules version changes (expiring rules past ``expires_at``) and re-subscribes;
3. drains bus events: quotes are evaluated (``evaluate.evaluate_quote``); a source status (disconnected, not
   entitled, ...) puts the key's rules in 'not evaluating' and notifies the owner once;
4. evaluates time rules, then new news-store items for news and filing rules;
5. delivers triggers: the inbox (SSE readers poll it), and a macOS notification, retried with backoff.

The engine imports nothing that can place, modify or cancel an order (tests check the import graph).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from . import evaluate
from .notify import MacNotifier
from .rules import MARKET_KINDS, NEWS_KINDS, describe
from .store import AlertStore, store_path

log = logging.getLogger("unknown.alerts")

DOWNTIME_GAP_NS = 30 * 1_000_000_000
HEARTBEAT_EVERY_NS = 5 * 1_000_000_000
STATE_FLUSH_NS = 2 * 1_000_000_000
NEWS_POLL_NS = 2 * 1_000_000_000
STUDY_REFRESH_NS = 15 * 60 * 1_000_000_000
ISSUER_REFRESH_NS = 10 * 60 * 1_000_000_000
RETRY_BACKOFF_NS = (1_000_000_000, 5_000_000_000, 30_000_000_000)
NEWS_BATCH = 200


class AlertEngine:
    def __init__(self, root: Path, *, bus_factory=None, notifier=None, clock_ns=time.time_ns, tick_s: float = 0.5):
        self.root = Path(root)
        self.clock_ns = clock_ns
        self.store = AlertStore(store_path(self.root), clock_ns=clock_ns)
        self.bus_factory = bus_factory
        self.notifier = notifier or MacNotifier(self.root)
        self.tick_s = tick_s
        self.research_db = self.root / "research" / "research.sqlite"
        self.news_path = self.root / "news" / "news.sqlite"
        self.version = -1
        self.rules: list[dict] = []
        self.by_key: dict[str, list[dict]] = {}
        self.keys: frozenset[str] = frozenset()
        self.bus = None
        self.client = None
        self.bus_error: str | None = None
        self.closes: dict[str, tuple[int, list[float]]] = {}
        self.ciks: dict[str, tuple[int, set[int]]] = {}
        self.dirty: set[str] = set()
        self.retry: list[dict] = []
        self.started_ns: int | None = None
        self._last_beat = 0
        self._last_flush = 0
        self._last_news = 0
        self._news = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="alert-engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._flush(force=True)
        self._unsubscribe()
        if self._news is not None:
            self._news.close()
        self.store.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step(self.tick_s)
            except Exception:   # an engine fault is logged and retried; it never takes the API down
                log.exception("alert engine step failed")
                self._stop.wait(1.0)

    # ------------------------------------------------------------------ one step
    def step(self, timeout: float = 0.0) -> None:
        now = self.clock_ns()
        if self.started_ns is None:
            self._resume(now)
        if self.store.rules_version() != self.version:
            self._reload(now)
        events = self.client.drain(timeout) if self.client is not None else []
        if self.client is None and timeout:
            self._stop.wait(timeout)
        now = self.clock_ns()
        for name, ev in events:
            if name == "quote":
                self._on_quote(ev, now)
            elif name == "status":
                self._on_status(ev, now)
        self._on_clock(now)
        if now - self._last_news >= NEWS_POLL_NS:
            self._last_news = now
            self._on_news(now)
        self._deliver_retries(now)
        self._flush()
        if now - self._last_beat >= HEARTBEAT_EVERY_NS:
            self._last_beat = now
            self.store.set_meta("heartbeat_ns", str(now))
            self.store.set_meta("engine", json.dumps(self.describe(now), sort_keys=True))

    def describe(self, now: int | None = None) -> dict:
        state, detail = self.notifier.status()
        return {"started_ns": self.started_ns, "heartbeat_ns": now, "pid": os.getpid(), "keys": sorted(self.keys),
                "bus_error": self.bus_error, "rules_live": len(self.rules),
                "notifications": {"state": state, "detail": detail}}

    def _resume(self, now: int) -> None:
        self.started_ns = now
        last = self.store.meta("heartbeat_ns")
        if last is not None and now - int(last) > DOWNTIME_GAP_NS:
            self.store.audit("engine", "not_evaluated", detail={"from_ns": int(last), "to_ns": now,
                                                                "reason": "the alert engine was not running"})
        self.store.audit("engine", "start", detail={"pid": os.getpid()})

    # ------------------------------------------------------------------ rules and subscriptions
    def _reload(self, now: int) -> None:
        self._flush(force=True)
        self.version = self.store.rules_version()
        rules = []
        for r in self.store.live_rules():
            exp = r["rule"].get("expires_ns")
            if exp is not None and exp <= now:
                self.store.set_status(r["rule_id"], "expired", actor="engine", reason="past expires_at")
                continue
            rules.append(r)
        self.version = self.store.rules_version()
        self.rules = rules
        self.by_key = {}
        for r in rules:
            if r["rule"]["kind"] in MARKET_KINDS:
                self.by_key.setdefault(r["rule"]["key"], []).append(r)
        keys = frozenset(self.by_key)
        if keys != self.keys:
            self._subscribe(keys)

    def _subscribe(self, keys: frozenset[str]) -> None:
        self._unsubscribe()
        self.keys = keys
        if not keys:
            return
        try:
            if self.bus is None:
                if self.bus_factory is None:
                    raise RuntimeError("no market-data bus in this process")
                self.bus = self.bus_factory()
            self.client = self.bus.open_client(sorted(keys))
            self.bus_error = None
        except Exception as exc:  # the bus could not start: every market rule is 'not evaluating' with the reason
            self.bus_error = f"market-data bus unavailable: {exc}"
            for key in keys:
                self._not_evaluating(key, "NOT_CONNECTED", self.bus_error, self.clock_ns())

    def _unsubscribe(self) -> None:
        if self.client is not None and self.bus is not None:
            try:
                self.bus.close_client(self.client)
            except Exception:
                log.exception("closing the alert engine's bus client failed")
        self.client = None

    # ------------------------------------------------------------------ evaluation
    def _set_eval(self, r: dict, status: dict, now: int, *, notify: bool) -> None:
        prev = r.get("evaluation") or {}
        status = {**status, "at_ns": now}
        if not status["evaluating"]:
            status["since_ns"] = prev.get("since_ns", now) if not prev.get("evaluating", True) else now
            status["notified"] = prev.get("notified", False) if not prev.get("evaluating", True) else False
            if notify and not status["notified"]:
                status["notified"] = True
                self.store.audit("engine", "not_evaluating", rule_id=r["rule_id"],
                                 detail={"reason": status["reason"], "detail": status["detail"]})
                self.notifier.send("Alert not evaluating", f"{r['rule']['name']}: {status['detail']}",
                                   status["reason"] or "")
        elif prev and prev.get("evaluating") is False:
            self.store.audit("engine", "evaluating", rule_id=r["rule_id"], detail={"after": prev.get("reason")})
        if {k: v for k, v in prev.items() if k != "at_ns"} != {k: v for k, v in status.items() if k != "at_ns"}:
            self.dirty.add(r["rule_id"])
        r["evaluation"] = status

    def _not_evaluating(self, key: str, reason: str, detail: str, now: int) -> None:
        for r in self.by_key.get(key, []):
            self._set_eval(r, {"evaluating": False, "reason": reason, "detail": detail}, now, notify=True)

    def _closes(self, key: str, n: int, now: int) -> list[float] | None:
        hit = self.closes.get(key)
        if hit is None or now - hit[0] > STUDY_REFRESH_NS:   # re-read by age only, never per quote
            from ..analytics.market_risk import load_closes
            s = load_closes(self.research_db, key, [key], now_ns=now, lookback=2500)  # n <= 500
            hit = (now, [float(c) for _, c in s.closes])
            self.closes[key] = hit
        return hit[1]

    def _on_quote(self, ev: dict, now: int) -> None:
        for r in list(self.by_key.get(ev.get("key"), [])):
            closes = self._closes(ev["key"], r["rule"]["n"], now) if r["rule"]["kind"] == "study" else None
            trigger, state, status = evaluate.evaluate_quote(r["rule"], r["state"], ev, closes)
            if state != r["state"]:
                r["state"] = state
                self.dirty.add(r["rule_id"])
            self._set_eval(r, status, now, notify=False)
            if trigger:
                self._trigger(r, trigger, now)

    def _on_status(self, ev: dict, now: int) -> None:
        if ev.get("state") == "SUBSCRIBED" or "not delivered" in str(ev.get("reason")):
            return   # subscribed, or a trade-buffer overflow notice (quotes are unaffected)
        self._not_evaluating(ev.get("key"), ev.get("code") or "NOT_CONNECTED", f"{ev.get('state')}: {ev.get('reason')}", now)

    def _on_clock(self, now: int) -> None:
        for r in list(self.rules):
            if r["rule"]["kind"] != "time":
                continue
            trigger, state = evaluate.evaluate_time(r["rule"], r["state"], now)
            if trigger:
                r["state"] = state
                self.dirty.add(r["rule_id"])
                self._trigger(r, trigger, now)

    def _issuer_ciks(self, token: str, now: int) -> set[int]:
        hit = self.ciks.get(token)
        if hit is None or now - hit[0] > ISSUER_REFRESH_NS:
            from ..news.entities import IssuerIndex
            index = IssuerIndex.open(self.research_db, now)
            try:
                ciks, _ = index.resolve([token])
            finally:
                index.close()
            if token.isdigit():
                ciks = ciks | {int(token)}
            hit = (now, set(ciks))
            self.ciks[token] = hit
        return hit[1]

    def _on_news(self, now: int) -> None:
        news_rules = [r for r in self.rules if r["rule"]["kind"] in NEWS_KINDS]
        if not news_rules:
            return
        if self._news is None:
            from ..news.store import NewsStore
            try:
                self._news = NewsStore(self.news_path, readonly=True)
            except FileNotFoundError:
                for r in news_rules:
                    self._set_eval(r, {"evaluating": False, "reason": "NO_SOURCE",
                                       "detail": "no news store yet: start the news poller"}, now, notify=True)
                return
        after = self.store.meta("news_after_id")
        if after is None:
            after = self._news.max_item_id()
            self.store.set_meta("news_after_id", str(after))
        after = int(after)
        items = self._news.query(after_id=after, limit=NEWS_BATCH)
        resolved = {}
        for r in news_rules:
            ciks = self._issuer_ciks(r["rule"]["issuer"], now)
            resolved[r["rule_id"]] = ciks
            if not ciks:
                self._set_eval(r, {"evaluating": False, "reason": "NOT_FOUND",
                                   "detail": f"issuer {r['rule']['issuer']} is not in the SEC ticker snapshot"}, now,
                               notify=True)
            else:
                self._set_eval(r, {"evaluating": True, "reason": None, "detail": None}, now, notify=False)
        for item in items:
            for r in news_rules:
                if int(item.get("first_seen_ns") or 0) < r["created_ns"]:
                    continue
                hit = evaluate.news_matches(r["rule"], item, resolved[r["rule_id"]])
                cluster = (item.get("cluster") or {}).get("cluster_id")
                seen = r["state"].get("clusters", [])
                if not hit or (cluster is not None and cluster in seen):
                    continue
                if cluster is not None:
                    r["state"] = {**r["state"], "clusters": (seen + [cluster])[-200:]}
                    self.dirty.add(r["rule_id"])
                self._trigger(r, evaluate.news_trigger(item, hit), now)
            after = item["id"]
        if items:
            self.store.set_meta("news_after_id", str(after))

    # ------------------------------------------------------------------ delivery
    def _trigger(self, r: dict, trigger: dict, now: int) -> None:
        rule = r["rule"]
        snooze = r.get("snooze_until_ns")
        if snooze and now < snooze:
            self.store.audit("engine", "suppressed", rule_id=r["rule_id"],
                             detail={"snoozed_until_ns": snooze, "observed": trigger.get("observed"),
                                     "source": trigger.get("source")})
            return
        retire = rule.get("repeat") == "once"
        title = rule["name"]
        self._flush(force=True)
        eid = self.store.fire(r, trigger, title=title, now_ns=now, retire=retire)
        if retire:
            self.rules = [x for x in self.rules if x["rule_id"] != r["rule_id"]]
            for key, rs in list(self.by_key.items()):
                self.by_key[key] = [x for x in rs if x["rule_id"] != r["rule_id"]]
        message = describe(rule) if rule["kind"] not in NEWS_KINDS else str(trigger.get("observed") or describe(rule))
        subtitle = trigger.get("label") or (f"observed {trigger['observed']}" if rule["kind"] in MARKET_KINDS else "")
        self._notify(eid, title, message, subtitle, attempt=0, now=now)

    def _notify(self, eid: int, title: str, message: str, subtitle: str, *, attempt: int, now: int) -> None:
        status, detail = self.notifier.send(title, message, subtitle)
        if status == "failed" and attempt < len(RETRY_BACKOFF_NS):
            self.retry.append({"due_ns": now + RETRY_BACKOFF_NS[attempt], "event_id": eid, "title": title,
                               "message": message, "subtitle": subtitle, "attempt": attempt + 1})
            status = "retrying"
        self.store.set_delivery(eid, status, detail)

    def _deliver_retries(self, now: int) -> None:
        due = [x for x in self.retry if x["due_ns"] <= now]
        self.retry = [x for x in self.retry if x["due_ns"] > now]
        for x in due:
            self._notify(x["event_id"], x["title"], x["message"], x["subtitle"], attempt=x["attempt"], now=now)

    def _flush(self, force: bool = False) -> None:
        now = self.clock_ns()
        if not self.dirty or (not force and now - self._last_flush < STATE_FLUSH_NS):
            return
        by_id = {r["rule_id"]: r for r in self.rules}
        for rid in self.dirty:
            r = by_id.get(rid)
            if r is not None:
                self.store.save_state(rid, r["state"], r.get("evaluation") or {})
        self.dirty.clear()
        self._last_flush = now


_LOCK = threading.Lock()
_ENGINES: dict[str, AlertEngine] = {}


def ensure_started(ctx) -> AlertEngine | None:
    """The process's engine for ``ctx.paths.root``, started on first call; None when ``ctx`` has no market-data bus
    (unit-test contexts), so routes stay usable without a running engine."""
    factory = getattr(ctx, "marketdata_bus", None)
    if not callable(factory):
        return None
    root = str(ctx.paths.root)
    with _LOCK:
        eng = _ENGINES.get(root)
        if eng is None:
            eng = AlertEngine(Path(root), bus_factory=factory)
            eng.start()
            _ENGINES[root] = eng
        return eng
