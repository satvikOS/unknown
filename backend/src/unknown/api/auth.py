"""Owner authentication for the terminal API (ADR-0013).

Passphrase hashed with scrypt (stdlib); opaque session ids; per-session CSRF
tokens; step-up re-authentication timestamps; failed-login backoff. Sessions
live in this process's memory only: restarting the API logs the owner out."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "dklen": 32}
SESSION_TTL_S = 12 * 3600
MIN_PASSPHRASE_CHARS = 12


def hash_passphrase(passphrase: str, salt: bytes | None = None, params: dict | None = None) -> dict:
    params = params or SCRYPT
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                            dklen=params["dklen"], maxmem=128 * 1024 * 1024)
    return {"scheme": "scrypt", **params, "salt": salt.hex(), "hash": digest.hex()}


def verify_passphrase(passphrase: str, record: dict) -> bool:
    if record.get("scheme") != "scrypt":
        return False
    params = {k: record[k] for k in ("n", "r", "p", "dklen")}
    candidate = hash_passphrase(passphrase, bytes.fromhex(record["salt"]), params)["hash"]
    return hmac.compare_digest(candidate, record["hash"])


def write_owner_record(path: Path, passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE_CHARS} characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(hash_passphrase(passphrase), f)
    os.replace(tmp, path)


@dataclass
class OwnerSession:
    session_id: str
    csrf_token: str
    created_ns: int
    authenticated_at_ns: int
    expires_ns: int


class Authenticator:
    def __init__(self, owner_record_path: Path, clock_ns=time.time_ns):
        self.path = owner_record_path
        self.clock_ns = clock_ns
        self.sessions: dict[str, OwnerSession] = {}
        self.lock = threading.Lock()
        self.failures = 0
        self.locked_until_ns = 0

    def _record(self) -> dict:
        if not self.path.exists():
            raise PermissionError("owner passphrase not set; run `python -m unknown.cli set-passphrase`")
        mode = self.path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(f"{self.path} must be mode 600")
        return json.loads(self.path.read_text())

    def _check(self, passphrase: str) -> bool:
        now = self.clock_ns()
        with self.lock:
            if now < self.locked_until_ns:
                raise PermissionError("too many failed attempts; wait before retrying")
        ok = isinstance(passphrase, str) and verify_passphrase(passphrase, self._record())
        with self.lock:
            if ok:
                self.failures = 0
            else:
                self.failures += 1
                self.locked_until_ns = now + min(300, 2 ** min(self.failures, 8)) * 1_000_000_000
        return ok

    def login(self, passphrase: str) -> OwnerSession | None:
        if not self._check(passphrase):
            return None
        now = self.clock_ns()
        s = OwnerSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now, now + SESSION_TTL_S * 1_000_000_000)
        with self.lock:
            self.sessions[s.session_id] = s
        return s

    def reauth(self, session: OwnerSession, passphrase: str) -> bool:
        if not self._check(passphrase):
            return False
        session.authenticated_at_ns = self.clock_ns()
        return True

    def get(self, session_id: str | None) -> OwnerSession | None:
        if not session_id:
            return None
        with self.lock:
            s = self.sessions.get(session_id)
            if s and s.expires_ns < self.clock_ns():
                del self.sessions[session_id]
                return None
            return s

    def logout(self, session_id: str) -> None:
        with self.lock:
            self.sessions.pop(session_id, None)

    @staticmethod
    def csrf_ok(session: OwnerSession, header: str | None) -> bool:
        return bool(header) and hmac.compare_digest(session.csrf_token, header)
