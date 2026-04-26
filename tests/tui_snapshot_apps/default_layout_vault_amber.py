"""Snapshot scenario: default layout under the ``vault-amber`` theme.

Mirrors :mod:`default_layout` but uses the second built-in theme so
the SVG baseline doubles as visual proof that swapping a theme
changes *only* the appearance — widget IDs, layout fractions, and
ASCII art slot all stay in the same coordinates.

Pair this baseline with ``default_layout`` (wasteland) when reviewing
Phase B / Phase C theme changes: a diff that touches both files is
expected; a diff that only touches one is suspicious because it
implies the theme leaked into the topology layer.
"""

from __future__ import annotations

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump


_bundle = load_builtin_theme("vault-amber")
_pump = UiPump()
app = PipBoyTuiApp(theme=_bundle, pump=_pump)


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
