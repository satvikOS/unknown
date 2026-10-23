"""Online backups, restore drills and restores (ADR-0003 #6, ADR-0010, runbook RB-10).

``take_backup`` copies a live core database with SQLite's online backup from a
separate read-only connection (WAL lets it run while the core writes; it never
takes the writer lease). Each copy is marked inside itself as a backup, so
opening it anywhere is detected as a restore: the stop latches and in-flight
orders become UNKNOWN until reconciled. It then copies the account's
broker-event journal (``<data>/<account>.events``, BR-ORD-20) the same way, to
``<account>-<UTC time>.events`` next to the database copy. The journal is
copied AFTER the database: every entry is journaled before it is applied, so
every journal command in the database copy has its entry in the journal copy
(the journal copy may hold a few newer entries, which a restore applies). A
JSON sidecar records the source epoch, last event sequence, database identity,
integrity check and SHA-256 of the copy, and what the journal copy holds.

``restore_drill`` proves a backup is usable without touching live state: in a
temporary directory it checks the SHA-256 and SQLite integrity, verifies the
hash chain, replays the event log into an empty database and compares every
projection, then opens the copy as the core would and checks that the restore
was detected and the stop is latched. With a journal copy it also checks the
copy's SHA-256, integrity, chain, account and database identity, and that it
covers every journal command in the database copy, then applies it to the
opened copy as the core's start-up replay would
(``GatewayRunner.replay_journal``); the ledger is verified after that.

``restore_backup`` puts a backup in place for a real restore, only while no core
holds the lease, and moves the old database with its ``-wal``/``-shm`` aside so a
stale write-ahead log can never be replayed onto the restored file. The live
journal stays in place when it belongs to the restored database (same identity:
it is at least as new as the backup's journal copy, and the next start applies
what the restored database lacks); otherwise it moves aside with the old
database and the backup's journal copy, if any, is put in its place."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import ledger
from ..core.desk import open_desk
from ..core.replay import replay_and_compare
from ..domain.clock import SystemClock
from ..domain.ids import IdGenerator
from ..gateway.journal import COMMAND_ID_PREFIX, EventJournal, JournalUnavailable, backup_journal, describe, describe_file
from ..gateway.runner import GatewayRunner
from ..service.paths import Paths
from ..store.db import backup_database, database_identity, lease_alive

PARTIAL_MAX_AGE_S = 3600


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _name_pattern(account_key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(account_key)}-\d{{8}}T\d{{12}}Z\.sqlite\Z")


def _taken_at(path: Path) -> int:
    try:
        return int(json.loads(path.with_suffix(".json").read_text())["taken_at_ns"])
    except (OSError, ValueError, KeyError):
        return -1


def backups_for(paths: Paths, account_key: str) -> list[Path]:
    """This account's backups only (exact name match: ``A`` never matches ``A-2``), oldest first."""
    pattern = _name_pattern(account_key)
    found = [p for p in paths.backups.glob("*.sqlite") if pattern.match(p.name)] if paths.backups.is_dir() else []
    return sorted(found, key=lambda p: (_taken_at(p), p.name))


def journal_file(paths: Paths, account_key: str) -> Path:
    """The account's live broker-event journal (``CoreService``)."""
    return paths.data / f"{account_key}.events"


def _sweep(paths: Paths, account_key: str, now_s: float) -> None:
    for partial in paths.backups.glob(f"{glob_escape(account_key)}-*.partial"):
        if now_s - partial.stat().st_mtime > PARTIAL_MAX_AGE_S:
            partial.unlink(missing_ok=True)
    # A journal copy or sidecar whose database copy never landed (the database copy is renamed into place last).
    pattern = _name_pattern(account_key)
    for suffix in (".events", ".json"):
        for orphan in paths.backups.glob(f"{glob_escape(account_key)}-*{suffix}"):
            database_copy = orphan.with_suffix(".sqlite")
            if (pattern.match(database_copy.name) and not database_copy.exists()
                    and now_s - orphan.stat().st_mtime > PARTIAL_MAX_AGE_S):
                orphan.unlink(missing_ok=True)


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _copy_journal(paths: Paths, account_key: str, target: Path, database_id: str | None) -> dict | None:
    """Copy the live journal next to the database copy ``target`` (online, one consistent snapshot); None if
    there is no journal. A journal that cannot be copied does not cost the database backup: the sidecar records
    the error and the restore drill reports it as a problem."""
    source = journal_file(paths, account_key)
    if not source.exists():
        return None
    final, tmp = target.with_suffix(".events"), target.with_suffix(".events.partial")
    try:
        info = backup_journal(source, tmp)
        if info["integrity_check"] != "ok":
            raise sqlite3.DatabaseError(f"the copy fails integrity_check: {info['integrity_check']}")
        info.update(file=final.name, sha256=_sha256(tmp), bytes=tmp.stat().st_size)
        _fsync(tmp)
        os.replace(tmp, final)
    except (sqlite3.Error, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        tmp.unlink(missing_ok=True)
    if info.get("database_id") not in (None, database_id):
        info["problem"] = f"the journal belongs to core database {info['database_id']}, not this one ({database_id})"
    return info


def take_backup(paths: Paths, account_key: str, *, keep: int = 96, now_ns: int | None = None) -> dict:
    """Back up ``<data>/<account>.sqlite`` into ``<backups>/<account>-<UTC time>.sqlite`` (mode 600), then its
    broker-event journal into ``<account>-<UTC time>.events``; keep the newest ``keep``."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    source = paths.db(account_key)
    if not source.exists():
        raise FileNotFoundError(f"no core database at {source}")
    paths.ensure()
    now_ns = now_ns if now_ns is not None else time.time_ns()
    _sweep(paths, account_key, time.time())
    stamp = datetime.fromtimestamp(now_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = paths.backups / f"{account_key}-{stamp}.sqlite"
    tmp = target.with_suffix(".partial")
    old_umask = os.umask(0o077)   # the copy is created mode 600, never briefly readable by others
    try:
        meta = backup_database(source, tmp, now_ns)   # the database first, then the journal (module docstring)
        if meta["integrity_check"] != "ok":
            raise RuntimeError(f"backup failed its integrity check: {meta['integrity_check']}")
        meta = {**meta, "account_key": account_key, "file": target.name, "sha256": _sha256(tmp),
                "bytes": tmp.stat().st_size}
        meta["journal"] = _copy_journal(paths, account_key, target, meta.get("database_id"))
        target.with_suffix(".json").write_text(json.dumps(meta, indent=2, sort_keys=True))
        os.replace(tmp, target)
    finally:
        os.umask(old_umask)
        tmp.unlink(missing_ok=True)
    for old in backups_for(paths, account_key)[:-keep]:
        if old != target:
            old.unlink(missing_ok=True)
            old.with_suffix(".events").unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
    return meta


def restore_drill(backup: Path, account_key: str, *, require_sidecar: bool = True) -> dict:
    """Prove ``backup`` restores: integrity, chain and replay equivalence, restore detection, a latched stop, and
    its journal copy applied as the core would apply it."""
    backup = Path(backup)
    report: dict = {"backup": str(backup), "problems": []}
    sidecar = backup.with_suffix(".json")
    meta: dict = {}
    if sidecar.exists():
        meta = json.loads(sidecar.read_text())
        report["sha256_matches_sidecar"] = meta.get("sha256") == _sha256(backup)
        if not report["sha256_matches_sidecar"]:
            report["problems"].append("backup file does not match its recorded SHA-256")
    elif require_sidecar:
        report["sha256_matches_sidecar"] = None
        report["problems"].append("no sidecar: the backup's SHA-256 cannot be checked")
    with tempfile.TemporaryDirectory(prefix="unknown-restore-drill-") as d:
        home = Paths(Path(d))
        home.ensure()
        copy = home.db(account_key)
        shutil.copyfile(backup, copy)
        conn = sqlite3.connect(str(copy))
        try:
            report["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            has_origin = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_origin'").fetchone()
            origin = conn.execute("SELECT source_max_seq FROM backup_origin ORDER BY rowid DESC LIMIT 1").fetchone() if has_origin else None
            max_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0] if report["integrity_check"] == "ok" else None
        except sqlite3.DatabaseError as exc:
            report["integrity_check"], origin, max_seq = f"{type(exc).__name__}: {exc}", None, None
        finally:
            conn.close()
        if report["integrity_check"] != "ok":
            report["problems"].append(f"SQLite integrity check: {report['integrity_check']}")
            report["ok"] = False
            return report
        if origin is None:
            report["problems"].append("no backup_origin marker: not a backup taken by this tool")
        elif origin[0] != max_seq:
            report["problems"].append(f"marker claims event seq {origin[0]} but the copy holds {max_seq}")
        replay_problems = replay_and_compare(copy, Path(d) / "replay.sqlite")
        report["replay_problems"] = replay_problems[:50]
        report["problems"] += [f"replay: {p}" for p in replay_problems[:50]]
        desk = open_desk(copy, SystemClock(), IdGenerator(lambda: time.time_ns() // 1_000_000),
                         deployment_id="restore-drill", holder="restore-drill", takeover=True)
        try:
            report["database_id"] = desk.store.database_id
            # As the core starts: the journal copy is applied before anything else, then everything is checked.
            report["journal"] = _drill_journal(backup, meta.get("journal"), home, account_key, desk, report["problems"])
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


def _drill_journal(backup: Path, recorded: dict | None, home: Paths, account_key: str, desk, problems: list[str]) -> dict:
    """Check the backup's journal copy and apply it to the opened database copy; returns what was found.

    No copy and none recorded: the core had no journal when the backup was taken (or the backup predates
    journal copies); nothing to check."""
    copy = backup.with_suffix(".events")
    out: dict = {"file": str(copy) if copy.exists() else None}
    if isinstance(recorded, dict) and "error" in recorded:
        problems.append(f"journal: the backup has no journal copy ({recorded['error']})")
        return {**out, "error": recorded["error"]}
    if not copy.exists():
        if recorded:
            problems.append("journal: the sidecar records a journal copy but the file is missing")
        return out
    if recorded:
        out["sha256_matches_sidecar"] = recorded.get("sha256") == _sha256(copy)
        if not out["sha256_matches_sidecar"]:
            problems.append("journal: the copy does not match its recorded SHA-256")
    target = journal_file(home, account_key)
    shutil.copyfile(copy, target)
    held = None
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            out["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if out["integrity_check"] == "ok":
                held = describe(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        out["integrity_check"] = f"{type(exc).__name__}: {exc}"
    if held is None:
        problems.append(f"journal: SQLite integrity check: {out['integrity_check']}")
        return out
    out.update(held)
    database_id = desk.store.database_id
    if held["account_key"] != account_key:
        problems.append(f"journal: it belongs to account {held['account_key']!r}, not {account_key!r}")
        return out
    if held["database_id"] not in (None, database_id):
        problems.append(f"journal: it belongs to core database {held['database_id']}, not the backup's ({database_id})")
        return out
    covered = max(held["last_seq"] or 0, held["pruned_through"] or 0)
    seqs = [int(r["command_id"].rsplit(".", 1)[1]) for r in desk.store.query(
        "SELECT command_id FROM commands WHERE command_id LIKE ?", (f"{COMMAND_ID_PREFIX}{held['journal_id']}.%",))]
    if seqs and max(seqs) > covered:
        problems.append(f"journal: the copy ends at entry {covered} but the database copy applied entry {max(seqs)} "
                        "(the journal was copied before the database)")
    journal = EventJournal(target, account_key, desk.clock.now_ns)
    try:
        chain = journal.verify()
        problems += [f"journal: {p}" for p in chain[:50]]
        if not chain:
            try:
                out["applied"] = GatewayRunner(desk, None, account_key, journal=journal).replay_journal()
                out["unapplied_after"] = journal.unapplied_count()
            except JournalUnavailable as exc:
                problems.append(f"journal: the start-up replay failed: {type(exc).__name__}: {exc}")
    except JournalUnavailable as exc:
        problems.append(f"journal: {exc}")
    finally:
        journal.close()
    return out


def _journal_plan(paths: Paths, account_key: str, backup: Path) -> dict:
    """What a restore does with the journal, decided before anything moves (``restore_backup``)."""
    live = journal_file(paths, account_key)
    check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        backup_database_id = database_identity(check)
    finally:
        check.close()
    copy = backup.with_suffix(".events")
    plan: dict = {"backup_database_id": backup_database_id, "backup_copy": str(copy) if copy.exists() else None,
                  "live": None}
    if live.exists():
        try:
            plan["live"] = describe_file(live)
        except sqlite3.Error as exc:
            plan["live"] = {"error": f"{type(exc).__name__}: {exc}"}
        if backup_database_id is not None and plan["live"].get("database_id") == backup_database_id:
            plan["action"] = "kept"
            plan["why"] = ("the live journal belongs to the restored database and is at least as new as the backup's "
                           "copy: the next start applies the entries the restored database lacks")
            return plan
    if copy.exists():
        plan["action"], plan["why"] = "installed", "the backup's journal copy belongs to the restored database"
    elif live.exists():
        plan["action"], plan["why"] = "moved_aside", "the live journal is not the restored database's; the backup has none"
    else:
        plan["action"], plan["why"] = "none", "no journal: the next start creates one"
    return plan


def _restore_journal(paths: Paths, account_key: str, plan: dict, aside: Path) -> dict:
    live = journal_file(paths, account_key)
    out = {**plan, "moved_aside": [], "aside_dir": None}
    if plan["action"] == "kept":
        return out
    for suffix in ("", "-wal", "-shm"):
        f = Path(f"{live}{suffix}")
        if f.exists():
            aside.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(f, aside / f.name)
            out["moved_aside"].append(f.name)
            out["aside_dir"] = str(aside)
    if plan["backup_copy"]:
        tmp = paths.data / f".{account_key}.events.restoring"
        tmp.unlink(missing_ok=True)
        old_umask = os.umask(0o077)
        try:
            src = sqlite3.connect(f"file:{plan['backup_copy']}?mode=ro", uri=True)
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("restored journal copy fails integrity_check")
            finally:
                dst.close()
                src.close()
            _fsync(tmp)
            os.replace(tmp, live)
            _fsync(paths.data)
        finally:
            os.umask(old_umask)
            tmp.unlink(missing_ok=True)
    return out


def restore_backup(paths: Paths, account_key: str, backup: Path, *, now_ns: int | None = None) -> dict:
    """Put ``backup`` in place as ``<data>/<account>.sqlite`` for a real restore (RB-10).

    Refuses while a core holds the lease. The current database and its ``-wal``/``-shm`` move together into
    ``<data>/replaced-<account>-<UTC time>/``. The backup is copied with SQLite's backup API into a temporary
    file, checked, fsynced and renamed into place. The journal: kept when it belongs to the restored database,
    else moved into the same directory and replaced by the backup's journal copy (module docstring). Starting
    the core then detects the restore, latches the stop and applies what the restored database lacks."""
    backup = Path(backup)
    live = paths.db(account_key)
    now_ns = now_ns if now_ns is not None else time.time_ns()
    if live.exists() and lease_alive(live, now_ns):
        raise RuntimeError("a core process holds the writer lease: stop it (launchctl bootout ...) before restoring")
    check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        raise RuntimeError(f"backup fails integrity_check: {ok}")
    plan = _journal_plan(paths, account_key, backup)
    stamp = datetime.fromtimestamp(now_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    aside = paths.data / f"replaced-{account_key}-{stamp}"
    moved = []
    for suffix in ("", "-wal", "-shm"):
        f = Path(f"{live}{suffix}")
        if f.exists():
            aside.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(f, aside / f.name)
            moved.append(f.name)
    tmp = paths.data / f".{account_key}.restoring"
    tmp.unlink(missing_ok=True)
    old_umask = os.umask(0o077)
    try:
        src = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("restored copy fails integrity_check")
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, live)
    finally:
        os.umask(old_umask)
        tmp.unlink(missing_ok=True)
    journal = _restore_journal(paths, account_key, plan, aside)
    return {"restored": str(live), "from": str(backup), "moved_aside": moved, "aside_dir": str(aside) if moved else None,
            "journal": journal,
            "next": "start the core: it detects the restore, latches the stop and applies the journal entries the "
                    "restored database lacks; then reconcile, resolve, green start"}
