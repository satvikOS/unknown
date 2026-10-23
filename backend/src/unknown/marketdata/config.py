"""Source configuration and per-key routing (broker-agnostic).

The owner's file ``<UNKNOWN_HOME>/config/marketdata.json`` (outside git) declares the sources and, per key pattern,
the fallback order::

    {"sources": {"IBKR":   {"enabled": true, "client_id": 19, "lines": 40},
                 "ALPACA": {"enabled": true, "feed": "iex"}},
     "routes":  [{"match": "*.XNAS", "sources": ["ALPACA", "IBKR"]},
                 {"match": "*",      "sources": ["IBKR"]}]}

The first route whose ``match`` (fnmatch) fits the key wins; its sources are tried in order, so a key refused by the
first (not entitled, line budget, coverage) falls back to the next. Every event names the source that served it.

Without the file, the only source is SYNTHETIC (development and the demo). SYNTHETIC is refused in a configuration
that enables any real source, so generated data is never used when a real source is configured.

Adapters are referenced by dotted factory path (``module:function``) and imported only when enabled, so this package
never imports a broker package (tests/test_ibkr_adapter.py enforces that for IBKR).
"""
from __future__ import annotations

import fnmatch
import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .source import MarketDataSource, UnavailableSource

log = logging.getLogger("unknown.marketdata")

LOCAL_CONFIG_NAME = "marketdata.json"
SYNTHETIC = "SYNTHETIC"
FACTORIES = {
    "SYNTHETIC": "unknown.marketdata.synthetic_factory:build",
    "IBKR": "unknown.broker.ibkr.mdsource:build",
    "ALPACA": "unknown.broker.alpaca.stream:build",
}


class MarketDataConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    match: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class BusConfig:
    sources: dict = field(default_factory=dict)     # source id -> options dict (enabled ones only)
    routes: tuple[Route, ...] = ()
    origin: str = "default"                         # "default" (no local file) or the file path
    trade_buffer: int = 8192                        # per-client queued events before trades overflow
    coalesce_ms: int = 50

    @property
    def real_sources(self) -> list[str]:
        return [s for s in self.sources if s != SYNTHETIC]

    def sources_for(self, key: str) -> tuple[str, ...]:
        for route in self.routes:
            if fnmatch.fnmatchcase(key, route.match):
                return tuple(s for s in route.sources if s in self.sources)
        return ()


DEFAULT = {"sources": {SYNTHETIC: {"enabled": True, "hz": 4}}, "routes": [{"match": "*", "sources": [SYNTHETIC]}]}


def config_from_dict(data: dict, origin: str = "dict") -> BusConfig:
    if not isinstance(data, dict):
        raise MarketDataConfigError("market-data configuration must be an object")
    raw = data.get("sources")
    if not isinstance(raw, dict) or not raw:
        raise MarketDataConfigError("'sources' must be a non-empty object")
    sources = {}
    for sid, opts in raw.items():
        if not isinstance(sid, str) or not sid.isupper() or not sid.replace("_", "").isalnum():
            raise MarketDataConfigError(f"source id {sid!r} must be upper-case letters, digits or '_'")
        if not isinstance(opts, dict):
            raise MarketDataConfigError(f"{sid}: options must be an object")
        if opts.get("enabled", True):
            if sid not in FACTORIES and "factory" not in opts:
                raise MarketDataConfigError(f"{sid}: unknown source; give a 'factory' (module:function)")
            sources[sid] = dict(opts)
    if SYNTHETIC in sources and len(sources) > 1:
        raise MarketDataConfigError("SYNTHETIC cannot be enabled together with a real source: generated data is never "
                                    "used when a real source is configured")
    routes = []
    for i, r in enumerate(data.get("routes") or [{"match": "*", "sources": list(sources)}]):
        if not isinstance(r, dict) or not isinstance(r.get("match"), str) or not isinstance(r.get("sources"), list):
            raise MarketDataConfigError(f"routes[{i}] needs 'match' (string) and 'sources' (list)")
        unknown = [s for s in r["sources"] if s not in raw]
        if unknown:
            raise MarketDataConfigError(f"routes[{i}] names undeclared sources {unknown}")
        routes.append(Route(r["match"], tuple(r["sources"])))
    buf = int(data.get("trade_buffer", 8192))
    if not 64 <= buf <= 1_000_000:
        raise MarketDataConfigError("trade_buffer must be in [64, 1000000]")
    return BusConfig(sources, tuple(routes), origin, buf)


def load_config(home: Path) -> BusConfig:
    path = Path(home) / "config" / LOCAL_CONFIG_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config_from_dict(DEFAULT, "default: no config/marketdata.json, SYNTHETIC only")
    except json.JSONDecodeError as exc:
        raise MarketDataConfigError(f"{path} is not valid JSON: {exc}") from None
    return config_from_dict(data, str(path))


def build_sources(config: BusConfig, home: Path) -> dict[str, MarketDataSource]:
    """Instantiate each enabled source through its factory; a failure becomes a visible UnavailableSource."""
    out: dict[str, MarketDataSource] = {}
    for sid, opts in config.sources.items():
        target = opts.get("factory") or FACTORIES[sid]
        try:
            mod_name, fn_name = target.split(":", 1)
            factory = getattr(importlib.import_module(mod_name), fn_name)
            src = factory(opts, Path(home))
            src.source_id = sid
        except Exception as exc:   # missing client library, keys or broker config: show it, never hide it
            log.warning("market-data source %s unavailable: %s: %s", sid, type(exc).__name__, exc)
            src = UnavailableSource(sid, f"{type(exc).__name__}: {exc}")
        out[sid] = src
    return out


_REGISTRY = None


def venue(mic: str | None):
    """The venue profile from config/markets/venues.json, or None (unknown MIC or a core instrument id)."""
    global _REGISTRY
    if mic is None:
        return None
    if _REGISTRY is None:
        from ..domain.markets import load_registry
        _REGISTRY = load_registry()
    try:
        return _REGISTRY.venue(mic)
    except Exception:
        return None


def currency_for(key: str) -> str | None:
    from .events import InvalidKey, parse_key
    try:
        _, mic = parse_key(key)
    except InvalidKey:
        return None
    v = venue(mic)
    return v.currency if v is not None else None
