"""Feature rows under the approved feature contract (BR-DAT-18; book ch. 5 "Record warm-up status explicitly. The
strategy must reject an input vector whose feature version or validity window differs from the approved model
contract", ch. 11 feature contract, ch. 26 "Resume only after warm-up and feature-state consistency checks").

The contract is the spec's ``features[]`` (schema v2): id, version, availability basis, freshness budget, warm-up
observations (an upper bound) and ``validity_window_s``. For one decision, ``feature_rows`` builds one row per
instrument and declared feature from the bars visible at the decision instant and the strategy's own feature
values (``strategy.feature_values``), with:

* ``valid_from_ns`` = the availability time of the feature's newest input, ``valid_until_ns`` = that plus
  ``min(validity_window_s, freshness_budget_s)``: the contract's validity window (book ch. 5), bounded by its
  freshness budget (ch. 11 "Freshness is checked both at signal creation and before order transmission"). A row is
  usable at an instant inside [valid_from, valid_until): at the decision, and again at transmission (the strategy
  runner withholds an intent whose inputs are no longer valid);
* ``warm_up_state`` COLD (no observation), WARMING (fewer than the implementation's warm-up, which may not exceed
  the contract's), WARM;
* a value, or exactly one enumerated ``missing_reason`` (``FEATURE_MISSINGNESS``):

  ``NOT_REPORTED``    the source has no observation for the instrument, or the session's bar is partial;
  ``PROVIDER_DELAY``  the newest input is older than the decision session: its bar is not available yet;
  ``SOURCE_GAP``      a calendar session inside the warm-up span has no observation (a hole in the history);
  ``NOT_APPLICABLE``  the implementation produces no value for this feature;
  ``QUARANTINED``     the computed value failed validation (not finite).

The implementation's feature ids, versions and warm-up are checked against the contract when a strategy is built
from a v2 spec (``strategies.build``, which the study's backtests and the strategy runner use): a different version,
or a warm-up longer than the contract's, is refused before anything runs.
Feature values are floats (statistics); a row carries them as ``repr`` strings so a recomputation compares exactly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from ..domain.calendar import BusinessCalendar
from .bars import Bar

FEATURE_MISSINGNESS = ("NOT_REPORTED", "PROVIDER_DELAY", "SOURCE_GAP", "NOT_APPLICABLE", "QUARANTINED")
WARM_UP_STATES = ("COLD", "WARMING", "WARM")
NS_PER_S = 1_000_000_000


@dataclass(frozen=True)
class FeatureRow:
    feature_id: str
    version: int
    instrument_id: str
    input_session: date | None        # session of the newest input
    valid_from_ns: int | None         # availability of the newest input
    valid_until_ns: int | None
    warm_up_state: str
    observations: int
    min_observations: int
    value: str | None                 # repr of the float, exact on recomputation
    missing_reason: str | None

    def __post_init__(self) -> None:
        if self.value is not None and self.missing_reason is not None:
            raise ValueError("a feature row has a value or a missing reason, never both")
        if self.value is None and self.missing_reason is None and self.warm_up_state == "WARM":
            raise ValueError("a warm feature row has a value or a missing reason")   # only warm-up may leave neither
        if self.missing_reason is not None and self.missing_reason not in FEATURE_MISSINGNESS:
            raise ValueError(f"unknown missing reason {self.missing_reason}")
        if self.warm_up_state not in WARM_UP_STATES:
            raise ValueError(f"unknown warm-up state {self.warm_up_state}")

    def usable_at(self, t_ns: int) -> bool:
        return (self.value is not None and self.valid_from_ns is not None and self.valid_until_ns is not None
                and self.valid_from_ns <= t_ns < self.valid_until_ns)

    def to_json(self) -> dict:
        return {"feature_id": self.feature_id, "version": self.version, "instrument_id": self.instrument_id,
                "input_session": self.input_session.isoformat() if self.input_session else None,
                "valid_from_ns": self.valid_from_ns, "valid_until_ns": self.valid_until_ns,
                "warm_up_state": self.warm_up_state, "observations": self.observations,
                "min_observations": self.min_observations, "value": self.value, "missing_reason": self.missing_reason}


def check_contract(spec, implementation) -> None:
    """Refuse an implementation whose feature ids or versions differ from the v2 spec's contract (book ch. 5)."""
    from .common import SpecError
    if getattr(spec, "schema_version", 1) < 2:
        return                                   # a v1 spec has no validity contract: the legacy behaviour
    declared = {f["id"]: f["version"] for f in spec.features}
    implemented = dict(getattr(implementation, "FEATURES", {}))
    if declared != implemented:
        raise SpecError(f"feature contract mismatch: the spec approves {declared}, the implementation "
                        f"{type(implementation).__name__} computes {implemented}")
    for f in spec.features:
        need = implementation.feature_warm_up(f["id"]) if hasattr(implementation, "feature_warm_up") else None
        if need is not None and need > f["warm_up_observations"]:
            raise SpecError(f"feature {f['id']} needs {need} observations to warm up, more than the contract's "
                            f"{f['warm_up_observations']}")


def _gap(bars: list[Bar], span: int, calendar: BusinessCalendar) -> date | None:
    """The first calendar session without a bar among the last ``span`` sessions ending at the newest bar."""
    if len(bars) < 2 or span < 2:
        return None
    from datetime import timedelta

    from ..domain.calendar import CalendarHorizonError
    have = {b.session_date for b in bars}
    newest = bars[-1].session_date
    day, seen = newest, 0
    while seen < span - 1 and day > bars[0].session_date:
        day = day - timedelta(days=1)
        try:
            business = calendar.is_business_day(day)
        except CalendarHorizonError:
            return None         # before the calendar's sourced data: no session is known to be missing there
        if business:
            seen += 1
            if day not in have:
                return day
    return None


def feature_rows(spec, implementation, instrument_id: str, bars: list[Bar], *, decision_session: date,
                 session_close_ns: int | None, calendar: BusinessCalendar) -> list[FeatureRow]:
    """The contract's rows for one instrument at one decision (see the module docstring)."""
    values = implementation.feature_values(instrument_id) if hasattr(implementation, "feature_values") else {}
    rows = []
    for f in spec.features:
        need = implementation.feature_warm_up(f["id"]) if hasattr(implementation, "feature_warm_up") else None
        min_obs = need if need is not None else f["warm_up_observations"]
        n = len(bars)
        warm = "COLD" if n == 0 else ("WARMING" if n < min_obs else "WARM")
        newest = bars[-1] if bars else None
        valid_from = newest.available_ns if newest else None
        window = f.get("validity_window_s")
        if window is not None and f.get("freshness_budget_s") is not None:
            window = min(window, f["freshness_budget_s"])           # the freshness budget bounds the validity window
        valid_until = valid_from + window * NS_PER_S if valid_from is not None and window is not None else None
        missing = None
        if newest is None:
            missing = "NOT_REPORTED"
        elif newest.session_date < decision_session:
            missing = "PROVIDER_DELAY"
        elif session_close_ns is not None and newest.end_ns < session_close_ns:
            missing = "NOT_REPORTED"                                  # a partial bar is not the completed session
        elif _gap(bars, max(min_obs, 2), calendar) is not None:
            missing = "SOURCE_GAP"
        value = None
        if missing is None:
            if f["id"] not in values:
                missing = "NOT_APPLICABLE"
            else:
                x = values[f["id"]]
                if x is None:
                    # not defined yet: allowed only while warming up (e.g. a volatility before two returns)
                    missing = None if warm != "WARM" else "NOT_REPORTED"
                elif not isinstance(x, float) or not math.isfinite(x):
                    missing = "QUARANTINED"
                else:
                    value = repr(x)
        rows.append(FeatureRow(f["id"], f["version"], instrument_id, newest.session_date if newest else None, valid_from,
                               valid_until, warm, n, min_obs, value, missing))
    return rows
