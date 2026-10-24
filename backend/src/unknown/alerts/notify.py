"""macOS notifications for fired alerts: ``osascript`` ``display notification`` (local only, no network).

The text is passed as ``argv`` to an AppleScript ``on run`` handler, never spliced into the script, so a headline
(untrusted publisher text) cannot change what the script does. Control characters are removed and the text is
bounded. Turn notifications off with ``{"macos_notifications": false}`` in ``<UNKNOWN_HOME>/config/alerts.json``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
          "end run")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
TIMEOUT_S = 5.0


def clean(text: object, limit: int) -> str:
    s = CONTROL.sub(" ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def enabled(root: Path) -> bool:
    try:
        cfg = json.loads((Path(root) / "config" / "alerts.json").read_text(encoding="utf-8"))
        return cfg.get("macos_notifications", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


class MacNotifier:
    def __init__(self, root: Path, run=subprocess.run, platform: str = sys.platform, which=shutil.which):
        self.root, self._run, self.platform, self._which = Path(root), run, platform, which

    def status(self) -> tuple[str, str | None]:
        """('available' | 'disabled' | 'unavailable', detail)."""
        if not enabled(self.root):
            return "disabled", "macos_notifications is false in config/alerts.json"
        if self.platform != "darwin":
            return "unavailable", f"macOS notifications need macOS (this is {self.platform})"
        if not self._which("osascript"):
            return "unavailable", "osascript was not found"
        return "available", None

    def argv(self, title: str, message: str, subtitle: str = "") -> list[str]:
        args = ["osascript"]
        for line in SCRIPT:
            args += ["-e", line]
        return args + [clean(message, 240), clean(title, 80), clean(subtitle, 80)]

    def send(self, title: str, message: str, subtitle: str = "") -> tuple[str, str | None]:
        """('delivered' | 'failed' | 'disabled' | 'unavailable', detail). Never raises."""
        state, detail = self.status()
        if state != "available":
            return state, detail
        try:
            r = self._run(self.argv(title, message, subtitle), capture_output=True, timeout=TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return "failed", str(exc)[:300]
        if r.returncode != 0:
            return "failed", (r.stderr or b"").decode("utf-8", "replace")[:300] or f"exit {r.returncode}"
        return "delivered", None
