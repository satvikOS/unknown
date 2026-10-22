"""Transactional, single-currency SIMULATOR for the book's accounting examples.

Not a broker adapter, tax ledger, authentication system or production risk engine.
All amounts are integer USD cents; all quantities are whole shares. Commands are
durably deduplicated. Actual broker anomalies require an exception journal that
this intentionally constrained simulator does not implement.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


class Rejected(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 10**15:
        raise Rejected("invalid integer or outside reference range")
    return value


def identity(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        raise Rejected("invalid identifier")
    return value


class Engine:
    def __init__(self, path: str | Path, initial_cash: int = 0):
        integer(initial_cash)
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, command_id TEXT NOT NULL UNIQUE, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL)")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
            if row is None:
                state = {"mode": "SIMULATION", "halted": True, "version": 0,
                         "deposits": initial_cash, "unallocated": initial_cash,
                         "protected": 0, "realized_net": 0, "sessions": {},
                         "orders": {}, "fills": {}, "receivables": {},
                         "requests": {}, "loss_limit": 200, "drawdown_bps": 200}
                self.db.execute("INSERT INTO state VALUES (1, ?)", (canonical(state),))
            elif initial_cash:
                raise Rejected("existing database: initial_cash cannot be reapplied")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise
        # Reopening a process never establishes broker reconciliation.
        self.reconciled = False

    def close(self) -> None:
        self.db.close()

    def snapshot(self) -> dict:
        return json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def apply(self, command_id: str, kind: str, **payload: Any) -> dict:
        identity(command_id)
        request = canonical({"kind": kind, "payload": payload})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT payload,result FROM commands WHERE id=?", (command_id,)).fetchone()
            if old:
                if old[0] != request:
                    raise Rejected("command identity reused with changed payload")
                self.db.execute("COMMIT")
                return json.loads(old[1])
            state = self.snapshot()
            handler = getattr(self, "_cmd_" + kind, None)
            if handler is None:
                raise Rejected("unknown command")
            result = handler(state, payload)
            state["version"] += 1
            self.check(state)
            result = {"version": state["version"], **result}
            self.db.execute("UPDATE state SET body=? WHERE id=1", (canonical(state),))
            self.db.execute("INSERT INTO commands VALUES (?,?,?)", (command_id, request, canonical(result)))
            previous = self.db.execute("SELECT event_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            previous_hash = previous[0] if previous else "0" * 64
            event_hash = hashlib.sha256((previous_hash + command_id + request + canonical(result)).encode()).hexdigest()
            self.db.execute("INSERT INTO audit(command_id,previous_hash,event_hash) VALUES (?,?,?)", (command_id, previous_hash, event_hash))
            self.db.execute("COMMIT")
            if kind == "start":
                self.reconciled = True
            return result
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _session(state: dict, sid: str) -> dict:
        try:
            return state["sessions"][sid]
        except KeyError as exc:
            raise Rejected("unknown session") from exc

    @staticmethod
    def _open(order: dict) -> bool:
        return order["status"] in ("OPEN", "CANCEL_PENDING")

    @classmethod
    def reserved(cls, state: dict, sid: str) -> int:
        return sum(o["remaining"] * o["limit"] + o["fee_left"]
                   for o in state["orders"].values()
                   if o["session"] == sid and o["side"] == "BUY" and cls._open(o))

    @classmethod
    def check(cls, state: dict) -> None:
        total = state["unallocated"] + state["protected"]
        if min(state["unallocated"], state["protected"]) < 0:
            raise AssertionError("negative global policy bucket")
        for sid, session in state["sessions"].items():
            if session["cash"] < cls.reserved(state, sid):
                raise AssertionError("reservation exceeds cash")
            total += session["cash"]
            for pos in session["positions"].values():
                if min(pos["qty"], pos["cost"]) < 0:
                    raise AssertionError("negative position")
                total += pos["cost"]
            if session["state"] == "COMPLETED" and (session["positions"] or session["cash"]):
                raise AssertionError("completed session retains inventory/cash")
        for rec in state["receivables"].values():
            if not rec["settled"]:
                total += rec["amount"]
        if total != state["deposits"] + state["realized_net"]:
            raise AssertionError("economic conservation failure")
        for order in state["orders"].values():
            if not 0 <= order["remaining"] <= order["qty"]:
                raise AssertionError("invalid remaining quantity")
        for sid, session in state["sessions"].items():
            for symbol, pos in session["positions"].items():
                reserved_qty = sum(o["remaining"] for o in state["orders"].values()
                                   if o["session"] == sid and o["symbol"] == symbol
                                   and o["side"] == "SELL" and cls._open(o))
                if reserved_qty > pos["qty"]:
                    raise AssertionError("oversold inventory")

    def _cmd_start(self, state: dict, p: dict) -> dict:
        if p.get("owner") is not True or p.get("reconciled") is not True:
            raise Rejected("simulated owner and reconciliation assertions required")
        if any(x["state"] == "RISK_STOP" for x in state["sessions"].values()):
            raise Rejected("risk-stopped session requires review; no automatic reset")
        state["halted"] = False
        return {"status": "STARTED_SIMULATION"}

    def _cmd_stop(self, state: dict, p: dict) -> dict:
        state["halted"] = True
        for order in state["orders"].values():
            if order["side"] == "BUY" and self._open(order):
                order["status"] = "CANCEL_PENDING"
        return {"status": "STOP_LATCHED"}

    def _create(self, state: dict, sid: str, seed: int, goal: int) -> dict:
        if state["halted"] or not self.reconciled:
            raise Rejected("not started and reconciled")
        if sid in state["sessions"]:
            raise Rejected("session identity already exists")
        if any(s["state"] in ("RUNNING", "RISK_STOP") for s in state["sessions"].values()):
            raise Rejected("reference supports one active session")
        if goal <= seed or seed > state["unallocated"]:
            raise Rejected("invalid target or insufficient unallocated funds")
        state["unallocated"] -= seed
        state["sessions"][sid] = {"seed": seed, "goal": goal, "cash": seed,
                                  "positions": {}, "state": "RUNNING", "high_water": seed}
        return {"session": sid, "status": "RUNNING"}

    def _cmd_session(self, state: dict, p: dict) -> dict:
        return self._create(state, identity(p["session"]), integer(p["seed"], 1), integer(p["goal"], 1))

    def _cmd_order(self, state: dict, p: dict) -> dict:
        oid, sid, symbol = identity(p["order"]), identity(p["session"]), identity(p["symbol"])
        qty, limit, fee = integer(p["qty"], 1), integer(p["limit"], 1), integer(p["fee_budget"])
        session = self._session(state, sid)
        side = p["side"]
        if oid in state["orders"] or side not in ("BUY", "SELL"):
            raise Rejected("duplicate order or invalid side")
        if not self.reconciled or session["state"] not in ("RUNNING", "RISK_STOP"):
            raise Rejected("unreconciled or inactive session")
        if side == "BUY":
            if state["halted"] or session["state"] != "RUNNING":
                raise Rejected("new exposure blocked")
            if qty * limit + fee > session["cash"] - self.reserved(state, sid):
                raise Rejected("insufficient session cash")
        else:
            pos = session["positions"].get(symbol, {"qty": 0})
            held = sum(o["remaining"] for o in state["orders"].values()
                       if o["session"] == sid and o["symbol"] == symbol
                       and o["side"] == "SELL" and self._open(o))
            if qty > pos["qty"] - held or fee >= qty * limit:
                raise Rejected("unowned quantity or fees consume proceeds")
        state["orders"][oid] = {"session": sid, "symbol": symbol, "side": side,
                                "qty": qty, "remaining": qty, "limit": limit,
                                "fee_left": fee, "status": "OPEN"}
        return {"order": oid, "status": "OPEN_SIMULATED"}

    def _cmd_fill(self, state: dict, p: dict) -> dict:
        fid = identity(p["fill"])
        signature = canonical(p)
        if fid in state["fills"]:
            if state["fills"][fid] != signature:
                raise Rejected("execution identity changed")
            return {"fill": fid, "duplicate": True}
        oid = identity(p["order"])
        if oid not in state["orders"]:
            raise Rejected("unknown order")
        order = state["orders"][oid]
        qty, price, fee = integer(p["qty"], 1), integer(p["price"], 1), integer(p["fee"])
        if not self._open(order) or qty > order["remaining"] or fee > order["fee_left"]:
            raise Rejected("invalid simulated fill or exceeded fee envelope")
        buy = order["side"] == "BUY"
        if (buy and price > order["limit"]) or (not buy and price < order["limit"]):
            raise Rejected("fill violates price limit")
        session = self._session(state, order["session"])
        symbol = order["symbol"]
        notional = qty * price
        if buy:
            session["cash"] -= notional + fee
            pos = session["positions"].setdefault(symbol, {"qty": 0, "cost": 0})
            pos["qty"] += qty
            pos["cost"] += notional
            state["realized_net"] -= fee
        else:
            pos = session["positions"][symbol]
            cost = pos["cost"] if qty == pos["qty"] else pos["cost"] * qty // pos["qty"]
            pos["qty"] -= qty
            pos["cost"] -= cost
            if not pos["qty"]:
                del session["positions"][symbol]
            proceeds = notional - fee
            if proceeds < 0:
                raise Rejected("negative synthetic proceeds")
            state["receivables"][fid] = {"amount": proceeds, "owner": order["session"], "settled": False}
            state["realized_net"] += proceeds - cost
        order["remaining"] -= qty
        order["fee_left"] -= fee
        if not order["remaining"]:
            order["status"] = "FILLED"
            order["fee_left"] = 0
        state["fills"][fid] = signature
        return {"fill": fid, "duplicate": False}

    def _cmd_cancel(self, state: dict, p: dict) -> dict:
        order = state["orders"].get(p["order"])
        if order is None:
            raise Rejected("unknown order")
        if self._open(order):
            order["status"] = "CANCEL_PENDING"
        return {"status": order["status"]}

    def _cmd_canceled(self, state: dict, p: dict) -> dict:
        order = state["orders"].get(p["order"])
        if order is None or order["status"] not in ("CANCEL_PENDING", "CANCELED", "FILLED"):
            raise Rejected("no cancellation in progress")
        if order["status"] == "CANCEL_PENDING":
            order["status"] = "CANCELED"
            order["fee_left"] = 0
        return {"status": order["status"]}

    def _cmd_settle(self, state: dict, p: dict) -> dict:
        rec = state["receivables"].get(p["fill"])
        if rec is None or p.get("broker_confirmed") is not True:
            raise Rejected("unknown receivable or missing simulated confirmation")
        if not rec["settled"]:
            if rec["owner"] == "PROTECTED":
                state["protected"] += rec["amount"]
            else:
                self._session(state, rec["owner"])["cash"] += rec["amount"]
            rec["settled"] = True
        return {"status": "SETTLED"}

    def _cmd_complete(self, state: dict, p: dict) -> dict:
        sid = identity(p["session"])
        session = self._session(state, sid)
        request_id = sid + ":next"
        if session["state"] == "COMPLETED":
            return {"successor": request_id, "status": state["requests"][request_id]["state"]}
        if session["state"] != "RUNNING" or session["positions"] or p.get("fees_final") is not True:
            raise Rejected("session not flat, running and fee-finalized")
        if any(o["session"] == sid and self._open(o) for o in state["orders"].values()):
            raise Rejected("open orders remain")
        pending = [r for r in state["receivables"].values() if r["owner"] == sid and not r["settled"]]
        value = session["cash"] + sum(r["amount"] for r in pending)
        if value < session["goal"]:
            raise Rejected("net goal not reached")
        state["protected"] += session["cash"]
        session["cash"] = 0
        session["state"] = "COMPLETED"
        for rec in pending:
            rec["owner"] = "PROTECTED"
        state["requests"][request_id] = {"state": "WAITING_FUNDS", "seed": session["seed"], "goal": session["goal"]}
        if not state["halted"] and self.reconciled and state["unallocated"] >= session["seed"]:
            self._create(state, request_id, session["seed"], session["goal"])
            state["requests"][request_id]["state"] = "FUNDED"
        return {"successor": request_id, "status": state["requests"][request_id]["state"], "protected_value": value}

    def _cmd_mark(self, state: dict, p: dict) -> dict:
        prices = p["prices"]
        unrealized = 0
        for sid, session in state["sessions"].items():
            if session["state"] == "COMPLETED":
                continue
            value = session["cash"] + sum(r["amount"] for r in state["receivables"].values()
                                           if r["owner"] == sid and not r["settled"])
            for symbol, pos in session["positions"].items():
                price = integer(prices.get(symbol), 1)
                value += pos["qty"] * price
                unrealized += pos["qty"] * price - pos["cost"]
            session["high_water"] = max(value, session["high_water"])
            if value * 10000 <= session["high_water"] * (10000 - state["drawdown_bps"]):
                session["state"] = "RISK_STOP"
                state["halted"] = True
        if state["realized_net"] + unrealized <= -state["loss_limit"]:
            state["halted"] = True
        if state["halted"]:
            self._cmd_stop(state, {})
        return {"halted": state["halted"], "net_marked_pnl": state["realized_net"] + unrealized}

    def verify_audit(self) -> bool:
        previous = "0" * 64
        for command_id, prior, digest, payload, result in self.db.execute(
                "SELECT a.command_id,a.previous_hash,a.event_hash,c.payload,c.result FROM audit a JOIN commands c ON a.command_id=c.id ORDER BY a.seq"):
            actual = hashlib.sha256((previous + command_id + payload + result).encode()).hexdigest()
            if prior != previous or digest != actual:
                return False
            previous = digest
        return True


def demo() -> dict:
    engine = Engine(":memory:", 30000)
    engine.apply("start", "start", owner=True, reconciled=True)
    engine.apply("session", "session", session="A", seed=10000, goal=10500)
    engine.apply("buy", "order", order="B1", session="A", symbol="DEMO", side="BUY", qty=10, limit=900, fee_budget=10)
    engine.apply("buy-fill", "fill", fill="E1", order="B1", qty=10, price=900, fee=10)
    engine.apply("sell", "order", order="S1", session="A", symbol="DEMO", side="SELL", qty=10, limit=960, fee_budget=10)
    engine.apply("sell-fill", "fill", fill="E2", order="S1", qty=10, price=960, fee=10)
    result = engine.apply("complete", "complete", session="A", fees_final=True)
    engine.apply("settle", "settle", fill="E2", broker_confirmed=True)
    snapshot = engine.snapshot()
    answer = {"completion": result, "unallocated_cents": snapshot["unallocated"],
              "protected_settled_cents": snapshot["protected"], "net_pnl_cents": snapshot["realized_net"],
              "successor_cash_cents": snapshot["sessions"]["A:next"]["cash"], "audit_valid": engine.verify_audit()}
    engine.close()
    return answer


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2))
