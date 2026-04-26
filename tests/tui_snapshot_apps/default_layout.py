"""Snapshot scenario: default layout right after mount.

Exercises:

* All five locked widget IDs are visible.
* The wasteland theme's TCSS resolves cleanly (background + accents).
* ASCII art renders inside ``#pipboy-art``.
"""

from __future__ import annotations

from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump
from pip_agent.tui.app import PipBoyTuiApp


_bundle = load_builtin_theme("wasteland")
_pump = UiPump()
app = PipBoyTuiApp(theme=_bundle, pump=_pump)


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
