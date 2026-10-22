"""Effective-dated tariff registry, exact cost and worst-case reservation (ADR-0016 section 4; CAP-MODEL-05).

Prices are data (``config/models/tariffs.json``), never code. Each row is keyed by
(provider, model_id, tier, region) and carries an effective date range and a context
band. Rates are USD per 1M tokens as decimal strings; arithmetic is exact ``Fixed``:
``tokens * rate`` is shifted by 10^-6 without rounding.

Band rules (CAP-MODEL-05 analytics ``band(request)``): the band is selected by the
request's prompt tokens (all input classes: uncached + cache reads + cache writes);
every token class of that request is then billed at the selected row's rates
(``all_tokens_at_band_rate``; xAI states this explicitly for its 200k band). A band
whose rule or rates the register does not record has ``status: "unverified"``; both
``cost()`` and ``worst_case()`` refuse requests that fall into it rather than guess
(e.g. OpenAI long context > 272K, CAP-MODEL-05 open question 1).

``cost()`` is accounting: it needs every token class present in the usage to have a
recorded rate (cache writes need a ``verified`` multiplier) and refuses otherwise, so
the caller books the reservation and flags it for reconciliation (CAP-MODEL-05
degraded behaviour). ``worst_case()`` is admission: it additionally requires the row to
be owner-approved (``approved_by``) and prices all prompt tokens at the highest rate
that could apply, including any cache-write multiplier the request can trigger
(CAP-AGT-02 reservation formula). Provider-reported catalog prices are never read here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from ..domain.fixed import Fixed
from ..domain.money import Money, currency
from .common import CONFIG_MODELS

BAND_MEASURES = frozenset({"prompt_tokens"})
BAND_RULES = frozenset({"all_tokens_at_band_rate"})
STATUSES = frozenset({"verified", "unverified"})
CACHE_WRITE_STATUSES = frozenset({"verified", "unverified", "none_recorded"})
ROW_KEYS = frozenset({"provider", "model_id", "tier", "region", "band", "currency", "per_million", "cache_write",
                      "effective_from", "effective_until", "source_url", "retrieved_on", "records", "approved_by", "note"})


class TariffError(ValueError):
    pass


class NoTariff(TariffError):
    """No row for (provider, model, tier, region, time, band): the model cannot be routed."""


class BandUnverified(TariffError):
    """The request falls in a band whose rule or rates the register does not record."""


class TariffIncomplete(TariffError):
    """The usage contains a token class with no recorded (or no verified) rate."""


class TariffNotApproved(TariffError):
    """The row exists but no owner approval is recorded; it cannot be used for admission."""


@dataclass(frozen=True)
class Usage:
    """Canonical usage of ONE model attempt. Token classes are disjoint.

    ``cache_write_tokens`` maps a TTL label ("5m", "1h", "30m", "12h", or "unknown")
    to tokens written to the cache at that TTL.
    """
    provider: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cache_write_tokens: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    tier: str = "standard"
    region: str = "global"

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cached_read_tokens"):
            v = getattr(self, name)
            if type(v) is not int or v < 0:
                raise TariffError(f"{name} must be a non-negative int")
        for ttl, v in self.cache_write_tokens.items():
            if not isinstance(ttl, str) or type(v) is not int or v < 0:
                raise TariffError("cache_write_tokens must map TTL labels to non-negative ints")

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cached_read_tokens + sum(self.cache_write_tokens.values())


@dataclass(frozen=True)
class Band:
    measure: str
    min_tokens: int
    max_tokens: int | None
    status: str
    rule: str | None
    note: str

    def contains(self, tokens: int) -> bool:
        return tokens >= self.min_tokens and (self.max_tokens is None or tokens <= self.max_tokens)


@dataclass(frozen=True)
class TariffRow:
    provider: str
    model_id: str
    tier: str
    region: str
    band: Band
    currency: str
    input: Fixed | None
    output: Fixed | None
    cached_input: Fixed | None
    cache_write_multipliers: Mapping[str, Fixed]
    cache_write_status: str
    effective_from: date
    effective_until: date | None
    source_url: str
    retrieved_on: date
    records: tuple[str, ...]
    approved_by: str | None
    note: str

    def in_effect(self, at_ns: int) -> bool:
        d = datetime.fromtimestamp(at_ns // 1_000_000_000, tz=timezone.utc).date()
        return d >= self.effective_from and (self.effective_until is None or d < self.effective_until)


def per_million(tokens: int, rate: Fixed) -> Fixed:
    """Exact ``tokens * rate / 10^6``."""
    if type(tokens) is not int or tokens < 0:
        raise TariffError("tokens must be a non-negative int")
    return Fixed(tokens * rate.mantissa, rate.scale + 6)


def _date(value, where: str) -> date:
    if not isinstance(value, str):
        raise TariffError(f"{where} must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise TariffError(f"{where}: invalid date {value!r}") from None


def _rate(value, where: str) -> Fixed | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TariffError(f"{where} must be a decimal string or null (JSON numbers are refused)")
    r = Fixed.parse(value)
    if r.is_negative():
        raise TariffError(f"{where} must not be negative")
    return r


def parse_row(d: dict, i: int) -> TariffRow:
    where = f"rows[{i}]"
    if not isinstance(d, dict):
        raise TariffError(f"{where} must be an object")
    unknown = set(d) - ROW_KEYS
    missing = ROW_KEYS - set(d) - {"note", "effective_until", "approved_by"}
    if unknown or missing:
        raise TariffError(f"{where}: unknown {sorted(unknown)} missing {sorted(missing)}")
    b = d["band"]
    if not isinstance(b, dict) or set(b) - {"measure", "min_tokens", "max_tokens", "status", "rule", "note"}:
        raise TariffError(f"{where}.band malformed")
    if b.get("measure") not in BAND_MEASURES or b.get("status") not in STATUSES:
        raise TariffError(f"{where}.band measure/status invalid")
    if type(b.get("min_tokens")) is not int or b["min_tokens"] < 0:
        raise TariffError(f"{where}.band.min_tokens invalid")
    mx = b.get("max_tokens")
    if mx is not None and (type(mx) is not int or mx < b["min_tokens"]):
        raise TariffError(f"{where}.band.max_tokens invalid")
    rule = b.get("rule")
    if b["status"] == "verified" and rule not in BAND_RULES:
        raise TariffError(f"{where}.band: a verified band needs a rule in {sorted(BAND_RULES)}")
    band = Band(b["measure"], b["min_tokens"], mx, b["status"], rule, b.get("note", ""))
    pm = d["per_million"]
    if not isinstance(pm, dict) or set(pm) != {"input", "output", "cached_input"}:
        raise TariffError(f"{where}.per_million must have exactly input, output, cached_input")
    inp, out, cached = (_rate(pm[k], f"{where}.per_million.{k}") for k in ("input", "output", "cached_input"))
    if band.status == "verified" and (inp is None or out is None):
        raise TariffError(f"{where}: a verified band needs input and output rates")
    cw = d["cache_write"]
    if not isinstance(cw, dict) or set(cw) - {"multipliers", "status", "note"} or cw.get("status") not in CACHE_WRITE_STATUSES:
        raise TariffError(f"{where}.cache_write malformed")
    mults = {}
    for ttl, m in (cw.get("multipliers") or {}).items():
        mults[ttl] = _rate(m, f"{where}.cache_write.multipliers.{ttl}")
    ccy = d["currency"]
    currency(ccy)
    records = d["records"]
    if not isinstance(records, list) or not records or not all(isinstance(r, str) for r in records):
        raise TariffError(f"{where}.records must list the evidence record ids")
    approved = d.get("approved_by")
    if approved is not None and (not isinstance(approved, str) or not approved.strip()):
        raise TariffError(f"{where}.approved_by must be null or a non-empty string")
    for k in ("provider", "model_id", "tier", "region", "source_url"):
        if not isinstance(d[k], str) or not d[k]:
            raise TariffError(f"{where}.{k} must be a non-empty string")
    until = _date(d["effective_until"], f"{where}.effective_until") if d.get("effective_until") else None
    frm = _date(d["effective_from"], f"{where}.effective_from")
    if until is not None and until <= frm:
        raise TariffError(f"{where}: effective_until must be after effective_from")
    return TariffRow(d["provider"], d["model_id"], d["tier"], d["region"], band, ccy, inp, out, cached,
                     MappingProxyType(mults), cw["status"], frm, until, d["source_url"],
                     _date(d["retrieved_on"], f"{where}.retrieved_on"), tuple(records), approved, d.get("note", ""))


def _overlap(a: TariffRow, b: TariffRow) -> bool:
    a_end = a.effective_until or date.max
    b_end = b.effective_until or date.max
    dates = a.effective_from < b_end and b.effective_from < a_end
    a_hi = a.band.max_tokens if a.band.max_tokens is not None else 2**62
    b_hi = b.band.max_tokens if b.band.max_tokens is not None else 2**62
    bands = a.band.min_tokens <= b_hi and b.band.min_tokens <= a_hi
    return dates and bands


class TariffBook:
    def __init__(self, rows: Iterable[TariffRow]):
        self.rows = tuple(rows)
        by_key: dict[tuple, list[TariffRow]] = {}
        for r in self.rows:
            by_key.setdefault((r.provider, r.model_id, r.tier, r.region), []).append(r)
        for key, rows in by_key.items():
            for i, a in enumerate(rows):
                for b in rows[i + 1:]:
                    if _overlap(a, b):
                        raise TariffError(f"overlapping tariff rows for {key}: an amount would be ambiguous")
        self._by_key = by_key

    @classmethod
    def load(cls, path: Path | None = None) -> "TariffBook":
        path = path or CONFIG_MODELS / "tariffs.json"
        doc = json.loads(Path(path).read_text())
        if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("rows"), list):
            raise TariffError("tariffs.json must be {schema_version: 1, rows: [...]}")
        return cls(parse_row(r, i) for i, r in enumerate(doc["rows"]))

    # ------------------------------------------------------------------ selection
    def _candidates(self, provider: str, model_id: str, tier: str, region: str, at_ns: int) -> list[TariffRow]:
        rows = [r for r in self._by_key.get((provider, model_id, tier, region), []) if r.in_effect(at_ns)]
        if not rows:
            raise NoTariff(f"no tariff row for {provider}/{model_id} tier={tier} region={region} at {at_ns}")
        return rows

    def select(self, provider: str, model_id: str, *, at_ns: int, prompt_tokens: int,
               tier: str = "standard", region: str = "global") -> TariffRow:
        rows = [r for r in self._candidates(provider, model_id, tier, region, at_ns) if r.band.contains(prompt_tokens)]
        if not rows:
            raise NoTariff(f"no band of {provider}/{model_id} covers {prompt_tokens} prompt tokens")
        row = rows[0]
        if row.band.status != "verified":
            raise BandUnverified(f"{provider}/{model_id}: {prompt_tokens} prompt tokens fall in an unverified band "
                                 f"[{row.band.min_tokens}, {row.band.max_tokens}]: {row.band.note}")
        return row

    def is_routable(self, provider: str, model_id: str, *, at_ns: int, tier: str = "standard", region: str = "global") -> bool:
        try:
            rows = self._candidates(provider, model_id, tier, region, at_ns)
        except NoTariff:
            return False
        return any(r.band.status == "verified" and r.approved_by for r in rows)

    # ------------------------------------------------------------------ cost (accounting)
    def cost(self, usage: Usage, *, at_ns: int) -> Money:
        row = self.select(usage.provider, usage.model_id, at_ns=at_ns, prompt_tokens=usage.prompt_tokens,
                          tier=usage.tier, region=usage.region)
        total = Fixed(0)
        total = total + per_million(usage.input_tokens, row.input)
        total = total + per_million(usage.output_tokens, row.output)
        if usage.cached_read_tokens:
            if row.cached_input is None:
                raise TariffIncomplete(f"{usage.model_id}: cached-input rate not recorded")
            total = total + per_million(usage.cached_read_tokens, row.cached_input)
        for ttl, tokens in usage.cache_write_tokens.items():
            if not tokens:
                continue
            if row.cache_write_status != "verified" or ttl not in row.cache_write_multipliers:
                raise TariffIncomplete(f"{usage.model_id}: cache-write rate for TTL {ttl!r} not verified")
            total = total + per_million(tokens, row.input * row.cache_write_multipliers[ttl])
        return Money(row.currency, total)

    def cost_all(self, usages: Iterable[Usage], *, at_ns: int) -> Money:
        """Sum over attempts (e.g. a refused attempt plus a server-side fallback attempt)."""
        usages = list(usages)
        if not usages:
            raise TariffError("no usage")
        total = None
        for u in usages:
            c = self.cost(u, at_ns=at_ns)
            total = c if total is None else total + c
        return total

    # ------------------------------------------------------------------ worst case (admission)
    def worst_case(self, provider: str, model_id: str, *, at_ns: int, prompt_tokens: int, max_output_tokens: int,
                   tier: str = "standard", region: str = "global", cache_write_ttls: Iterable[str] = (),
                   require_approved: bool = True) -> Money:
        """Upper bound for one attempt: prompt tokens at the highest applicable input rate
        (including cache-write multipliers the request can trigger) plus max output tokens
        at the highest output rate of any band at or below the request's band."""
        if type(max_output_tokens) is not int or max_output_tokens < 0 or type(prompt_tokens) is not int or prompt_tokens < 0:
            raise TariffError("token counts must be non-negative ints")
        row = self.select(provider, model_id, at_ns=at_ns, prompt_tokens=prompt_tokens, tier=tier, region=region)
        if require_approved and not row.approved_by:
            raise TariffNotApproved(f"tariff row for {provider}/{model_id} has no owner approval (approved_by)")
        reachable = [r for r in self._candidates(provider, model_id, tier, region, at_ns)
                     if r.band.status == "verified" and r.band.min_tokens <= prompt_tokens]
        in_rate = max((r.input for r in reachable), key=lambda f: f)
        out_rate = max((r.output for r in reachable), key=lambda f: f)
        cached = [r.cached_input for r in reachable if r.cached_input is not None]
        if cached:
            in_rate = max([in_rate, *cached], key=lambda f: f)
        for ttl in cache_write_ttls:
            mults = [r.cache_write_multipliers.get(ttl) for r in reachable]
            if any(m is None for m in mults):
                raise TariffIncomplete(f"{model_id}: no cache-write multiplier recorded for TTL {ttl!r}; cannot bound cost")
            for r in reachable:
                in_rate = max(in_rate, r.input * r.cache_write_multipliers[ttl], key=lambda f: f)
        amount = per_million(prompt_tokens, in_rate) + per_million(max_output_tokens, out_rate)
        return Money(row.currency, amount)
