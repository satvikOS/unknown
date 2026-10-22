"""Online backups and restore drills (ADR-0003 #6, ADR-0010, runbooks RB-06/RB-07).

``take_backup`` copies a live core database with SQLite's online backup from a
separate read-only connection (WAL lets it run while the core writes; it never
takes the writer lease). Each copy is marked inside itself as a backup, so
opening it anywhere is detected as a restore: the stop latches and in-flight
orders become UNKNOWN until reconciled. A JSON sidecar records the source
epoch, last event sequence, integrity check and SHA-256 of the copy.

``restore_drill`` proves a backup is usable without touching live state: in a
temporary directory it verifies the hash chain, replays the event log into an
empty database and compares every projection, then opens the copy as the core
would and checks that the restore was detected and the stop is latched."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import ledger
from ..core.desk import open_desk
from ..core.replay import replay_and_compare
from ..domain.clock import SystemClock
from ..domain.ids import IdGenerator
from ..service.paths import Paths
from ..store.db import backup_database


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backups_for(paths: Paths, account_key: str) -> list[Path]:
    return sorted(paths.backups.glob(f"{account_key}-*.sqlite"))


def take_backup(paths: Paths, account_key: str, *, keep: int = 96, now_ns: int | None = None) -> dict:
    """Back up ``<data>/<account>.sqlite`` into ``<backups>/<account>-<UTC time>.sqlite`` (mode 600) and keep the newest ``keep``."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    source = paths.db(account_key)
    if not source.exists():
        raise FileNotFoundError(f"no core database at {source}")
    paths.ensure()
    now_ns = now_ns if now_ns is not None else time.time_ns()
    stamp = datetime.fromtimestamp(now_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = paths.backups / f"{account_key}-{stamp}.sqlite"
    tmp = target.with_suffix(".partial")
    meta = backup_database(source, tmp, now_ns)
    if meta["integrity_check"] != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed its integrity check: {meta['integrity_check']}")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    meta = {**meta, "account_key": account_key, "file": target.name, "sha256": _sha256(target),
            "bytes": target.stat().st_size}
    sidecar = target.with_suffix(".json")
    sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True))
    os.chmod(sidecar, 0o600)
    for old in backups_for(paths, account_key)[:-keep]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    return meta


def restore_drill(backup: Path, account_key: str) -> dict:
    """Prove ``backup`` restores: chain and replay equivalence, then restore detection and a latched stop."""
    backup = Path(backup)
    report: dict = {"backup": str(backup), "problems": []}
    sidecar = backup.with_suffix(".json")
    if sidecar.exists():
        expected = json.loads(sidecar.read_text()).get("sha256")
        actual = _sha256(backup)
        report["sha256_matches_sidecar"] = expected == actual
        if expected != actual:
            report["problems"].append("backup file does not match its recorded SHA-256")
    with tempfile.TemporaryDirectory(prefix="unknown-restore-drill-") as d:
        home = Paths(Path(d))
        home.ensure()
        copy = home.db(account_key)
        shutil.copyfile(backup, copy)
        replay_problems = replay_and_compare(copy, Path(d) / "replay.sqlite")
        report["replay_problems"] = replay_problems[:50]
        report["problems"] += [f"replay: {p}" for p in replay_problems[:50]]
        desk = open_desk(copy, SystemClock(), IdGenerator(lambda: time.time_ns() // 1_000_000),
                         deployment_id="restore-drill", holder="restore-drill", takeover=True)
        try:
            with desk.store.read() as tx:
                control = {r["account_key"]: json.loads(r["row"]) for r in tx.all("SELECT account_key, row FROM control")}
                kinds = sorted({json.loads(r["row"])["kind"] for r in tx.all("SELECT row FROM exceptions WHERE status='OPEN'")})
                unknown = tx.one("SELECT COUNT(*) AS n FROM orders WHERE json_extract(row,'$.state')='UNKNOWN'")["n"]
                verify = ledger.verify(tx)
        finally:
            desk.store.close()
        c = control.get(account_key)
        report.update(restore_detected="RESTORED_FROM_BACKUP" in kinds, open_exception_kinds=kinds, unknown_orders=unknown,
                      stop_latched=bool(c and c["stop_latched"]), latch_reason=c.get("latch_reason") if c else None,
                      ledger_problems=verify[:50])
        if c is None:
            report["problems"].append(f"account {account_key} not found in the backup")
        elif not report["restore_detected"] or not report["stop_latched"]:
            report["problems"].append("the restore was not detected or the stop is not latched")
        report["problems"] += [f"ledger: {p}" for p in verify[:50]]
    report["ok"] = not report["problems"]
    return report
