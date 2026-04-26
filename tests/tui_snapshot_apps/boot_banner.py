"""Snapshot scenario: the ROBCO welcome banner just landed in the TUI.

Mirrors the host's first ``emit_banner`` call so the reviewer sees
exactly what the user sees on cold boot.
"""

from __future__ import annotations

from textual.events import Mount

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent, StatusEvent


_bundle = load_builtin_theme("wasteland")
_pump = UiPump()
app = PipBoyTuiApp(theme=_bundle, pump=_pump)


async def _on_mount(_: Mount) -> None:
    _pump.status_sink(StatusEvent(
        kind="banner",
        text="ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL",
    ))
    _pump.agent_sink(AgentEvent(
        kind="markdown",
        text=(
            "============================================\n"
            "  ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL\n"
            "  PIP-BOY 3000 MARK IV  [SDK HOST]\n"
            "  Personal Assistant Module v0.4.3\n"
            "============================================\n"
            "  Welcome, Vault Dweller. Type '/exit' to\n"
            "  power down.\n"
            "  Channels: cli\n"
            "  Agents: pip-boy\n"
            "============================================"
        ),
    ))
    _pump.status_sink(StatusEvent(
        kind="channel_ready", text="channel: cli ready",
    ))
    _pump.status_sink(StatusEvent(
        kind="ready", text="(type and press Enter; /exit to quit)",
    ))


app.on_mount = _on_mount  # type: ignore[assignment]


if __name__ == "__main__":  # pragma: no cover — manual review only
    app.run()
