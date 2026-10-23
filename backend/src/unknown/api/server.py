"""``unknown-api``: terminal HTTP API and static file server (ADR-0013).

Read paths use a read-only SQLite connection per request. Writes are owner
commands forwarded to the core socket with the authenticated principal; the
core re-checks permissions and, for green start, the re-authentication age.

Wire conventions (every JSON response):

- Errors are ``{"error": <message>, "code": <stable code>, ...}``. Codes: ``BAD_REQUEST``,
  ``LOGIN_REQUIRED``, ``INVALID_PASSPHRASE``, ``CSRF_INVALID``, ``COMMAND_NOT_AVAILABLE``, ``FORBIDDEN``,
  ``NOT_FOUND``, ``COMMAND_NOT_FOUND``, ``CONFLICT``, ``COMMAND_ID_CONFLICT``, ``REAUTH_REQUIRED``,
  ``RATE_LIMITED``, ``COMMAND_INVALID``, ``CORE_REFUSED``, ``CORE_UNAVAILABLE``, ``CORE_BUSY``,
  ``CORE_DATABASE_UNAVAILABLE``, ``CORE_FAULT``, ``COMMAND_OUTCOME_UNKNOWN``, ``CAPABILITIES_INVALID``,
  ``INTERNAL_ERROR``. Errors of POST /api/commands about the command itself (every error after the
  session, CSRF and JSON-body checks) also carry ``command_id`` and ``outcome``: ``NOT_EXECUTED`` or
  ``UNKNOWN`` (then query GET /api/commands/<id>; resend only with the same id). A 200 may carry
  ``post_commit_error``: the command was recorded and the core failed afterwards.
- Every ``*_ns`` key holding an integer or null also comes as ``*_ns_str``, a decimal string (or null),
  because nanosecond timestamps exceed 2**53 (``queries.with_ns_strings``).
- Path segments are percent-decoded after the path is split on ``/``, so ``%2F`` addresses an id that
  contains ``/``.

Endpoints added for the terminal: GET /api/commands/<command_id> (the owner's stored command result);
GET /api/stream sends ``id: <seq>`` and a ``retry:`` hint and resumes from ``Last-Event-ID``;
GET /api/capabilities is served verbatim with an ETag, ``If-None-Match`` (304) and gzip."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..core import ledger
from ..core.replay import verify_chain
from ..domain.principal import AGENT_ALLOWED, OWNER_ONLY, SHARED_OWNER_SYSTEM
from ..service.ipc import CoreClient, CoreError, CoreUnavailable, IpcError, OutcomeUnknown
from ..service.paths import Paths
from ..store.db import Tx, connect
from . import queries
from .auth import Authenticator

log = logging.getLogger("unknown.api")

NS = 1_000_000_000
TERMINAL_KINDS = (OWNER_ONLY | SHARED_OWNER_SYSTEM | AGENT_ALLOWED) - {"process_started"}
STEP_UP_KINDS = frozenset({"start", "publish_policy", "activate_policy", "authorize_capital", "deauthorize_capital",
                           "acquire_execution_ownership", "release_execution_ownership", "resolve_exception",
                           "broker_admin_action"})
# The step-up window is the active policy's reauth_max_age_s, as the core checks it, taking the shorter of the API
# account's and the commanded account's (Handler._step_up); this fallback applies to an account with no active policy.
STEP_UP_FALLBACK_NS = 15 * 60 * NS
# Optional STRICTER cap (None in production). The terminal's end-to-end harness sets it to reach the 428 path in
# seconds; it can only shorten the window, never lengthen it.
STEP_UP_MAX_AGE_NS: int | None = None
OWNER = {"id": "owner", "role": "OWNER"}
COOKIE = "unk_session"
SSE_RETRY_MS = 3000
SSE_KEEPALIVE_S = 15.0
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
_DEFAULT_CODES = {400: "BAD_REQUEST", 401: "LOGIN_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
                  428: "REAUTH_REQUIRED", 429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 503: "CORE_UNAVAILABLE",
                  504: "COMMAND_OUTCOME_UNKNOWN"}
_EVENT_ID = re.compile(r"[0-9]{1,18}")


_PLUGINS = None
_POST_PLUGINS = None
_STREAM_PLUGINS = None


def _plugin_routes():
    global _PLUGINS
    if _PLUGINS is None:
        from .routes import load
        _PLUGINS = load()
    return _PLUGINS


def _plugin_stream_routes():
    global _STREAM_PLUGINS
    if _STREAM_PLUGINS is None:
        from .routes import load_stream
        _STREAM_PLUGINS = load_stream()
    return _STREAM_PLUGINS


def _plugin_post_routes():
    global _POST_PLUGINS
    if _POST_PLUGINS is None:
        from .routes import load_post
        _POST_PLUGINS = load_post()
    return _POST_PLUGINS


def path_parts(path: str) -> list[str]:
    """``/api/a/b%2Fc`` -> ``["a", "b/c"]``: split the raw path on ``/`` first, then percent-decode each segment
    (``+`` stays ``+``), so an id may contain any reserved character."""
    return [unquote(p, errors="strict") for p in path.split("/") if p][1:]


def accepts_gzip(header: str | None) -> bool:
    """Whether an ``Accept-Encoding`` value allows gzip (an explicit ``gzip``/``x-gzip`` entry wins over ``*``)."""
    star = None
    for item in (header or "").split(","):
        coding, _, params = item.strip().partition(";")
        coding = coding.strip().lower()
        q = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if coding in ("gzip", "x-gzip"):
            return q > 0
        if coding == "*":
            star = q
    return star is not None and star > 0


def etag_matches(if_none_match: str | None, digest: str) -> bool:
    """Weak comparison (RFC 9110 13.1.2) of an ``If-None-Match`` list against our tag."""
    for tag in (if_none_match or "").split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == f'"{digest}"':
            return True
    return False


class ApiContext:
    def __init__(self, paths: Paths, account_key: str, static_dir: Path | None, capabilities_path: Path | None,
                 secure_cookie: bool):
        self.paths = paths
        self.account_key = account_key
        self.db_path = paths.db(account_key)
        self.static_dir = static_dir
        self.capabilities_path = capabilities_path
        self.auth = Authenticator(paths.secrets / "owner.json")
        self.core = CoreClient(paths.socket, paths.token("api"))
        self.secure_cookie = secure_cookie
        self._capabilities_lock = threading.Lock()
        self._capabilities: tuple[tuple, tuple[str, bytes, bytes]] | None = None
        # Command ids this API process is forwarding right now. The core's "not found" is final only for requests
        # it has received; one still on its way from another API thread is not yet in the core's queue.
        self._inflight_lock = threading.Lock()
        self._inflight: dict[str, int] = {}
        # Market-data bus (ADR-0018): created on first use, runs in this process on its own threads, never touches the
        # core database. Tests may assign a bus before the first request.
        self.marketdata = None
        self._marketdata_lock = threading.Lock()

    def marketdata_bus(self):
        with self._marketdata_lock:
            if self.marketdata is None:
                from ..marketdata.bus import MarketDataBus
                self.marketdata = MarketDataBus.from_home(self.paths.root)
            return self.marketdata

    def begin_forward(self, command_id: str) -> None:
        with self._inflight_lock:
            self._inflight[command_id] = self._inflight.get(command_id, 0) + 1

    def end_forward(self, command_id: str) -> None:
        with self._inflight_lock:
            n = self._inflight.get(command_id, 0) - 1
            if n > 0:
                self._inflight[command_id] = n
            else:
                self._inflight.pop(command_id, None)

    def forwarding(self, command_id: str) -> bool:
        with self._inflight_lock:
            return command_id in self._inflight

    def ro(self) -> sqlite3.Connection:
        return connect(self.db_path, readonly=True)

    def capabilities(self) -> tuple[str, bytes, bytes] | None:
        """``(sha256 hex, file bytes, gzip bytes)`` of the capability register, re-read when the file changes;
        None when it is not published. Raises ValueError when the file is not JSON."""
        p = self.capabilities_path
        if p is None:
            return None
        try:
            st = p.stat()
        except FileNotFoundError:
            return None
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        with self._capabilities_lock:
            if self._capabilities is not None and self._capabilities[0] == key:
                return self._capabilities[1]
        raw = p.read_bytes()
        json.loads(raw)
        entry = (hashlib.sha256(raw).hexdigest(), raw, gzip.compress(raw, compresslevel=6, mtime=0))
        with self._capabilities_lock:
            self._capabilities = (key, entry)
        return entry


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopping = threading.Event()   # ends open state streams on shutdown

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def handle_error(self, request, client_address) -> None:
        """A browser that goes away mid-request is one log line; any other failure keeps its traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, _DISCONNECTS):
            log.info("client disconnected mid-request (%s)", type(exc).__name__)
            return
        log.exception("request handler failed")


class Handler(BaseHTTPRequestHandler):
    server_version = "unknown-api"
    ctx: ApiContext
    server: ApiServer

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args) -> None:  # route through logging, never print secrets
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: object, headers: dict | None = None) -> None:
        data = json.dumps(queries.with_ns_strings(body), default=str).encode()
        self._send_bytes(status, data, headers)

    def _send_bytes(self, status: int, data: bytes, headers: dict | None = None, *,
                    content_type: str = "application/json; charset=utf-8", cache_control: str | None = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if cache_control is not None:
            self.send_header("Cache-Control", cache_control)
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, code: str | None = None, **extra) -> None:
        self._send(status, {"error": message, "code": code or _DEFAULT_CODES.get(status, "ERROR"), **extra})

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("JSON object expected")
        return body

    def _session(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        sid = cookie[COOKIE].value if COOKIE in cookie else None
        return self.ctx.auth.get(sid)

    def _cookie(self, value: str, max_age: int) -> str:
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.ctx.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def _quotes(self) -> dict:
        try:
            return self.ctx.core.query("quotes")
        except (IpcError, OSError):
            return {}

    def _step_up(self, kind: str | None = None, payload: dict | None = None) -> tuple[int, dict]:
        """The step-up window, in ns, and its basis. Each account's window is its active policy's
        ``reauth_max_age_s`` (read as the core reads it), else ``STEP_UP_FALLBACK_NS``. The window is the shorter
        of the API account's and, for a command, that of the account the core will act on
        (``queries.command_account``), so no payload field can lengthen it; ``STEP_UP_MAX_AGE_NS`` may only
        shorten it further. Without ``kind`` (the session view) it is the API account's window."""
        conn = self.ctx.ro()
        try:
            windows = [self._account_window(conn, self.ctx.account_key)]
            target = queries.command_account(conn, kind, payload or {}) if kind else None
            if target is not None and target != self.ctx.account_key:
                windows.append(self._account_window(conn, target))
        finally:
            conn.close()
        window, info = min(windows, key=lambda w: w[0])   # ties keep the API account
        if STEP_UP_MAX_AGE_NS is not None and STEP_UP_MAX_AGE_NS < window:
            window, info = STEP_UP_MAX_AGE_NS, {**info, "basis": "override (test harness)"}
        return window, {**info, "max_age_s": window // NS}

    @staticmethod
    def _account_window(conn: sqlite3.Connection, account_key: str) -> tuple[int, dict]:
        max_age_s, policy_hash = queries.reauth_window(conn, account_key)
        if max_age_s is None:
            return STEP_UP_FALLBACK_NS, {"account_key": account_key, "basis": "fallback: no active policy",
                                         "policy_hash": None}
        return max_age_s * NS, {"account_key": account_key, "basis": "policy", "policy_hash": policy_hash}

    # ------------------------------------------------------------ routes
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            return self._static(url.path)
        try:
            parts = path_parts(url.path)
        except UnicodeDecodeError:
            return self._error(400, "path segments must be UTF-8 after percent-decoding")
        if parts == ["auth", "session"]:
            return self._auth_session()
        if self._session() is None:
            return self._error(401, "login required")
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        acct = q.get("account", self.ctx.account_key)
        if parts == ["stream"]:
            return self._stream(q)
        if parts == ["marketdata", "stream"]:
            return self._marketdata_stream(q)
        for prefix, handler in _plugin_stream_routes():
            if parts == list(prefix):
                return self._plugin_stream(handler, q)
        try:
            conn = self.ctx.ro()
        except sqlite3.Error as exc:
            return self._error(503, f"core database unavailable: {exc}", "CORE_DATABASE_UNAVAILABLE")
        try:
            self._get(parts, q, acct, conn)
        except _DISCONNECTS:
            raise
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except sqlite3.Error as exc:
            log.warning("GET %s: core database error: %s", url.path, exc)
            self._error(503, f"core database unavailable: {exc}", "CORE_DATABASE_UNAVAILABLE")
        except Exception:
            log.exception("GET %s failed", url.path)
            self._error(500, "internal error; see the API log")
        finally:
            conn.close()

    def _auth_session(self) -> None:
        s = self._session()
        body = {"authenticated": bool(s), "authenticated_at_ns": s.authenticated_at_ns if s else None,
                "csrf_token": s.csrf_token if s else None, "now_ns": time.time_ns()}
        if s:
            try:
                window, step_up = self._step_up()
                body["step_up"] = {**step_up, "expires_at_ns": s.authenticated_at_ns + window}
            except Exception as exc:   # e.g. the core database is unreadable: the window is unknown, never guessed
                log.warning("step-up window unavailable for the session view: %s: %s", type(exc).__name__, exc)
                body["step_up"] = None
        return self._send(200, body)

    def _get(self, parts: list[str], q: dict, acct: str, conn: sqlite3.Connection) -> None:
        if parts == ["overview"]:
            return self._send(200, queries.overview(conn, acct, self._quotes()))
        if parts == ["accounts"]:
            return self._send(200, queries.accounts(conn))
        if parts == ["sessions"]:
            return self._send(200, queries.sessions(conn, acct))
        if len(parts) == 2 and parts[0] == "sessions":
            d = queries.session_detail(conn, parts[1])
            return self._send(200, d) if d else self._error(404, "no such session")
        if parts == ["orders"]:
            return self._send(200, queries.orders(conn, acct, open_only=q.get("state") == "open", session_id=q.get("session"),
                                                  limit=int(q.get("limit", 500))))
        if len(parts) == 2 and parts[0] == "orders":
            d = queries.order_detail(conn, parts[1])
            return self._send(200, d) if d else self._error(404, "no such order")
        if len(parts) == 2 and parts[0] == "commands":
            return self._command_result(parts[1], conn)
        if parts == ["positions"]:
            return self._send(200, queries.positions(conn, acct, self._quotes()))
        if parts == ["buckets"]:
            return self._send(200, queries.buckets(conn, acct))
        if parts == ["executions"]:
            return self._send(200, queries.executions(conn, acct, int(q.get("limit", 500))))
        if parts == ["exceptions"]:
            return self._send(200, queries.exceptions(conn, acct, open_only=q.get("status", "OPEN") == "OPEN"))
        if parts == ["reconciliations"]:
            return self._send(200, queries.reconciliations(conn, acct, int(q.get("limit", 50))))
        if parts == ["ledger", "entries"]:
            return self._send(200, queries.journal(conn, acct, owner=q.get("owner"), limit=int(q.get("limit", 200)),
                                                   before_seq=int(q["before"]) if q.get("before") else None))
        if parts == ["events"]:
            return self._send(200, queries.events(conn, after=int(q.get("after", 0)), limit=int(q.get("limit", 200)),
                                                  type_=q.get("type"), session_id=q.get("session"), order_id=q.get("order"),
                                                  account_key=q.get("account")))
        if parts == ["audit", "verify"]:
            return self._send(200, {"chain_problems": verify_chain(conn), "ledger_problems": ledger.verify(Tx(conn)),
                                    "latest_seq": queries.latest_seq(conn)})
        if parts == ["policies"]:
            return self._send(200, queries.policies(conn))
        if parts == ["policy", "active"]:
            return self._send(200, queries.active_policy(conn, acct))
        if parts == ["instruments"]:
            return self._send(200, queries.instruments(conn))
        if parts == ["risk"]:
            return self._send(200, {"risk": queries.risk(conn, acct), "policy": queries.active_policy(conn, acct)})
        if parts == ["quotes"]:
            return self._send(200, self._quotes())
        if parts == ["health"]:
            try:
                core = self.ctx.core.query("health")
            except (IpcError, OSError) as exc:
                core = {"error": f"core unreachable: {exc}"}
            return self._send(200, {"core": core, "api_time_ns": time.time_ns(), "latest_seq": queries.latest_seq(conn)})
        if parts == ["preflight"]:
            s = self._session()
            try:
                return self._send(200, self.ctx.core.query("preflight", authenticated_at_ns=s.authenticated_at_ns))
            except (IpcError, OSError) as exc:
                return self._error(503, f"core unreachable: {exc}")
        for prefix, handler in _plugin_routes():
            if parts[:len(prefix)] == list(prefix):
                return self._plugin_get(handler, parts, q, acct, conn)
        if parts == ["capabilities"]:
            return self._capabilities()
        return self._error(404, "unknown endpoint")

    def _plugin_get(self, handler, parts: list[str], q: dict, acct: str, conn: sqlite3.Connection) -> None:
        """Plugin GET (routes/__init__.py contract): LookupError 404, ValueError 400, PermissionError 403."""
        try:
            body = handler(self.ctx, parts, q, acct, conn)
        except LookupError as exc:
            return self._error(404, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        except PermissionError as exc:
            return self._error(403, str(exc))
        return self._send(200, body)

    def _command_result(self, command_id: str, conn: sqlite3.Connection) -> None:
        """The owner's stored result for ``command_id``. Asked of the core first: it answers after every request it
        had already queued, so "not found" from the core is final. If the core cannot answer, the database's
        answer is given, and "not found" is then provisional (a request the core holds may still run)."""
        unreachable = None
        try:
            found = self.ctx.core.query("command_result", command_id=command_id, principal_id=OWNER["id"])["command"]
            source = "core"
        except (IpcError, OSError) as exc:
            unreachable = f"{type(exc).__name__}: {exc}"
            found = queries.stored_command(conn, command_id, principal_id=OWNER["id"], principal_role=OWNER["role"])
            source = "database"
        if found is not None:
            # The event seqs are read here, on the API's read-only connection, never on the core's writer thread.
            events = queries.command_events(conn, command_id, principal_id=OWNER["id"])
            return self._send(200, {**found, "events": events, "source": source})
        if unreachable is None and self.ctx.forwarding(command_id):
            # Another request of this API is handing the same id to the core right now: not final.
            return self._error(404, f"no result is stored for command {command_id} yet, but this API is forwarding a "
                                    "request with this id to the core right now: wait for it, then look again; resend "
                                    "only with the same command_id.", "COMMAND_NOT_FOUND", command_id=command_id,
                               source=source, definitive=False, in_flight=True)
        if unreachable is None:
            message = (f"no command {command_id} is recorded for the owner, and the core has finished every request it "
                       "received before this query, so a request that already ended without a result was not "
                       "executed. Resending it with the same command_id is safe.")
        else:
            message = (f"no result is stored for command {command_id}. The core did not answer ({unreachable}), so a "
                       "request it already holds may still run: resend only with the same command_id.")
        return self._error(404, message, "COMMAND_NOT_FOUND", command_id=command_id, source=source,
                           definitive=unreachable is None)

    def _capabilities(self) -> None:
        try:
            entry = self.ctx.capabilities()
        except ValueError as exc:
            return self._error(500, f"capability register is not valid JSON: {exc}", "CAPABILITIES_INVALID")
        if entry is None:
            return self._send(200, {"capabilities": [], "note": "capability register not yet published"})
        digest, raw, gz = entry
        # Weak: one tag names the register whether it is sent as identity or gzip (RFC 9110 8.8.3).
        headers = {"ETag": f'W/"{digest}"', "Vary": "Accept-Encoding"}
        cache = "private, no-cache"   # the browser may keep it but must revalidate (If-None-Match -> 304)
        if etag_matches(self.headers.get("If-None-Match"), digest):
            self.send_response(304)
            self.send_header("Cache-Control", cache)
            for k, v in {**SECURITY_HEADERS, **headers}.items():
                self.send_header(k, v)
            self.end_headers()
            return None
        if accepts_gzip(self.headers.get("Accept-Encoding")):
            return self._send_bytes(200, gz, {**headers, "Content-Encoding": "gzip"}, cache_control=cache)
        return self._send_bytes(200, raw, headers, cache_control=cache)

    def _stream(self, q: dict) -> None:
        """Server-Sent Events: sequence-numbered change notifications (the client refetches snapshots).

        Each notification is ``id: <seq>`` / ``event: seq`` / ``data: {"seq": <seq>}`` for the latest event-log
        sequence (several events can coalesce into one). A reconnecting client's ``Last-Event-ID`` (or
        ``?last_event_id=``) is the seq it last saw: nothing is resent until the log moves past it, and a lower
        current seq (a restored database) is sent at once. ``retry:`` sets the browser's reconnect delay."""
        last = None
        for candidate in (self.headers.get("Last-Event-ID"), q.get("last_event_id")):
            if candidate is not None and _EVENT_ID.fullmatch(candidate.strip()):
                last = int(candidate.strip())
                break
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        stopping = getattr(self.server, "stopping", None) or threading.Event()
        try:
            self.wfile.write(f"retry: {SSE_RETRY_MS}\n\n".encode())
            self.wfile.flush()
            last_beat = time.monotonic()
            while not stopping.is_set():
                try:
                    conn = self.ctx.ro()
                    try:
                        seq = queries.latest_seq(conn)
                    finally:
                        conn.close()
                except sqlite3.Error as exc:
                    log.warning("state stream closed: core database unavailable (%s)", exc)
                    return
                if seq != last:
                    self.wfile.write(f"id: {seq}\nevent: seq\ndata: {json.dumps({'seq': seq})}\n\n".encode())
                    self.wfile.flush()
                    last, last_beat = seq, time.monotonic()
                elif time.monotonic() - last_beat > SSE_KEEPALIVE_S:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                stopping.wait(0.25)
        except _DISCONNECTS:
            return

    def _marketdata_stream(self, q: dict) -> None:
        """GET /api/marketdata/stream?keys=... (docs/design/marketdata-stream-contract.md; ADR-0018)."""
        from ..marketdata import sse
        try:
            bus = self.ctx.marketdata_bus()
        except Exception as exc:
            log.warning("market-data bus unavailable: %s", exc)
            return self._error(503, f"market-data bus unavailable: {exc}", "MARKETDATA_UNAVAILABLE")
        keys, refused = sse.parse_keys(q.get("keys"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        stopping = getattr(self.server, "stopping", None) or threading.Event()
        try:
            sse.serve(bus, keys, refused, self.wfile.write, self.wfile.flush, stopping)
        except _DISCONNECTS:
            return

    def _plugin_stream(self, handler, q: dict) -> None:
        """Plugin SSE (routes/__init__.py STREAM_ROUTES contract); never opens the core database."""
        try:
            run = handler(self.ctx, q)
        except LookupError as exc:
            return self._error(404, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        last = None
        for candidate in (self.headers.get("Last-Event-ID"), q.get("last_event_id")):
            if candidate is not None and _EVENT_ID.fullmatch(candidate.strip()):
                last = int(candidate.strip())
                break
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        stopping = getattr(self.server, "stopping", None) or threading.Event()
        try:
            run(last, self.wfile.write, self.wfile.flush, stopping)
        except _DISCONNECTS:
            return

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        try:
            parts = path_parts(url.path) if url.path.startswith("/api/") else []
            body = self._json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))
        if parts == ["auth", "login"]:
            try:
                s = self.ctx.auth.login(body.get("passphrase", ""))
            except PermissionError as exc:
                return self._error(429, str(exc))
            if s is None:
                return self._error(401, "invalid passphrase", "INVALID_PASSPHRASE")
            return self._send(200, {"csrf_token": s.csrf_token, "authenticated_at_ns": s.authenticated_at_ns},
                              {"Set-Cookie": self._cookie(s.session_id, 12 * 3600)})
        s = self._session()
        if s is None:
            return self._error(401, "login required")
        if not self.ctx.auth.csrf_ok(s, self.headers.get("X-CSRF-Token")):
            return self._error(403, "missing or invalid CSRF token", "CSRF_INVALID")
        if parts == ["auth", "logout"]:
            self.ctx.auth.logout(s.session_id)
            return self._send(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
        if parts == ["auth", "reauth"]:
            try:
                ok = self.ctx.auth.reauth(s, body.get("passphrase", ""))
            except PermissionError as exc:
                return self._error(429, str(exc))
            return self._send(200, {"authenticated_at_ns": s.authenticated_at_ns}) if ok else \
                self._error(401, "invalid passphrase", "INVALID_PASSPHRASE")
        if parts == ["commands"]:
            return self._command(body, s)
        for prefix, handler in _plugin_post_routes():
            if parts and parts[:len(prefix)] == list(prefix):
                return self._plugin_post(handler, parts, body, s)
        return self._error(404, "unknown endpoint")

    def _command(self, body: dict, s) -> None:
        kind = body.get("kind")
        command_id = body.get("command_id") or f"ui.{kind}.{secrets.token_hex(12)}"
        not_executed = {"command_id": command_id, "outcome": "NOT_EXECUTED"}
        if not isinstance(kind, str) or kind not in TERMINAL_KINDS:
            return self._error(403, f"command {kind!r} is not available from the terminal", "COMMAND_NOT_AVAILABLE",
                               **not_executed)
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            return self._error(400, "payload must be an object", **not_executed)
        if kind in STEP_UP_KINDS:
            try:
                window, step_up = self._step_up(kind, payload)
            except Exception as exc:   # fail closed: without the policy there is no window to check against
                log.warning("step-up window unavailable for %s: %s: %s", kind, type(exc).__name__, exc)
                return self._error(503, f"cannot read the active policy to check re-authentication: {exc}",
                                   "CORE_DATABASE_UNAVAILABLE", **not_executed)
            if time.time_ns() - s.authenticated_at_ns > window:
                return self._error(428, "re-authentication required for this command", **not_executed,
                                   step_up={**step_up, "expires_at_ns": s.authenticated_at_ns + window})
        self.ctx.begin_forward(command_id)
        try:
            result = self.ctx.core.command(command_id, kind, payload,
                                           {**OWNER, "authenticated_at_ns": s.authenticated_at_ns})
        except CoreUnavailable as exc:
            return self._error(503, f"core unavailable; command not executed: {exc}", **not_executed)
        except OutcomeUnknown as exc:
            return self._error(504, f"no answer from the core ({exc}). The outcome of {command_id} is unknown: "
                                    f"GET /api/commands/{command_id} before resending, and resend only with the same "
                                    "command_id.", command_id=command_id, outcome="UNKNOWN")
        except CoreError as exc:
            return self._core_refusal(exc, not_executed)
        except OSError as exc:   # a connect-level failure from a client without typed errors
            return self._error(503, f"core unavailable; command not executed: {exc}", **not_executed)
        finally:
            self.ctx.end_forward(command_id)
        if result.get("post_commit_error"):
            # Recorded, then the core failed (e.g. writing its restore marker): the result stands; the fault is logged.
            log.error("command %s was recorded, then the core failed: %s", command_id, result["post_commit_error"])
        return self._send(200, {"command_id": command_id, **result})

    def _core_refusal(self, exc: CoreError, not_executed: dict) -> None:
        """The core answered with an error: nothing was stored (it rolls back all but deterministic rejections)."""
        kind, message = exc.error_type, str(exc)
        if message.startswith(f"{kind}: "):
            message = message[len(kind) + 2:]
        if kind == "CommandConflict":
            m = re.search(r"different (payload|principal)\b", message)
            return self._error(409, f"{message}; the original command keeps its result and this request was not "
                                    "executed. Use a new command id for a different command.", "COMMAND_ID_CONFLICT",
                               difference=m.group(1) if m else None, **not_executed)
        if kind in ("PermissionError", "Unauthorized"):
            return self._error(403, f"the core refused the request: {message}", "CORE_REFUSED", **not_executed)
        if kind in ("ValueError", "InvalidIdentifier", "KeyError", "TypeError"):
            return self._error(400, f"the core refused the command as invalid: {message}", "COMMAND_INVALID",
                               **not_executed)
        if kind == "RuntimeError" and message.startswith("core busy"):
            return self._error(503, f"{message}; command not executed", "CORE_BUSY", **not_executed)
        if kind == "RuntimeError" and message.startswith("core stopped"):
            return self._error(503, f"core unavailable; command not executed: {message}", **not_executed)
        log.warning("core fault on a terminal command (%s): %s", kind, message)
        return self._error(500, f"the core failed the command and rolled it back; nothing was recorded "
                                f"({kind}: {message})", "CORE_FAULT", **not_executed)

    def _plugin_post(self, handler, parts: list[str], body: dict, session) -> None:
        """Owner POST to a plugin route; the caller has already checked the session and the CSRF token."""
        from .routes import RouteConflict
        try:
            return self._send(200, handler(self.ctx, parts, body, session))
        except LookupError as exc:
            return self._error(404, str(exc))
        except RouteConflict as exc:
            return self._error(409, str(exc))
        except PermissionError as exc:
            return self._error(403, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))
        except _DISCONNECTS:
            raise
        except Exception:
            log.exception("plugin POST %s failed", "/".join(parts))
            return self._error(500, "internal error; see the API log")

    # ------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        root = self.ctx.static_dir
        if root is None or not root.exists():
            return self._error(404, "terminal not built; run `npm run build` in terminal/")
        base = root.resolve()
        try:
            target = (base / unquote(path).lstrip("/")).resolve()
            found = target.is_relative_to(base) and target.is_file()
        except (OSError, ValueError):   # e.g. an encoded NUL byte
            found = False
        if not found:
            target = base / "index.html"  # single-page app fallback
            if not target.is_file():
                return self._error(404, "terminal not built; run `npm run build` in terminal/")
        data = target.read_bytes()
        self._send_bytes(200, data, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                         cache_control=None)


def make_server(ctx: ApiContext, host: str, port: int) -> ApiServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    return ApiServer((host, port), handler)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Project Unknown terminal API")
    ap.add_argument("--account", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--home", default=None)
    ap.add_argument("--static", default=str(Path(__file__).resolve().parents[4] / "terminal" / "dist"))
    ap.add_argument("--capabilities", default=str(Path(__file__).resolve().parents[4] / "docs" / "capabilities" / "register.json"))
    ap.add_argument("--secure-cookie", action="store_true", help="set when served over TLS")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        log.warning("binding to %s: ensure this is a private interface (ADR-0013)", args.host)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = Paths(Path(args.home).expanduser()) if args.home else Paths.default()
    ctx = ApiContext(paths, args.account, Path(args.static), Path(args.capabilities), args.secure_cookie)
    server = make_server(ctx, args.host, args.port)
    log.info("api listening on http://%s:%d (account %s)", args.host, args.port, args.account)
    server.serve_forever()


if __name__ == "__main__":
    main()
