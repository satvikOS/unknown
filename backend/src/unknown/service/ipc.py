"""Core command socket (ADR-0013): newline-delimited JSON over a Unix domain socket.

Request:  {"token": str, "op": "command"|"query", ...}
  command: {"command_id", "kind", "payload", "principal": {"id", "role", "authenticated_at_ns"}}
  query:   {"name", "args"}
Response: {"ok": bool, "result"|"error": ...}

The token, not the request, decides which roles a client may assert, so a
compromised research worker holding the agent token cannot act as the owner."""
from __future__ import annotations

import hmac
import json
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable

MAX_REQUEST_BYTES = 1_000_000


class IpcError(RuntimeError):
    pass


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
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout_s)
            s.connect(self.socket_path)
            s.sendall(data)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf:
            raise IpcError("core closed the connection without a response")
        resp = json.loads(buf)
        if not resp.get("ok"):
            raise IpcError(resp.get("error", "unknown error"))
        return resp["result"]

    def command(self, command_id: str, kind: str, payload: dict, principal: dict) -> dict:
        return self._call({"op": "command", "command_id": command_id, "kind": kind, "payload": payload, "principal": principal})

    def query(self, name: str, **args) -> dict:
        return self._call({"op": "query", "name": name, "args": args})


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "CoreSocketServer" = self.server  # type: ignore[assignment]
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(line) > MAX_REQUEST_BYTES:
            self._reply({"ok": False, "error": "request too large"})
            return
        try:
            req = json.loads(line)
            roles = server.roles_for_token(req.get("token", ""))
            if not roles:
                self._reply({"ok": False, "error": "unauthorized"})
                return
            result = server.dispatch(req, roles)
            self._reply({"ok": True, "result": result})
        except Exception as exc:  # report, never crash the server thread
            self._reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _reply(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj, default=str) + "\n").encode())


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

    def start(self) -> None:
        self.thread = threading.Thread(target=self.serve_forever, name="core-socket", daemon=True)
        self.thread.start()
