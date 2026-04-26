"""Snapshot scenario: the ROBCO welcome banner just landed in the TUI.

Mirrors the host's first ``emit_banner`` call so the reviewer sees
exactly what the user sees on cold boot.

Frozen clock + fixed initial snapshot keep the side panel
deterministic. The banner / channel-ready / ready events still flow
through the pump so the status bar transition is covered.
"""

from __future__ import annotations

from datetime import datetime

from textual.events import Mount

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent, StatusEvent

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
