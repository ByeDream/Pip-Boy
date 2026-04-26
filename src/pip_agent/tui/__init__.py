"""Pip-Boy Textual UI: locked sink protocol + capability ladder + theme API.

The TUI is the default interactive surface from v0.5+. ``line mode`` is
preserved as a fallback for terminals that fail the capability ladder.

Design contract (locked v1, see PipBoyCLITheme/design.md):

* Three sinks, framework-owned: ``agent_sink`` / ``log_sink`` /
  ``status_sink``. Producers push events; the UI pump turns them into
  thread-safe Textual messages.
* Themes own colors, TCSS, ASCII art, and widget on/off toggles.
  Themes do NOT own widget topology, lifecycle, or any code path that
  mutates host state.
* Cross-thread dispatch goes through ``app.post_message`` only — never
  ``call_from_thread``.

The submodules deliberately keep heavy imports (``textual``, ``rich``)
lazy so a ``pip-boy --no-tui`` boot or a non-TTY environment never pays
the import cost.
"""

from __future__ import annotations

from pip_agent.tui.capability import (
    CapabilityResult,
    detect_tui_capability,
    write_capability_log,
)
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.manager import (
    DEFAULT_THEME_NAME,
    ThemeDiscovery,
    ThemeLoadIssue,
    ThemeManager,
    load_theme_bundle,
)
from pip_agent.tui.pump import UiPump
from pip_agent.tui.runner import TuiBootResult, build_app, launch_tui
from pip_agent.tui.sinks import (
    AGENT_EVENT_KINDS,
    STATUS_EVENT_KINDS,
    AgentEvent,
    AgentSink,
    LogSink,
    NullAgentSink,
    NullLogSink,
    NullStatusSink,
    StatusEvent,
    StatusSink,
)
from pip_agent.tui.theme_api import (
    ThemeBundle,
    ThemeManifest,
    ThemePalette,
)

__all__ = [
    "AGENT_EVENT_KINDS",
    "DEFAULT_THEME_NAME",
    "STATUS_EVENT_KINDS",
    "AgentEvent",
    "AgentSink",
    "CapabilityResult",
    "LogSink",
    "NullAgentSink",
    "NullLogSink",
    "NullStatusSink",
    "StatusEvent",
    "StatusSink",
    "ThemeBundle",
    "ThemeDiscovery",
    "ThemeLoadIssue",
    "ThemeManager",
    "ThemeManifest",
    "ThemePalette",
    "TuiBootResult",
    "UiPump",
    "build_app",
    "detect_tui_capability",
    "launch_tui",
    "load_builtin_theme",
    "load_theme_bundle",
    "write_capability_log",
]
