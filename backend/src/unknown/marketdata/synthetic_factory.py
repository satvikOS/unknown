"""Factory for the SYNTHETIC source (config.FACTORIES)."""
from __future__ import annotations

from pathlib import Path

from .config import currency_for
from .synthetic import SyntheticSource


def build(opts: dict, home: Path) -> SyntheticSource:
    return SyntheticSource(hz=float(opts.get("hz", 4)), depth_levels=int(opts.get("depth_levels", 5)),
                           currency_for=currency_for)
