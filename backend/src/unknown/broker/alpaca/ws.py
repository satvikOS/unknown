"""Minimal RFC 6455 websocket client on the standard library (no third-party dependency).

Only what the Alpaca market-data stream needs: client handshake with Sec-WebSocket-Accept verification, masked client
frames, text messages (fragmented or not), automatic pong, close handshake, a message size cap. TLS uses
``ssl.create_default_context()`` (certificate and host name verification on).
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import threading
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"          # RFC 6455 section 1.3
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 16 * 1024 * 1024
MAX_HEADER = 16 * 1024


class WebSocketError(ConnectionError):
    pass


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    k = (key * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(len(data), "big")


def encode_frame(opcode: int, payload: bytes, *, mask_key: bytes | None, fin: bool = True) -> bytes:
    n = len(payload)
    b0 = (0x80 if fin else 0) | opcode
    mbit = 0x80 if mask_key is not None else 0
    if n < 126:
        head = bytes([b0, mbit | n])
    elif n < 65536:
        head = bytes([b0, mbit | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([b0, mbit | 127]) + n.to_bytes(8, "big")
    if mask_key is not None:
        return head + mask_key + mask(payload, mask_key)
    return head + payload


def read_frame(read_exact) -> tuple[bool, int, bytes]:
    b0, b1 = read_exact(2)
    n = b1 & 0x7F
    if n == 126:
        n = int.from_bytes(read_exact(2), "big")
    elif n == 127:
        n = int.from_bytes(read_exact(8), "big")
    if n > MAX_MESSAGE:
        raise WebSocketError(f"frame of {n} bytes exceeds {MAX_MESSAGE}")
    key = read_exact(4) if b1 & 0x80 else None
    payload = read_exact(n) if n else b""
    return bool(b0 & 0x80), b0 & 0x0F, mask(payload, key) if key else payload


class WebSocket:
    def __init__(self, sock, rfile):
        self.sock = sock
        self._rf = rfile
        self._send_lock = threading.Lock()
        self.closed = False

    def _read_exact(self, n: int) -> bytes:
        data = self._rf.read(n)
        if data is None or len(data) < n:
            raise WebSocketError("connection closed by peer")
        return data

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(encode_frame(opcode, payload, mask_key=os.urandom(4)))

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def recv(self) -> str | None:
        """The next text (or binary, decoded as UTF-8) message; None after a close frame."""
        parts: list[bytes] = []
        size = 0
        while True:
            fin, op, payload = read_frame(self._read_exact)
            if op == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                if not self.closed:
                    self.closed = True
                    try:
                        self._send(OP_CLOSE, payload[:2])
                    except OSError:
                        pass
                return None
            if op not in (OP_TEXT, OP_BINARY, OP_CONT):
                raise WebSocketError(f"unknown opcode {op}")
            size += len(payload)
            if size > MAX_MESSAGE:
                raise WebSocketError("message too large")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._send(OP_CLOSE, (1000).to_bytes(2, "big"))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


def connect(url: str, *, headers: dict | None = None, timeout: float = 10.0,
            ssl_context: ssl.SSLContext | None = None) -> WebSocket:
    u = urlsplit(url)
    if u.scheme not in ("ws", "wss") or not u.hostname:
        raise WebSocketError(f"not a websocket URL: {url!r}")
    port = u.port or (443 if u.scheme == "wss" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    try:
        if u.scheme == "wss":
            sock = (ssl_context or ssl.create_default_context()).wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
        lines = [f"GET {(u.path or '/') + ('?' + u.query if u.query else '')} HTTP/1.1", f"Host: {host}",
                 "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        rf = sock.makefile("rb")
        status = rf.readline(MAX_HEADER).decode("latin-1")
        got: dict[str, str] = {}
        total = len(status)
        while True:
            line = rf.readline(MAX_HEADER).decode("latin-1")
            total += len(line)
            if total > MAX_HEADER:
                raise WebSocketError("handshake response headers too large")
            if line in ("\r\n", "\n", ""):
                break
            name, _, value = line.partition(":")
            got[name.strip().lower()] = value.strip()
        if " 101 " not in status + " ":
            raise WebSocketError(f"handshake refused: {status.strip()}")
        if got.get("upgrade", "").lower() != "websocket" or got.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("handshake response failed Sec-WebSocket-Accept verification")
        sock.settimeout(None)
        return WebSocket(sock, rf)
    except BaseException:
        sock.close()
        raise
