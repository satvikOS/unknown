"""Owner command line: setup, passphrase, status, emergency stop and simulated bootstrap.

  python -m unknown.cli init
  python -m unknown.cli set-passphrase
  python -m unknown.cli status --account SIM-1
  python -m unknown.cli stop --account SIM-1 --reason "owner emergency"
  python -m unknown.cli bootstrap-sim --account SIM-1 --deployment mac-dev
  python -m unknown.cli alerts
  python -m unknown.cli backup --account SIM-1 [--keep 96]
  python -m unknown.cli restore-drill --account SIM-1 [--backup FILE]
  python -m unknown.cli broker-admin --account IBKR-PAPER-1 clear-entry-block SHORT_SALE_SIGNAL --note "..."
  python -m unknown.cli broker-admin --account IBKR-PAPER-1 ack-history-gap 2026-10-20 --note "..."

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

from .api.auth import verify_passphrase, write_owner_record
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
    sub.add_parser("alerts", help="broker adapter anomalies and latched entry blocks")
    ba = sub.add_parser("broker-admin", help="owner action on broker adapter state (needs the passphrase)")
    ba.add_argument("--account", required=True)
    ba.add_argument("action", choices=["clear-entry-block", "ack-history-gap"])
    ba.add_argument("value", help="entry-block name, or the YYYY-MM-DD day reconciled from statements")
    ba.add_argument("--note", required=True)
    bk = sub.add_parser("backup", help="online backup of the core database (safe while the core runs)")
    bk.add_argument("--account", required=True)
    bk.add_argument("--keep", type=int, default=96)
    rd = sub.add_parser("restore-drill", help="prove a backup restores (temporary copy; live state untouched)")
    rd.add_argument("--account", required=True)
    rd.add_argument("--backup", default=None, help="backup file (default: the newest for the account)")
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
    if args.cmd in ("backup", "restore-drill"):
        from .ops.backup import backups_for, restore_drill, take_backup
        if args.cmd == "backup":
            print(json.dumps(take_backup(paths, args.account, keep=args.keep), indent=2))
            return 0
        found = backups_for(paths, args.account)
        target = Path(args.backup) if args.backup else (found[-1] if found else None)
        if target is None:
            print(f"no backups for {args.account} in {paths.backups}", file=sys.stderr)
            return 1
        report = restore_drill(target, args.account)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1
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
    if args.cmd == "alerts":
        print(json.dumps(client.query("broker_alerts"), indent=2, default=str))
        return 0
    if args.cmd == "broker-admin":
        auth_ns = _reauthenticate(paths)
        if auth_ns is None:
            print("invalid passphrase", file=sys.stderr)
            return 1
        payload = {"account_key": args.account, "note": args.note}
        if args.action == "clear-entry-block":
            payload.update(action="clear_entry_block", name=args.value)
        else:
            payload.update(action="acknowledge_history_gap", through_day=args.value)
        r = client.command(f"cli.broker_admin.{secrets.token_hex(8)}", "broker_admin_action", payload, _owner(auth_ns))
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["accepted"] else 1
    return 1


def _reauthenticate(paths: Paths) -> int | None:
    """Owner step-up for sensitive CLI actions: verify the passphrase against the stored scrypt record."""
    record = json.loads((paths.secrets / "owner.json").read_text())
    if not verify_passphrase(getpass.getpass("Owner passphrase: "), record):
        return None
    return time.time_ns()


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
