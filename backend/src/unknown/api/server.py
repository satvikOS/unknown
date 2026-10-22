"""``unknown-api``: terminal HTTP API and static file server (ADR-0013).

Read paths use a read-only SQLite connection per request. Writes are owner
commands forwarded to the core socket with the authenticated principal; the
core re-checks permissions and, for green start, the re-authentication age."""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core import ledger
from ..core.replay import verify_chain
from ..domain.principal import AGENT_ALLOWED, OWNER_ONLY, SHARED_OWNER_SYSTEM
from ..service.ipc import CoreClient, IpcError
from ..service.paths import Paths
from ..store.db import Tx, connect
from . import queries
from .auth import Authenticator

log = logging.getLogger("unknown.api")

TERMINAL_KINDS = (OWNER_ONLY | SHARED_OWNER_SYSTEM | AGENT_ALLOWED) - {"process_started"}
STEP_UP_KINDS = frozenset({"start", "publish_policy", "activate_policy", "authorize_capital", "deauthorize_capital",
                           "acquire_execution_ownership", "release_execution_ownership", "resolve_exception",
                           "broker_admin_action"})
STEP_UP_MAX_AGE_NS = 15 * 60 * 1_000_000_000
COOKIE = "unk_session"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}


_PLUGINS = None
_POST_PLUGINS = None


def _plugin_routes():
    global _PLUGINS
    if _PLUGINS is None:
        from .routes import load
        _PLUGINS = load()
    return _PLUGINS


def _plugin_post_routes():
    global _POST_PLUGINS
    if _POST_PLUGINS is None:
        from .routes import load_post
        _POST_PLUGINS = load_post()
    return _POST_PLUGINS


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

    def ro(self) -> sqlite3.Connection:
        return connect(self.db_path, readonly=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "unknown-api"
    ctx: ApiContext

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args) -> None:  # route through logging, never print secrets
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: object, headers: dict | None = None) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

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

    # ------------------------------------------------------------ routes
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._static(url.path)
        if url.path == "/api/auth/session":
            s = self._session()
            return self._send(200, {"authenticated": bool(s), "authenticated_at_ns": s.authenticated_at_ns if s else None,
                                    "csrf_token": s.csrf_token if s else None, "now_ns": time.time_ns()})
        if self._session() is None:
            return self._error(401, "login required")
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        acct = q.get("account", self.ctx.account_key)
        try:
            if url.path == "/api/stream":
                return self._stream()
            conn = self.ctx.ro()
        except sqlite3.OperationalError as exc:
            return self._error(503, f"core database unavailable: {exc}")
        try:
            self._get(url.path, q, acct, conn)
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        finally:
            conn.close()

    def _get(self, path: str, q: dict, acct: str, conn: sqlite3.Connection) -> None:
        parts = [p for p in path.split("/") if p][1:]  # drop 'api'
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
                try:
                    return self._send(200, handler(self.ctx, parts, q, acct, conn))
                except LookupError as exc:
                    return self._error(404, str(exc))
        if parts == ["capabilities"]:
            p = self.ctx.capabilities_path
            if p and p.exists():
                return self._send(200, json.loads(p.read_text()))
            return self._send(200, {"capabilities": [], "note": "capability register not yet published"})
        return self._error(404, "unknown endpoint")

    def _stream(self) -> None:
        """Server-Sent Events: sequence-numbered change notifications (client refetches snapshots)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        last = -1
        last_beat = time.monotonic()
        try:
            while True:
                conn = self.ctx.ro()
                try:
                    seq = queries.latest_seq(conn)
                finally:
                    conn.close()
                if seq != last:
                    self.wfile.write(f"event: seq\ndata: {json.dumps({'seq': seq})}\n\n".encode())
                    self.wfile.flush()
                    last = seq
                elif time.monotonic() - last_beat > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            body = self._json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))
        if url.path == "/api/auth/login":
            try:
                s = self.ctx.auth.login(body.get("passphrase", ""))
            except PermissionError as exc:
                return self._error(429, str(exc))
            if s is None:
                return self._error(401, "invalid passphrase")
            return self._send(200, {"csrf_token": s.csrf_token, "authenticated_at_ns": s.authenticated_at_ns},
                              {"Set-Cookie": self._cookie(s.session_id, 12 * 3600)})
        s = self._session()
        if s is None:
            return self._error(401, "login required")
        if not self.ctx.auth.csrf_ok(s, self.headers.get("X-CSRF-Token")):
            return self._error(403, "missing or invalid CSRF token")
        if url.path == "/api/auth/logout":
            self.ctx.auth.logout(s.session_id)
            return self._send(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
        if url.path == "/api/auth/reauth":
            try:
                ok = self.ctx.auth.reauth(s, body.get("passphrase", ""))
            except PermissionError as exc:
                return self._error(429, str(exc))
            return self._send(200, {"authenticated_at_ns": s.authenticated_at_ns}) if ok else self._error(401, "invalid passphrase")
        if url.path == "/api/commands":
            kind = body.get("kind")
            if kind not in TERMINAL_KINDS:
                return self._error(403, f"command {kind!r} is not available from the terminal")
            if kind in STEP_UP_KINDS and time.time_ns() - s.authenticated_at_ns > STEP_UP_MAX_AGE_NS:
                return self._error(428, "re-authentication required for this command")
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                return self._error(400, "payload must be an object")
            command_id = body.get("command_id") or f"ui.{kind}.{secrets.token_hex(12)}"
            try:
                result = self.ctx.core.command(command_id, kind, payload,
                                               {"id": "owner", "role": "OWNER", "authenticated_at_ns": s.authenticated_at_ns})
            except (IpcError, OSError) as exc:
                return self._error(503, f"core unavailable; command not executed: {exc}")
            return self._send(200, {"command_id": command_id, **result})
        if url.path.startswith("/api/"):
            parts = [p for p in url.path.split("/") if p][1:]
            for prefix, handler in _plugin_post_routes():
                if parts[:len(prefix)] == list(prefix):
                    return self._plugin_post(handler, parts, body, s)
        return self._error(404, "unknown endpoint")

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
        except Exception:
            log.exception("plugin POST %s failed", "/".join(parts))
            return self._error(500, "internal error; see the API log")

    # ------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        root = self.ctx.static_dir
        if root is None or not root.exists():
            return self._error(404, "terminal not built; run `npm run build` in terminal/")
        target = (root / path.lstrip("/")).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file():
            target = root / "index.html"  # single-page app fallback
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(ctx: ApiContext, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


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
