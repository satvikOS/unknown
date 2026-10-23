"""Gateway runner: moves outbox rows to the broker and broker events into the core.

The runner holds no state of its own: every decision is a core command. It is
driven explicitly (``pump``) by tests and by the supervised service loop.
Economic deduplication is done by the handlers on scoped broker keys (ADR-0005
#5), so a re-delivered execution with different transport metadata is a
duplicate, never a conflict.

Broker events are popped from the broker (``BrokerPort.drain_events``). Popped
events that have not been applied yet are kept in a backlog that belongs to the
broker object, so the service's runner, the reconciler's runner and any other
runner on the same broker take them once and in order. An event whose command
raises stays at the head with everything after it, and the next drain retries
it first (review finding 58).

With an event journal attached to the broker (``attach_journal``; the core
service always attaches one), the backlog holds only events not yet journaled
(book-review finding BR-ORD-20). Each drain appends them to the journal in one
fsync'd transaction before anything is applied, then applies unapplied journal
entries in order, each under the deterministic command id of its entry, and
marks it. A crash at any point loses nothing that was journaled, and a
re-application is a duplicate. The bound on the in-memory hand-off: if the
journal cannot be written, the runner stops taking events from the broker (at
most ``limit`` popped events wait in memory, the rest stay in the adapter),
latches the stop through the system ``stop`` command, records an INCOMPLETE
reconciliation (so reconciliation is required before a green start) and raises
``JournalUnavailable``; journaled entries are still applied. ``pump`` makes
the broker's events durable before its outbox pass and, when that append
fails, fails closed before the pass: cancels still go out, but no new buy is
transmitted while events cannot be journaled. The first successful append
ends the failure; the stop stays latched for the owner.

Before anything is appended to or applied from a journal file (normally by
the start-up ``replay_journal``; by the first drain if that replay failed),
the runner admits it (``_admit``): the journal must belong to this core
database (``Store.database_id``, recorded in the journal), or it is set aside
unapplied; then the core, not the journal's marks, decides what was applied:
marked entries whose commands the core does not have are applied again, in
journal order (a core database restored from an older backup, even one older
than the journal's first applied entry; ``rewind_to_core``).

A broken journal is set aside, never skipped silently: an entry that fails its
checksum, a file SQLite reports damaged, or an entry whose command id the core
holds for a different command or for a non-system principal (a conflict: a
journal file older than the core). The verified entries before the broken one
are applied; the failure is recorded in the core (stop latched with
``EVENT_JOURNAL_SET_ASIDE``, INCOMPLETE reconciliation); the file is moved
aside with a report and a fresh journal of this database takes over
(``JournalSetAside`` is raised once). The same command under another
deployment's system principal (the core restarted with another
``--deployment``) is a duplicate, not a conflict.

An event the gateway cannot map to a command blocks itself and what follows
(head-of-line), but the events before it are journaled and applied, and so are
entries already journaled.

The hand-off stays a pop (``drain_events``) and is not peek/ack: the adapter
and the runner share one process, so an event waiting in the adapter's deque
and one waiting in this backlog are lost alike by a crash, and the IBKR API
does not redeliver callbacks. The journal is the first durable point. Events
the process held in memory at a crash are recovered only by reconciliation."""
from __future__ import annotations

import json
import logging
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Callable, NoReturn

from ..broker.port import (BrokerOrder, BrokerPort, CommissionEvent, CommissionRow, ConnectivityEvent, ExecutionEvent,
                           ExecutionRow, OrderStatusEvent, SubmitStatus)
from ..core.desk import Desk, Result
from ..domain.canonical import canonical_json, sha256_hex
from ..domain.fixed import Fixed
from ..domain.principal import Role
from .journal import (COMMAND_ID_PREFIX, EventJournal, JournalConflict, JournalCorrupt, JournalEntry,
                      JournalSetAside, JournalUnavailable)

log = logging.getLogger("unknown.gateway")

JOURNAL_STOP_REASON = "EVENT_JOURNAL_UNWRITABLE"
JOURNAL_SET_ASIDE_REASON = "EVENT_JOURNAL_SET_ASIDE"
JOURNAL_GAP_REASON = "EVENT_JOURNAL_GAP"
REWIND_BEAT_EVERY = 100   # marks read and looked up in the core between heartbeats (rewind_to_core)


def execution_payload(account_key: str, e: ExecutionRow) -> dict:
    return {"account_key": account_key, "exec_key": e.exec_key, "provider_exec_id": e.provider_exec_id,
            "client_order_id": e.client_order_id, "order_ref": e.order_ref, "broker_order_id": e.broker_order_id,
            "instrument_id": e.instrument_id, "provider_ref": e.provider_ref, "side": e.side,
            "quantity": e.quantity.to_str(), "price": e.price.to_str(), "currency": e.currency,
            "exec_time_ns": e.exec_time_ns, "correction_of": e.correction_of}


def commission_payload(account_key: str, c: CommissionRow) -> dict:
    return {"account_key": account_key, "exec_key": c.exec_key, "amount": c.amount.to_str(), "currency": c.currency}


def status_payload(account_key: str, s: OrderStatusEvent) -> dict:
    return {"account_key": account_key, "client_order_id": s.client_order_id, "order_ref": s.order_ref,
            "broker_order_id": s.broker_order_id, "status": s.status.value, "filled_quantity": s.filled_quantity.to_str(),
            "remaining_quantity": s.remaining_quantity.to_str(), "reason": s.reason}


def event_command(account_key: str, ev) -> tuple[str, dict]:
    """The core command (kind, payload) a broker event is applied as."""
    if isinstance(ev, ExecutionEvent):
        return "broker_execution", execution_payload(account_key, ev.execution)
    if isinstance(ev, CommissionEvent):
        return "broker_commission", commission_payload(account_key, ev.commission)
    if isinstance(ev, OrderStatusEvent):
        return "broker_order_status", status_payload(account_key, ev)
    if isinstance(ev, ConnectivityEvent):
        return "broker_connectivity", {"account_key": account_key, "connected": ev.connected,
                                       "authenticated": ev.authenticated, "detail": ev.detail}
    raise TypeError(f"unknown broker event {type(ev).__name__}")


_BACKLOGS: "weakref.WeakKeyDictionary[object, deque]" = weakref.WeakKeyDictionary()
_PINNED_BACKLOGS: dict[int, tuple[object, deque]] = {}   # brokers that cannot be weakly referenced (kept alive)
_JOURNALS: "weakref.WeakKeyDictionary[object, EventJournal]" = weakref.WeakKeyDictionary()
_PINNED_JOURNALS: dict[int, tuple[object, EventJournal]] = {}


def event_backlog(broker: BrokerPort) -> deque:
    """Events taken from ``broker`` but not yet applied (no journal) or not yet journaled, oldest first."""
    try:
        backlog = _BACKLOGS.get(broker)
        if backlog is None:
            backlog = _BACKLOGS[broker] = deque()
        return backlog
    except TypeError:
        pinned = _PINNED_BACKLOGS.get(id(broker))
        if pinned is None or pinned[0] is not broker:
            pinned = _PINNED_BACKLOGS[id(broker)] = (broker, deque())
        return pinned[1]


def attach_journal(broker: BrokerPort, journal: EventJournal) -> None:
    """Route every runner on ``broker`` (the service's, the reconciler's, any other) through ``journal``."""
    try:
        _JOURNALS[broker] = journal
    except TypeError:
        _PINNED_JOURNALS[id(broker)] = (broker, journal)


def detach_journal(broker: BrokerPort) -> None:
    try:
        _JOURNALS.pop(broker, None)
    except TypeError:
        _PINNED_JOURNALS.pop(id(broker), None)


def journal_of(broker: BrokerPort) -> EventJournal | None:
    try:
        return _JOURNALS.get(broker)
    except TypeError:
        pinned = _PINNED_JOURNALS.get(id(broker))
        return pinned[1] if pinned is not None and pinned[0] is broker else None


def entry_payload_hash(entry: JournalEntry) -> str:
    """The core's payload hash of an entry's command (``Desk.execute``)."""
    return sha256_hex(canonical_json({"kind": entry.command_kind, "payload": entry.command_payload}))


def _unreported(exc: BaseException | None) -> JournalUnavailable | None:
    """A journal failure still to be recorded in the core (a set-aside journal was recorded before it moved)."""
    return exc if isinstance(exc, JournalUnavailable) and not isinstance(exc, JournalSetAside) else None


def _outcome(result: Result) -> str:
    if result.duplicate:
        return "duplicate"
    return "applied" if result.accepted else "rejected"


@dataclass
class PumpStats:
    sent: int = 0
    skipped: int = 0
    events: int = 0


class GatewayRunner:
    def __init__(self, desk: Desk, broker: BrokerPort, account_key: str, *,
                 heartbeat: Callable[[], None] | None = None, entry_gate: Callable[[], str | None] | None = None,
                 journal: EventJournal | None = None):
        self.desk = desk
        self.broker = broker
        self.account_key = account_key
        # Called between commands of a long pump so the writer lease stays fresh (review finding 57).
        self.heartbeat = heartbeat
        # Returns why no buy may be transmitted now, or None: the core service's in-memory stop latch, for a red
        # stop the database has not recorded yet (BR-SES-13). Sells and cancels are never held back.
        self.entry_gate = entry_gate
        self._journal = journal

    @property
    def journal(self) -> EventJournal | None:
        """This runner's journal, else the one attached to its broker, else None (in-memory backlog only)."""
        return self._journal if self._journal is not None else journal_of(self.broker)

    def _beat(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat()

    # ---------------------------------------------------------------- outbox
    def pending_outbox(self) -> list[str]:
        rows = self.desk.store.query("SELECT outbox_id FROM outbox WHERE account_key=? AND state='PENDING' ORDER BY created_seq, outbox_id",
                                     (self.account_key,))
        return [r["outbox_id"] for r in rows]

    def pump_outbox(self, limit: int = 100) -> PumpStats:
        stats = PumpStats()
        for outbox_id in self.pending_outbox()[:limit]:
            begin = self.desk.run_system("outbox_begin", {"outbox_id": outbox_id})
            if not begin.accepted or not begin.body.get("send"):
                stats.skipped += 1
                continue
            body = begin.body
            blocked = self.entry_gate() if self.entry_gate is not None and body["action"] == "SUBMIT" \
                and body["order"]["side"] == "BUY" else None
            if blocked:
                # Claimed but never sent: a conclusive local refusal, so the order is REJECTED and its holds released.
                self.desk.run_system("outbox_result", {"outbox_id": outbox_id, "status": "REJECTED", "broker_order_id": None,
                                                       "reason": f"NOT_TRANSMITTED: {blocked}"})
                stats.skipped += 1
                self._beat()
                continue
            try:
                if body["action"] == "SUBMIT":
                    o = body["order"]
                    result = self.broker.submit(BrokerOrder(
                        client_order_id=o["client_order_id"], order_ref=o["order_ref"], instrument_id=o["instrument_id"],
                        provider_ref=o["provider_ref"], side=o["side"], quantity=Fixed.parse(o["quantity"]),
                        limit_price=Fixed.parse(o["limit_price"]), currency=o["currency"], time_in_force=o["time_in_force"]))
                else:
                    result = self.broker.cancel(body["client_order_id"], body.get("broker_order_id"))
                status, broker_id, reason = result.status.value, result.broker_order_id, result.reason
            except Exception as exc:  # transport failure: the outcome is unknown, never assumed
                status, broker_id, reason = SubmitStatus.UNKNOWN.value, None, f"{type(exc).__name__}: {exc}"
            self.desk.run_system("outbox_result", {"outbox_id": outbox_id, "status": status,
                                                   "broker_order_id": broker_id, "reason": reason})
            stats.sent += 1
            self._beat()
        return stats

    # ---------------------------------------------------------------- events
    def drain_events(self, limit: int = 1000) -> int:
        """Apply up to ``limit`` broker events in order and return how many were applied.

        Without a journal, events are taken from the broker only to fill the in-memory backlog up to
        ``limit``. If an event's command raises, that event and every later one stay in the backlog, in
        order, and the exception propagates (the service counts a failed cycle; a reconciliation records
        an INCOMPLETE run). Nothing taken from the broker is dropped (review finding 58).

        With a journal, see the module docstring: journal first, then apply journal entries in order."""
        journal = self.journal
        if journal is None:
            return self._drain_backlog(limit)
        intake_error: Exception | None = None
        try:
            self.journal_broker_events(limit)
        except Exception as exc:   # an unwritable journal, one set aside, or an event that cannot be mapped
            intake_error = exc
        journal_error = _unreported(intake_error)
        try:
            applied = self._apply_journal(journal, limit)   # what is journaled is applied either way
        except JournalSetAside:
            raise   # recorded in the core before the file was moved
        except JournalUnavailable as exc:
            self._fail_closed(journal, journal_error or exc)
            raise
        except Exception:
            if journal_error is not None:
                self._fail_closed(journal, journal_error)
            raise
        if journal_error is not None:
            self._fail_closed(journal, journal_error)
        if intake_error is not None:
            raise intake_error
        journal.reported_failure = None   # a drain without a journal error ends a failure episode
        return applied

    def _drain_backlog(self, limit: int) -> int:
        backlog = event_backlog(self.broker)
        if len(backlog) < limit:
            backlog.extend(self.broker.drain_events(limit - len(backlog)))
        n = 0
        while backlog and n < limit:
            self.apply_event(backlog[0])
            backlog.popleft()
            n += 1
            self._beat()
        return n

    def journal_broker_events(self, limit: int = 1000) -> int:
        """Append events taken from the broker to the journal (one fsync'd transaction) and return how many.

        The journal is admitted first (``_admit``: its database identity, then ``rewind_to_core``), and a broken
        one is set aside (after its verified entries are applied) before anything is appended. Events that could
        not be appended stay in the in-memory backlog, in order, and are appended first next time; while the
        journal is failing, no further events are taken from the broker until those are journaled. Raises
        ``JournalUnavailable`` (``JournalSetAside`` when the journal was set aside)."""
        journal = self.journal
        if journal is None:
            return 0
        self._admit(journal)
        if journal.broken is not None:
            self._apply_journal(journal, None)   # the verified entries before the broken one; then it is set aside
        backlog = event_backlog(self.broker)
        if not (journal.failure and backlog) and len(backlog) < limit:
            backlog.extend(self.broker.drain_events(limit - len(backlog)))
        records, unmappable = [], None
        for ev in backlog:
            try:
                records.append((ev, *event_command(self.account_key, ev)))
            except Exception as exc:   # it and everything after it wait at the head of the backlog
                unmappable = exc
                break
        if records:
            journal.append(records)
            for _ in records:
                backlog.popleft()
        if unmappable is not None:
            raise unmappable
        return len(records)

    def _apply_journal(self, journal: EventJournal, limit: int | None) -> int:
        """Apply up to ``limit`` unapplied entries in order. A broken journal (corrupt or conflicting entry) has
        every verified entry before the broken one applied, whatever ``limit``, and is then set aside."""
        self._admit(journal)
        n = self._apply_entries(journal, None if journal.broken is not None else limit)
        if journal.broken is not None:
            n += self._apply_entries(journal, None)
            self._set_aside(journal)
        return n

    def _apply_entries(self, journal: EventJournal, limit: int | None) -> int:
        n = 0
        try:
            for entry in journal.unapplied(limit):
                result = self.apply_entry(entry, journal)
                journal.mark_applied(entry, _outcome(result))
                n += 1
                self._beat()
        except (JournalCorrupt, JournalConflict):
            if journal.broken is None:
                raise
        return n

    def _admit(self, journal: EventJournal) -> None:
        """Once per journal file, before anything is appended to or applied from it.

        1. Identity (BR-ORD-20): a journal records the core database it belongs to (``Store.database_id``). A
           journal of another database -- the database was recreated, or restored from another database's
           backup, next to an old journal -- is set aside without applying any of its entries. A journal written
           before identities existed is bound to this database if this database holds one of its applied entries
           (or it has none); otherwise it is set aside too. A file SQLite reports damaged is set aside.
        2. ``rewind_to_core``: marked entries this database does not have are applied again."""
        if journal.heartbeat is None and self.heartbeat is not None:
            journal.heartbeat = self.heartbeat   # long passes inside the journal keep the writer lease fresh
        if not journal.identity_checked:
            try:
                bound = journal.database_id   # opens the file and verifies every unapplied entry
            except JournalCorrupt:
                self._set_aside(journal)
            core = self.desk.store.database_id
            if bound is not None and bound != core:
                self._set_aside(journal, f"it belongs to core database {bound}, not to this one ({core}); none of "
                                         "its entries is applied")
            if bound is None:
                if journal.has_entries() and self._anchor(journal)[0] is None:
                    self._set_aside(journal, "it records no core database and this core database holds none of its "
                                             "applied entries; none of its entries is applied")
                journal.bind(core)
            journal.identity_checked = True
        if not journal.rewound:
            self.rewind_to_core(journal)

    def apply_entry(self, entry: JournalEntry, journal: EventJournal | None = None) -> Result:
        """Apply one journal entry. Its command id is the entry's own, so a re-application returns the
        stored result (a duplicate with no new event).

        The id is looked up first. Held by the core for a different command (payload) or principal role,
        it is not this entry's command: the journal is broken there (``JournalConflict``) and the core is
        not asked, so a conflict is never re-presented on every cycle. Held for the same command by the
        system principal of another deployment, it is a duplicate."""
        rows = self.desk.store.query("SELECT payload_hash, status, result, principal_id, principal_role FROM commands "
                                     "WHERE command_id=?", (entry.command_id,))
        if rows:
            row = rows[0]
            journal = journal if journal is not None else self.journal
            if row["principal_role"] != Role.SYSTEM.value or row["payload_hash"] != entry_payload_hash(entry):
                why = (f"the core holds {entry.command_id} for a different command"
                       if row["principal_role"] == Role.SYSTEM.value else
                       f"the core holds {entry.command_id} for a {row['principal_role']} principal")
                if journal is None:
                    raise JournalUnavailable(why)
                raise journal.conflict(entry, why)
            if row["principal_id"] != self.desk.system().id:
                return Result(entry.command_id, row["status"] == "ACCEPTED", True, json.loads(row["result"]))
        return self.desk.run_system(entry.command_kind, entry.command_payload, command_id=entry.command_id)

    def replay_journal(self, batch: int = 1000) -> int:
        """Apply every unapplied journal entry in order without taking new events (process start).

        First (once per journal file, in ``_admit``), the journal's database identity is checked and marked
        entries the core does not have are made unapplied again (``rewind_to_core``). The heartbeat runs
        between entries and during every long pass, so a very large journal never lets the lease go stale."""
        journal = self.journal
        if journal is None:
            return 0
        total = 0
        try:
            while True:
                n = self._apply_journal(journal, batch)
                total += n
                if n < batch:
                    return total
        except JournalSetAside:
            raise   # recorded in the core before the file was moved
        except JournalUnavailable as exc:
            self._fail_closed(journal, exc)
            raise

    def _core_holds(self, command_id: str) -> bool:
        return bool(self.desk.store.query("SELECT 1 FROM commands WHERE command_id=?", (command_id,)))

    def _anchor(self, journal: EventJournal) -> tuple[int | None, int]:
        """The newest marked entry whose command the core holds, and how many marked entries after it the core
        lacks (all of them when there is no such entry). Walks the marks newest first, beating as it goes."""
        missing = 0
        for i, (seq, command_id) in enumerate(journal.marked_newest_first(page=REWIND_BEAT_EVERY), 1):
            if i % REWIND_BEAT_EVERY == 0:
                self._beat()
            if self._core_holds(command_id):
                return seq, missing
            missing += 1
        return None, missing

    def rewind_to_core(self, journal: EventJournal) -> int:
        """Make marked entries unapplied again when the core does not have their commands; returns how many.

        Every entry is applied strictly in journal order, so the core has every entry up to the newest marked
        one whose command it holds (the anchor) and none of the marked ones after it: it was restored from a
        backup taken in between, and those are applied again. Without an anchor, a journal of this database
        (same identity) is older than nothing in the core: the database was restored from a backup taken
        before the journal's first applied entry, and every marked entry the journal still holds is applied
        again. Re-application is in journal order under the entries' own command ids; anything the restored
        core already holds under other command ids (a reconciliation's ingest, a duplicate callback) is a
        duplicate by the handlers' broker keys. Entries pruned before the restore point cannot be applied
        again: that gap is recorded (stop latched, reconciliation required) and reconciliation recovers them."""
        anchor, missing = self._anchor(journal)
        ours = journal.database_id == self.desk.store.database_id
        floor = journal.pruned_through
        if missing and (anchor is not None or ours):
            journal.reapply_after(anchor if anchor is not None else floor)
            log.warning("the core lacks %d broker events this journal marked applied after entry %d (restored "
                        "database): applying them again in journal order", missing, anchor or floor)
        if ours and anchor is None and floor and not self._core_holds(f"{COMMAND_ID_PREFIX}{journal.journal_id}.{floor}"):
            self._fail_closed(journal, JournalUnavailable(
                f"this core database lacks broker events of journal {journal.journal_id} up to entry {floor}, which "
                "were pruned (retention) before it was restored: they cannot be applied again; reconciliation "
                "recovers what the broker still reports"), reason=JOURNAL_GAP_REASON,
                what="journaled broker events were pruned before the restore point")
        journal.rewound = True
        return missing if (anchor is not None or ours) else 0

    def _set_aside(self, journal: EventJournal, why: str | None = None) -> NoReturn:
        """Set a foreign, corrupt or conflicting journal aside, never skipping it silently: record the failure in
        the core first (stop latched with ``EVENT_JOURNAL_SET_ASIDE``, INCOMPLETE reconciliation, so
        ``needs_reconciliation``), then move the file aside with its report (``EventJournal.set_aside``) and
        continue on a fresh journal of this database. Raises ``JournalSetAside``."""
        broken = journal.broken
        cause = why or f"{type(broken).__name__}: {broken}"
        error = JournalSetAside(f"event journal {journal.path.name} (journal {journal.journal_id}) set aside: {cause}")
        self._fail_closed(journal, error, reason=JOURNAL_SET_ASIDE_REASON, what="the broker event journal was set aside")
        error.report = journal.set_aside(str(error), database_id=self.desk.store.database_id)
        log.critical("%s. Moved to %s with report.json; continuing on a fresh journal. Reconcile before a green start.",
                     error, error.report.get("aside_dir"))
        raise error

    def _fail_closed(self, journal: EventJournal, exc: JournalUnavailable, *, reason: str = JOURNAL_STOP_REASON,
                     what: str = "broker events could not be journaled") -> None:
        """BR-ORD-20: broker events cannot be made durable or applied, so the desk must not trade on a partial view.

        Latches the stop through the system ``stop`` command (which also cancels open buys) with the reason
        ``<reason>: <error>`` (``EVENT_JOURNAL_UNWRITABLE`` by default) -- a StopLatched or, if the stop was
        already latched for another reason, a StopReasserted event is the durable report -- and records an
        INCOMPLETE reconciliation, which sets ``needs_reconciliation``: a green start then needs a clean
        reconciliation after the journal works again. A failure is recorded once per episode (until a
        drain completes without a journal error), again if its cause changes, and the stop or the
        reconciliation requirement is re-asserted if the owner cleared it while the journal still fails."""
        detail = f"{type(exc).__name__}: {exc}"
        log.critical("%s; not taking events from the broker until it works: %s", what, detail)
        rows = self.desk.store.query("SELECT row FROM control WHERE account_key=?", (self.account_key,))
        if not rows:
            return   # no registered account: nothing to stop
        ctl = json.loads(rows[0]["row"])
        new_failure = journal.reported_failure != detail
        if new_failure or not ctl["stop_latched"]:
            self.desk.run_system("stop", {"account_key": self.account_key, "reason": f"{reason}: {detail}"[:400]})
        if new_failure or not ctl["needs_reconciliation"]:
            from ..reconcile.reconciler import record_incomplete   # the reconciler imports this module
            record_incomplete(self.desk, self.account_key, f"{what}: {detail}")
        journal.reported_failure = detail

    def apply_event(self, ev) -> None:
        """Apply one event directly (fresh command id, no journal): tests and the in-memory path."""
        kind, payload = event_command(self.account_key, ev)
        self.desk.run_system(kind, payload)

    def pump(self) -> PumpStats:
        journal = self.journal
        if journal is not None:
            # Make what the broker has already sent durable before the (possibly slow) outbox pass. A journal
            # that cannot take them fails closed HERE, before the pass: the stop is latched and reconciliation
            # is required, so the pass still sends cancels (risk-reducing) but transmits no new buy while the
            # gateway cannot make broker events durable.
            try:
                self.journal_broker_events()
            except JournalSetAside:
                pass   # recorded in the core (stop latched) before the file was moved
            except JournalUnavailable as exc:
                try:
                    self._fail_closed(journal, exc)
                except Exception:   # the core cannot record it either: never hold back the cancels below
                    log.exception("could not fail closed before the outbox pass")
            except Exception:
                pass   # an event that cannot be mapped: raised again by drain_events below, after the outbox pass
        stats = self.pump_outbox()
        stats.events = self.drain_events()
        return stats

    # ---------------------------------------------------------------- periodic
    def tick(self) -> None:
        self.desk.run_system("valuation", {"account_key": self.account_key})
        self.desk.run_system("session_tick", {"account_key": self.account_key})

    def last_watermark(self) -> str | None:
        # The last CLEAN run recorded, by insertion order: wall time can step back (review finding 51).
        r = self.desk.store.query("SELECT row FROM reconciliations WHERE account_key=? AND status='CLEAN' "
                                  "ORDER BY rowid DESC LIMIT 1", (self.account_key,))
        return json.loads(r[0]["row"]).get("watermark") if r else None
