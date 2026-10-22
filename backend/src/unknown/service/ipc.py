"""Core command socket (ADR-0013): newline-delimited JSON over a Unix domain socket.

Request:  {"token": str, "op": "command"|"query", ...}
  command: {"command_id", "kind", "payload", "principal": {"id", "role", "authenticated_at_ns"}}
  query:   {"name", "args"}
Response: {"ok": true, "result": ...} or {"ok": false, "error": "<Type>: <message>", "error_type": "<Type>"}

``error_type`` names the exception the core raised (``CommandConflict``, ``PermissionError``,
``ValueError``, ``OutcomeUnknown``, ...), so a client can tell a refusal from a fault without parsing
the message. The client raises one of three exceptions, each saying what happened to the request:

- ``CoreUnavailable`` (an ``OSError``): the request never reached the core; nothing was executed.
- ``OutcomeUnknown``: the request was sent but no complete reply came back (a timeout, a closed
  connection, or the core's own wait timing out while the request stays queued). A command may have
  been executed: query its stored result, or resend it with the SAME command id (the core records
  each id once).
- ``CoreError``: the core answered with an error; ``error_type`` names it. A command that fails this
  way was not stored: the core rolls back everything except a deterministic rejection, and that is a
  normal result, not an error.

The token, not the request, decides which roles a client may assert, so a
compromised research worker holding the agent token cannot act as the owner."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import Callable

MAX_REQUEST_BYTES = 1_000_000
_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

log = logging.getLogger("unknown.core.ipc")


class IpcError(RuntimeError):
    """Base class: the core did not return a result."""


class CoreError(IpcError):
    """The core answered ``ok: false``; ``error_type`` is the exception name it reported."""

    def __init__(self, message: str, error_type: str | None = None):
        super().__init__(message)
        self.error_type = error_type or _type_from_message(message)


class OutcomeUnknown(IpcError):
    """The request may have been executed: it was sent, but no complete reply arrived."""


class CoreUnavailable(ConnectionError):
    """The request never reached the core (no socket, connection refused); nothing was executed.

    An ``OSError``, so callers written for a raw connect failure keep working."""


def _type_from_message(message: str) -> str | None:
    """The exception name in an older core's ``"<Type>: <message>"`` error text."""
    m = re.match(r"^([A-Z][A-Za-z0-9_]*): ", message or "")
    return m.group(1) if m else None


class Grant(frozenset):
    """The roles a token may assert. If ``principal_id`` is set, the token may assert only that principal id:
    strategy tokens are bound to one artifact (ADR-0017), so a runner cannot act as anyone else."""

    def __new__(cls, roles, principal_id: str | None = None):
        grant = super().__new__(cls, roles)
        grant.principal_id = principal_id
        return grant


class CoreClient:
    def __init__(self, socket_path: Path, token: str, timeout_s: float = 30.0):
        self.socket_path = str(socket_path)
        self.token = token
        self.timeout_s = timeout_s

    def _call(self, request: dict) -> dict:
        data = (json.dumps({"token": self.token, **request}) + "\n").encode()
        buf = b""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout_s)
            try:
                s.connect(self.socket_path)
            except OSError as exc:
                raise CoreUnavailable(f"core socket unavailable: {type(exc).__name__}: {exc}") from exc
            try:
                s.sendall(data)
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except OSError as exc:   # TimeoutError included: the core may still be executing the request
                raise OutcomeUnknown(f"no reply from the core: {type(exc).__name__}: {exc}") from exc
        if not buf.endswith(b"\n"):
            raise OutcomeUnknown("core closed the connection without a complete response")
        try:
            resp = json.loads(buf)
        except ValueError as exc:
            raise OutcomeUnknown(f"unreadable reply from the core: {exc}") from exc
        if not resp.get("ok"):
            error = str(resp.get("error", "unknown error"))
            error_type = resp.get("error_type") or _type_from_message(error)
            if error_type in ("OutcomeUnknown", "TimeoutError"):
                raise OutcomeUnknown(error)
            raise CoreError(error, error_type)
        return resp["result"]

    def command(self, command_id: str, kind: str, payload: dict, principal: dict) -> dict:
        return self._call({"op": "command", "command_id": command_id, "kind": kind, "payload": payload, "principal": principal})

    def query(self, name: str, **args) -> dict:
        return self._call({"op": "query", "name": name, "args": args})


def describe_request(req: object) -> str:
    """Names a request for the log, without its token or payload."""
    if not isinstance(req, dict):
        return "an unparsed request"
    if req.get("op") == "command":
        return f"command {str(req.get('kind'))[:40]} {str(req.get('command_id'))[:160]}"
    return f"{str(req.get('op'))[:20]} {str(req.get('name'))[:40]}"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "CoreSocketServer" = self.server  # type: ignore[assignment]
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        except _DISCONNECTS as exc:
            log.warning("core socket: client disconnected before sending a request (%s)", type(exc).__name__)
            return
        if not line:
            return   # connected and closed without a request: there is nobody to answer
        if len(line) > MAX_REQUEST_BYTES:
            self._reply({"ok": False, "error": "request too large", "error_type": "RequestTooLarge"}, None)
            return
        req: object = None
        try:
            req = json.loads(line)
            roles = server.roles_for_token(req.get("token", ""))
            if not roles:
                self._reply({"ok": False, "error": "unauthorized", "error_type": "Unauthorized"}, None)
                return
            reply = {"ok": True, "result": server.dispatch(req, roles)}
        except Exception as exc:  # report, never crash the server thread
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_type": type(exc).__name__}
        self._reply(reply, req)

    def _reply(self, obj: dict, req: object) -> None:
        try:
            self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
        except _DISCONNECTS as exc:
            # One line, no traceback: the client went away. A command's result is already stored under its id,
            # so the client recovers it by querying (or resending) that id.
            log.warning("core socket: client disconnected before the reply to %s was sent (%s; result %s)",
                        describe_request(req), type(exc).__name__, "ok" if obj.get("ok") else obj.get("error_type"))


class CoreSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, tokens: dict[str, frozenset[str]], dispatch: Callable[[dict, frozenset[str]], dict]):
        # values are frozensets of roles, or Grants that also bind a principal id
        socket_path = Path(socket_path)
        if socket_path.exists():
            socket_path.unlink()
        old = os.umask(0o177)
        try:
            super().__init__(str(socket_path), _Handler)
        finally:
            os.umask(old)
        os.chmod(socket_path, 0o600)
        self._tokens = tokens
        self.dispatch = dispatch
        self.thread: threading.Thread | None = None

    def roles_for_token(self, token: str) -> frozenset[str]:
        for known, roles in self._tokens.items():
            if hmac.compare_digest(known.encode(), str(token).encode()):
                return roles
        return frozenset()

    def handle_error(self, request, client_address) -> None:
        """A client that disconnects mid-request is one log line; any other failure keeps its traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, _DISCONNECTS):
            log.warning("core socket: client disconnected mid-request (%s)", type(exc).__name__)
            return
        log.exception("core socket: request handler failed")

    def start(self) -> None:
        self.thread = threading.Thread(target=self.serve_forever, name="core-socket", daemon=True)
        self.thread.start()
