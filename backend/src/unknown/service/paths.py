"""Runtime layout outside the repository (secrets and state never live in git).

Default root is ``~/.unknown`` (override with ``UNKNOWN_HOME``):
  run/core.sock          core command socket (0600) in a 0700 directory
  secrets/api_token      token allowing the API process to assert OWNER
  secrets/agent_token    token allowing research workers to assert AGENT
  secrets/owner.json     scrypt hash of the owner passphrase
  data/<account>.sqlite  core database (one per execution account)
  backups/               online backups
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def default(cls) -> "Paths":
        return cls(Path(os.environ.get("UNKNOWN_HOME", Path.home() / ".unknown")).expanduser())

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def socket(self) -> Path:
        """Unix sockets are limited to ~104 bytes on macOS. A long runtime root gets a
        private 0700 directory under the per-user temp dir, keyed by the root's hash."""
        preferred = self.run / "core.sock"
        if len(str(preferred)) <= 100:
            return preferred
        import hashlib
        import tempfile
        short = Path(tempfile.gettempdir()) / f"unknown-{hashlib.sha256(str(self.root).encode()).hexdigest()[:12]}"
        short.mkdir(mode=0o700, exist_ok=True)
        os.chmod(short, 0o700)
        if short.stat().st_uid != os.getuid():
            raise PermissionError(f"{short} is not owned by this user")
        return short / "core.sock"

    def db(self, account_key: str) -> Path:
        return self.data / f"{account_key}.sqlite"

    def ensure(self) -> None:
        for d in (self.root, self.run, self.secrets, self.data, self.backups):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)

    def strategy_tokens(self) -> dict[str, str]:
        """{artifact id: token} for every owner-issued strategy token (``secrets/strategy/<artifact>.token``)."""
        from ..domain.principal import ARTIFACT_ID
        d = self.secrets / "strategy"
        out: dict[str, str] = {}
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.token")):
            artifact = f.name[:-len(".token")]
            if not ARTIFACT_ID.match(artifact):
                raise ValueError(f"{f}: not an ADR-0017 artifact id")
            if f.stat().st_mode & 0o077:
                raise PermissionError(f"{f} is readable by others; chmod 600")
            out[artifact] = f.read_text().strip()
        return out

    def create_strategy_token(self, artifact: str) -> Path:
        from ..domain.principal import ARTIFACT_ID
        if not ARTIFACT_ID.match(artifact):
            raise ValueError("artifact id must be <CLASS>@<16 lowercase hex> (ADR-0017)")
        d = self.secrets / "strategy"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        path = d / f"{artifact}.token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        return path

    def token(self, name: str, *, create: bool = False) -> str:
        path = self.secrets / f"{name}_token"
        if not path.exists():
            if not create:
                raise FileNotFoundError(f"{path} missing; run `python -m unknown.cli init`")
            self.ensure()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(f"{path} is readable by others (mode {oct(mode)}); chmod 600")
        return path.read_text().strip()
