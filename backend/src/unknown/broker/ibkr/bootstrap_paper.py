"""Owner bootstrap for the IBKR PAPER account (the PAPER counterpart of ``unknown.cli bootstrap-sim``).

    <ibkr venv>/bin/python -m unknown.broker.ibkr.bootstrap_paper --account IBKR-PAPER-1 --deployment mac-dev \
        --universe AAPL:NASDAQ,MSFT:NASDAQ,JPM:NYSE [--dry-run]

Run it after the core is up (``python -m unknown.broker.ibkr.serve``) and the owner has logged in to IB
Gateway. Steps, all as owner commands through the core socket, each printed with its outcome:

1. register the account (provider ibkr, mode PAPER) and publish + activate ``config/policies/paper-default.v1.json``;
2. resolve every listing of the universe on the read-only market-data connection by symbol AND primary
   exchange (never by name, CAP-INS-04) and register only VERIFIED contracts, as instruments with their
   ``ibkr`` conId mapping; anything AMBIGUOUS, UNRESOLVED or CONFLICT is reported and skipped;
3. acquire execution ownership with the owner's explicit attestation that no other deployment trades this
   account (ADR-0011; asked interactively unless --attest-no-other-deployment is given);
4. wait for the first reconciliation (the opening import of the account's existing cash and positions) and
   authorize the policy's capital.

It never starts trading: green start stays the owner's action in the terminal after reviewing preflight. The
passphrase is checked first (step-up). ``--dry-run`` only resolves and prints what it would register."""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ...service.ipc import CoreClient
from ...service.paths import Paths
from .contracts import ListingQuery

REPO = Path(__file__).resolve().parents[5]
PAPER_POLICY = REPO / "config" / "policies" / "paper-default.v1.json"
# IBKR primary-exchange codes for US listings and their settlement cycle (US T+1 since 2024-05-28, SEC Rule
# 15c6-1; config/markets/venues.json). Other markets are not eligible in the paper policy.
US_PRIMARY_EXCHANGES = ("NASDAQ", "NYSE", "ARCA", "AMEX", "BATS")
US_SETTLEMENT_DAYS = 1


@dataclass(frozen=True)
class Listing:
    symbol: str
    primary_exchange: str

    @property
    def instrument_id(self) -> str:
        return f"US:{self.symbol}"


def parse_universe(text: str) -> list[Listing]:
    out = []
    for item in [t.strip() for t in text.split(",") if t.strip()]:
        symbol, _, exchange = item.partition(":")
        symbol, exchange = symbol.strip().upper(), exchange.strip().upper()
        if not symbol or exchange not in US_PRIMARY_EXCHANGES:
            raise ValueError(f"{item!r}: give SYMBOL:PRIMARY_EXCHANGE with exchange in {', '.join(US_PRIMARY_EXCHANGES)}")
        out.append(Listing(symbol, exchange))
    if not out:
        raise ValueError("empty universe")
    return out


def bootstrap(client, resolver, *, account: str, deployment: str, universe: list[Listing], authenticated_at_ns: int,
              attest: bool, dry_run: bool = False, wait_s: float = 120.0, out=print, sleep=time.sleep) -> dict:
    """The bootstrap itself, with the core client and the contract resolver injected (tests use fakes)."""
    owner = {"id": "owner", "role": "OWNER", "authenticated_at_ns": authenticated_at_ns}
    report: dict = {"registered": [], "skipped": {}, "commands": []}

    def run(kind: str, **payload) -> dict:
        r = client.command(f"cli.paper.{kind}.{secrets.token_hex(8)}", kind, payload, owner)
        report["commands"].append({"kind": kind, "accepted": r["accepted"], "code": (r.get("body") or {}).get("code")})
        out(f"{kind}: {'accepted' if r['accepted'] else 'REJECTED'} {json.dumps(r.get('body'), default=str)[:240]}")
        return r

    resolutions = []
    for listing in universe:
        res = resolver.resolve(ListingQuery(symbol=listing.symbol, currency="USD", primary_exchange=listing.primary_exchange,
                                            listing_mic=None), instrument_id=listing.instrument_id,
                               settlement_days=US_SETTLEMENT_DAYS)
        resolutions.append((listing, res))
        if res.status != "VERIFIED":
            report["skipped"][listing.instrument_id] = {"status": res.status, "reasons": list(res.reasons)}
            out(f"{listing.symbol}:{listing.primary_exchange}: {res.status} - not registered ({'; '.join(res.reasons)[:200]})")
        else:
            out(f"{listing.symbol}:{listing.primary_exchange}: VERIFIED conId {res.con_id}")
    if dry_run:
        report["dry_run"] = True
        return report
    if not any(res.status == "VERIFIED" for _, res in resolutions):
        raise RuntimeError("no listing verified; nothing to trade, bootstrap stopped before registering anything")
    if not attest:
        raise RuntimeError("ownership needs the owner's attestation that no other deployment trades this account")
    if not run("register_account", account_key=account, provider="ibkr", mode="PAPER", base_currency="USD")["accepted"]:
        out("(account may already exist; continuing)")
    pol = run("publish_policy", body=json.loads(PAPER_POLICY.read_text()))
    if not pol["accepted"]:
        raise RuntimeError("paper policy was not published")
    run("activate_policy", account_key=account, policy_hash=pol["body"]["policy_hash"])
    for listing, res in resolutions:
        if res.status == "VERIFIED":
            mapping = {k: res.mapping[k] for k in ("provider", "external_id", "verified_at_ns", "details")}
            if run("register_instrument", instrument=res.instrument, mappings=[mapping])["accepted"]:
                report["registered"].append(listing.instrument_id)
    run("acquire_execution_ownership", account_key=account, deployment_id=deployment, attest_no_other_deployment=True)
    out("waiting for the first reconciliation (opening import of the paper account) ...")
    deadline = time.monotonic() + wait_s
    blockers = None
    while time.monotonic() < deadline:
        blockers = client.query("preflight", authenticated_at_ns=authenticated_at_ns)["blockers"]
        if "RECONCILIATION_REQUIRED" not in blockers:
            break
        sleep(2)
    else:
        raise RuntimeError(f"no clean reconciliation within {wait_s:.0f}s; check IB Gateway and `unknown.cli status` "
                           f"(blockers: {blockers})")
    policy = json.loads(PAPER_POLICY.read_text())
    run("authorize_capital", account_key=account, amount=policy["account_limits"]["authorized_capital"])
    report["preflight_blockers"] = client.query("preflight", authenticated_at_ns=authenticated_at_ns)["blockers"]
    out("bootstrap complete. Review preflight in the terminal; green start remains your action.")
    return report


BOOTSTRAP_CLIENT_ID = 91   # the connectivity check uses 90; the core's market-data session uses its configured id


def bootstrap_md_config(md, client_id: int):
    """The bootstrap's market-data session config: the owner's, with its own client id. The runbook starts the core
    (whose market-data session holds the configured id) before the bootstrap, so reusing that id is refused by IBKR
    (error 326, client id already in use)."""
    if client_id == md.client_id:
        raise ValueError(f"bootstrap client id {client_id} is the core's market-data session id; choose another")
    return dataclasses.replace(md, client_id=client_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", required=True, help="internal account key, e.g. IBKR-PAPER-1")
    ap.add_argument("--deployment", required=True, help="deployment id the core runs as, e.g. mac-dev")
    ap.add_argument("--universe", default="AAPL:NASDAQ,MSFT:NASDAQ", help="SYMBOL:PRIMARY_EXCHANGE,... (US only)")
    ap.add_argument("--attest-no-other-deployment", action="store_true",
                    help="attest that no other deployment trades this paper account (asked interactively otherwise)")
    ap.add_argument("--dry-run", action="store_true", help="resolve and print only; no command is sent")
    ap.add_argument("--home", default=None)
    ap.add_argument("--config", default=None, help="IBKR config (default ~/.unknown/ibkr.json)")
    ap.add_argument("--client-id", type=int, default=BOOTSTRAP_CLIENT_ID,
                    help=f"API client id for the bootstrap's own market-data session (default {BOOTSTRAP_CLIENT_ID}); "
                         "must differ from the running core's market-data session")
    args = ap.parse_args(argv)
    universe = parse_universe(args.universe)
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()
    from ...api.auth import verify_passphrase
    from .config import load_config
    from .contracts import ContractResolver
    from .marketdata import IbkrMarketDataSession, load_market_data_config
    record = json.loads((paths.secrets / "owner.json").read_text())
    if not verify_passphrase(getpass.getpass("Owner passphrase: "), record):
        print("invalid passphrase", file=sys.stderr)
        return 1
    auth_ns = time.time_ns()
    attest = args.attest_no_other_deployment
    if not attest and not args.dry_run:
        attest = input("Does any OTHER deployment trade this paper account? Type 'no' to attest: ").strip().lower() == "no"
    md = bootstrap_md_config(load_market_data_config(paths.root), args.client_id)
    session = IbkrMarketDataSession(load_config(args.config), md)
    state = session.connect()
    if not state.get("verified_paper"):
        print(f"market-data connection not verified: {state.get('reason')}", file=sys.stderr)
        return 1
    try:
        report = bootstrap(CoreClient(paths.socket, paths.token("api")), ContractResolver(session), account=args.account,
                           deployment=args.deployment, universe=universe, authenticated_at_ns=auth_ns, attest=attest,
                           dry_run=args.dry_run)
    finally:
        session.disconnect()
    print(json.dumps({k: v for k, v in report.items() if k != "commands"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
