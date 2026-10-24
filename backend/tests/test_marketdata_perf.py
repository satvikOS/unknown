"""Performance: 500 keys at 4 Hz from the TEST feed double through the bus and the real API server to an SSE client on localhost.

The client runs in a separate process (its own interpreter, so its JSON parsing does not share the server's GIL) and
measures bus-to-client latency = client receipt time - ts_pub_ns for every quote and trade.

Bound: p99 < 50 ms. Justification: 50 ms is the contract's per-key quote coalescing window. If the bus delivered the
99th percentile event later than one window, a key's next coalesced quote could already be due before the previous one
reached the client, i.e. the bus would no longer keep up with its own publish rate. Load: 500 x 4 = 2,000 quotes/s,
1,000 trades/s and 1,000 depth snapshots/s, plus the tick store writing every trade. The measured p50/p99 are printed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from unknown.api.auth import write_owner_record
from unknown.api.server import ApiContext, make_server
from unknown.marketdata import config as C
from unknown.marketdata.bus import MarketDataBus
from unknown.marketdata.store import TickStore
from support.fake_marketdata import FeedDouble, bus_config
from unknown.service.paths import Paths

PASSPHRASE = "market data perf test passphrase"
KEYS = 500
HZ = 4.0
WARMUP_S = 1.5
MEASURE_S = 6.0
P99_BOUND_NS = 50_000_000

CLIENT = r"""
import http.client, json, sys, time
port, cookie, keys, warmup, measure = int(sys.argv[1]), sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
conn.request("GET", "/api/marketdata/stream?keys=" + keys, headers={"Cookie": cookie})
resp = conn.getresponse()
assert resp.status == 200, resp.status
t0 = time.monotonic()
lat, counts, name, statuses = [], {}, None, {}
while True:
    line = resp.fp.readline()
    if not line:
        break
    now = time.time_ns()
    if line.startswith(b"event: "):
        name = line[7:].strip().decode()
    elif line.startswith(b"data: "):
        el = time.monotonic() - t0
        counts[name] = counts.get(name, 0) + 1
        if name == "status":
            ev = json.loads(line[6:])
            statuses[ev["state"]] = statuses.get(ev["state"], 0) + 1
        elif el >= warmup and name in ("quote", "trade"):
            i = line.find(b'"ts_pub_ns":"')
            pub = int(line[i + 13:line.index(b'"', i + 13)])
            lat.append(now - pub)
        if el >= warmup + measure:
            break
conn.close()
lat.sort()
pct = lambda p: lat[max(-(-p * len(lat) // 100), 1) - 1]
print(json.dumps({"n": len(lat), "p50": pct(50), "p99": pct(99), "max": lat[-1], "counts": counts,
                  "statuses": statuses}))
"""


class StreamPerformanceTest(unittest.TestCase):
    def test_500_keys_at_4hz_bus_to_client_p99(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "home")
            paths.ensure()
            paths.token("api", create=True)
            write_owner_record(paths.secrets / "owner.json", PASSPHRASE)
            ctx = ApiContext(paths, "SIM-PERF", None, None, False)
            cfg = bus_config(hz=HZ)
            ctx.marketdata = MarketDataBus(cfg, {"TEST": FeedDouble(hz=HZ, currency_for=C.currency_for)},
                                           store=TickStore(Path(tmp) / "ticks.sqlite"))
            ctx.marketdata.start()
            server = make_server(ctx, "127.0.0.1", 0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/auth/login", method="POST",
                                             data=json.dumps({"passphrase": PASSPHRASE}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    cookie = r.headers["Set-Cookie"].split(";")[0]
                keys = ",".join(f"S{i:03d}.XNAS" for i in range(KEYS))
                started = time.monotonic()
                out = subprocess.run([sys.executable, "-c", CLIENT, str(port), cookie, keys, str(WARMUP_S),
                                      str(MEASURE_S)], capture_output=True, text=True, timeout=60)
                elapsed = time.monotonic() - started
                self.assertEqual(out.returncode, 0, out.stderr[-2000:])
                res = json.loads(out.stdout)
                server_side = ctx.marketdata.latency.report()
                print(f"\n[marketdata perf] {KEYS} keys x {HZ:g} Hz TEST feed, {MEASURE_S:g}s measured: "
                      f"bus->client p50 {res['p50'] / 1e6:.2f} ms, p99 {res['p99'] / 1e6:.2f} ms, "
                      f"max {res['max'] / 1e6:.2f} ms over {res['n']} events; events {res['counts']}; "
                      f"server recv->pub p99 {server_side['recv_to_pub']['p99_ns'] / 1e6:.2f} ms; "
                      f"wall {elapsed:.1f}s", file=sys.stderr)
                self.assertEqual(res["statuses"].get("SUBSCRIBED"), KEYS)
                expected_quotes = KEYS * HZ * MEASURE_S
                self.assertGreater(res["counts"]["quote"], 0.8 * expected_quotes)
                self.assertGreater(res["counts"]["trade"], 0.8 * expected_quotes / 2)
                self.assertEqual(ctx.marketdata.counters["trades_dropped"], 0)
                self.assertLess(res["p99"], P99_BOUND_NS)
            finally:
                server.shutdown()
                server.server_close()
                ctx.marketdata.stop()


if __name__ == "__main__":
    unittest.main()
