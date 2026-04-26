"""Host-side I/O shim: branch print / sink based on the active TUI mode.

This module is the single place that knows "are we rendering through
the TUI right now?". Producer code (banners, channel registration
notes, error printouts, slash-command responses) imports the helpers
below and calls ``emit_*``; the helper picks the right path:

* TUI active → push an event into the active :class:`UiPump`'s sinks.
* Line mode  → write to ``sys.stdout`` (legacy behaviour).

That keeps the existing line-mode code paths untouched in spirit
(``--no-tui`` boots still print exactly what they used to), while
giving the TUI a single funnel for every host-generated message.

Why a separate module rather than reaching into ``pip_agent.tui.pump``
directly: the TUI-active branch must never import textual at module
load time (line-mode boots, ``pip-boy --version``, ``pip-boy doctor``
plumbing). The helpers below can be imported by any code path because
they only touch the pump's already-loaded state.

Design.md §7 explicitly forbids ``print`` from inside theme code or
sink consumers. This module is the *producer-side* twin: producers
must NOT call ``print`` directly when there's a chance the TUI is
running. Use ``emit_*`` instead.
"""

from __future__ import annotations

import logging
from typing import Any

from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent, StatusEvent

log = logging.getLogger(__name__)

__all__ = [
    "active_pump",
    "build_cli_stream_event_cb",
    "emit_agent_error",
    "emit_agent_finalize",
    "emit_agent_markdown",
    "emit_agent_text_line",
    "emit_operator_plain",
    "emit_banner",
    "emit_channel_ready",
    "emit_ready",
    "emit_shutdown",
    "emit_side_status_snapshot",
    "emit_status",
    "install_pump",
    "is_tui_active",
    "uninstall_pump",
]

# Module-level reference. ``None`` in line mode and during boot before
# the TUI runner installs a pump. Mutating from anywhere other than
# ``run_host`` is a contract violation — we expose ``install_pump`` /
# ``uninstall_pump`` rather than a settable global to make that
# explicit.
_PUMP: UiPump | None = None


def install_pump(pump: UiPump) -> None:
    """Tell the host-io shim about the active pump.

    Called once during ``run_host`` after the capability ladder
    accepts TUI mode. Calling twice is a programming error — log a
    warning and overwrite, since silently dropping the second call
    would hide a bug.
    """
    global _PUMP
    if _PUMP is not None:
        log.warning("host_io.install_pump called twice; overwriting.")
    _PUMP = pump


def uninstall_pump() -> None:
    """Forget the active pump. Call from the host's shutdown ``finally``."""
    global _PUMP
    _PUMP = None


def active_pump() -> UiPump | None:
    """Return the active pump, or ``None`` in line mode."""
    return _PUMP


def is_tui_active() -> bool:
    """True when a pump is installed AND attached to a running App."""
    return _PUMP is not None and _PUMP.is_attached


# ---------------------------------------------------------------------------
# Status / banner emitters
# ---------------------------------------------------------------------------


def emit_banner(text: str) -> None:
    """The ROBCO welcome banner — multi-line, status-bar-bound."""
    if is_tui_active():
        # The TUI status bar is one line; multi-line banners get
        # collapsed to the first non-empty line. The full banner
        # also lands in the agent log so the user can scroll back
        # through it. Keeps the TUI dense without losing the text.
        first_line = next(
            (ln for ln in text.splitlines() if ln.strip()), text
        ).strip()
        _PUMP.status_sink(StatusEvent(kind="banner", text=first_line))  # type: ignore[union-attr]
        _PUMP.agent_sink(AgentEvent(kind="markdown", text=text))  # type: ignore[union-attr]
        return
    print(text)


def emit_channel_ready(name: str) -> None:
    """Channel registration notice. ``name`` is the channel id (cli/wecom/..)."""
    if is_tui_active():
        _PUMP.status_sink(  # type: ignore[union-attr]
            StatusEvent(
                kind="channel_ready",
                text=f"channel: {name} ready",
                fields={"channel": name},
            )
        )
        return
    print(f"  [+] Channel registered: {name}")


def emit_ready(text: str) -> None:
    """The "type and press Enter; /exit to quit" footer hint."""
    if is_tui_active():
        _PUMP.status_sink(StatusEvent(kind="ready", text=text))  # type: ignore[union-attr]
        return
    print(f"  {text}")


def emit_status(text: str) -> None:
    """Generic status line — channel init failures, scheduler, etc."""
    if is_tui_active():
        _PUMP.status_sink(StatusEvent(kind="banner", text=text))  # type: ignore[union-attr]
        return
    print(f"  {text}")


def emit_shutdown(text: str) -> None:
    """The 'powering down' line emitted at /exit completion."""
    if is_tui_active():
        _PUMP.status_sink(StatusEvent(kind="shutdown", text=text))  # type: ignore[union-attr]
        # Also mirror into the agent log so the user sees a final
        # message before the App actually exits — without this the TUI
        # tears down with the agent log frozen on the last reply.
        _PUMP.agent_sink(AgentEvent(kind="markdown", text=f"_{text}_"))  # type: ignore[union-attr]
        return
    print(f"  {text}")


def emit_side_status_snapshot(fields: dict[str, str]) -> None:
    """Push a ``#side-status`` refresh into the TUI.

    ``fields`` merges into the cached snapshot on the App side — so a
    caller that only knows about (say) the channel list can push just
    ``{"channels": "cli, wecom"}`` and the model/memory/cron fields
    stay untouched. Line mode drops the call silently; there's no
    line-mode equivalent of the side panel.

    Emitted at boot after all channels / scheduler / memory are up.
    Subsequent events (channel lost, theme swap, memory write) are
    free to call this again with partial dicts; the cumulative view
    is what's rendered.
    """
    if not is_tui_active():
        return
    # Coerce everything to str: StatusEvent.fields is typed as
    # ``dict[str, str]`` and callers pass counts / percentages as
    # ints/floats. Doing the conversion here keeps every call site
    # from repeating the pattern.
    str_fields = {k: str(v) for k, v in fields.items()}
    _PUMP.status_sink(  # type: ignore[union-attr]
        StatusEvent(kind="side_status_snapshot", fields=str_fields)
    )


# ---------------------------------------------------------------------------
# Agent-pane emitters
# ---------------------------------------------------------------------------


def emit_agent_text_line(text: str) -> None:
    """Legacy one-off agent-pane line (tests, rare call sites).

    Slash-command output from the host uses :func:`emit_agent_markdown`
    after :func:`pip_agent.host_commands.ensure_cli_command_markdown`.
    """
    if is_tui_active():
        _PUMP.agent_sink(AgentEvent(kind="plain", text=text))  # type: ignore[union-attr]
        return
    print(f"  {text}")


def emit_agent_markdown(text: str) -> None:
    """GFM blocks: model replies, ``/help``, banners, and similar."""
    if is_tui_active():
        _PUMP.agent_sink(AgentEvent(kind="markdown", text=text))  # type: ignore[union-attr]
        return
    print(text)


def emit_operator_plain(text: str) -> None:
    """Host / channel operator text (WeChat QR steps, poll notices).

    Same destination as :func:`emit_agent_markdown` in TUI mode, but
    line mode prints ``text`` verbatim — no ``emit_agent_text_line``
    ``  `` prefix — so existing ``print(f\"  [wechat] …\")`` strings
    stay byte-identical in ``--no-tui`` runs.
    """
    if is_tui_active():
        _PUMP.agent_sink(  # type: ignore[union-attr]
            AgentEvent(kind="markdown", text=text.rstrip() + "\n"),
        )
        return
    print(text)


def emit_agent_error(text: str) -> None:
    """Inline error in the agent pane."""
    if is_tui_active():
        _PUMP.agent_sink(AgentEvent(kind="error", text=text))  # type: ignore[union-attr]
        return
    print(f"\n  [error] {text}")


def emit_agent_finalize(
    *,
    num_turns: int = 0,
    cost_usd: float | None = None,
    usage: dict[str, int] | None = None,
    elapsed_s: float = 0.0,
) -> None:
    """End-of-turn footer (turn count / cost / usage)."""
    if is_tui_active():
        _PUMP.agent_sink(  # type: ignore[union-attr]
            AgentEvent(
                kind="finalize",
                num_turns=num_turns,
                cost_usd=cost_usd,
                usage=usage or {},
                elapsed_s=elapsed_s,
            )
        )
        return
    # Line mode: agent_runner already prints a newline to close the
    # streamed line; nothing to do here.


# ---------------------------------------------------------------------------
# Stream-event callback for CLI inbounds (TUI mode only)
# ---------------------------------------------------------------------------


async def _cli_tui_stream_cb(event_type: str, **kwargs: Any) -> None:
    """SDK ``on_stream_event`` callback that feeds the TUI agent pane.

    Mirrors the contract from :mod:`pip_agent.agent_runner`: the SDK
    calls this from the agent-message loop with ``thinking_delta``,
    ``text_delta``, ``tool_use``, and ``finalize`` event types. We
    translate each into the corresponding :class:`AgentEvent`.

    The pump's own thread-safety guarantees the dispatch — this
    callback runs on the asyncio thread, but ``post_message`` works
    regardless.
    """
    pump = _PUMP
    if pump is None or not pump.is_attached:
        return
    try:
        if event_type == "thinking_delta":
            pump.agent_sink(
                AgentEvent(
                    kind="thinking_delta", text=str(kwargs.get("text", ""))
                )
            )
        elif event_type == "text_delta":
            pump.agent_sink(
                AgentEvent(kind="text_delta", text=str(kwargs.get("text", "")))
            )
        elif event_type == "tool_use":
            pump.agent_sink(
                AgentEvent(kind="tool_use", name=str(kwargs.get("name", "")))
            )
        elif event_type == "finalize":
            usage = kwargs.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            try:
                elapsed = float(kwargs.get("elapsed_s", 0.0))
            except (TypeError, ValueError):
                elapsed = 0.0
            pump.agent_sink(
                AgentEvent(
                    kind="finalize",
                    num_turns=int(kwargs.get("num_turns", 0)),
                    cost_usd=kwargs.get("cost_usd"),
                    usage=usage,
                    elapsed_s=elapsed,
                )
            )
    except Exception:  # noqa: BLE001 — never raise into the SDK loop
        log.exception("CLI TUI stream callback failed; suppressing.")


def build_cli_stream_event_cb() -> Any | None:
    """Return the stream-event callback to attach for a CLI inbound.

    ``None`` when TUI is inactive — the caller takes the legacy print
    path through :mod:`pip_agent.agent_runner`.
    """
    if is_tui_active():
        return _cli_tui_stream_cb
    return None
