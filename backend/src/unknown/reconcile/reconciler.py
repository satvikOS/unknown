"""Reconciliation driver (REC-01).

Algorithm (book Ch. 9, with the review's resolutions):
1. drain queued broker events;
2. ingest executions and commissions since the last clean watermark (overlap
   is deduplicated by the core on scoped execution keys);
3. take the account snapshot and the open-order list;
4. fetch executions again; if anything new arrived, ingest and repeat (at most
   ``max_rounds``) so the snapshot and the execution set describe one cut;
5. look up every local open order the broker no longer lists;
6. send one ``reconciliation_result``. Any broker read failure produces an
   INCOMPLETE run: absence in partial data never proves anything. So does any
   other failure before the result (for example a broker event whose command
   raises while queued events are drained): the run is recorded as INCOMPLETE,
   and the unapplied events stay queued (review finding 58).
"""
from __future__ import annotations

from ..broker.port import BrokerPort
from ..core.desk import Desk, Result
from ..gateway.runner import GatewayRunner, commission_payload, execution_payload


class Reconciler:
    def __init__(self, desk: Desk, broker: BrokerPort, account_key: str, *, max_rounds: int = 3):
        self.desk = desk
        self.broker = broker
        self.account_key = account_key
        self.runner = GatewayRunner(desk, broker, account_key)
        self.max_rounds = max_rounds

    def _ingest(self, batch) -> int:
        for e in batch.executions:
            self.desk.run_system("broker_execution", execution_payload(self.account_key, e))
        for c in batch.commissions:
            self.desk.run_system("broker_commission", commission_payload(self.account_key, c))
        return len(batch.executions)

    def run(self) -> Result:
        key = self.account_key
        run_id = self.desk.ids.new("rec-")
        try:
            self.runner.drain_events()
            batch = self.broker.executions_since(self.runner.last_watermark())
            self._ingest(batch)
            executions_complete = batch.complete
            watermark = batch.watermark
            snapshot = open_rows = None
            for _ in range(self.max_rounds):
                snapshot = self.broker.account_snapshot()
                open_rows, open_complete = self.broker.open_orders()
                after = self.broker.executions_since(watermark)
                executions_complete = executions_complete and after.complete
                if not after.executions and not after.commissions:
                    break
                self._ingest(after)
                watermark = after.watermark
                self.runner.drain_events()
            else:
                executions_complete = False  # never converged: treat as incomplete
            open_ids = {r.client_order_id for r in open_rows if r.client_order_id}
            lookups = {}
            for r in self.desk.store.query("SELECT order_id, state FROM orders WHERE account_key=? AND state IN "
                                           "('SUBMIT_PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_PENDING','UNKNOWN','RESERVED')",
                                           (key,)):
                if r["order_id"] in open_ids:
                    continue
                lk = self.broker.lookup(r["order_id"])
                lookups[r["order_id"]] = {
                    "found": lk.found, "status": lk.status.value if lk.status else None, "broker_order_id": lk.broker_order_id,
                    "filled_quantity": lk.filled_quantity.to_str() if lk.filled_quantity is not None else None,
                    "remaining_quantity": lk.remaining_quantity.to_str() if lk.remaining_quantity is not None else None}
            payload = {
                "account_key": key, "run_id": run_id, "executions_complete": executions_complete, "watermark": watermark,
                "snapshot": {"as_of_ns": snapshot.as_of_ns, "complete": snapshot.complete, "server_time_ns": snapshot.server_time_ns,
                             "cash": [{"currency": c.currency, "settled": c.settled.to_str(), "total": c.total.to_str()} for c in snapshot.cash],
                             "positions": [{"instrument_id": p.instrument_id, "provider_ref": p.provider_ref,
                                            "quantity": p.quantity.to_str(),
                                            "average_cost": p.average_cost.to_str() if p.average_cost is not None else None,
                                            "currency": p.currency} for p in snapshot.positions]},
                "open_orders": {"complete": open_complete, "rows": [{
                    "client_order_id": o.client_order_id, "order_ref": o.order_ref, "broker_order_id": o.broker_order_id,
                    "instrument_id": o.instrument_id, "side": o.side, "status": o.status.value,
                    "filled_quantity": o.filled_quantity.to_str(), "remaining_quantity": o.remaining_quantity.to_str()} for o in open_rows]},
                "lookups": lookups,
            }
        except Exception as exc:  # broker read failure, or an event or ingest command that raised
            payload = {"account_key": key, "run_id": run_id, "executions_complete": False, "watermark": None,
                       "snapshot": {"as_of_ns": self.desk.clock.now_ns(), "complete": False, "cash": [], "positions": [],
                                    "error": f"{type(exc).__name__}: {exc}"},
                       "open_orders": {"complete": False, "rows": []}, "lookups": {}}
        return self.desk.run_system("reconciliation_result", payload, command_id=run_id.replace("rec-", "recon.", 1))
