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

import asyncio
import logging
from typing import Any

from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent, StatusEvent
from pip_agent.tui.tool_format import format_tool_summary

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
# Blocking-tool status-bar indicator
# ---------------------------------------------------------------------------
#
# Some tools (``open_file`` waiting on editor close, slow ``Bash``, etc.)
# block an agent turn for seconds to minutes. From the user's side this
# looks like the agent went silent — there's no way to tell "tool in
# flight" from "crashed". We surface the blocking tool in ``#status-bar``.
#
# Semantics:
#   * A tool that returns within ``_TOOL_WAIT_THRESHOLD_S`` never touches
#     the bar — would just flicker.
#   * After the threshold, we show ``⏳ waiting on <name>(<args>)``.
#     While multiple tools are in flight: ``⏳ N tools in flight: <first>``.
#   * When the last tool finishes (or the turn finalizes), we clear the
#     indicator back to the bar's default text.
#   * No status stack — a ``channel_ready`` / ``scheduler`` text that
#     sat in the bar before a tool_wait is lost when the tool clears.
#     That's by design (see the tool_wait discussion in design notes);
#     the bar's default is useful enough that reverting to it is fine.
#
# State lives at module scope. There's exactly one SDK query in flight
# per process (agent_host serialises turns), so no cross-turn races.

_TOOL_WAIT_THRESHOLD_S = 0.5

# id -> (name, one-line arg summary from format_tool_summary)
_tool_wait_in_flight: dict[str, tuple[str, str]] = {}
_tool_wait_pending_task: asyncio.Task[None] | None = None
_tool_wait_showing: bool = False


def _tool_wait_text() -> str:
    """Render the current in-flight set as a one-line bar message."""
    if not _tool_wait_in_flight:
        return ""
    first_name, first_summary = next(iter(_tool_wait_in_flight.values()))
    base = f"{first_name}({first_summary})" if first_summary else first_name
    count = len(_tool_wait_in_flight)
    if count == 1:
        return f"⏳ waiting on {base}"
    return f"⏳ {count} tools in flight: {base}"


def _tool_wait_post(pump: UiPump, text: str) -> None:
    """Push a ``tool_wait`` status event; empty text restores default."""
    pump.status_sink(StatusEvent(kind="tool_wait", text=text))


async def _tool_wait_fire_after_delay(pump: UiPump) -> None:
    """Timer body: after the grace period, promote pending → showing."""
    global _tool_wait_showing, _tool_wait_pending_task
    try:
        await asyncio.sleep(_TOOL_WAIT_THRESHOLD_S)
    except asyncio.CancelledError:
        return
    _tool_wait_pending_task = None
    if not _tool_wait_in_flight:
        return
    _tool_wait_showing = True
    _tool_wait_post(pump, _tool_wait_text())


def _tool_wait_on_start(pump: UiPump, tool_id: str, name: str, summary: str) -> None:
    global _tool_wait_pending_task
    _tool_wait_in_flight[tool_id] = (name, summary)
    if _tool_wait_showing:
        _tool_wait_post(pump, _tool_wait_text())
    elif _tool_wait_pending_task is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # called outside an asyncio loop — give up silently
        _tool_wait_pending_task = loop.create_task(
            _tool_wait_fire_after_delay(pump)
        )


def _tool_wait_on_end(pump: UiPump, tool_id: str) -> None:
    global _tool_wait_showing, _tool_wait_pending_task
    _tool_wait_in_flight.pop(tool_id, None)
    if _tool_wait_in_flight:
        if _tool_wait_showing:
            _tool_wait_post(pump, _tool_wait_text())
        return
    if _tool_wait_pending_task is not None and not _tool_wait_pending_task.done():
        _tool_wait_pending_task.cancel()
    _tool_wait_pending_task = None
    if _tool_wait_showing:
        _tool_wait_showing = False
        _tool_wait_post(pump, "")


def _tool_wait_reset(pump: UiPump) -> None:
    """Called on ``finalize``: drop any leftover in-flight state.

    Shouldn't normally have leftovers — every ``tool_use`` has a
    matching ``tool_result`` — but if the SDK/turn aborts we don't
    want a stuck spinner in the bar across turns.
    """
    global _tool_wait_showing, _tool_wait_pending_task
    if _tool_wait_pending_task is not None and not _tool_wait_pending_task.done():
        _tool_wait_pending_task.cancel()
    _tool_wait_pending_task = None
    was_showing = _tool_wait_showing
    _tool_wait_showing = False
    _tool_wait_in_flight.clear()
    if was_showing:
        _tool_wait_post(pump, "")


# ---------------------------------------------------------------------------
# Stream-event callback for CLI inbounds (TUI mode only)
# ---------------------------------------------------------------------------


async def _cli_tui_stream_cb(event_type: str, **kwargs: Any) -> None:
    """SDK ``on_stream_event`` callback that feeds the TUI agent pane.

    Mirrors the contract from :mod:`pip_agent.agent_runner`: the SDK
    calls this from the agent-message loop with ``thinking_delta``,
    ``text_delta``, ``tool_use``, ``tool_result``, and ``finalize``
    event types. We translate each into the corresponding
    :class:`AgentEvent`, and piggyback ``tool_use`` / ``tool_result``
    to drive the ``#status-bar`` "blocking tool" indicator.

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
            raw_input = kwargs.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            name = str(kwargs.get("name", ""))
            pump.agent_sink(
                AgentEvent(
                    kind="tool_use",
                    name=name,
                    tool_input=tool_input,
                )
            )
            tool_id = str(kwargs.get("id", ""))
            if tool_id:
                _tool_wait_on_start(
                    pump, tool_id, name, format_tool_summary(name, tool_input)
                )
        elif event_type == "tool_result":
            tool_id = str(kwargs.get("tool_use_id", ""))
            if tool_id:
                _tool_wait_on_end(pump, tool_id)
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
            _tool_wait_reset(pump)
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
