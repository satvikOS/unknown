"""Owner command line: setup, passphrase, status, emergency stop and simulated bootstrap.

  python -m unknown.cli init
  python -m unknown.cli set-passphrase
  python -m unknown.cli status --account SIM-1
  python -m unknown.cli stop --account SIM-1 --reason "owner emergency"
  python -m unknown.cli bootstrap-sim --account SIM-1 --deployment mac-dev

``stop`` goes through the core socket. If the core is not running, nothing in
this system is sending orders, but orders already resting at the broker remain:
use the broker's own interface (e.g. TWS/Client Portal "cancel all") as the
out-of-band path (docs/runbooks)."""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
import time
from pathlib import Path

from .api.auth import write_owner_record
from .service.ipc import CoreClient
from .service.paths import Paths

REPO = Path(__file__).resolve().parents[3]


def _client(paths: Paths) -> CoreClient:
    return CoreClient(paths.socket, paths.token("api"))


def _owner(auth_ns: int | None = None) -> dict:
    return {"id": "owner", "role": "OWNER", "authenticated_at_ns": auth_ns}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="unknown.cli")
    ap.add_argument("--home", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("set-passphrase")
    st = sub.add_parser("status")
    st.add_argument("--account", required=True)
    sp = sub.add_parser("stop")
    sp.add_argument("--account", required=True)
    sp.add_argument("--reason", default="OWNER_CLI_STOP")
    bs = sub.add_parser("bootstrap-sim")
    bs.add_argument("--account", required=True)
    bs.add_argument("--deployment", required=True)
    bs.add_argument("--authorize", default="300.00")
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()

    if args.cmd == "init":
        paths.ensure()
        for name in ("api", "agent"):
            paths.token(name, create=True)
        print(f"initialized {paths.root} (tokens created with mode 600)")
        return 0
    if args.cmd == "set-passphrase":
        p1 = getpass.getpass("New owner passphrase (min 12 chars): ")
        p2 = getpass.getpass("Repeat: ")
        if p1 != p2:
            print("passphrases differ", file=sys.stderr)
            return 1
        write_owner_record(paths.secrets / "owner.json", p1)
        print("owner passphrase stored as an scrypt hash")
        return 0
    client = _client(paths)
    if args.cmd == "status":
        print(json.dumps({"health": client.query("health"), "preflight": client.query("preflight")}, indent=2, default=str))
        return 0
    if args.cmd == "stop":
        r = client.command(f"cli.stop.{secrets.token_hex(8)}", "stop", {"account_key": args.account, "reason": args.reason}, _owner())
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["accepted"] else 1
    if args.cmd == "bootstrap-sim":
        return _bootstrap_sim(client, args)
    return 1


def _bootstrap_sim(client: CoreClient, args) -> int:
    """Register a SIMULATED account with fixture instruments and the proposed simulated policy.
    Everything created here is simulation-only and labelled as fixture data."""
    from .domain.fixed import Fixed
    from .domain.instruments import AssetClass, Instrument, TickRule
    now = time.time_ns()
    owner = _owner(now)

    def run(kind: str, **payload) -> dict:
        r = client.command(f"cli.{kind}.{secrets.token_hex(8)}", kind, payload, owner)
        print(f"{kind}: {'accepted' if r['accepted'] else 'REJECTED'} {json.dumps(r['body'], default=str)[:300]}")
        return r

    run("register_account", account_key=args.account, provider="simulated", mode="SIMULATED", base_currency="USD")
    body = json.loads((REPO / "config" / "policies" / "simulated-default.v1.json").read_text())
    pol = run("publish_policy", body=body)
    run("activate_policy", account_key=args.account, policy_hash=pol["body"]["policy_hash"])
    for iid, sym, name in (("FIX-EQ-1", "FIXA", "Fixture Equity A (simulation only)"),
                           ("FIX-EQ-2", "FIXB", "Fixture Equity B (simulation only)")):
        inst = Instrument(instrument_id=iid, asset_class=AssetClass.EQUITY, symbol=sym, name=name, currency="USD",
                          settlement_currency="USD", venue_mic="XNAS", quantity_increment=Fixed(1), min_quantity=Fixed(1),
                          multiplier=Fixed(1), settlement_days=1,
                          tick_rules=(TickRule(Fixed(0), Fixed.parse("0.0001")), TickRule(Fixed(1), Fixed.parse("0.01"))),
                          attributes={"fixture": True})
        run("register_instrument", instrument=inst.to_json(), mappings=[{"provider": "simulated", "external_id": iid}])
    run("acquire_execution_ownership", account_key=args.account, deployment_id=args.deployment)
    print("waiting for the first reconciliation (opening import) ...")
    for _ in range(60):
        pf = client.query("preflight", authenticated_at_ns=now)
        if "RECONCILIATION_REQUIRED" not in pf["blockers"]:
            break
        time.sleep(1)
    run("authorize_capital", account_key=args.account, amount=args.authorize)
    print("bootstrap complete; press green (start) in the terminal after reviewing preflight")
    return 0


if __name__ == "__main__":
    sys.exit(main())
