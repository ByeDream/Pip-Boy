"""Snapshot scenario: default layout right after mount.

Exercises:

* All key widget IDs are visible.
* The wasteland theme's TCSS resolves cleanly (background + accents).
* Banner + deco + clock + side-status render inside ``#side-pane``.

Deterministic inputs: a frozen ``clock_provider`` pinned to 2077-10-23
18:56:42 (nuclear-Saturday nod) and a fully-populated
``initial_side_snapshot`` so the SVG baseline never drifts with real
time or host state.
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
    "theme": "Wasteland Radiation v0.1.0",
    "memory": "12 obs · 3 mems",
    "cron": "2 jobs",
    "uptime": "boot 18:56",
    "context": "—",
}

_bundle = load_builtin_theme("wasteland")
_pump = UiPump()
app = PipBoyTuiApp(
    theme=_bundle,
    pump=_pump,
    clock_provider=lambda: _FROZEN,
    initial_side_snapshot=_SNAPSHOT,
)


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
