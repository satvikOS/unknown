"""Owner settings for the AI processes: ``<UNKNOWN_HOME>/config/ai_processes.json`` (read-only here).

Absent file: every process is off (nothing is scheduled, no request runs). The approval queue is not a model
process and needs no switch. Each process has its own on/off switch and its own caps, decimal strings in the
owner policy's currency; they can only lower the owner caps in ``agents.json``, which the ledger still enforces:

  brief       per_day 0.50   per_run 0.50   time 07:30 in the calendar's zone, on its trading days only
  summaries   per_day 2.00   per_run 0.25   forms 8-K, 10-Q, 10-K; regulator releases that name a watched issuer
  explainer   per_day 2.00   per_run 0.50   (per request) window 5 days

Unknown keys, JSON numbers for money, a per-run cap above the per-day cap, an unknown calendar and a file
others can write are refused: an invalid file turns every process off with the reason. Nothing in the agent
layer writes this file.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ...domain.fixed import Fixed
from ...domain.money import Money
from ...service.paths import Paths
from ..common import REPO

CALENDARS = REPO / "config" / "calendars"
PROCESS_IDS = ("brief", "summaries", "explainer")
# Each process runs under its own ledger role, so its spend per day is one ledger query and its own day cap
# is a role/day check inside the reservation transaction (budget.SpendLedger.reserve role_day_cap).
ROLES = {"brief": "ai_brief", "summaries": "ai_summary", "explainer": "ai_explainer"}
PROCESS_OF_ROLE = {v: k for k, v in ROLES.items()}
DEFAULT_FORMS = ("8-K", "10-Q", "10-K")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
FORM_RE = re.compile(r"^[0-9A-Z][0-9A-Z\-/]{0,15}$")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
MAX_WATCHLIST = 50

DEFAULTS: dict[str, dict[str, Any]] = {
    "brief": {"enabled": False, "per_day": "0.50", "per_run": "0.50", "time": "07:30", "calendar": "XNYS",
              "max_output_tokens": 4000, "max_items": 40},
    "summaries": {"enabled": False, "per_day": "2.00", "per_run": "0.25", "forms": list(DEFAULT_FORMS), "regulator": True,
                  "max_output_tokens": 2000, "max_per_cycle": 3},
    "explainer": {"enabled": False, "per_day": "2.00", "per_run": "0.50", "window_days": 5, "max_output_tokens": 3000,
                  "max_items": 30},
}
TOP_KEYS = {"schema_version", "comment", "currency", "watchlist", "include_holdings", "approvals", *PROCESS_IDS}
APPROVAL_KEYS = {"queue_research_proposals", "expiry_hours"}


class ProcessConfigError(ValueError):
    pass


def default_config_path(home: Path | None = None) -> Path:
    root = Paths(home).root if home else Paths.default().root
    return root / "config" / "ai_processes.json"


@dataclass(frozen=True)
class ProcessSettings:
    process: str
    enabled: bool
    per_day: Money
    per_run: Money
    max_output_tokens: int
    options: Mapping[str, Any]

    @property
    def role(self) -> str:
        return ROLES[self.process]

    def view(self) -> dict:
        return {"process": self.process, "role": self.role, "enabled": self.enabled, "per_day": self.per_day.amount.to_str(),
                "per_run": self.per_run.amount.to_str(), "currency": self.per_day.currency,
                "max_output_tokens": self.max_output_tokens, **{k: (list(v) if isinstance(v, tuple) else v)
                                                                 for k, v in self.options.items()}}


@dataclass(frozen=True)
class ProcessesConfig:
    source: str                         # "file" or "absent"
    path: Path
    currency: str
    watchlist: tuple[str, ...]
    include_holdings: bool
    processes: Mapping[str, ProcessSettings]
    queue_research_proposals: bool = True
    approval_expiry_hours: int = 168

    def get(self, process: str) -> ProcessSettings:
        return self.processes[process]

    def view(self) -> dict:
        return {"source": self.source, "path": str(self.path), "currency": self.currency, "watchlist": list(self.watchlist),
                "include_holdings": self.include_holdings,
                "processes": {p: s.view() for p, s in self.processes.items()},
                "approvals": {"queue_research_proposals": self.queue_research_proposals,
                              "expiry_hours": self.approval_expiry_hours}}


def _money(value: Any, where: str, currency: str) -> Money:
    if not isinstance(value, str):
        raise ProcessConfigError(f"{where} must be a decimal string (JSON numbers are refused for money)")
    try:
        amount = Fixed.parse(value)
    except (ValueError, TypeError):
        raise ProcessConfigError(f"{where} is not a decimal: {value!r}") from None
    if amount.is_negative():
        raise ProcessConfigError(f"{where} must not be negative")
    return Money(currency, amount)


def _int(value: Any, where: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise ProcessConfigError(f"{where} must be an integer in [{lo}, {hi}]")
    return value


def _bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise ProcessConfigError(f"{where} must be true or false")
    return value


def _settings(process: str, raw: Any, currency: str) -> ProcessSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProcessConfigError(f"{process} must be an object")
    merged = {**DEFAULTS[process], **raw}
    unknown = set(merged) - set(DEFAULTS[process])
    if unknown:
        raise ProcessConfigError(f"{process}: unknown keys {sorted(unknown)}")
    per_day = _money(merged["per_day"], f"{process}.per_day", currency)
    per_run = _money(merged["per_run"], f"{process}.per_run", currency)
    if per_run > per_day:
        raise ProcessConfigError(f"{process}.per_run {per_run.amount.to_str()} is above {process}.per_day")
    options: dict[str, Any] = {}
    if process == "brief":
        if not isinstance(merged["time"], str) or not TIME_RE.match(merged["time"]):
            raise ProcessConfigError("brief.time must be HH:MM (24 h) in the calendar's time zone")
        cal = merged["calendar"]
        if not isinstance(cal, str) or not re.match(r"^[A-Z0-9-]{2,16}$", cal) or not (CALENDARS / f"{cal}.json").is_file():
            raise ProcessConfigError(f"brief.calendar {cal!r} is not a calendar in config/calendars")
        options = {"time": merged["time"], "calendar": cal, "max_items": _int(merged["max_items"], "brief.max_items", 1, 200)}
    elif process == "summaries":
        forms = merged["forms"]
        if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and FORM_RE.match(f) for f in forms):
            raise ProcessConfigError("summaries.forms must be a non-empty list of form types such as 8-K")
        options = {"forms": tuple(forms), "regulator": _bool(merged["regulator"], "summaries.regulator"),
                   "max_per_cycle": _int(merged["max_per_cycle"], "summaries.max_per_cycle", 1, 50)}
    else:
        options = {"window_days": _int(merged["window_days"], "explainer.window_days", 1, 30),
                   "max_items": _int(merged["max_items"], "explainer.max_items", 1, 200)}
    return ProcessSettings(process, _bool(merged["enabled"], f"{process}.enabled"), per_day, per_run,
                           _int(merged["max_output_tokens"], f"{process}.max_output_tokens", 256, 32_000), options)


def parse_config(doc: Any, path: Path, *, source: str = "file") -> ProcessesConfig:
    if not isinstance(doc, dict):
        raise ProcessConfigError("the file must hold a JSON object")
    unknown = set(doc) - TOP_KEYS
    if unknown:
        raise ProcessConfigError(f"unknown keys {sorted(unknown)}")
    if doc.get("schema_version", 1) != 1:
        raise ProcessConfigError("schema_version must be 1")
    currency = doc.get("currency", "USD")
    if not isinstance(currency, str) or not re.match(r"^[A-Z]{3}$", currency):
        raise ProcessConfigError("currency must be an ISO 4217 code")
    wl = doc.get("watchlist", [])
    if not isinstance(wl, list) or len(wl) > MAX_WATCHLIST or not all(isinstance(t, str) and TICKER_RE.match(t) for t in wl):
        raise ProcessConfigError(f"watchlist must be a list of at most {MAX_WATCHLIST} upper-case tickers")
    approvals = doc.get("approvals", {})
    if not isinstance(approvals, dict) or set(approvals) - APPROVAL_KEYS:
        raise ProcessConfigError(f"approvals accepts only {sorted(APPROVAL_KEYS)}")
    return ProcessesConfig(
        source, path, currency, tuple(dict.fromkeys(wl)), _bool(doc.get("include_holdings", True), "include_holdings"),
        {p: _settings(p, doc.get(p), currency) for p in PROCESS_IDS},
        _bool(approvals.get("queue_research_proposals", True), "approvals.queue_research_proposals"),
        _int(approvals.get("expiry_hours", 168), "approvals.expiry_hours", 1, 24 * 90))


def load_processes_config(path: Path) -> ProcessesConfig:
    """The owner's settings, or every process off when the file is absent. Raises ProcessConfigError when the
    file exists but is invalid or writable by others."""
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return parse_config({}, path, source="absent")
    if st.st_uid != os.getuid():
        raise ProcessConfigError(f"{path} is not owned by this user")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProcessConfigError(f"{path} is writable by others ({oct(st.st_mode & 0o777)}); chmod 600")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProcessConfigError(f"{path} is not valid JSON: {exc}") from None
    return parse_config(doc, path)
