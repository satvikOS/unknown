"""Shared plumbing for the free official sources (ADR-0015; DAT-02, DAT-04).

Declared User-Agent. Every request carries the owner's declared contact string from
``~/.unknown/config/data.json`` (``data_user_agent``, falling back to ``sec_user_agent``); nothing
is sent without one.

Capture-time availability (sources without upstream vintages). A value is legitimately usable
from the moment this system first saw it. When it is captured on or before the end of its
observation day in the source's time zone (a live capture), ``available_time = receipt_time``. When
it is captured later (a backfill of history), the receipt time would make history useless for
backtests, so ``available_time`` is set to 23:59:59 on the observation date in the source's time
zone and the observation is flagged ``availability_estimated_backfill``. The source's published
schedule is earlier than that bound (Treasury "usually ... by 6:00 PM Eastern"; ECB "around 16:00
CET"), so a backtest clock on the observation day before the end of day never sees it.

Change-only recording. ``record_if_changed`` writes a value once; an identical later fetch writes
nothing; a different value for the same key is a new publication available at its own receipt
time and flagged ``revised_after_first_seen``, so a revision never leaks backwards.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from ...domain.clock import NS_PER_S
from ..http import Fetcher
from ..pit import OPEN_END, Observation
from ..registry import load_registry, local_config


class SourceConfigError(RuntimeError):
    pass


def data_config(home: Path | None = None) -> dict:
    try:
        return local_config("data", home)
    except FileNotFoundError:
        return {}


def declared_user_agent(cfg: dict) -> str:
    ua = cfg.get("data_user_agent") or cfg.get("sec_user_agent")
    if not ua or "@" not in ua:
        raise SourceConfigError("a declared User-Agent with contact details is required in ~/.unknown/config/data.json "
                                "(data_user_agent or sec_user_agent)")
    return ua


def make_fetcher(provider_id: str, cfg: dict, **kw) -> Fetcher:
    registry = load_registry()
    if provider_id not in registry:
        raise SourceConfigError(f"provider {provider_id} is not registered")
    return Fetcher(declared_user_agent(cfg), registry[provider_id].rate_limit, **kw)


def end_of_day_ns(d: date, tz: str) -> int:
    return int(datetime.combine(d, time(23, 59, 59), tzinfo=ZoneInfo(tz)).timestamp()) * NS_PER_S


def capture_availability(receipt_ns: int, obs_date: date, tz: str) -> tuple[int, tuple[str, ...]]:
    bound = end_of_day_ns(obs_date, tz)
    if receipt_ns <= bound:
        return receipt_ns, ()
    return bound, ("availability_estimated_backfill",)


def current_publication(store, obs: Observation):
    """Latest current publication for the observation's key (dataset, entity, field, period, unit)."""
    return store.conn.execute(
        "SELECT * FROM observation WHERE dataset=? AND entity_id=? AND field=? AND COALESCE(period_start,'')=COALESCE(?,'') "
        "AND COALESCE(period_end,'')=COALESCE(?,'') AND unit=? AND known_to_ns=? "
        "ORDER BY available_time_ns DESC, publication_id DESC LIMIT 1",
        (obs.dataset, obs.entity_id, obs.field, obs.period_start, obs.period_end, obs.unit, OPEN_END)).fetchone()


def record_if_changed(store, obs: Observation, known_at_ns: int) -> tuple[int, str]:
    """Returns (obs_id, NEW | UNCHANGED | REVISED)."""
    cur = current_publication(store, obs)
    if cur is None:
        obs_id, outcome = store.record(obs, known_at_ns)
        return obs_id, "NEW" if outcome == "NEW" else outcome
    new_value = obs.value.to_str() if obs.value is not None else None
    if (cur["value"], cur["value_text"], cur["missing_reason"], cur["currency"]) == (new_value, obs.value_text, obs.missing_reason, obs.currency):
        return cur["obs_id"], "UNCHANGED"
    revised = replace(obs, available_time_ns=max(obs.receipt_time_ns, cur["available_time_ns"] + 1),
                      publication_id=f"{obs.publication_id}@rev:{obs.receipt_time_ns}",
                      quality=tuple(q for q in obs.quality if q != "availability_estimated_backfill") + ("revised_after_first_seen",))
    obs_id, _ = store.record(revised, known_at_ns)
    return obs_id, "REVISED"


def guard_xml(body: bytes) -> None:
    """Refuse DTDs and entity declarations in fetched XML (defence against entity expansion)."""
    head = body[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
        raise ValueError("XML with a DTD or entity declarations is refused")
