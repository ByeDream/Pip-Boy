"""Snapshot scenario: ``/help`` slash-command response in the agent pane.

``/help`` is GFM and uses ``AgentEvent.markdown`` (same path as model
replies) so lists and headings render consistently in the TUI.

Frozen clock + fixed initial snapshot keep the side panel
deterministic across runs.
"""

from __future__ import annotations

from datetime import datetime

from textual.events import Mount

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent

HELP_TEXT = """\
## Available commands

- **`/help`** — Show this help.
- **`/status`** — Current agent, session, and binding.

Type any other line to talk to the active agent.
"""

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


async def _on_mount(_: Mount) -> None:
    _pump.agent_sink(AgentEvent(kind="user_input", text="/help"))
    _pump.agent_sink(AgentEvent(kind="markdown", text=HELP_TEXT))


app.on_mount = _on_mount  # type: ignore[assignment]


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
