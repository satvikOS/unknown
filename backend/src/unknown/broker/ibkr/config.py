"""Local configuration for the IBKR (TWS API) paper adapter.

The file lives OUTSIDE the repository (default ``~/.unknown/ibkr.json``) because
it names the broker account; the account id never goes in git (CLAUDE.md,
ADR-0012). ``load_config`` refuses a file inside any git work tree.

Only ``mode: "PAPER"`` is accepted in this release. LIVE is refused before any
socket is opened. Ports are user-modifiable in TWS / IB Gateway, so the mode is
never inferred from the port alone: the configured port must match the PAPER
profile AND the managed-account list at handshake must equal the expected
account id (research record R2 ``ibkr-host-connection-and-paper-identity``,
analytics ``mode_verified``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.unknown/ibkr.json")
DEFAULT_STATE_DIR = Path("~/.unknown/ibkr-state")

# R2: documented defaults, all user-modifiable. TWS live 7496 / paper 7497,
# IB Gateway live 4001 / paper 4002.
PAPER_DEFAULT_PORTS = {"GATEWAY": 4002, "TWS": 7497}
LIVE_DEFAULT_PORTS = {"GATEWAY": 4001, "TWS": 7496}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9]{2,32}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_KNOWN_KEYS = frozenset({
    "host", "port", "client_id", "expected_account_id", "mode", "host_type", "state_dir", "host_time_zone",
    "base_currency", "currencies", "markets", "legal_entity", "expected_account_type", "custom_paper_port",
    "connect_timeout_s", "submit_timeout_s", "cancel_timeout_s", "request_timeout_s", "market_data_lines",
    "reserved_pacing_percent", "pacing_wait_s", "pacing_backoff_s", "execution_last_n_days",
    "allocation_retention_days", "sdk_version_pin", "sdk_sha256_pin", "comment",
})


class IbkrConfigError(ValueError):
    """The local IBKR configuration is missing, malformed or not permitted in this release."""


@dataclass(frozen=True)
class IbkrConfig:
    host: str
    port: int
    client_id: int
    expected_account_id: str
    host_time_zone: str                       # IANA zone of the host's "since midnight" day and zone-less times
    mode: str = "PAPER"
    host_type: str = "GATEWAY"                # GATEWAY | TWS
    state_dir: Path = DEFAULT_STATE_DIR
    base_currency: str = "USD"
    currencies: tuple[str, ...] = ("USD",)
    markets: tuple[str, ...] = ()             # venue MICs; empty = not established by the adapter
    legal_entity: str | None = None
    expected_account_type: str | None = None  # R11: AccountType string to require (verify on paper first)
    custom_paper_port: int | None = None      # owner attestation that the PAPER host listens on a non-default port
    connect_timeout_s: float = 10.0
    submit_timeout_s: float = 10.0
    cancel_timeout_s: float = 10.0
    request_timeout_s: float = 20.0
    market_data_lines: int = 100              # R8: default allowance; requests/s = floor(lines / 2)
    reserved_pacing_percent: int = 20         # R8: share held for cancels and reconciliation
    pacing_wait_s: float = 2.0
    pacing_backoff_s: float = 10.0
    execution_last_n_days: int | None = None  # R5: unverified; None = since midnight only
    allocation_retention_days: int = 14
    sdk_version_pin: str | None = None        # R1: e.g. "1051.1"; refuse any other imported ibapi version
    sdk_sha256_pin: str | None = None         # R1: sha256 fingerprint of the imported ibapi package files
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ derived
    @property
    def profile_port(self) -> int:
        return self.custom_paper_port if self.custom_paper_port is not None else PAPER_DEFAULT_PORTS[self.host_type]

    @property
    def max_requests_per_second(self) -> int:
        # R8: "requests per second = market-data lines / 2"; floor is the project's conservative choice.
        return max(1, self.market_data_lines // 2)

    @property
    def execution_history_days(self) -> int:
        # R5: IB Gateway returns only the current day since midnight; lastNDays is unverified.
        return self.execution_last_n_days or 1

    def validate(self) -> None:
        problems = []
        if self.mode != "PAPER":
            problems.append(f"mode {self.mode!r} refused: only PAPER is permitted in this release; LIVE is never connected")
        if self.host_type not in PAPER_DEFAULT_PORTS:
            problems.append(f"host_type must be one of {sorted(PAPER_DEFAULT_PORTS)}")
        if self.host not in LOOPBACK_HOSTS:
            problems.append("host must be a loopback address (127.0.0.1); the host's trusted IP list is limited to localhost")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            problems.append("port must be an integer 1..65535")
        elif self.port in LIVE_DEFAULT_PORTS.values():
            problems.append(f"port {self.port} is a documented LIVE default (4001 IB Gateway / 7496 TWS); refused")
        elif self.host_type in PAPER_DEFAULT_PORTS and self.port != self.profile_port:
            problems.append(f"port {self.port} does not match the PAPER profile port {self.profile_port} for "
                            f"{self.host_type}; set custom_paper_port only if the owner changed the host's socket port")
        if self.custom_paper_port is not None and self.custom_paper_port in LIVE_DEFAULT_PORTS.values():
            problems.append("custom_paper_port may not be a documented LIVE default port")
        if type(self.client_id) is not int or self.client_id <= 0 or self.client_id >= 2**31 - 1:
            # R3: "The order-entry clientId is never 0 unless manual-order takeover is explicitly enabled";
            # takeover is not supported in this release.
            problems.append("client_id must be a positive integer (0 binds manual TWS orders and is not permitted)")
        if not isinstance(self.expected_account_id, str) or not _ACCOUNT_ID.match(self.expected_account_id):
            problems.append("expected_account_id must be the paper account id (letters and digits)")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(self.host_time_zone)
        except Exception:
            problems.append(f"host_time_zone {self.host_time_zone!r} is not a valid IANA time zone")
        if not _CURRENCY.match(self.base_currency or ""):
            problems.append("base_currency must be an ISO 4217 code")
        if not self.currencies or any(not _CURRENCY.match(c) for c in self.currencies):
            problems.append("currencies must be a non-empty list of ISO 4217 codes")
        for name in ("connect_timeout_s", "submit_timeout_s", "cancel_timeout_s", "request_timeout_s",
                     "pacing_wait_s", "pacing_backoff_s"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                problems.append(f"{name} must be a positive number of seconds")
        if type(self.market_data_lines) is not int or self.market_data_lines < 2:
            problems.append("market_data_lines must be an integer >= 2")
        if type(self.reserved_pacing_percent) is not int or not 0 <= self.reserved_pacing_percent < 100:
            problems.append("reserved_pacing_percent must be an integer in [0, 100)")
        if self.execution_last_n_days is not None and (type(self.execution_last_n_days) is not int
                                                       or not 1 <= self.execution_last_n_days <= 7):
            problems.append("execution_last_n_days must be 1..7 (ExecutionFilter.lastNDays maximum 7) or null")
        if type(self.allocation_retention_days) is not int or self.allocation_retention_days < 2:
            problems.append("allocation_retention_days must be an integer >= 2")
        if problems:
            raise IbkrConfigError("; ".join(problems))

    def masked_account(self) -> str:
        acct = self.expected_account_id
        return acct[:2] + "*" * max(0, len(acct) - 4) + acct[-2:] if len(acct) > 4 else "*" * len(acct)


def _inside_git_work_tree(path: Path) -> bool:
    for parent in [path.parent, *path.parent.parents]:
        if (parent / ".git").exists():
            return True
    return False


def config_from_dict(data: dict) -> IbkrConfig:
    if not isinstance(data, dict):
        raise IbkrConfigError("configuration must be a JSON object")
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise IbkrConfigError(f"unknown configuration keys: {unknown}")
    required = ("host", "port", "client_id", "expected_account_id", "mode", "host_time_zone")
    missing = [k for k in required if k not in data]
    if missing:
        raise IbkrConfigError(f"missing required keys: {missing}")
    kwargs = {k: v for k, v in data.items() if k != "comment"}
    if "state_dir" in kwargs:
        kwargs["state_dir"] = Path(kwargs["state_dir"])
    for key in ("currencies", "markets"):
        if key in kwargs:
            if not isinstance(kwargs[key], list):
                raise IbkrConfigError(f"{key} must be a list")
            kwargs[key] = tuple(kwargs[key])
    cfg = IbkrConfig(**kwargs)
    cfg.validate()
    return cfg


def load_config(path: Path | str | None = None) -> IbkrConfig:
    """Load and validate the owner's local configuration file (outside any git work tree)."""
    p = Path(path or DEFAULT_CONFIG_PATH).expanduser().resolve()
    if _inside_git_work_tree(p):
        raise IbkrConfigError(f"{p} is inside a git work tree; the IBKR configuration names the account id and "
                              "must live outside the repository (e.g. ~/.unknown/ibkr.json)")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise IbkrConfigError(f"no IBKR configuration at {p}; see docs/integrations/ibkr.md") from None
    except json.JSONDecodeError as exc:
        raise IbkrConfigError(f"{p} is not valid JSON: {exc}") from None
    cfg = config_from_dict(data)
    state_dir = cfg.state_dir.expanduser().resolve()
    if _inside_git_work_tree(state_dir / "x"):
        raise IbkrConfigError(f"state_dir {state_dir} is inside a git work tree; it must live outside the repository")
    return cfg
