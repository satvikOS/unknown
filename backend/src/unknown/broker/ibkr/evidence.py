"""Read-only IBKR PAPER evidence runner for the checklist in docs/integrations/ibkr.md section 5 (owner command).

    cd backend && PYTHONPATH=src ~/.unknown/venvs/ibkr/bin/python -m unknown.broker.ibkr.evidence --account IBKR-PAPER-1

Against the owner's logged-in IB Gateway (PAPER profile only; ``config.validate`` refuses anything else) it runs
every READ-ONLY item of the checklist and records the outcome:

* identity (5.2, R2) on both of its connections, server version and SDK fingerprint (5.1, R1), clock skew against
  the paper policy;
* contract resolution (CAP-INS-04) of the registered universe: every instrument with an ``ibkr`` mapping in the
  core database (read with SQLite ``mode=ro``), or ``--universe`` when nothing is registered yet; a registered conId
  that IBKR now resolves differently fails;
* the market-data type IBKR delivers for each resolved instrument (CAP-IBKR-12), one streaming line at a time;
* the account summary and positions snapshot that reconciliation uses (5.7, R6/R11), the open orders of every client
  (5.8), the executions and the commission reports IBKR still returns (5.6, R5);
* the error-code mapping on a deliberately invalid read request (contract details for conId 1, expected IBKR 200 ->
  UNRESOLVED).

Items that need an order, a configuration change or a Gateway restart are listed as OWNER_SESSION with the exact
steps; this runner never executes them. It places, modifies and cancels nothing, never presses start, sends nothing to
the core and writes only the dated evidence record ``<UNKNOWN_HOME>/evidence/ibkr-paper-<UTC>.json`` (mode 600) and
its table ``.md`` beside it, outside the repository. Its two API client ids (92 and 93 by default) are its own: the
order adapter's, the core's market-data session's and the market-data bus source's ids are refused, and so is 0
(client 0 binds manual TWS orders). The order adapter connection uses a throw-away order-id state directory.

Results: PASS, FAIL, INCONCLUSIVE (the run could not decide, e.g. a frozen market-data type outside regular hours),
NOT_RUN (with the reason) and OWNER_SESSION. Exit 0 only when every read-only item passed; 1 otherwise; 2 when the
runner could not start (configuration). Status (ADR-0001): implemented and tested offline against the fake IBKR host;
not yet run against IB Gateway."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ...market.quotes import QuoteBook
from ...service.paths import Paths
from .bootstrap_paper import US_SETTLEMENT_DAYS
from .check import PAPER_POLICY, CheckSession, _entitlement
from .config import IbkrConfigError, load_config
from .contracts import ContractResolver, ListingQuery
from .marketdata import MARKET_DATA_TYPES, ContractSpec, MarketDataConfigError, load_market_data_config

PROVIDER = "ibkr"
SCHEMA = "unknown.ibkr.evidence.v1"
DEFAULT_MD_CLIENT_ID = 92        # check.py uses 90, bootstrap_paper.py 91
DEFAULT_ACCOUNT_CLIENT_ID = 93
INVALID_CON_ID = 1               # no IBKR contract has conId 1: contract details must answer error 200
RESULTS = ("PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN", "OWNER_SESSION")
READ_ONLY_ITEMS = ("identity", "server_version", "clock", "contracts", "market_data_type", "error_mapping",
                   "account_snapshot", "open_orders", "executions", "commissions")


@dataclass
class Item:
    id: str
    checklist: str
    title: str
    result: str = "NOT_RUN"
    detail: str = ""
    data: dict = field(default_factory=dict)
    owner_steps: list = field(default_factory=list)


# Items that need an order, a configuration change or a Gateway restart: never executed here, only described.
OWNER_ITEMS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("pin_records", "5.1 (R1)", "Installer SHA-256, API_VersionNum.txt and IB Gateway build", (
        "Run `shasum -a 256` on the TWS API installer you installed from and on the IB Gateway installer.",
        "Copy the contents of API_VersionNum.txt from the TWS API installation folder.",
        "Read the IB Gateway build from its Help -> About window.",
        "Record the three values with this run's server version and SDK fingerprint in docs/status/STATUS.md.")),
    ("identity_negative", "5.2 (R2)", "Wrong account, duplicate client id, stopped Gateway, Read-Only API", (
        "Wrong account: copy ~/.unknown/ibkr.json to a scratch file outside the repository, change "
        "expected_account_id to a different paper id, run `python -m unknown.broker.ibkr.check --account "
        "IBKR-PAPER-1 --config <scratch file>`, and confirm `NOT VERIFIED: DISCONNECTED_UNSAFE` and no request after "
        "the handshake in the IB Gateway API log. Delete the scratch file.",
        "Duplicate client id: while `check` runs with --client-id 90, start a second `check --client-id 90` and "
        "confirm error 326 latches DISCONNECTED_UNSAFE.",
        "Stopped Gateway: quit IB Gateway, run `check`, confirm HOST_DOWN 502; log in again.",
        "Read-Only API: needs an order. In an owner session turn Read-Only API on, submit one 1-share limit order "
        "through the terminal, confirm IBKR rejects it and the terminal shows the reject; turn Read-Only API off.")),
    ("order_ids", "5.3 (R3)", "Order ids across a crash and a forced duplicate", (
        "Needs orders. In an owner session stop the core (launchctl bootout) between allocation and send, and "
        "again after send, while a 1-share limit order is being entered; restart it and confirm no id is reused, "
        "no order is duplicated and lookup returns False and None respectively.",
        "Record the 103 rejection and resync for a forced duplicate id from the adapter test procedure.")),
    ("status_table", "5.4 (R4)", "Real orderStatus sequences", (
        "Needs orders. Through the terminal after green start: a resting limit then cancel; a marketable limit far "
        "through the market (expect 202); a partial fill then cancel; an order held by precautions.",
        "Record each orderStatus sequence from the core's broker-event journal (health -> event_journal) and "
        "confirm duplicate status streams produced one event each.")),
    ("order_ref", "5.5 (R3)", "orderRef echo and maximum length", (
        "Needs orders. Submit one order and check whether orderRef is echoed in openOrder, execDetails and "
        "completedOrder; find the maximum accepted length; then set order_reference_max_length.",)),
    ("executions_live", "5.6 (R5)", "Multi-fill commissions, restart backfill, lastNDays, corrections", (
        "Needs orders. Fill one multi-fill order and confirm one commission report per execId and their arrival "
        "order; restart the core mid-day and compare the backfill with the live journal; rerun this evidence runner "
        "the next day (executions item) to see what lastNDays still returns; record any correction.",)),
    ("snapshots_after_buy", "5.7 (R6, R11)", "Cash after a buy and after settlement, update cadence", (
        "Needs orders. After a 1-share buy rerun this runner and compare SettledCash and TotalCashValue (and again "
        "after settlement, T+1); watch the 3-minute accountSummary cadence; confirm reqPositions agrees with the "
        "ledger; confirm a third summary subscription is refused locally.",)),
    ("foreign_orders", "5.8", "A manual TWS order appears as foreign; completedOrder fields", (
        "Needs orders. Place a manual order in TWS (not through this platform), rerun this runner and confirm the "
        "open_orders item lists it as foreign (no client order id); record the populated completedOrder fields.",)),
    ("connectivity", "5.9 (R7, R9)", "Network loss, Gateway restart, nightly reset, weekly re-login", (
        "Disable the network for 60 s while an order is working and record the 1100 / 1101 / 1102 sequence "
        "(needs an order).",
        "Quit and restart IB Gateway and log in: confirm health shows broker_connection 'reconnecting' then "
        "'connected' and market_data CONNECTED without restarting any process.",
        "Observe one nightly reset and one weekly re-login and record what was needed and when.")),
    ("pacing_soak", "5.10 (R8)", "Pacing soak at 1.2 x R_max", (
        "Needs saturated entries (orders). Run the soak in an owner session and confirm no error 100 and that "
        "cancels still go out.",)),
    ("fidelity", "5.11 (R13)", "Paper-only behaviour", (
        "Record paper-only behaviour seen during the owner sessions above (for example an unprocessed dividend or "
        "top-of-book fills).",)),
)


# ----------------------------------------------------------------------------------------------- universe
def registered_universe(paths: Paths, account_key: str) -> tuple[list[dict], str]:
    """Instruments with an ``ibkr`` mapping (latest version), read with SQLite ``mode=ro``; never writes."""
    db = paths.db(account_key)
    if not db.exists():
        return [], f"no core database at {db}"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT m.external_id, m.instrument_id, m.version, i.body FROM provider_mappings m "
                            "JOIN instruments i ON i.instrument_id = m.instrument_id WHERE m.provider=? "
                            "ORDER BY m.version", (PROVIDER,)).fetchall()
    except sqlite3.Error as exc:
        return [], f"core database not readable: {exc}"
    finally:
        conn.close()
    latest: dict[str, dict] = {}
    for con_id, instrument_id, _version, body in rows:
        b = json.loads(body)
        latest[instrument_id] = {"instrument_id": instrument_id, "symbol": b.get("symbol", ""),
                                 "currency": b.get("currency", "USD"),
                                 "primary_exchange": (b.get("attributes") or {}).get("ibkr.primaryExchange", ""),
                                 "registered_con_id": int(con_id) if str(con_id).isdigit() else None}
    return sorted(latest.values(), key=lambda u: u["instrument_id"]), f"{len(latest)} registered in {db.name}"


def universe_from_text(text: str) -> list[dict]:
    out = []
    for item in [t.strip() for t in text.split(",") if t.strip()]:
        symbol, _, exchange = item.partition(":")
        if not symbol.strip() or not exchange.strip():
            raise ValueError(f"{item!r}: give SYMBOL:PRIMARY_EXCHANGE")
        out.append({"instrument_id": f"EVIDENCE:{symbol.strip().upper()}", "symbol": symbol.strip().upper(),
                    "currency": "USD", "primary_exchange": exchange.strip().upper(), "registered_con_id": None})
    return out


# ----------------------------------------------------------------------------------------------- runner
def _dec(v) -> str | None:
    return None if v is None else str(v)


def run_evidence(cfg, md_cfg, *, account_key: str, universe: list[dict], universe_source: str,
                 md_client_id: int = DEFAULT_MD_CLIENT_ID, account_client_id: int = DEFAULT_ACCOUNT_CLIENT_ID,
                 reserved_client_ids: dict | None = None, max_clock_skew_ms: int | None = None, wait_s: float = 10.0,
                 sdk=None, md_client_factory=None, adapter_client_factory=None, adapter_factory=None, clock=None,
                 out=print) -> dict:
    """Run every read-only item; returns the evidence record (nothing is written here, see ``write_record``)."""
    reserved = {"order adapter": cfg.client_id, "core market-data session": md_cfg.client_id,
                **(reserved_client_ids or {})}
    for name, cid in (("market-data", md_client_id), ("account", account_client_id)):
        if cid <= 0:
            raise ValueError(f"{name} client id must be positive (client 0 binds manual TWS orders)")
        clash = [k for k, v in reserved.items() if v == cid]
        if clash:
            raise ValueError(f"{name} client id {cid} is in use by the {clash[0]}; pass a dedicated id")
    if md_client_id == account_client_id:
        raise ValueError("the two evidence connections need two different client ids")
    items = {i: Item(i, c, t) for i, c, t in (
        ("identity", "5.2 (R2)", "PAPER identity on both evidence connections"),
        ("server_version", "5.1 (R1)", "Negotiated server version and SDK fingerprint"),
        ("clock", "R1 preflight", "Host clock skew against the paper policy"),
        ("contracts", "CAP-INS-04", "Contract resolution of the registered universe"),
        ("market_data_type", "CAP-IBKR-12", "Market-data type IBKR delivers per instrument"),
        ("error_mapping", "R4 / error codes", "Error-code mapping on a deliberately invalid read request"),
        ("account_snapshot", "5.7 (R6, R11)", "Account summary and positions snapshot for reconciliation"),
        ("open_orders", "5.8 (R3)", "Open orders of every client"),
        ("executions", "5.6 (R5)", "Executions IBKR still returns"),
        ("commissions", "5.6 (R5)", "Commission reports for those executions"))}
    started = datetime.now(timezone.utc)
    record = {"schema": SCHEMA, "run_at_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "account_key": account_key,
              "account": cfg.masked_account(), "host": f"{cfg.host}:{cfg.port}", "mode": cfg.mode,
              "client_ids": {"market_data": md_client_id, "account": account_client_id},
              "universe_source": universe_source, "evidence_level": "BROKER_PAPER", "places_orders": False,
              "presses_start": False, "writes_to_core": False}
    out(f"IBKR paper evidence run (read-only) {record['run_at_utc']}: {account_key} -> {record['account']} at "
        f"{record['host']}")

    # ---------------------------------------------------------------- market-data connection
    kwargs = {"quote_book": QuoteBook(), "status_path": None}
    for k, v in (("sdk", sdk), ("client_factory", md_client_factory), ("clock", clock)):
        if v is not None:
            kwargs[k] = v
    session = CheckSession(cfg, replace(md_cfg, client_id=md_client_id), **kwargs)
    md_ok = False
    try:
        state = session.connect()
        md_ok = bool(state.get("verified_paper"))
        items["identity"].data["market_data_connection"] = {"verified": md_ok, "reason": state.get("reason")}
        if md_ok:
            _md_items(session, items, universe, universe_source, md_cfg, max_clock_skew_ms, wait_s, cfg)
    finally:
        session.disconnect()

    # ---------------------------------------------------------------- account connection (the order adapter, reads only)
    acct_ok = False
    if md_ok:
        with tempfile.TemporaryDirectory(prefix="unknown-evidence-ids-") as state_dir:
            acfg = replace(cfg, client_id=account_client_id, state_dir=Path(state_dir))
            resolver = {str(u["registered_con_id"]): u["instrument_id"] for u in universe if u["registered_con_id"]}
            if adapter_factory is None:
                from .adapter import IbkrAdapter

                def adapter_factory(c):
                    kw = {k: v for k, v in (("sdk", sdk), ("client_factory", adapter_client_factory),
                                            ("clock", clock)) if v is not None}
                    return IbkrAdapter(c, instrument_resolver=lambda con_id: resolver.get(str(con_id)), **kw)
            adapter = adapter_factory(acfg)
            try:
                s = adapter.connect()
                acct_ok = bool(s.connected and s.authenticated)
                items["identity"].data["account_connection"] = {"verified": acct_ok, "reason": s.auth_required_reason}
                if acct_ok:
                    _account_items(adapter, items)
            finally:
                adapter.disconnect()
    ident = items["identity"]
    ident.result = "PASS" if md_ok and acct_ok else "FAIL"
    ident.detail = ("PAPER verified on both connections (paper port, managedAccounts == the expected paper account)"
                    if ident.result == "PASS" else
                    "NOT VERIFIED: " + "; ".join(f"{k}: {v.get('reason')}" for k, v in ident.data.items()
                                                 if not v.get("verified")))
    for it in items.values():
        if it.result == "NOT_RUN" and not it.detail:
            it.detail = "not run: the PAPER identity was not verified, so nothing else was requested"
    owner = [Item(i, c, t, "OWNER_SESSION", "requires owner session: never executed by this runner", {}, list(steps))
             for i, c, t, steps in OWNER_ITEMS]
    record["items"] = [asdict(i) for i in list(items.values()) + owner]
    counts = {r: sum(1 for i in record["items"] if i["result"] == r) for r in RESULTS}
    record["summary"] = counts
    record["ok"] = all(items[i].result == "PASS" for i in READ_ONLY_ITEMS)
    record["table"] = render_table(record)
    out(record["table"])
    return record


def _md_items(session, items, universe, universe_source, md_cfg, max_skew, wait_s, cfg) -> None:
    sv = items["server_version"]
    version = session.server_version()
    sdk = session.sdk
    sv.data = {"server_version": version, "sdk_version": getattr(sdk, "version", None),
               "sdk_fingerprint": getattr(sdk, "fingerprint", None), "sdk_path": getattr(sdk, "path", None)}
    sv.result = "PASS" if version is not None else "FAIL"
    sv.detail = f"server version {version}, SDK {sv.data['sdk_version']}" if version is not None else \
        "the host did not report a server version"

    ck = items["clock"]
    c = session.measure_clock(cfg.request_timeout_s)
    if c["ok"] and max_skew is not None and abs(c["skew_ms"]) > max_skew:
        c = {**c, "ok": False, "reason": f"|skew| > {max_skew} ms (paper policy limit)"}
    ck.data = {**c, "limit_ms": max_skew}
    ck.result = "PASS" if c["ok"] else "FAIL"
    ck.detail = (f"{c['skew_ms']:+d} ms (round trip {c['rtt_ms']} ms; limit {max_skew} ms)" if c["ok"]
                 else str(c.get("reason")))

    resolver = ContractResolver(session)
    ct = items["contracts"]
    resolved = []
    if not universe:
        ct.detail = f"not run: no registered universe ({universe_source}); run bootstrap_paper or pass --universe"
    else:
        rows, bad = [], []
        for u in universe:
            res = resolver.resolve(ListingQuery(symbol=u["symbol"], currency=u["currency"],
                                                primary_exchange=u["primary_exchange"]),
                                   instrument_id=u["instrument_id"], settlement_days=US_SETTLEMENT_DAYS)
            match = None if u["registered_con_id"] is None else res.con_id == u["registered_con_id"]
            rows.append({"instrument_id": u["instrument_id"], "symbol": u["symbol"],
                         "primary_exchange": u["primary_exchange"], "status": res.status, "con_id": res.con_id,
                         "registered_con_id": u["registered_con_id"], "matches_registration": match,
                         "reasons": list(res.reasons)[:5]})
            if res.status != "VERIFIED" or match is False:
                bad.append(f"{u['symbol']}:{u['primary_exchange']} {res.status}"
                           + (f" conId {res.con_id} != registered {u['registered_con_id']}" if match is False else ""))
            else:
                resolved.append((u, res.con_id))
        ct.data = {"universe_source": universe_source, "listings": rows}
        ct.result = "FAIL" if bad else "PASS"
        ct.detail = "; ".join(bad) if bad else f"{len(rows)} listing(s) VERIFIED" + (
            ", conIds match the registration" if any(r["registered_con_id"] for r in rows) else "")

    md = items["market_data_type"]
    if not resolved:
        md.detail = "not run: no verified contract to subscribe"
    else:
        per, verdicts = [], []
        for u, con_id in resolved:
            key = f"EVIDENCE:{u['instrument_id']}"
            r = session.subscribe(key, ContractSpec(con_id=con_id, currency=u["currency"]))
            sub = None
            if r.ok:
                session.wait(lambda: (lambda s: s is not None and (s["delivered_type"] is not None or s["unavailable"]))(
                    session.subscription(key)), wait_s)
                sub = session.subscription(key)
                session.unsubscribe(key)
            entitled, why = _entitlement(sub) if r.ok else (None, r.reason)
            verdicts.append(entitled)
            per.append({"instrument_id": u["instrument_id"], "requested_type":
                        MARKET_DATA_TYPES[md_cfg.requested_market_data_type],
                        "delivered_type": sub.get("delivered_type") if sub else None,
                        "delivered_type_name": sub.get("delivered_type_name") if sub else None,
                        "realtime_entitled": entitled, "reason": why})
        md.data = {"instruments": per}
        md.result = "FAIL" if False in verdicts else ("INCONCLUSIVE" if None in verdicts else "PASS")
        md.detail = "; ".join(f"{p['instrument_id']}: {p['delivered_type_name'] or 'nothing'}" for p in per)

    em = items["error_mapping"]
    res = resolver.resolve(ListingQuery(con_id=INVALID_CON_ID, currency="USD"), instrument_id="EVIDENCE:INVALID",
                           settlement_days=None)
    em.data = {"request": f"reqContractDetails conId {INVALID_CON_ID}", "expected": "IBKR 200 -> UNRESOLVED",
               "status": res.status, "reasons": list(res.reasons)[:3]}
    got_200 = any(str(r).startswith("IBKR 200") for r in res.reasons)
    em.result = "PASS" if res.status == "UNRESOLVED" and got_200 else "FAIL"
    em.detail = f"{res.status}: {'; '.join(res.reasons)[:160]}"


def _account_items(adapter, items) -> None:
    sn = items["account_snapshot"]
    try:
        snap = adapter.account_snapshot()
        sn.data = {"account_type": snap.raw.get("account_type"), "complete": snap.complete,
                   "incomplete_reasons": snap.raw.get("incomplete_reasons"),
                   "cash": [{"currency": c.currency, "settled": _dec(c.settled), "total": _dec(c.total),
                             "settled_source": c.settled_source} for c in snap.cash],
                   "settlement_evidence": snap.raw.get("settlement_evidence"),
                   "positions": [{"provider_ref": p.provider_ref, "instrument_id": p.instrument_id,
                                  "quantity": _dec(p.quantity), "average_cost": _dec(p.average_cost),
                                  "currency": p.currency} for p in snap.positions],
                   "unmapped_positions": [p.provider_ref for p in snap.positions if p.instrument_id is None]}
        sn.result = "PASS" if snap.complete else "FAIL"
        sn.detail = (f"AccountType {sn.data['account_type']!r}, {len(snap.cash)} cash row(s), "
                     f"{len(snap.positions)} position(s)" + (", unmapped: " + ",".join(sn.data["unmapped_positions"])
                                                              if sn.data["unmapped_positions"] else "")
                     + "".join(f", {c.currency} settled = {c.settled_source}" for c in snap.cash if c.settled_source)
                     if snap.complete else "incomplete: " + "; ".join(snap.raw.get("incomplete_reasons") or []))
    except Exception as exc:
        sn.result, sn.detail = "FAIL", f"{type(exc).__name__}: {exc}"

    oo = items["open_orders"]
    try:
        rows, complete = adapter.open_orders()
        oo.data = {"complete": complete, "orders": [
            {"broker_order_id": r.broker_order_id, "client_order_id": r.client_order_id, "order_ref": r.order_ref,
             "instrument_id": r.instrument_id, "side": r.side, "status": r.status.value,
             "filled": _dec(r.filled_quantity), "remaining": _dec(r.remaining_quantity),
             "foreign": r.client_order_id is None} for r in rows]}
        oo.result = "PASS" if complete else "FAIL"
        oo.detail = (f"{len(rows)} open order(s), {sum(1 for r in rows if r.client_order_id is None)} foreign"
                     if complete else "openOrderEnd not received for every request")
    except Exception as exc:
        oo.result, oo.detail = "FAIL", f"{type(exc).__name__}: {exc}"

    ex, cm = items["executions"], items["commissions"]
    try:
        batch = adapter.executions_since(None)
        ex.data = {"complete": batch.complete, "watermark": batch.watermark, "executions": [
            {"exec_key": e.exec_key, "time_text": e.raw.get("time"), "time_source": e.raw.get("time_source"),
             "side": e.side, "quantity": _dec(e.quantity), "price": _dec(e.price), "currency": e.currency,
             "order_ref": e.order_ref, "correction_of": e.correction_of} for e in batch.executions]}
        ex.result = "PASS" if batch.complete else "FAIL"
        ex.detail = (f"{len(batch.executions)} execution(s)" + (
            f"; Execution.time format e.g. {batch.executions[0].raw.get('time')!r}" if batch.executions else "")
            if batch.complete else "execDetailsEnd not received (or a history gap)")
        with_report = {c.exec_key for c in batch.commissions}
        missing = [e.exec_key for e in batch.executions if e.exec_key not in with_report]
        cm.data = {"commissions": [{"exec_key": c.exec_key, "amount": _dec(c.amount), "currency": c.currency}
                                   for c in batch.commissions], "executions_without_report": missing}
        cm.result = "PASS" if batch.complete else "FAIL"
        cm.detail = (f"{len(batch.commissions)} commission report(s)" +
                     (f"; {len(missing)} execution(s) without a report yet" if missing else ""))
    except Exception as exc:
        ex.result, ex.detail = "FAIL", f"{type(exc).__name__}: {exc}"
        cm.result, cm.detail = "FAIL", "executions query failed"


# ----------------------------------------------------------------------------------------------- output
def render_table(record: dict) -> str:
    lines = [f"| # | Item | Checklist | Result | Detail |", "|---|---|---|---|---|"]
    for n, it in enumerate(record["items"], 1):
        detail = it["detail"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {n} | {it['title']} | {it['checklist']} | {it['result']} | {detail[:220]} |")
    s = record.get("summary") or {}
    lines.append("")
    lines.append("Summary: " + ", ".join(f"{k} {s.get(k, 0)}" for k in RESULTS) +
                 (" - every read-only item passed" if record.get("ok") else ""))
    return "\n".join(lines)


def write_record(record: dict, evidence_dir: Path) -> Path:
    """Write ``ibkr-paper-<UTC>.json`` (mode 600) and its ``.md`` table; refuses a directory inside a git tree."""
    from .config import _inside_git_work_tree
    evidence_dir = Path(evidence_dir).expanduser()
    if _inside_git_work_tree(evidence_dir / "x"):
        raise ValueError(f"{evidence_dir} is inside a git work tree; evidence never goes into the repository")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = record["run_at_utc"].replace("-", "").replace(":", "")
    path = evidence_dir / f"ibkr-paper-{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = evidence_dir / f"ibkr-paper-{stamp}-{n}.json"
    for p, text in ((path, json.dumps(record, indent=2, sort_keys=True, default=str)),
                    (path.with_suffix(".md"), f"# IBKR paper evidence {record['run_at_utc']}\n\n{record['table']}\n")):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m unknown.broker.ibkr.evidence", description=__doc__.split("\n\n")[0])
    ap.add_argument("--account", required=True, help="internal account key, e.g. IBKR-PAPER-1")
    ap.add_argument("--client-id", type=int, default=DEFAULT_MD_CLIENT_ID,
                    help=f"API client id of the read-only market-data connection (default {DEFAULT_MD_CLIENT_ID})")
    ap.add_argument("--account-client-id", type=int, default=DEFAULT_ACCOUNT_CLIENT_ID,
                    help=f"API client id of the account-data connection (default {DEFAULT_ACCOUNT_CLIENT_ID})")
    ap.add_argument("--universe", default=None,
                    help="SYMBOL:PRIMARY_EXCHANGE,... used only when no instrument is registered in the core")
    ap.add_argument("--wait-s", type=float, default=10.0, help="seconds to wait for each marketDataType report")
    ap.add_argument("--config", default=None, help="IBKR config (default ~/.unknown/ibkr.json)")
    ap.add_argument("--home", default=None, help="runtime root (default ~/.unknown or $UNKNOWN_HOME)")
    ap.add_argument("--json", action="store_true", help="also print the record as JSON")
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()
    try:
        cfg = load_config(args.config)
        md_cfg = load_market_data_config(paths.root)
        universe, source = registered_universe(paths, args.account)
        if not universe and args.universe:
            universe, source = universe_from_text(args.universe), f"--universe (nothing registered: {source})"
        reserved = {}
        try:
            bus = json.loads((paths.root / "config" / "marketdata.json").read_text())
            bus_id = ((bus.get("sources") or {}).get("IBKR") or {}).get("client_id")
            if isinstance(bus_id, int):
                reserved["market-data bus source"] = bus_id
        except (OSError, ValueError, AttributeError):
            pass
        skew = json.loads(PAPER_POLICY.read_text())["account_limits"]["max_clock_skew_ms"]
        record = run_evidence(cfg, md_cfg, account_key=args.account, universe=universe, universe_source=source,
                              md_client_id=args.client_id, account_client_id=args.account_client_id,
                              reserved_client_ids=reserved, max_clock_skew_ms=skew, wait_s=args.wait_s)
        path = write_record(record, paths.root / "evidence")
    except (IbkrConfigError, MarketDataConfigError, ValueError, ImportError) as exc:
        print(f"evidence not run: {exc}", file=sys.stderr)
        return 2
    print(f"\nevidence record: {path} (and {path.with_suffix('.md').name}); copy the outcome into docs/status/STATUS.md")
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True, default=str))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
