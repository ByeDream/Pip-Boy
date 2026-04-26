"""Snapshot scenario: ``/help`` slash-command response in the agent pane.

``/help`` is GFM and uses ``AgentEvent.markdown`` (same path as model
replies) so lists and headings render consistently in the TUI.
"""

from __future__ import annotations

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


_bundle = load_builtin_theme("wasteland")
_pump = UiPump()
app = PipBoyTuiApp(theme=_bundle, pump=_pump)


async def _on_mount(_: Mount) -> None:
    _pump.agent_sink(AgentEvent(kind="user_input", text="/help"))
    _pump.agent_sink(AgentEvent(kind="markdown", text=HELP_TEXT))


app.on_mount = _on_mount  # type: ignore[assignment]


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
