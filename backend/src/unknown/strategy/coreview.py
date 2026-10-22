"""Read-only view of the core database for a strategy runner (ADR-0017 #4: "reads current and pending exposure
from the core").

The runner never writes to the core database. It opens it ``mode=ro`` (``store.db.connect(readonly=True)``),
exactly as the terminal API's read models do (``api/queries.py``), and reads inside one read transaction so
every figure of a snapshot comes from the same committed state. The core keeps writing concurrently (WAL);
nothing read here is trusted for authority: every intent is re-checked by the core's pretrade pipeline.

The snapshot holds what algorithm A15 needs for one session:

* the session (auto-detected when not pinned: the account's single active session), its mandate's policy hash
  (an intent must carry it) and currency, and the account mode;
* per instrument: owned quantity of the session's ledger owner, unreserved quantity (owned minus active quantity
  holds), and the leaves of the session's open BUY and SELL orders, with each open order's state and principal;
* the session's liquid value and available cash (``core.ledger``), and the active policy's fee model for the
  reservation envelope;
* the registered instrument master rows.

``commands`` returns stored outcomes by command id, which is how a runner whose local journal was lost learns
what the core already executed instead of submitting again.

Pending exposure (BR-DAT-02). An order is pending while it is in one of the core's open states
(``core.state.OPEN_ORDER_STATES``): RESERVED, SUBMIT_PENDING, ACKNOWLEDGED, PARTIALLY_FILLED, CANCEL_PENDING and
UNKNOWN. Its leaves (remaining quantity) count, whoever placed it in the session: a pending buy as if it fills
(``Holding.pending_buy``), a pending sell as already sold (``Holding.pending_sell``). FILLED, CANCELED, REJECTED and
EXPIRED orders have no leaves and count nothing. The core's pretrade uses the same states: the CONCENTRATION check
adds the leaves of the session's open buys in the instrument, GROSS_EXPOSURE adds every open buy of the account,
and a sell reserves its quantity. The runner adds two gates on top (``runner._decide``): no new intent for an
instrument with an UNKNOWN or CANCEL_PENDING order in the session, and at most one live intent per session,
artifact and instrument."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..core import ledger, state
from ..domain.errors import Rejected
from ..domain.fixed import Fixed, Rounding
from ..domain.instruments import Instrument
from ..domain.money import Money, currency
from ..policy.model import FeeModel
from ..risk import fees as fee_model
from ..store.db import Tx, connect
from .sizing import Holding

ACTIVE_SESSION_STATES = ("RUNNING", "SUSPENDED", "WINDING_DOWN")
PENDING_STATES = state.OPEN_ORDER_STATES          # BR-DAT-02: the order states whose leaves are pending exposure
UNRESOLVED_STATES = ("UNKNOWN", "CANCEL_PENDING")  # no new intent for the instrument while one of these is open


class CoreUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CoreSnapshot:
    account_key: str
    account_mode: str
    session_id: str | None
    session_state: str | None
    policy_hash: str | None            # the session mandate's policy hash
    currency: str | None
    ledger_scale: int
    instruments: dict[str, Instrument | None]
    holdings: dict[str, Holding]
    liquid_value: Fixed
    available_cash: Fixed
    fees: FeeModel | None
    reason: str = ""
    # per instrument: the session's open orders (order_id, state, side, leaves_qty, principal_id, principal_role)
    open_orders: dict[str, tuple[dict, ...]] = field(default_factory=dict)

    def fee_envelope(self, side: str, quantity: Fixed, limit: Fixed) -> Fixed:
        """The core's reservation envelope for a BUY (``risk.fees.envelope`` under the active policy)."""
        if side != "BUY" or self.fees is None or self.currency is None:
            return Fixed(0, self.ledger_scale)
        notional = (quantity * limit).rescale(self.ledger_scale, Rounding.CEILING)
        return fee_model.envelope(self.fees, quantity, Money(self.currency, notional)).amount

    def equity(self, marks: dict[str, Fixed | None]) -> Fixed:
        """Session equity at the strategy's marks: liquid value + owned quantity x mark (the backtest's
        ``equity_at_marks``). An instrument with a position but no mark counts at zero, as in the engine."""
        total = self.liquid_value
        for i, h in self.holdings.items():
            m = marks.get(i)
            if h.owned.is_positive() and m is not None:
                total = total + h.owned * m
        return total

    def to_json(self) -> dict:
        return {"account_key": self.account_key, "account_mode": self.account_mode, "session_id": self.session_id,
                "session_state": self.session_state, "policy_hash": self.policy_hash, "currency": self.currency,
                "holdings": {i: {"owned": h.owned.to_str(), "unreserved": h.unreserved.to_str(),
                                 "pending_buy": h.pending_buy.to_str(), "pending_sell": h.pending_sell.to_str()}
                             for i, h in sorted(self.holdings.items())},
                "registered": sorted(i for i, v in self.instruments.items() if v is not None),
                "liquid_value": self.liquid_value.to_str(), "available_cash": self.available_cash.to_str()}


class CoreView:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def _open(self):
        if not self.db_path.is_file():
            raise CoreUnavailable(f"core database {self.db_path} does not exist")
        conn = connect(self.db_path, readonly=True)
        conn.execute("BEGIN")       # one read snapshot for the whole view
        return conn

    def snapshot(self, account_key: str, session_id: str | None, instrument_ids: tuple[str, ...] | list[str]) -> CoreSnapshot:
        conn = self._open()
        try:
            tx = Tx(conn)
            try:
                acct = state.account(tx, account_key)
            except Rejected as exc:
                raise CoreUnavailable(f"{exc.code}: {account_key}") from None
            insts = {i: state.instrument(tx, i) for i in instrument_ids}
            sess = None
            reason = ""
            if session_id is not None:
                try:
                    sess = state.session(tx, session_id)
                except Rejected:
                    reason = f"UNKNOWN_SESSION:{session_id}"
                if sess is not None and sess["account_key"] != account_key:
                    sess, reason = None, f"SESSION_ACCOUNT_MISMATCH:{session_id}"
            else:
                active = state.sessions(tx, account_key, ACTIVE_SESSION_STATES)
                if len(active) == 1:
                    sess = active[0]
                else:
                    reason = "NO_ACTIVE_SESSION" if not active else f"AMBIGUOUS_ACTIVE_SESSIONS:{len(active)}"
            try:
                fees = state.active_policy(tx, account_key).fees
            except Rejected:
                fees = None
            if sess is None:
                return CoreSnapshot(account_key, acct["mode"], None, None, None, None, 6, insts, {}, Fixed(0), Fixed(0),
                                    fees, reason)
            m = state.mandate(tx, sess["mandate_hash"])
            ccy = m.currency
            scale = currency(ccy).ledger_scale
            owner = ledger.session_owner(sess["session_id"])
            open_orders = state.orders(tx, account_key, session_id=sess["session_id"], open_only=True)
            holdings: dict[str, Holding] = {}
            per_instrument = {i: tuple({k: o.get(k) for k in ("order_id", "state", "side", "leaves_qty", "principal_id",
                                                               "principal_role")}
                                       for o in open_orders if o["instrument_id"] == i) for i in instrument_ids}
            for i in instrument_ids:
                owned = ledger.owned_quantity(tx, account_key, owner, i)
                held = ledger.active_quantity_holds(tx, account_key, owner, i)
                pb = sum((Fixed.parse(o["leaves_qty"]) for o in open_orders if o["instrument_id"] == i and o["side"] == "BUY"),
                         Fixed(0))
                ps = sum((Fixed.parse(o["leaves_qty"]) for o in open_orders if o["instrument_id"] == i and o["side"] == "SELL"),
                         Fixed(0))
                holdings[i] = Holding(owned=owned, unreserved=owned - held, pending_buy=pb, pending_sell=ps)
            return CoreSnapshot(
                account_key=account_key, account_mode=acct["mode"], session_id=sess["session_id"], session_state=sess["state"],
                policy_hash=m.policy_hash, currency=ccy, ledger_scale=scale, instruments=insts, holdings=holdings,
                liquid_value=ledger.liquid_value(tx, account_key, owner, ccy),
                available_cash=ledger.available_cash(tx, account_key, owner, ccy), fees=fees, reason=reason,
                open_orders=per_instrument)
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def transmission_context(self, account_key: str, instrument_id: str) -> tuple[Instrument | None, int | None, str]:
        """(instrument, the active policy's quote_max_age_ms, account mode) for pricing an intent at transmission."""
        conn = self._open()
        try:
            tx = Tx(conn)
            try:
                mode = state.account(tx, account_key)["mode"]
            except Rejected as exc:
                raise CoreUnavailable(f"{exc.code}: {account_key}") from None
            try:
                max_age = state.active_policy(tx, account_key).limits.quote_max_age_ms
            except Rejected:
                max_age = None
            return state.instrument(tx, instrument_id), max_age, mode
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def order(self, order_id: str) -> dict | None:
        """The core's order row, or None."""
        conn = self._open()
        try:
            r = conn.execute("SELECT row FROM orders WHERE order_id=?", (order_id,)).fetchone()
            return json.loads(r["row"]) if r is not None else None
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def commands(self, command_ids: list[str]) -> dict[str, dict]:
        """{command id: {status, result, principal_id, principal_role}} for the ids the core has stored."""
        if not command_ids:
            return {}
        conn = self._open()
        try:
            out = {}
            for cid in command_ids:
                r = conn.execute("SELECT status, result, principal_id, principal_role FROM commands WHERE command_id=?",
                                 (cid,)).fetchone()
                if r is not None:
                    out[cid] = {"status": r["status"], "result": json.loads(r["result"]), "principal_id": r["principal_id"],
                                "principal_role": r["principal_role"]}
                    ev = conn.execute("SELECT payload FROM events WHERE command_id=? AND type='StrategyDecisionRecorded'",
                                      (cid,)).fetchone()
                    if ev is not None:
                        out[cid]["decision"] = json.loads(ev["payload"])["info"]
            return out
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
