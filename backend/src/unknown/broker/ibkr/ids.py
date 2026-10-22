"""Crash-safe IBKR order-id allocation (research record R3 ``ibkr-order-identity-and-ownership``).

IBKR's orderId is client-managed and must be strictly increasing and greater
than every earlier id used by that client; error 103 "Duplicate order ID"
fires otherwise. R3 analytics: ``next_order_id = max(nextValidId_last,
max_persisted_order_id(username, clientId) + 1)``.

Every allocation, and the fact that ``placeOrder`` is about to be called for
it, is written to a JSON state file with fsync (file and directory) BEFORE the
caller proceeds. A crash after ``placeOrder`` therefore never reuses an id, and
``lookup`` can tell "never transmitted" (conclusive) from "transmitted, no
trace" (ambiguous). The file lives outside the repository and stores only a
hash of the account id.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STATE_VERSION = 1
NS_PER_DAY = 86_400 * 10**9


class OrderIdStateError(RuntimeError):
    """The persisted allocator state is unreadable or belongs to another account/client."""


@dataclass(frozen=True)
class Allocation:
    client_order_id: str
    order_id: int
    allocated_at_ns: int
    transmit_attempted_at_ns: int | None = None
    perm_id: int = 0
    order_ref: str | None = None
    instrument_id: str | None = None
    provider_ref: str | None = None
    side: str | None = None
    quantity: str | None = None
    superseded_order_ids: tuple[int, ...] = ()

    @property
    def transmitted(self) -> bool:
        return self.transmit_attempted_at_ns is not None


def account_fingerprint(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


class OrderIdStore:
    """Persisted allocator for one (account, clientId) pair; thread-safe."""

    def __init__(self, state_dir: Path, *, account_id: str, client_id: int, now_ns: Callable[[], int],
                 retention_days: int = 14):
        self._dir = Path(state_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fp = account_fingerprint(account_id)
        self.path = self._dir / f"ibkr-order-ids-{fp}-c{client_id}.json"
        self._now_ns = now_ns
        self._lock = threading.Lock()
        self._scope = {"account_sha256_16": fp, "client_id": client_id}
        self.existed_before = self.path.exists()
        if self.existed_before:
            self._state = self._read()
        else:
            self._state = {"version": STATE_VERSION, "scope": self._scope, "created_at_ns": now_ns(),
                           "max_order_id": 0, "max_transmitted_order_id": 0, "allocations": {},
                           "history_gap_acks": []}
            self._persist()
        self._prune(retention_days)

    # ------------------------------------------------------------- reading
    def _read(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Never silently reset: a fresh state could reuse an order id (R3 acceptance criterion 1).
            raise OrderIdStateError(f"cannot read IBKR order-id state {self.path}: {exc}") from None
        if state.get("version") != STATE_VERSION:
            raise OrderIdStateError(f"unsupported order-id state version {state.get('version')!r}")
        if state.get("scope") != self._scope:
            raise OrderIdStateError("order-id state belongs to a different account or client id")
        return state

    def _prune(self, retention_days: int) -> None:
        cutoff = self._now_ns() - retention_days * NS_PER_DAY
        with self._lock:
            allocs = self._state["allocations"]
            stale = [cid for cid, a in allocs.items() if a["allocated_at_ns"] < cutoff]
            if stale:
                for cid in stale:
                    del allocs[cid]   # max ids are kept, so pruning never lowers the next id
                self._persist()

    @staticmethod
    def _to_alloc(cid: str, a: dict) -> Allocation:
        return Allocation(client_order_id=cid, order_id=a["order_id"], allocated_at_ns=a["allocated_at_ns"],
                          transmit_attempted_at_ns=a.get("transmit_attempted_at_ns"), perm_id=a.get("perm_id", 0),
                          order_ref=a.get("order_ref"), instrument_id=a.get("instrument_id"),
                          provider_ref=a.get("provider_ref"), side=a.get("side"), quantity=a.get("quantity"),
                          superseded_order_ids=tuple(a.get("superseded_order_ids", ())))

    def get(self, client_order_id: str) -> Allocation | None:
        with self._lock:
            a = self._state["allocations"].get(client_order_id)
            return None if a is None else self._to_alloc(client_order_id, a)

    def by_order_id(self, order_id: int) -> Allocation | None:
        with self._lock:
            for cid, a in self._state["allocations"].items():
                if a["order_id"] == order_id:
                    return self._to_alloc(cid, a)
        return None

    def by_perm_id(self, perm_id: int) -> Allocation | None:
        if not perm_id:
            return None
        with self._lock:
            for cid, a in self._state["allocations"].items():
                if a.get("perm_id") == perm_id:
                    return self._to_alloc(cid, a)
        return None

    @property
    def max_order_id(self) -> int:
        with self._lock:
            return self._state["max_order_id"]

    # ------------------------------------------------------------- writing
    def _next_id(self, next_valid_id: int) -> tuple[int, str | None]:
        floor = self._state["max_order_id"] + 1
        anomaly = None
        if next_valid_id < floor and self._state["max_order_id"] > 0:
            # R3 analytics id_regression_anomaly; degraded behaviour: use persisted + 1 and log.
            anomaly = f"ID_REGRESSION: nextValidId {next_valid_id} < persisted max + 1 ({floor}); using {floor}"
        return max(next_valid_id, floor), anomaly

    def allocate(self, client_order_id: str, next_valid_id: int) -> tuple[Allocation, str | None]:
        """Return the existing allocation, or persist a new one before returning it."""
        if type(next_valid_id) is not int or next_valid_id < 0:
            raise ValueError("next_valid_id must be a non-negative int received from the host")
        with self._lock:
            existing = self._state["allocations"].get(client_order_id)
            if existing is not None:
                return self._to_alloc(client_order_id, existing), None
            order_id, anomaly = self._next_id(next_valid_id)
            self._state["allocations"][client_order_id] = {"order_id": order_id, "allocated_at_ns": self._now_ns()}
            self._state["max_order_id"] = order_id
            self._persist()
            return self._to_alloc(client_order_id, self._state["allocations"][client_order_id]), anomaly

    def prepare_transmit(self, client_order_id: str, next_valid_id: int, details: dict) -> tuple[Allocation, str | None]:
        """Allocate if needed and durably mark ``placeOrder`` as about to be called.

        An allocation that was never transmitted but is no longer above every
        transmitted id (another order went first) is replaced by a fresh id: the
        old id never reached the host, so abandoning it is safe, and sending it
        would only earn error 103."""
        allocation, anomaly = self.allocate(client_order_id, next_valid_id)
        with self._lock:
            a = self._state["allocations"][client_order_id]
            if a.get("transmit_attempted_at_ns") is not None:
                raise OrderIdStateError(f"order id {a['order_id']} for {client_order_id} was already transmitted")
            if a["order_id"] <= self._state["max_transmitted_order_id"] or a["order_id"] < next_valid_id:
                fresh, anomaly2 = self._next_id(next_valid_id)
                a.setdefault("superseded_order_ids", []).append(a["order_id"])
                a["order_id"] = fresh
                self._state["max_order_id"] = max(self._state["max_order_id"], fresh)
                anomaly = anomaly or anomaly2
            a.update({k: v for k, v in details.items() if k in ("order_ref", "instrument_id", "provider_ref", "side",
                                                                 "quantity")})
            a["transmit_attempted_at_ns"] = self._now_ns()
            self._state["max_transmitted_order_id"] = max(self._state["max_transmitted_order_id"], a["order_id"])
            self._persist()
            return self._to_alloc(client_order_id, a), anomaly

    def record_perm_id(self, order_id: int, perm_id: int) -> None:
        if not perm_id:
            return
        with self._lock:
            for a in self._state["allocations"].values():
                if a["order_id"] == order_id and a.get("perm_id") != perm_id:
                    a["perm_id"] = perm_id
                    self._persist()
                    return

    def acknowledge_history_gap(self, through_day: str, by: str, note: str) -> None:
        with self._lock:
            self._state["history_gap_acks"].append({"through_day": through_day, "by": by, "note": note,
                                                    "at_ns": self._now_ns()})
            self._persist()

    def history_gap_acked_through(self) -> str | None:
        with self._lock:
            days = [a["through_day"] for a in self._state["history_gap_acks"]]
        return max(days) if days else None

    def _persist(self) -> None:
        data = json.dumps(self._state, sort_keys=True, indent=1).encode("utf-8")
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        dir_fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
