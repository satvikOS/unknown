"""CLI plumbing shared by the global-data source modules: open the owner's research store
(``<UNKNOWN_HOME>/research/research.sqlite``, default ``~/.unknown``)."""
from __future__ import annotations

from pathlib import Path

from ..pit import ResearchStore


def open_store(home_arg: str | None):
    from ...service.paths import Paths
    home = Path(home_arg).expanduser() if home_arg else None
    paths = Paths(home) if home else Paths.default()
    research = paths.root / "research"
    research.mkdir(parents=True, exist_ok=True)
    return ResearchStore(research / "research.sqlite"), home
