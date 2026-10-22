"""Daily bars with availability times and point-in-time access (DAT-02, DAT-04; BR-DAT-09; CAP-RES-04).

A bar covers ``[start_ns, end_ns)`` and becomes usable for decisions at
``available_ns`` (end plus the source's publication lag, BR-DAT-09). A
corrected bar is a new *version* of the same session with a later
availability time; a decision made before the correction was available keeps
seeing the original. Every join is "latest version with availability time
<= decision instant", never a calendar-date join.

``BarSeries.as_of(t)`` implements exactly the semantics of
``data.pit.ResearchStore.as_of`` for bars stored in the point-in-time store
(``record_bars``/``load_bars``); the tests prove the two agree at every
availability boundary. ``DatasetView`` is the only thing the engine accepts:
it bounds what a run may ever read, and a view that reaches into an
experiment's untouched holdout can only be built with the ``HoldoutGrant``
that the registry issues once (CAP-RES-07, CAP-RES-10, BR-DAT-07).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from ..domain.fixed import Fixed
from .common import HoldoutError, LookaheadError, stable_hash

BAR_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    session_date: date
    start_ns: int
    end_ns: int
    available_ns: int
    open: Fixed
    high: Fixed
    low: Fixed
    close: Fixed
    volume: Fixed
    currency: str = "USD"
    source: str = ""
    fixture: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close", "volume"):
            if not isinstance(getattr(self, name), Fixed):
                raise TypeError(f"bar {name} must be Fixed")
        if not (self.open.is_positive() and self.high.is_positive() and self.low.is_positive() and self.close.is_positive()):
            raise ValueError(f"bar prices must be positive ({self.instrument_id} {self.session_date})")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"bar violates low <= open, close <= high ({self.instrument_id} {self.session_date})")
        if self.volume.is_negative() or not self.volume.is_multiple_of(Fixed(1)):
            raise ValueError("bar volume must be a non-negative whole number of shares")
        if not self.start_ns < self.end_ns <= self.available_ns:
            raise ValueError("bar times must satisfy start < end <= available")
        if self.version < 1:
            raise ValueError("bar version must be >= 1")

    def to_json(self) -> dict:
        return {"instrument_id": self.instrument_id, "session_date": self.session_date.isoformat(),
                "start_ns": self.start_ns, "end_ns": self.end_ns, "available_ns": self.available_ns,
                "open": self.open.to_str(), "high": self.high.to_str(), "low": self.low.to_str(),
                "close": self.close.to_str(), "volume": self.volume.to_str(), "currency": self.currency,
                "source": self.source, "fixture": self.fixture, "version": self.version}


class BarSeries:
    """All published versions of one instrument's daily bars."""

    def __init__(self, instrument_id: str, bars: Iterable[Bar]):
        self.instrument_id = instrument_id
        versions = sorted(bars, key=lambda b: (b.available_ns, b.session_date, b.version))
        seen: dict[tuple[date, int], Bar] = {}
        last_avail: dict[date, int] = {}
        for b in versions:
            if b.instrument_id != instrument_id:
                raise ValueError(f"bar for {b.instrument_id} in series {instrument_id}")
            if (b.session_date, b.version) in seen:
                raise ValueError(f"duplicate bar version {b.session_date} v{b.version}")
            seen[(b.session_date, b.version)] = b
            if b.session_date in last_avail and b.available_ns < last_avail[b.session_date]:
                raise ValueError("a later bar version cannot become available earlier")
            last_avail[b.session_date] = b.available_ns
        by_date: dict[date, list[Bar]] = {}
        for b in sorted(seen.values(), key=lambda b: (b.session_date, b.version)):
            by_date.setdefault(b.session_date, []).append(b)
        for d, vs in by_date.items():
            if [v.version for v in vs] != sorted(v.version for v in vs) or \
                    [v.available_ns for v in vs] != sorted(v.available_ns for v in vs):
                raise ValueError(f"versions of {d} must become available in version order")
        self._versions = versions
        self._avail = [b.available_ns for b in versions]
        self._by_date = by_date
        self.dates = tuple(sorted(by_date))

    def __len__(self) -> int:
        return len(self.dates)

    def versions(self) -> list[Bar]:
        return list(self._versions)

    def as_of(self, t_ns: int) -> list[Bar]:
        """Latest version of each session with ``available_ns <= t_ns``, in session order."""
        n = bisect.bisect_right(self._avail, t_ns)
        latest: dict[date, Bar] = {}
        for b in self._versions[:n]:
            cur = latest.get(b.session_date)
            if cur is None or b.version > cur.version:
                latest[b.session_date] = b
        return [latest[d] for d in sorted(latest)]

    def final(self, session_date: date) -> Bar | None:
        """Latest version of a session in this series: the simulator's account of what traded (never a decision input)."""
        vs = self._by_date.get(session_date)
        return vs[-1] if vs else None

    def truncated(self, until: date) -> "BarSeries":
        return BarSeries(self.instrument_id, [b for b in self._versions if b.session_date <= until])

    def fingerprint(self) -> str:
        return stable_hash([b.to_json() for b in sorted(self._versions, key=lambda b: (b.session_date, b.version))])


class Cursor:
    """Incremental point-in-time reader for one series (decisions advance monotonically)."""

    def __init__(self, series: BarSeries):
        self._series = series
        self._i = 0
        self._t = -1
        self._visible: dict[date, Bar] = {}
        self.history: list[Bar] = []

    def advance(self, t_ns: int) -> tuple[list[Bar], bool]:
        """Make visible everything with ``available_ns <= t_ns``. Returns (newly appended bars, revised)
        where ``revised`` means an already-visible session changed or an older session arrived late,
        so incremental features must be rebuilt from ``history``."""
        if t_ns < self._t:
            raise LookaheadError("cursor cannot move backwards in decision time")
        self._t = t_ns
        versions = self._series._versions
        appended: list[Bar] = []
        revised = False
        while self._i < len(versions) and versions[self._i].available_ns <= t_ns:
            b = versions[self._i]
            self._i += 1
            if b.session_date in self._visible:
                revised = True
            elif self.history and b.session_date < self.history[-1].session_date:
                revised = True
            else:
                self.history.append(b)
                appended.append(b)
            self._visible[b.session_date] = b
        if revised:
            self.history = [self._visible[d] for d in sorted(self._visible)]
            appended = []
        return appended, revised

    def visible(self, session_date: date) -> Bar:
        b = self._visible.get(session_date)
        if b is None:
            raise LookaheadError(f"{self._series.instrument_id} {session_date} is not available at the decision time")
        return b


@dataclass(frozen=True)
class HoldoutGrant:
    """Issued only by ``ResearchRegistry.consume_holdout``; recorded there before it is returned.

    ``token`` is a random nonce known only to the issuing process. ``DatasetView``
    accepts a grant only if this process issued exactly that grant (``issue_grant``),
    so constructing a ``HoldoutGrant`` by hand does not open the holdout (CAP-RES-07,
    BR-DAT-07; review fix 2026-10-22)."""
    access_id: str
    experiment_id: str
    artifact_hash: str
    holdout_start: date
    holdout_end: date
    token: str = field(default="", repr=False)


_ISSUED_GRANTS: set[HoldoutGrant] = set()


def issue_grant(access_id: str, experiment_id: str, artifact_hash: str, holdout_start: date,
                holdout_end: date) -> HoldoutGrant:
    """Create and remember a grant. Called only by the registry after the GRANTED row is written."""
    import secrets
    g = HoldoutGrant(access_id, experiment_id, artifact_hash, holdout_start, holdout_end, secrets.token_hex(16))
    _ISSUED_GRANTS.add(g)
    return g


def grant_is_issued(g: object) -> bool:
    return isinstance(g, HoldoutGrant) and bool(g.token) and g in _ISSUED_GRANTS


@dataclass
class DatasetView:
    """The bounded data a run may read. ``until`` is the last session any part of the run can see."""
    series: dict[str, BarSeries]
    until: date
    label: str
    holdout: tuple[date, date] | None = None
    grant: HoldoutGrant | None = None
    _fingerprint: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        if self.holdout is not None and self.until >= self.holdout[0]:
            g = self.grant
            if not grant_is_issued(g) or (g.holdout_start, g.holdout_end) != self.holdout:
                raise HoldoutError(f"view {self.label} reaches into the untouched holdout starting {self.holdout[0]} "
                                   "without a holdout grant issued by the registry")
            if self.until > self.holdout[1]:
                raise HoldoutError("view extends beyond the registered holdout")
        self.series = {k: v.truncated(self.until) for k, v in sorted(self.series.items())}

    @property
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(self.series)

    def sessions(self) -> list[date]:
        days: set[date] = set()
        for s in self.series.values():
            days.update(s.dates)
        return sorted(days)

    def fingerprint(self) -> str:
        if not self._fingerprint:
            self._fingerprint = fingerprint_until(self.series, self.until)
        return self._fingerprint

    def describe(self) -> dict:
        return {"label": self.label, "until": self.until.isoformat(), "fingerprint": self.fingerprint(),
                "holdout": [d.isoformat() for d in self.holdout] if self.holdout else None,
                "holdout_access_id": self.grant.access_id if self.grant else None,
                "instruments": {k: {"sessions": len(v), "fingerprint": v.fingerprint()} for k, v in self.series.items()}}


def fingerprint_until(series: dict[str, BarSeries], until: date) -> str:
    """Content hash of exactly what a view ending at ``until`` contains (equals ``DatasetView.fingerprint``).
    Hashing data is not reading it for evaluation, so a manifest can name a holdout view before the grant."""
    return stable_hash({"until": until.isoformat(),
                        "series": {k: v.truncated(until).fingerprint() for k, v in sorted(series.items())}})


# ------------------------------------------------------- point-in-time store bridge
def record_bars(store, bars: Iterable[Bar], dataset_id: str, known_at_ns: int) -> int:
    """Write bars into the point-in-time store (``data.pit``), one observation per field and version."""
    from ..data.pit import Observation
    n = 0
    for b in bars:
        common = dict(dataset=dataset_id, entity_id=b.instrument_id, receipt_time_ns=known_at_ns,
                      available_time_ns=b.available_ns, source_ref=f"{b.source}#v{b.version}",
                      period_start=b.session_date.isoformat(), period_end=b.session_date.isoformat(), period_kind="DAY",
                      event_time_ns=b.end_ns, published_time_ns=b.available_ns,
                      # zero-padded so the store's string tie-break orders versions numerically (v10 after v9)
                      quality=("fixture",) if b.fixture else (), publication_id=f"bar#v{b.version:09d}")
        for name in BAR_FIELDS:
            unit = "shares" if name == "volume" else f"{b.currency}/share"
            store.record(Observation(field=name, unit=unit, value=getattr(b, name),
                                     currency=None if name == "volume" else b.currency, **common), known_at_ns)
        store.record(Observation(field="interval_ns", unit="ns", value_text=f"{b.start_ns}/{b.end_ns}", **common), known_at_ns)
        n += 1
    return n


def _assemble(instrument_id: str, rows_by_field: dict[str, list], fixture_flag: bool | None = None) -> list[Bar]:
    keyed: dict[tuple[str, str], dict] = {}
    for name, rows in rows_by_field.items():
        for r in rows:
            k = (r["period_end"], r["publication_id"])
            keyed.setdefault(k, {"available": r["available_time_ns"], "source_ref": r["source_ref"],
                                 "quality": r["quality"], "currency": r["currency"]})
            keyed[k][name] = r["value"] if r["value"] is not None else r["value_text"]
            if r["currency"]:
                keyed[k]["currency"] = r["currency"]
    out = []
    for (day, pub), v in sorted(keyed.items()):
        if any(name not in v for name in BAR_FIELDS + ("interval_ns",)):
            continue  # incomplete publication: never fabricate a field
        start, end = (int(x) for x in v["interval_ns"].split("/"))
        source, _, ver = v["source_ref"].rpartition("#v")
        out.append(Bar(instrument_id, date.fromisoformat(day), start, end, v["available"],
                       Fixed.parse(v["open"]), Fixed.parse(v["high"]), Fixed.parse(v["low"]), Fixed.parse(v["close"]),
                       Fixed.parse(v["volume"]), v["currency"] or "USD", source, "fixture" in v["quality"], int(ver)))
    return out


def bars_as_of_pit(store, dataset_id: str, instrument_id: str, t_ns: int, *, knowledge_cutoff_ns: int | None = None) -> list[Bar]:
    """Bars exactly as ``ResearchStore.as_of`` returns them at ``t_ns`` (latest publication per session)."""
    rows = {name: store.as_of(instrument_id, name, t_ns, dataset=dataset_id, knowledge_cutoff_ns=knowledge_cutoff_ns)
            for name in BAR_FIELDS + ("interval_ns",)}
    bars = _assemble(instrument_id, rows)
    latest: dict[date, Bar] = {}
    for b in bars:
        if b.session_date not in latest or b.version > latest[b.session_date].version:
            latest[b.session_date] = b
    return [latest[d] for d in sorted(latest)]


def load_series(store, dataset_id: str, instrument_ids: Iterable[str], cutoff_ns: int) -> dict[str, BarSeries]:
    """Every bar version with ``available_time <= cutoff_ns`` under current knowledge, as BarSeries.

    Loading every version (not just the latest) is what lets the engine apply
    as-of semantics per decision: a correction published after a decision is
    never visible to it."""
    from ..data.pit import OPEN_END
    out = {}
    for inst in instrument_ids:
        rows: dict[str, list] = {}
        for name in BAR_FIELDS + ("interval_ns",):
            rows[name] = store.conn.execute(
                "SELECT * FROM observation WHERE dataset=? AND entity_id=? AND field=? AND available_time_ns<=? "
                "AND known_to_ns=? ORDER BY period_end, publication_id", (dataset_id, inst, name, cutoff_ns, OPEN_END)).fetchall()
        out[inst] = BarSeries(inst, _assemble(inst, rows))
    return out
