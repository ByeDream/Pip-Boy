"""Snapshot scenario: default layout under the ``vault-amber`` theme.

Mirrors :mod:`default_layout` but uses the second built-in theme so
the SVG baseline doubles as visual proof that swapping a theme
changes *only* the appearance — widget IDs, layout fractions, and
ASCII resource slots all stay in the same coordinates.

Pair this baseline with ``default_layout`` (wasteland) when reviewing
theme changes: a diff that touches both files is expected; a diff
that only touches one is suspicious because it implies the theme
leaked into the topology layer.

Uses the same frozen clock + snapshot as the wasteland scenario so
side-by-side diffs highlight colour / art differences only.
"""

from __future__ import annotations

from datetime import datetime

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump

_FROZEN = datetime(2077, 10, 23, 18, 56, 42)

_SNAPSHOT = {
    "agent": "pip-boy",
    "model": "t0 · claude-opus-4-7",
    "channels": "cli",
    "session": "new",
    "theme": "Vault Amber v0.2.0",
    "memory": "12 obs · 3 mems",
    "cron": "2 jobs",
    "uptime": "boot 18:56",
    "context": "—",
}

_bundle = load_builtin_theme("vault-amber")
_pump = UiPump()
app = PipBoyTuiApp(
    theme=_bundle,
    pump=_pump,
    clock_provider=lambda: _FROZEN,
    initial_side_snapshot=_SNAPSHOT,
)


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
