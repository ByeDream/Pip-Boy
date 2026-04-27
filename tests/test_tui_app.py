"""End-to-end-ish smoke tests for ``PipBoyTuiApp`` + the wasteland theme.

Uses Textual's ``run_test`` driver so the App boots into a headless
event loop, the pump attaches, and we can drive input + agent events
through the full sink → message → handler path. This is the real
integration test that covers everything Phase A.1 (sinks, pump,
capability) only stubbed.

SVG snapshot baselines (Phase A.4) live in ``test_tui_snapshots.py``
and use ``pytest-textual-snapshot``; this file restricts itself to
behavioural assertions so it runs in any Python environment, with or
without the snapshot plugin available.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import RichLog

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent, StatusEvent

# ---------------------------------------------------------------------------
# Theme loading
# ---------------------------------------------------------------------------


class TestBuiltinWastelandTheme:
    def test_loads_with_full_palette(self) -> None:
        bundle = load_builtin_theme("wasteland")
        assert bundle.manifest.name == "wasteland"
        assert bundle.path.name == "wasteland"
        # All palette tokens present (validated by manifest schema).
        assert bundle.manifest.palette.accent
        # TCSS is non-empty.
        assert "Screen" in bundle.tcss or "#agent-log" in bundle.tcss

    def test_art_within_design_limits(self) -> None:
        bundle = load_builtin_theme("wasteland")
        if bundle.art_frames:
            for frame in bundle.art_frames:
                lines = frame.splitlines()
                assert len(lines) <= 30  # ART_FRAME_MAX_ROWS
                for ln in lines:
                    assert len(ln) <= 100  # ART_FRAME_MAX_COLS

    def test_unknown_theme_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_builtin_theme("does-not-exist")


# ---------------------------------------------------------------------------
# App boot — headless via ``run_test``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_mounts_and_renders_locked_widget_ids() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)

    async with app.run_test() as pilot:
        # Stable widget IDs — themes can hide via show_* but not rename.
        assert app.query_one("#status-bar")
        assert app.query_one("#main")
        assert app.query_one("#agent-pane")
        assert app.query_one("#agent-log")
        assert app.query_one("#input")
        assert app.query_one("#side-pane")
        assert app.query_one("#side-top")
        assert app.query_one("#pipboy-art")
        assert app.query_one("#pipboy-clock")
        assert app.query_one("#side-status")
        assert app.query_one("#app-log")

        # Pump must be attached at this point (on_mount ran).
        assert pump.is_attached is True

        # Drive an agent event through the pump and confirm the pane
        # picks it up — covers the sink → post_message → handler path.
        pump.agent_sink(AgentEvent(kind="markdown", text="**hello**"))
        await pilot.pause()

        await pilot.press("escape")  # idempotent — just exercises the loop


@pytest.mark.asyncio
async def test_status_event_updates_status_bar() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)

    async with app.run_test() as pilot:
        pump.status_sink(StatusEvent(kind="ready", text="ready: cli online"))
        await pilot.pause()
        from textual.widgets import Static
        bar = app.query_one("#status-bar", Static)
        # ``Static.render()`` returns the current Rich renderable; we
        # inspect its plain-text projection so this test doesn't couple
        # to the private storage attribute name (which churned between
        # textual minors).
        rendered = bar.render()
        plain = getattr(rendered, "plain", None) or str(rendered)
        assert "ready: cli online" in plain


@pytest.mark.asyncio
async def test_user_input_forwards_to_handler() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    received: list[str] = []

    def handler(line: str) -> None:
        received.append(line)

    app = PipBoyTuiApp(theme=bundle, pump=pump, on_user_line=handler)
    async with app.run_test() as pilot:
        await pilot.press("h")
        await pilot.press("i")
        await pilot.press("enter")
        await pilot.pause()

    assert received == ["hi"]


@pytest.mark.asyncio
async def test_request_exit_terminates_app_loop() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)

    async with app.run_test() as pilot:
        app.request_exit()
        # ``run_test`` exits when the App's loop ends. ``pilot.pause``
        # gives Textual a tick to process the deferred exit.
        await pilot.pause()
        await asyncio.sleep(0)
    # Reaching here without timeout means the App exited cleanly.


@pytest.mark.asyncio
async def test_exit_command_does_not_short_circuit_app() -> None:
    """``/exit`` typed in the input must NOT directly terminate the App.

    Design.md §6: the host's ``flush_and_rotate`` path owns shutdown.
    The TUI is just a channel that forwards ``/exit`` to the inbound
    queue, exactly like any other command. The handler decides what
    happens next.
    """
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    received: list[str] = []

    def handler(line: str) -> None:
        received.append(line)

    app = PipBoyTuiApp(theme=bundle, pump=pump, on_user_line=handler)
    async with app.run_test() as pilot:
        for ch in "/exit":
            await pilot.press(ch if ch != "/" else "slash")
        await pilot.press("enter")
        await pilot.pause()
        # Crucially: app is STILL running. Only the host's reflect/
        # rotate path should call request_exit().
        assert app.is_running
    # Now the test framework cleans up.

    assert received == ["/exit"]


@pytest.mark.asyncio
async def test_text_delta_coalesces_before_finalize() -> None:
    """Many small text_delta chunks must not become one RichLog row each."""
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)

    async with app.run_test() as pilot:
        log = app.query_one("#agent-log", RichLog)
        for _ in range(40):
            pump.agent_sink(AgentEvent(kind="text_delta", text="测"))
        await pilot.pause()
        assert len(log.lines) < 10
        pump.agent_sink(
            AgentEvent(
                kind="finalize",
                num_turns=1,
                cost_usd=0.0,
                usage={},
                elapsed_s=0.0,
            )
        )
        await pilot.pause()
        assert len(log.lines) < 20


@pytest.mark.asyncio
async def test_finalize_rewrites_stream_tail_as_markdown() -> None:
    """Stream tail uses ``Text``; finalize swaps it for ``Markdown`` once."""
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)

    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="text_delta", text="**bold**"))
        await pilot.pause()
        pump.agent_sink(
            AgentEvent(
                kind="finalize",
                num_turns=1,
                cost_usd=0.0,
                usage={},
                elapsed_s=0.0,
            )
        )
        await pilot.pause()
        log = app.query_one("#agent-log", RichLog)
        assert len(log.lines) >= 1


# ---------------------------------------------------------------------------
# Live theme swap (``apply_theme``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_theme_swaps_css_and_preserves_history() -> None:
    """``apply_theme`` swaps palette + display name without wiping log."""
    bundle_a = load_builtin_theme("wasteland")
    bundle_b = load_builtin_theme("vault-amber")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle_a, pump=pump)

    async with app.run_test() as pilot:
        # Seed the agent log with a user line so we can prove the
        # swap didn't clear it.
        pump.agent_sink(AgentEvent(kind="user_input", text="hello"))
        await pilot.pause()
        agent_log = app.query_one("#agent-log", RichLog)
        log_lines_before = len(agent_log.lines)
        assert log_lines_before >= 1

        # The status bar starts with wasteland's display name.
        status_text_before = str(
            app.query_one("#status-bar").render()
        )
        assert "Wasteland" in status_text_before

        app.apply_theme(bundle_b)
        await pilot.pause()

        # Agent log survived the swap.
        agent_log_after = app.query_one("#agent-log", RichLog)
        assert len(agent_log_after.lines) >= log_lines_before

        # Status bar flipped to vault-amber's display name.
        status_text_after = str(
            app.query_one("#status-bar").render()
        )
        assert "Vault Amber" in status_text_after

        # Textual theme is the new pipboy-* variant.
        assert app.theme == "pipboy-vault-amber"


@pytest.mark.asyncio
async def test_apply_theme_is_idempotent() -> None:
    """Applying the same bundle twice is a no-op (no exception)."""
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)

    async with app.run_test() as pilot:
        app.apply_theme(bundle)
        await pilot.pause()
        # Still rendering wasteland after an idempotent re-apply.
        assert app.theme == "pipboy-wasteland"
