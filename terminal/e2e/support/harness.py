"""Test-only launchers for the terminal end-to-end suite. Never used outside `npm run test:e2e`.

  python harness.py core --broker-state FILE -- <unknown.service.core arguments>
  python harness.py api --step-up-window-s N -- <unknown.api.server arguments>

Both run the real entry points (`unknown.service.core.main`, `unknown.api.server.main`) unchanged
except for one seam each, so the suite exercises the real core, API, socket, database and terminal:

core: the simulated broker lives inside the core process, so restarting the core would make the
    broker forget every order, fill and cash movement, while the ledger remembers them. A real
    broker does not forget the account when our process restarts. To model that, the launcher
    pickles the simulator after a clean SIGTERM shutdown and restores it on the next start (the
    deterministic FIXTURE quote path restarts from its seed; only the broker's book persists).

api: the API's step-up window is the active policy's reauth_max_age_s (900 s in the default
    simulated policy), or 15 minutes without a policy. The suite caps it with
    server.STEP_UP_MAX_AGE_NS, which can only shorten the window, so the HTTP 428 -> passphrase ->
    same-command-id retry path can be exercised in seconds. The core's own policy check
    (reauth_max_age_s) is untouched.

Reads the pickle it wrote itself; the file lives in the suite's private runtime directory."""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from pathlib import Path


def run_core(state: Path, argv: list[str]) -> None:
    from unknown.service import core as core_mod

    original_factory = core_mod._simulated_broker
    original_serve = core_mod.serve

    def broker_factory(clock, cash):
        if state.exists():
            broker = pickle.loads(state.read_bytes())
            broker._clock = clock
            print(f"[e2e harness] restored simulated broker book from {state}", flush=True)
            return broker
        return original_factory(clock, cash)

    def serve(svc) -> None:
        try:
            original_serve(svc)
        finally:
            # svc.run() has returned: the loop thread is done with the broker.
            tmp = state.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(svc.broker))
            os.replace(tmp, state)
            print(f"[e2e harness] saved simulated broker book to {state}", flush=True)

    core_mod._simulated_broker = broker_factory
    core_mod.serve = serve
    core_mod.main(argv)


def run_api(window_s: int, argv: list[str]) -> None:
    from unknown.api import server

    server.STEP_UP_MAX_AGE_NS = window_s * 1_000_000_000
    print(f"[e2e harness] API step-up window set to {window_s} s for this test run", flush=True)
    server.main(argv)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="which", required=True)
    c = sub.add_parser("core")
    c.add_argument("--broker-state", required=True)
    a = sub.add_parser("api")
    a.add_argument("--step-up-window-s", type=int, required=True)
    args, rest = ap.parse_known_args()
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if args.which == "core":
        run_core(Path(args.broker_state), rest)
    else:
        run_api(args.step_up_window_s, rest)


if __name__ == "__main__":
    sys.exit(main())
