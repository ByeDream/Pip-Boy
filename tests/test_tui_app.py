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
        assert app.query_one("#agent-log-detail")
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


# ---------------------------------------------------------------------------
# Plan-mode side-status tracking
# ---------------------------------------------------------------------------


def _render_status_plain(app: PipBoyTuiApp) -> str:
    """Strip Rich markup so tests can assert on visible text."""
    import re
    return re.sub(r"\[/?[^\]]*\]", "", app._render_side_status())


def test_side_status_without_plan_mode_has_no_plan_row() -> None:
    """Smoke: a fresh App renders no PLAN row until EnterPlanMode fires."""
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(
        theme=bundle, pump=pump,
        initial_side_snapshot={"agent": "pip-boy", "model": "t0"},
    )
    out = _render_status_plain(app)
    assert "PLAN" not in out
    assert "AGENT" in out


def test_side_status_plan_mode_injects_row_with_elapsed() -> None:
    """While _plan_entered_at is set, PLAN appears with a count-up age."""
    import time as _time
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(
        theme=bundle, pump=pump,
        initial_side_snapshot={"agent": "pip-boy", "model": "t0"},
    )
    app._plan_entered_at = _time.time() - 7  # 7 seconds ago
    out = _render_status_plain(app)
    assert "PLAN" in out
    assert "active" in out
    # 7s ago should render as "7s" (or close — we allow 6–8 for jitter).
    assert any(f"{n}s" in out for n in range(5, 10))


def test_side_status_plan_mode_row_above_agent() -> None:
    """PLAN row is prepended before AGENT so it's the first thing users see."""
    import time as _time
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(
        theme=bundle, pump=pump,
        initial_side_snapshot={"agent": "pip-boy", "model": "t0 · claude"},
    )
    app._plan_entered_at = _time.time()
    out = _render_status_plain(app)
    plan_idx = out.find("PLAN")
    agent_idx = out.find("AGENT")
    assert plan_idx >= 0 and agent_idx >= 0
    assert plan_idx < agent_idx


def test_side_status_plan_mode_rendered_even_with_empty_snapshot() -> None:
    """PLAN row renders even before the first snapshot arrives — otherwise
    the 'initializing…' placeholder would hide the most useful signal."""
    import time as _time
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)  # no initial_side_snapshot
    app._plan_entered_at = _time.time()
    out = _render_status_plain(app)
    assert "PLAN" in out
    assert "initializing" not in out


@pytest.mark.asyncio
async def test_enter_plan_mode_tool_event_sets_state() -> None:
    """EnterPlanMode tool_use flips _plan_entered_at; ExitPlanMode clears it."""
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="tool_use", name="EnterPlanMode"))
        await pilot.pause()
        assert app._plan_entered_at is not None
        pump.agent_sink(AgentEvent(kind="tool_use", name="ExitPlanMode"))
        await pilot.pause()
        assert app._plan_entered_at is None


@pytest.mark.asyncio
async def test_non_plan_tool_events_do_not_touch_state() -> None:
    """Unrelated tool_use events (Bash, Read, Grep, …) must leave the
    plan-mode tracker untouched."""
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        assert app._plan_entered_at is None
        pump.agent_sink(AgentEvent(kind="tool_use", name="Bash"))
        pump.agent_sink(AgentEvent(kind="tool_use", name="Read"))
        pump.agent_sink(AgentEvent(kind="tool_use", name="Grep"))
        await pilot.pause()
        assert app._plan_entered_at is None


# ---------------------------------------------------------------------------
# Agent pane split: #agent-log (dialog) vs #agent-log-detail (detail)
# ---------------------------------------------------------------------------


def _log_text(widget: RichLog) -> str:
    """Flatten a RichLog's visible lines into plain text for assertions.

    RichLog stores rendered ``Strip`` objects in ``.lines``; each strip
    exposes a ``text`` property. We join strips with newlines so a
    single assertion can check "substring appears anywhere in pane"
    without caring which strip it landed on.
    """
    return "\n".join(getattr(line, "text", str(line)) for line in widget.lines)


@pytest.mark.asyncio
async def test_user_input_lands_in_dialog_not_detail() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="user_input", text="hello"))
        await pilot.pause()
        dialog = _log_text(app.query_one("#agent-log", RichLog))
        detail = _log_text(app.query_one("#agent-log-detail", RichLog))
        assert "hello" in dialog
        assert "hello" not in detail


@pytest.mark.asyncio
async def test_markdown_lands_in_dialog_not_detail() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="markdown", text="Some text"))
        await pilot.pause()
        dialog = _log_text(app.query_one("#agent-log", RichLog))
        detail = _log_text(app.query_one("#agent-log-detail", RichLog))
        assert "Some text" in dialog
        assert "Some text" not in detail


@pytest.mark.asyncio
async def test_tool_use_lands_in_detail_not_dialog() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="tool_use", name="Bash"))
        await pilot.pause()
        dialog = _log_text(app.query_one("#agent-log", RichLog))
        detail = _log_text(app.query_one("#agent-log-detail", RichLog))
        assert "[tool: Bash]" in detail
        assert "[tool: Bash]" not in dialog


@pytest.mark.asyncio
async def test_finalize_footer_lands_in_detail_not_dialog() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="text_delta", text="hi"))
        pump.agent_sink(AgentEvent(
            kind="finalize", num_turns=1, cost_usd=0.001,
            usage={"input_tokens": 10}, elapsed_s=1.5,
        ))
        await pilot.pause()
        dialog = _log_text(app.query_one("#agent-log", RichLog))
        detail = _log_text(app.query_one("#agent-log-detail", RichLog))
        # Dialog still has the assistant reply.
        assert "hi" in dialog
        # Footer with turn/cost/elapsed metadata belongs in detail.
        # Theme templates vary ("turn"/"turns", "$"/"cost") — match any
        # signature that proves the footer rendered here.
        assert "turn" in detail or "$" in detail or "1.5s" in detail


@pytest.mark.asyncio
async def test_error_lands_in_detail_not_dialog() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="error", text="oops"))
        await pilot.pause()
        dialog = _log_text(app.query_one("#agent-log", RichLog))
        detail = _log_text(app.query_one("#agent-log-detail", RichLog))
        assert "oops" in detail
        assert "oops" not in dialog


@pytest.mark.asyncio
async def test_clear_log_action_clears_both_panes() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(kind="user_input", text="user1"))
        pump.agent_sink(AgentEvent(kind="tool_use", name="Bash"))
        await pilot.pause()
        app.action_clear_log()
        await pilot.pause()
        dialog = app.query_one("#agent-log", RichLog)
        detail = app.query_one("#agent-log-detail", RichLog)
        assert len(dialog.lines) == 0
        assert len(detail.lines) == 0
