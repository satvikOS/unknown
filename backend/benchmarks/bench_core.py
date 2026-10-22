"""Core latency benchmark (PRF-01, ADR-0002 reopen criterion).

Measures, on the machine it runs on, the wall-clock distribution of complete
command paths through the real Desk on a real file database:

* intent      submit_intent: pretrade checks + reservation + order + outbox, one durable commit
* send_begin  outbox_begin: claim + SUBMIT_PENDING, one durable commit
* fill        broker_execution: journal + lots + holds + settlement row, one durable commit
* commission  broker_commission: fee finalisation, one durable commit

Arrivals are issued back to back by one client (closed loop), so the numbers
are service times of the single writer, not open-loop latency under load;
coordinated omission does not apply to a closed-loop service-time measurement
but it also means queueing delay under bursts is NOT measured here.
Results include the per-command ledger verification that the core runs today.

Usage: PYTHONPATH=src:tests python3 benchmarks/bench_core.py [--n 300] [--no-fullfsync] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support import ACCOUNT, Harness  # noqa: E402
from unknown.domain.fixed import Fixed  # noqa: E402
from unknown.gateway.runner import commission_payload, execution_payload  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(xs: list[float]) -> dict:
    return {"n": len(xs), "p50_ms": round(pct(xs, 0.50), 3), "p95_ms": round(pct(xs, 0.95), 3),
            "p99_ms": round(pct(xs, 0.99), 3), "p999_ms": round(pct(xs, 0.999), 3), "max_ms": round(max(xs), 3),
            "mean_ms": round(statistics.fmean(xs), 3)}


def run(n: int, fullfsync: bool) -> dict:
    tmp = tempfile.TemporaryDirectory()
    policy = None
    from support import load_policy
    policy = load_policy(account_limits__max_orders_per_minute=600, account_limits__authorized_capital="1000000.00",
                         account_limits__max_gross_exposure="1000000.00", account_limits__max_order_notional="1000000.00",
                         session_template__seed="100000.00", session_template__target_terminal_equity="1000000.00",
                         session_template__session_max_loss="100000.00")
    h = Harness(initial_cash="1000000.00", policy=policy, db_path=Path(tmp.name) / "bench.sqlite")
    if not fullfsync:
        h.desk.store.conn.execute("PRAGMA fullfsync=OFF")
        h.desk.store.conn.execute("PRAGMA checkpoint_fullfsync=OFF")
    h.bootstrap(authorize="1000000.00")
    assert h.owner_cmd("create_session", account_key=ACCOUNT, session_id="B").accepted
    h.quote("9.99", "10.01")
    timings = {"intent": [], "send_begin": [], "fill": [], "commission": []}
    for i in range(n):
        h.clock.advance(100_000_000)  # 0.1 s of fixture time between orders keeps the rate limit satisfied
        h.quote("9.99", "10.01")
        t0 = time.perf_counter()
        r = h.intent(f"O{i}", "BUY", "1", "10.00", "B")
        timings["intent"].append((time.perf_counter() - t0) * 1000)
        assert r.accepted, r.body
        outbox_id = f"O{i}:submit"
        t0 = time.perf_counter()
        begin = h.desk.run_system("outbox_begin", {"outbox_id": outbox_id})
        timings["send_begin"].append((time.perf_counter() - t0) * 1000)
        assert begin.body.get("send"), begin.body
        h.desk.run_system("outbox_result", {"outbox_id": outbox_id, "status": "ACCEPTED", "broker_order_id": f"B{i}"})
        ex = {"account_key": ACCOUNT, "exec_key": f"bench:E{i}", "provider_exec_id": f"E{i}", "client_order_id": f"O{i}",
              "order_ref": None, "broker_order_id": f"B{i}", "instrument_id": "FIX-EQ-1", "provider_ref": "FIX-EQ-1",
              "side": "BUY", "quantity": "1", "price": "10.00", "currency": "USD", "exec_time_ns": h.clock.now_ns(),
              "correction_of": None}
        t0 = time.perf_counter()
        h.desk.run_system("broker_execution", ex)
        timings["fill"].append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        h.desk.run_system("broker_commission", {"account_key": ACCOUNT, "exec_key": f"bench:E{i}", "amount": "1.00", "currency": "USD"})
        timings["commission"].append((time.perf_counter() - t0) * 1000)
    problems = h.verify()
    events = h.desk.store.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
    h.close()
    tmp.cleanup()
    return {"timings": {k: summary(v) for k, v in timings.items()}, "ledger_problems": problems, "events": events}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--no-fullfsync", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    result = {
        "benchmark": "core command service time (closed loop, single writer, durable file DB)",
        "host": {"machine": platform.machine(), "platform": platform.platform(), "python": platform.python_version(),
                 "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count()},
        "settings": {"n": a.n, "journal_mode": "WAL", "synchronous": "FULL", "fullfsync": not a.no_fullfsync,
                     "ledger_verify_each_command": True},
        **run(a.n, not a.no_fullfsync),
    }
    text = json.dumps(result, indent=2)
    print(text)
    if a.json:
        Path(a.json).write_text(text + "\n")


if __name__ == "__main__":
    main()
