"""Durable raw broker-event journal (book-review finding BR-ORD-20; ADR-0003, ADR-0005 #5, ADR-0012).

The gateway appends every broker event it takes from an adapter here, in one
fsync'd transaction, BEFORE any of it is applied to the core. Applying an
entry is an ordinary core command whose id is derived from the entry
(``sys.bev.<journal id>.<seq>``), and an entry is marked applied after the
command returns. So:

* a crash between the append and the apply loses nothing: the next process
  applies the unapplied entries in order before any new event;
* a crash between the apply and the mark re-applies the entry under the same
  command id and payload, which the core answers with the stored result
  (``Result.duplicate``) and no new event; the handlers' scoped broker keys
  (``exec_key``, the order's own state) make even a re-application under a new
  command id a duplicate, never a second economic effect;
* the in-memory hand-off is bounded: events wait in process memory only until
  the next append, and if the journal cannot be written the gateway stops
  taking events and the desk fails closed (``GatewayRunner`` latches the stop
  and requires reconciliation).

Storage: ``<UNKNOWN_HOME>/data/<account>.events``, an SQLite file opened with
the core store's durability settings (WAL, ``synchronous=FULL``, F_FULLFSYNC on
macOS; ``store.db.connect``). Tables are append-only (triggers refuse UPDATE
and DELETE; only ``prune`` deletes, see below). Each entry carries a SHA-256
hash chained to the previous entry; entries are re-verified before they are
applied. Marks are committed with ``synchronous=NORMAL``: a mark lost to a
power failure only causes a duplicate re-application, never a lost or doubled
event.

A journal belongs to one core database. The core database has a stable
identity (``Store.database_id``: a random id written once and carried by its
backups), and the journal records it (``database_id`` in ``meta``) when it is
created, or, for a journal written before identities existed, when the
gateway first admits it. ``GatewayRunner`` refuses a journal of another
database (a database deliberately recreated, or restored from another
database's backup, next to the old journal): none of its entries is applied,
the file is set aside (``set_aside``) and reconciliation is required.

The core, not the marks, decides what was applied: ``reapply_after`` makes
marked entries unapplied again when the core does not have their commands (a
core database restored from an older backup: ``GatewayRunner.rewind_to_core``
re-applies them in journal order, relying on the handlers' broker keys for
anything the restored core already holds under other command ids).

A broken journal is set aside, never silently skipped: an entry that fails
its checksum or breaks the chain (``JournalCorrupt``), a file SQLite reports
as damaged, or an entry whose command id the core holds for a different
command (``JournalConflict``: a journal older than the core). The verified
entries before the broken one are applied first; then the gateway records the
failure in the core (stop latched, INCOMPLETE reconciliation), moves the file
with its ``-wal``/``-shm`` into ``<account>.events.set-aside-<UTC time>-<journal
id>/`` with a ``report.json``, and continues on a fresh journal (new journal
id, same database). Events from the broken entry on are left to the
reconciliation the desk then requires, which recovers executions only inside
the broker's history window; the set-aside file keeps them as evidence. A file
that cannot be moved stays in place, failing closed: the failure is recorded
once and every drain tries the move again. Damage anywhere the gateway's
admission reads (entries, marks, prune records) sets the file aside too.

Retention: ``prune`` deletes applied entries (and their marks) appended more
than a retention ago, as a prefix of the journal, and records the last pruned
entry's seq and hash (``prunes``) so the chain still verifies from there and
seqs never restart. It never deletes an unapplied entry, anything after one,
or an entry a rewind will apply again. The core service runs it on a schedule
(default retention 30 days).

Long passes (verifying a large journal at start-up, pruning) call
``heartbeat`` every ``HEARTBEAT_EVERY`` rows, so the core's writer lease stays
fresh however large the journal is (review finding 57).

What this does not cover: an event the adapter received but the gateway had
not yet appended when the process died (the adapter's own memory) is
recovered only by reconciliation (executions inside the broker's history
window, statuses through the open-order list and lookups); so are entries
pruned before a database restored from a backup older than the retention."""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from ..domain.canonical import canonical_json, sha256_hex
from ..domain.errors import LeaseLost
from ..domain.fixed import Fixed
from ..domain.ids import validate_id

FORMAT = 1
GENESIS = "0" * 64
COMMAND_ID_PREFIX = "sys.bev."
HEARTBEAT_EVERY = 500      # rows read in a long pass between calls to ``heartbeat``
PRUNE_BATCH = 5000         # entries deleted per prune transaction

_NO_DELETE_ENTRIES = ("CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries "
                      "BEGIN SELECT RAISE(ABORT, 'event journal entries are append-only'); END")
_NO_DELETE_APPLIED = ("CREATE TRIGGER IF NOT EXISTS applied_no_delete BEFORE DELETE ON applied "
                      "BEGIN SELECT RAISE(ABORT, 'event journal marks are append-only'); END")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY, appended_ns INTEGER NOT NULL, "
    "event_type TEXT NOT NULL, event TEXT NOT NULL, command_kind TEXT NOT NULL, command_payload TEXT NOT NULL, "
    "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS applied (seq INTEGER PRIMARY KEY REFERENCES entries(seq), applied_ns INTEGER NOT NULL, "
    "command_id TEXT NOT NULL, outcome TEXT NOT NULL)",
    # One row per prune: every entry up to through_seq is deleted; through_hash is the last deleted entry's hash.
    "CREATE TABLE IF NOT EXISTS prunes (through_seq INTEGER PRIMARY KEY, through_hash TEXT NOT NULL, "
    "pruned_ns INTEGER NOT NULL, entries INTEGER NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries "
    "BEGIN SELECT RAISE(ABORT, 'event journal entries are append-only'); END",
    _NO_DELETE_ENTRIES,
    "CREATE TRIGGER IF NOT EXISTS applied_no_update BEFORE UPDATE ON applied "
    "BEGIN SELECT RAISE(ABORT, 'event journal marks are append-only'); END",
    _NO_DELETE_APPLIED,
    "CREATE TRIGGER IF NOT EXISTS meta_no_update BEFORE UPDATE ON meta "
    "BEGIN SELECT RAISE(ABORT, 'event journal meta is written once'); END",
    "CREATE TRIGGER IF NOT EXISTS meta_no_delete BEFORE DELETE ON meta "
    "BEGIN SELECT RAISE(ABORT, 'event journal meta is written once'); END",
    "CREATE TRIGGER IF NOT EXISTS prunes_no_update BEFORE UPDATE ON prunes "
    "BEGIN SELECT RAISE(ABORT, 'event journal prune records are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS prunes_no_delete BEFORE DELETE ON prunes "
    "BEGIN SELECT RAISE(ABORT, 'event journal prune records are append-only'); END",
)


class JournalUnavailable(RuntimeError):
    """The journal cannot be opened, written or trusted. Events are not taken from the broker."""


class JournalCorrupt(JournalUnavailable):
    """An entry fails its checksum, the chain is broken or SQLite reports the file damaged: nothing from it on is
    applied, and the journal is set aside."""


class JournalConflict(JournalUnavailable):
    """An entry's command id is recorded in the core for a different command: this journal is not the history
    of this core (for example a journal file rolled back to an older copy), so nothing from it on is applied,
    and the journal is set aside."""


class JournalSetAside(JournalUnavailable):
    """The journal was moved aside (``EventJournal.set_aside``): foreign, corrupt or conflicting. The failure is
    recorded in the core (stop latched, reconciliation required) before the file is moved; ``report`` is what
    ``set_aside`` returned (None if the move itself failed)."""

    report: dict | None = None


@dataclass(frozen=True)
class JournalEntry:
    journal_id: str
    seq: int
    appended_ns: int
    event_type: str
    event: str                 # the event as the adapter delivered it (JSON; diagnostic and audit)
    command_kind: str          # the core command it is applied as
    command_payload: dict
    hash: str

    @property
    def command_id(self) -> str:
        return f"{COMMAND_ID_PREFIX}{self.journal_id}.{self.seq}"


def _plain(value: Any) -> Any:
    if isinstance(value, Fixed):
        return value.to_str()
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    return {"__repr__": repr(value)}


def event_json(event: Any) -> str:
    """The event with every field, including the adapter's ``raw`` map, as sorted JSON text."""
    return json.dumps(_plain(event), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _entry_hash(prev_hash: str, seq: int, appended_ns: int, event_type: str, event: str, command_kind: str,
                command_payload: str) -> str:
    return sha256_hex(prev_hash + canonical_json({"seq": seq, "appended_ns": appended_ns, "event_type": event_type,
                                                  "event": event, "command_kind": command_kind,
                                                  "command_payload": command_payload}))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def is_damaged(exc: BaseException) -> bool:
    """Whether SQLite reports the file itself as damaged (not a transient failure such as a lock or a full disk)."""
    name = getattr(exc, "sqlite_errorname", None) or ""
    return isinstance(exc, sqlite3.DatabaseError) and name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB"))


def _utc_stamp(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _floor(conn: sqlite3.Connection) -> tuple[int, str]:
    """The last pruned entry (seq, hash), or (0, GENESIS) when nothing was pruned."""
    r = conn.execute("SELECT through_seq, through_hash FROM prunes ORDER BY through_seq DESC LIMIT 1").fetchone()
    return (r[0], r[1]) if r is not None else (0, GENESIS)


def _tail(conn: sqlite3.Connection) -> tuple[int, str]:
    """The last entry (seq, hash); the prune floor when every entry was pruned."""
    r = conn.execute("SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
    return (r[0], r[1]) if r is not None else _floor(conn)


def describe(conn: sqlite3.Connection) -> dict:
    """What a journal connection holds (backups, restores and reports); reads only."""
    has = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")} if "meta" in has else {}
    out = {"journal_id": meta.get("journal_id"), "account_key": meta.get("account_key"), "format": meta.get("format"),
           "database_id": meta.get("database_id"), "entries": 0, "first_seq": None, "last_seq": None, "marked": 0,
           "unmarked": 0, "pruned_through": 0}
    if "entries" in has:
        first, last, n = conn.execute("SELECT MIN(seq), MAX(seq), COUNT(*) FROM entries").fetchone()
        out.update(entries=n, first_seq=first, last_seq=last)
    if "applied" in has:
        out["marked"] = conn.execute("SELECT COUNT(*) FROM applied").fetchone()[0]
        if "entries" in has:
            out["unmarked"] = conn.execute("SELECT COUNT(*) FROM entries e WHERE NOT EXISTS "
                                           "(SELECT 1 FROM applied a WHERE a.seq = e.seq)").fetchone()[0]
    if "prunes" in has:
        out["pruned_through"] = _floor(conn)[0]
    return out


def describe_file(path: str | Path) -> dict:
    """``describe`` of a journal file through a read-only connection. Raises sqlite3.Error if it cannot be read."""
    from ..store.db import connect
    conn = connect(path, readonly=True)
    try:
        return describe(conn)
    finally:
        conn.close()


def backup_journal(source: str | Path, target: str | Path) -> dict:
    """Online copy of a journal file: SQLite's backup API from a separate read-only connection, so the copy is one
    consistent snapshot even while the core appends and marks. Returns ``describe`` of the copy plus its
    ``integrity_check``."""
    from ..store.db import connect
    src = connect(source, readonly=True)
    try:
        dest = sqlite3.connect(str(target))
        try:
            src.backup(dest)
            out = describe(dest)
            out["integrity_check"] = dest.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dest.close()
    finally:
        src.close()
    return out


class EventJournal:
    """One account's broker-event journal. Opened lazily; every operation may raise ``JournalUnavailable``.

    ``database_id`` is recorded in a journal file this object creates (the core service passes its database's
    identity); an existing file keeps its own, and ``GatewayRunner`` compares it with the core's. ``heartbeat``
    is called during long passes (see the module docstring).

    Not thread-safe: the core service uses it from its loop thread only."""

    def __init__(self, path: str | Path, account_key: str, now_ns: Callable[[], int], *, journal_id: str | None = None,
                 database_id: str | None = None, heartbeat: Callable[[], None] | None = None):
        self.path = Path(path)
        self.account_key = validate_id(account_key, "account key")
        self._now_ns = now_ns
        self._requested_id = journal_id
        self._bind_to = database_id
        self.heartbeat = heartbeat
        self._conn: sqlite3.Connection | None = None
        self._meta: dict[str, str] = {}
        self.journal_id: str | None = None
        self._applied_through = 0          # every seq <= this is applied (marked, and not to be re-applied)
        self._reapply_through = 0          # marked entries up to this seq are applied again (reapply_after)
        self._broken: JournalUnavailable | None = None   # a corrupt or conflicting entry: no appends, apply stops
        self.failure: str | None = None    # the last failure; cleared by the next successful append
        self.reported_failure: str | None = None   # the failure the gateway recorded in the core (episode)
        self.identity_checked = False   # GatewayRunner compared this file's database id with the core's
        self.rewound = False   # GatewayRunner.rewind_to_core ran for this file (before its first apply)
        self.set_asides: list[dict] = []   # reports of the files this object set aside (newest last)

    # ------------------------------------------------------------ open
    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        from ..store.db import connect
        conn = None
        try:
            created = not self.path.exists()
            conn = connect(self.path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in SCHEMA:
                    conn.execute(statement)
                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                if not meta:
                    journal_id = self._requested_id or os.urandom(8).hex()
                    validate_id(f"{COMMAND_ID_PREFIX}{journal_id}.1", "journal command id")
                    meta = {"journal_id": journal_id, "account_key": self.account_key, "format": str(FORMAT),
                            "created_ns": str(self._now_ns())}
                    if self._bind_to is not None:
                        meta["database_id"] = self._bind_to
                    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", sorted(meta.items()))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            if created:
                _fsync_dir(self.path.parent)   # the new file's directory entry is durable too
            if meta.get("account_key") != self.account_key:
                raise self._refused(f"it belongs to account {meta.get('account_key')!r}, not {self.account_key!r}")
            if meta.get("format") != str(FORMAT):
                raise self._refused(f"it has journal format {meta.get('format')!r}; expected {FORMAT}")
            if self._requested_id is not None and meta["journal_id"] != self._requested_id:
                raise self._refused(f"it has journal id {meta['journal_id']!r}, not {self._requested_id!r}")
            first = conn.execute("SELECT MIN(e.seq) AS s FROM entries e WHERE NOT EXISTS "
                                 "(SELECT 1 FROM applied a WHERE a.seq = e.seq)").fetchone()["s"]
            through = (first - 1) if first is not None else _tail(conn)[0]
            # Reopened after a dropped connection: keep a rewind (reapply_after) that is still being re-applied.
            self._applied_through = through if self.journal_id is None else min(through, self._applied_through)
            self.journal_id = meta["journal_id"]
            self._meta = meta
            self._conn = conn
            # Every unapplied entry (and its link to the last applied one) is checked before anything is applied.
            # A broken entry leaves the journal open: the verified entries before it can still be applied,
            # and ``append`` refuses from now on.
            try:
                self._verified_rows(self._applied_through, None)
            except (JournalCorrupt, JournalConflict):
                pass   # ``broken`` is set: unapplied() raises once the verified entries are applied
        except LeaseLost:
            raise   # from the heartbeat: the process stops; nothing about the journal is wrong
        except JournalUnavailable:
            self._drop(conn)
            raise
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            self._drop(conn)
            raise self._failed(exc) from exc
        return conn

    def open(self) -> "EventJournal":
        """Open (create, verify) the journal now. Raises ``JournalUnavailable``."""
        self._connection()
        return self

    def _drop(self, conn: sqlite3.Connection | None) -> None:
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _failed(self, exc: BaseException) -> JournalUnavailable:
        if is_damaged(exc):   # the file itself is damaged: nothing more is read from it; it is set aside
            error = JournalCorrupt(f"event journal {self.path.name}: SQLite reports the file damaged "
                                   f"({type(exc).__name__}: {exc}); nothing more is applied from it")
            error.seq = 0     # every entry
            return self._break(error)
        self.failure = f"{type(exc).__name__}: {exc}"
        return JournalUnavailable(f"event journal {self.path.name}: {self.failure}")

    def _refused(self, why: str) -> JournalUnavailable:
        self.failure = f"refused: {why}"
        return JournalUnavailable(f"event journal {self.path.name} refused: {why}")

    def close(self) -> None:
        self._drop(self._conn)

    def _beat(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat()

    @property
    def broken(self) -> JournalUnavailable | None:
        """The corrupt or conflicting entry that stops this journal (until it is set aside), if any."""
        return self._broken

    def _break(self, error: JournalUnavailable) -> JournalUnavailable:
        """Record the first broken entry (sticky for this journal file) and return a fresh error for it."""
        if self._broken is None:
            self._broken = error
        self.failure = f"{type(self._broken).__name__}: {self._broken}"
        return type(self._broken)(str(self._broken))

    def conflict(self, entry: JournalEntry, why: str) -> JournalConflict:
        """Mark the journal broken at ``entry`` (its command id names something else in the core); returns the error."""
        error = JournalConflict(f"event journal {self.path.name}: entry {entry.seq} conflicts with the core: {why}; "
                                "nothing from this entry on is applied")
        error.seq = entry.seq
        return self._break(error)

    # ------------------------------------------------------------ identity
    @property
    def database_id(self) -> str | None:
        """The core database this journal belongs to (None for a journal written before identities existed)."""
        self._connection()
        return self._meta.get("database_id")

    def bind(self, database_id: str) -> None:
        """Record the core database this journal belongs to, once; refuses to change it."""
        conn = self._connection()
        current = self._meta.get("database_id")
        if current == database_id:
            return
        if current is not None:
            raise self._refused(f"it belongs to core database {current}, not {database_id}")
        try:
            conn.execute("INSERT INTO meta(key, value) VALUES ('database_id', ?)", (database_id,))
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        self._meta["database_id"] = database_id

    def has_entries(self) -> bool:
        """Whether this journal ever held an entry (pruned entries count)."""
        conn = self._connection()
        try:
            return _tail(conn)[0] > 0
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    @property
    def pruned_through(self) -> int:
        """The last pruned entry's seq (0: nothing pruned). Entries up to it can never be applied again."""
        conn = self._connection()
        try:
            return _floor(conn)[0]
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    # ------------------------------------------------------------ set aside
    def set_aside(self, why: str, *, database_id: str | None) -> dict:
        """Move this journal file, with its ``-wal`` and ``-shm``, into ``<name>.set-aside-<UTC time>-<journal id>/``
        next to it, with a ``report.json``; then start over on a fresh file at the same path (a new journal id,
        recording ``database_id``). Nothing in the moved file is applied or deleted: it is the evidence for the
        reconciliation the caller requires. Returns the report. Raises ``JournalUnavailable`` if it cannot move."""
        now = self._now_ns()
        report: dict[str, Any] = {
            "reason": why, "path": str(self.path), "account_key": self.account_key, "journal_id": self.journal_id,
            "journal_database_id": self._meta.get("database_id"), "core_database_id": database_id,
            "set_aside_at_ns": now, "broken_seq": getattr(self._broken, "seq", None),
            "applied_through": self._applied_through}
        if self._conn is not None:
            try:
                report["contents"] = describe(self._conn)
            except sqlite3.Error as exc:
                report["contents"] = {"error": f"{type(exc).__name__}: {exc}"}
        self._drop(self._conn)
        aside = None
        try:
            base = f"{self.path.name}.set-aside-{_utc_stamp(now)}-{self.journal_id or 'unreadable'}"
            aside, n = self.path.parent / base, 1
            while aside.exists():
                n += 1
                aside = self.path.parent / f"{base}-{n}"
            aside.mkdir(mode=0o700)
            moved = []
            for suffix in ("", "-wal", "-shm"):
                f = Path(f"{self.path}{suffix}")
                if f.exists():
                    os.replace(f, aside / f.name)
                    moved.append(f.name)
            report.update(aside_dir=str(aside), moved=moved)
            fd = os.open(aside / "report.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(report, indent=2, sort_keys=True, default=str))
                fh.flush()
                os.fsync(fh.fileno())
            _fsync_dir(aside)
            _fsync_dir(self.path.parent)
        except OSError as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            if aside is not None:
                try:
                    aside.rmdir()   # only if nothing was moved into it: a retry must not leave a directory per cycle
                except OSError:
                    pass
            if not self.path.exists():   # the file is gone: never reopen a new one under the old journal's id
                self._start_fresh(database_id, why)
                self.set_asides.append(report)
            raise self._failed(exc) from exc
        self._start_fresh(database_id, why)
        self.set_asides.append(report)
        return report

    def _start_fresh(self, database_id: str | None, why: str) -> None:
        """A fresh journal file from here on: a new id (never the old one, whose command ids the core may hold)."""
        self._requested_id = None
        self._bind_to = database_id
        self._meta = {}
        self.journal_id = None
        self._applied_through = self._reapply_through = 0
        self._broken = None
        self.identity_checked = self.rewound = False
        self.failure = f"set aside: {why}"   # until the next successful append

    # ------------------------------------------------------------ write
    def append(self, records: Sequence[tuple[Any, str, dict]]) -> list[JournalEntry]:
        """Append ``(event, command_kind, command_payload)`` records in order, in ONE fsync'd transaction.

        All or nothing: on any failure nothing is appended and ``JournalUnavailable`` is raised."""
        if not records:
            return []
        prepared = [(type(ev).__name__, event_json(ev), kind, canonical_json(payload)) for ev, kind, payload in records]
        conn = self._connection()
        if self._broken is not None:   # appended behind a broken entry, an event could never be applied
            raise self._break(self._broken)
        entries: list[JournalEntry] = []
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                seq, prev = _tail(conn)   # after a prune, seqs continue from the last pruned entry
                now = self._now_ns()
                for event_type, event, kind, payload in prepared:
                    seq += 1
                    digest = _entry_hash(prev, seq, now, event_type, event, kind, payload)
                    conn.execute("INSERT INTO entries(seq, appended_ns, event_type, event, command_kind, command_payload, "
                                 "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                                 (seq, now, event_type, event, kind, payload, prev, digest))
                    entries.append(JournalEntry(self.journal_id, seq, now, event_type, event, kind,
                                                json.loads(payload), digest))
                    prev = digest
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        self.failure = None
        return entries

    def mark_applied(self, entry: JournalEntry, outcome: str) -> None:
        """Record that ``entry``'s command returned (``outcome``: applied, duplicate or rejected)."""
        conn = self._connection()
        try:
            conn.execute("PRAGMA synchronous=NORMAL")   # a lost mark only causes a duplicate re-application
            try:
                conn.execute("INSERT OR IGNORE INTO applied(seq, applied_ns, command_id, outcome) VALUES (?,?,?,?)",
                             (entry.seq, self._now_ns(), entry.command_id, outcome))
            finally:
                conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        if entry.seq == self._applied_through + 1:
            self._applied_through = entry.seq

    def marked_newest_first(self, page: int = 500) -> Iterator[tuple[int, str]]:
        """``(seq, command_id)`` of every marked entry, newest first, read a page at a time (no open cursor)."""
        below = None
        while True:
            conn = self._connection()
            try:
                rows = conn.execute("SELECT seq, command_id FROM applied WHERE seq < COALESCE(?, seq + 1) "
                                    "ORDER BY seq DESC LIMIT ?", (below, page)).fetchall()
            except sqlite3.Error as exc:
                raise self._failed(exc) from exc
            if not rows:
                return
            yield from ((r["seq"], r["command_id"]) for r in rows)
            below = rows[-1]["seq"]

    def reapply_after(self, seq: int) -> int:
        """Treat every entry after ``seq`` as unapplied, marked or not: the core does not have their commands
        (it was restored from a backup taken before they were applied). Returns how many marked entries that
        makes unapplied again. Their old marks stay (append-only); a re-application adds no second mark.
        Pruned entries are gone: ``seq`` below the prune floor counts as the floor."""
        conn = self._connection()
        try:
            seq = max(seq, _floor(conn)[0])
            last_mark = conn.execute("SELECT MAX(seq) AS s FROM applied").fetchone()["s"] or 0
            again = conn.execute("SELECT COUNT(*) AS n FROM applied WHERE seq > ?", (seq,)).fetchone()["n"]
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc
        if seq < self._applied_through:
            self._applied_through = seq
        self._reapply_through = max(self._reapply_through, last_mark)
        return again

    # ------------------------------------------------------------ retention
    def prune(self, retention_ns: int, *, now_ns: int | None = None) -> int:
        """Delete applied entries appended more than ``retention_ns`` ago, with their marks, oldest first; returns
        how many. Only a prefix of the journal goes: never an unapplied entry or anything after one, never an
        entry a rewind will apply again, and nothing from a journal that is broken, not yet bound to a core
        database or not yet admitted by the gateway (``rewound``: until then, the core has not decided which
        marked entries it lacks). The last deleted entry's seq and hash are recorded (``prunes``) in the same
        transaction."""
        if retention_ns < 0:
            raise ValueError("retention must not be negative")
        conn = self._connection()
        if self._broken is not None or self._meta.get("database_id") is None or not self.rewound:
            return 0
        cutoff = (now_ns if now_ns is not None else self._now_ns()) - retention_ns
        total = 0
        try:
            while True:
                floor = _floor(conn)[0]
                upper = min(_tail(conn)[0], self._applied_through, floor + PRUNE_BATCH)
                stop = conn.execute("SELECT seq FROM entries e WHERE e.seq > ? AND e.seq <= ? AND (e.appended_ns >= ? "
                                    "OR NOT EXISTS (SELECT 1 FROM applied a WHERE a.seq = e.seq)) ORDER BY e.seq LIMIT 1",
                                    (floor, upper, cutoff)).fetchone()
                through = (stop["seq"] - 1) if stop is not None else upper
                if through <= floor:
                    return total
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    digest = conn.execute("SELECT hash FROM entries WHERE seq=?", (through,)).fetchone()["hash"]
                    # The append-only guards are lifted only inside this transaction, for this prefix.
                    conn.execute("DROP TRIGGER applied_no_delete")
                    conn.execute("DROP TRIGGER entries_no_delete")
                    conn.execute("DELETE FROM applied WHERE seq <= ?", (through,))
                    n = conn.execute("DELETE FROM entries WHERE seq <= ?", (through,)).rowcount
                    conn.execute(_NO_DELETE_ENTRIES)
                    conn.execute(_NO_DELETE_APPLIED)
                    conn.execute("INSERT INTO prunes(through_seq, through_hash, pruned_ns, entries) VALUES (?,?,?,?)",
                                 (through, digest, self._now_ns(), n))
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                total += n
                self._beat()
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    # ------------------------------------------------------------ read
    def unapplied(self, limit: int | None = None) -> list[JournalEntry]:
        """Unapplied entries, oldest first, each verified against its checksum and its predecessor.

        On a broken chain this returns the verified entries before the broken one and raises only when there
        are none (``broken`` stays set)."""
        self._connection()
        return self._verified_rows(self._applied_through, limit)

    def _verified_rows(self, after: int, limit: int | None) -> list[JournalEntry]:
        conn = self._conn
        assert conn is not None
        try:
            floor, floor_hash = _floor(conn)
            if after <= floor:
                after, prev = floor, floor_hash
            else:
                anchor = conn.execute("SELECT hash FROM entries WHERE seq=?", (after,)).fetchone()
                if anchor is None:
                    raise self._break(self._corrupt(f"entry {after} is missing", after))
                prev = anchor["hash"]
            rows = conn.execute("SELECT e.* , EXISTS (SELECT 1 FROM applied a WHERE a.seq = e.seq) AS done "
                                "FROM entries e WHERE e.seq > ? ORDER BY e.seq", (after,))
            out: list[JournalEntry] = []
            expected = after + 1
            for i, r in enumerate(rows, 1):
                if i % HEARTBEAT_EVERY == 0:
                    self._beat()
                problem = None
                if self._broken is not None and r["seq"] >= self._broken_seq:
                    problem = self._broken
                elif r["seq"] != expected:
                    problem = self._corrupt(f"entry {expected} is missing (next is {r['seq']})", expected)
                elif r["prev_hash"] != prev or r["hash"] != _entry_hash(
                        prev, r["seq"], r["appended_ns"], r["event_type"], r["event"], r["command_kind"], r["command_payload"]):
                    problem = self._corrupt(f"entry {r['seq']} fails its checksum", r["seq"])
                if problem is not None:
                    error = self._break(problem)
                    if out:
                        return out   # the verified entries before the broken one; the next call raises
                    raise error
                prev = r["hash"]
                expected += 1
                if not r["done"] or r["seq"] <= self._reapply_through:
                    out.append(JournalEntry(self.journal_id, r["seq"], r["appended_ns"], r["event_type"], r["event"],
                                            r["command_kind"], json.loads(r["command_payload"]), r["hash"]))
                    if limit is not None and len(out) >= limit:
                        break
            return out
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    @property
    def _broken_seq(self) -> int:
        return getattr(self._broken, "seq", 0) or 0

    def _corrupt(self, what: str, seq: int) -> JournalCorrupt:
        error = JournalCorrupt(f"event journal {self.path.name}: {what}; nothing from this entry on is applied")
        error.seq = seq
        return error

    def unapplied_count(self) -> int:
        conn = self._connection()
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM entries e WHERE e.seq > ? AND (e.seq <= ? OR NOT EXISTS "
                                "(SELECT 1 FROM applied a WHERE a.seq = e.seq))",
                                (self._applied_through, self._reapply_through)).fetchone()["n"]
        except sqlite3.Error as exc:
            raise self._failed(exc) from exc

    def entries(self) -> Iterable[JournalEntry]:
        """Every entry still held (not pruned), oldest first (tests and audit)."""
        conn = self._connection()
        for r in conn.execute("SELECT * FROM entries ORDER BY seq"):
            yield JournalEntry(self.journal_id, r["seq"], r["appended_ns"], r["event_type"], r["event"], r["command_kind"],
                               json.loads(r["command_payload"]), r["hash"])

    def marks(self) -> dict[int, dict]:
        conn = self._connection()
        return {r["seq"]: dict(r) for r in conn.execute("SELECT * FROM applied ORDER BY seq")}

    def verify(self) -> list[str]:
        """Recompute the chain from the prune floor; every mark must name an entry. Returns problems (empty when sound)."""
        problems: list[str] = []
        conn = self._connection()
        floor, prev = _floor(conn)
        expected = floor + 1
        for i, r in enumerate(conn.execute("SELECT * FROM entries ORDER BY seq"), 1):
            if i % HEARTBEAT_EVERY == 0:
                self._beat()
            if r["seq"] != expected:
                problems.append(f"entry {expected} missing (next is {r['seq']})")
            if r["prev_hash"] != prev:
                problems.append(f"entry {r['seq']} prev_hash mismatch")
            if r["hash"] != _entry_hash(r["prev_hash"], r["seq"], r["appended_ns"], r["event_type"], r["event"],
                                        r["command_kind"], r["command_payload"]):
                problems.append(f"entry {r['seq']} checksum mismatch")
            prev, expected = r["hash"], r["seq"] + 1
        for r in conn.execute("SELECT a.seq FROM applied a LEFT JOIN entries e ON e.seq = a.seq WHERE e.seq IS NULL"):
            problems.append(f"mark for missing entry {r['seq']}")
        return problems

    def status(self) -> dict:
        """For the health query: never raises."""
        out = {"path": str(self.path), "journal_id": self.journal_id, "database_id": self._meta.get("database_id"),
               "failure": self.failure, "unapplied": None, "pruned_through": None,
               "set_aside": self.set_asides[-1] if self.set_asides else None}
        try:
            out["unapplied"] = self.unapplied_count()
            out["pruned_through"] = self.pruned_through
            out["journal_id"] = self.journal_id
            out["database_id"] = self._meta.get("database_id")
        except JournalUnavailable as exc:
            out["failure"] = str(exc)
        return out
