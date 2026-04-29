"""``host_io`` shim tests: print vs sink branching.

These are regression tests for the contract that producer code (banner,
channel registration, agent reply, error printout) calls ``emit_*`` and
the shim picks the right backend automatically. The contract MUST hold
in both directions:

* Line mode (``install_pump`` not called) — every ``emit_*`` falls
  through to ``print()``.
* TUI mode (a pump is installed and attached) — every ``emit_*``
  pushes an event into the active sink.

The shim is the single switch point for design.md §7's "三类 sink 错位"
fix: if any producer reaches around the shim, it'll show up as
canvas-corruption in the TUI. Tests below cover every public emitter.
"""

from __future__ import annotations

import pytest

from pip_agent import host_io
from pip_agent.tui.pump import UiPump


class _RecordingApp:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def post_message(self, msg: object) -> None:
        self.messages.append(msg)


@pytest.fixture
def attached_pump() -> UiPump:
    """A pump bound to a recording app, installed in host_io."""
    pump = UiPump()
    app = _RecordingApp()
    pump.attach(app)
    host_io.install_pump(pump)
    yield pump
    host_io.uninstall_pump()


@pytest.fixture
def line_mode():
    """Ensure no pump is installed."""
    host_io.uninstall_pump()
    yield
    host_io.uninstall_pump()


# ---------------------------------------------------------------------------
# is_tui_active
# ---------------------------------------------------------------------------


class TestActiveDetection:
    def test_line_mode_returns_false(self, line_mode) -> None:
        assert host_io.is_tui_active() is False
        assert host_io.active_pump() is None

    def test_attached_pump_returns_true(self, attached_pump: UiPump) -> None:
        assert host_io.is_tui_active() is True
        assert host_io.active_pump() is attached_pump

    def test_unattached_pump_returns_false(self) -> None:
        pump = UiPump()
        host_io.install_pump(pump)
        try:
            assert host_io.is_tui_active() is False
        finally:
            host_io.uninstall_pump()


# ---------------------------------------------------------------------------
# Status emitters: TUI active → status_sink
# ---------------------------------------------------------------------------


def _kinds(messages: list[object]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if hasattr(m, "event"):
            out.append(getattr(m.event, "kind", ""))
        elif hasattr(m, "record"):
            out.append("log")
    return out


class TestEmitStatusBranches:
    def test_banner_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_banner("ROBCO\nWelcome")
        # Banner: 1 status event (collapsed first line) + 1 agent markdown.
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "banner" in kinds
        assert "markdown" in kinds

    def test_banner_in_line_mode_prints(
        self, line_mode, capsys: pytest.CaptureFixture[str]
    ) -> None:
        host_io.emit_banner("hello banner")
        out = capsys.readouterr().out
        assert "hello banner" in out

    def test_channel_ready_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_channel_ready("cli")
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "channel_ready" in kinds

    def test_channel_ready_in_line_mode_prints(
        self, line_mode, capsys: pytest.CaptureFixture[str]
    ) -> None:
        host_io.emit_channel_ready("cli")
        out = capsys.readouterr().out
        assert "Channel registered: cli" in out

    def test_ready_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_ready("type away")
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "ready" in kinds

    def test_shutdown_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_shutdown("Powering down.")
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "shutdown" in kinds
        assert "markdown" in kinds


# ---------------------------------------------------------------------------
# Agent emitters
# ---------------------------------------------------------------------------


class TestEmitAgentBranches:
    def test_agent_markdown_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_agent_markdown("**hi**")
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "markdown" in kinds

    def test_agent_markdown_in_line_mode_prints(
        self, line_mode, capsys: pytest.CaptureFixture[str]
    ) -> None:
        host_io.emit_agent_markdown("**hi**")
        assert "**hi**" in capsys.readouterr().out

    def test_agent_text_line_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_agent_text_line("ok")
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "plain" in kinds

    def test_agent_error_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_agent_error("boom")
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "error" in kinds

    def test_agent_error_in_line_mode_prints(
        self, line_mode, capsys: pytest.CaptureFixture[str]
    ) -> None:
        host_io.emit_agent_error("boom")
        assert "[error] boom" in capsys.readouterr().out

    def test_agent_finalize_in_tui_mode(self, attached_pump: UiPump) -> None:
        host_io.emit_agent_finalize(
            num_turns=2, cost_usd=0.012, usage={"tool_calls": 1},
        )
        # Finalize routes through agent_sink with kind="finalize".
        kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
        assert "finalize" in kinds


# ---------------------------------------------------------------------------
# CLI stream-event callback factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_stream_cb_returns_none_in_line_mode(line_mode) -> None:
    assert host_io.build_cli_stream_event_cb() is None


@pytest.mark.asyncio
async def test_cli_stream_cb_dispatches_in_tui_mode(
    attached_pump: UiPump,
) -> None:
    cb = host_io.build_cli_stream_event_cb()
    assert cb is not None
    await cb("text_delta", text="abc")
    await cb("thinking_delta", text="reflecting…")
    await cb("tool_use", name="Read")
    await cb(
        "finalize", num_turns=1, cost_usd=0.001,
        usage={"input_tokens": 100},
    )
    kinds = _kinds(attached_pump._app.messages)  # type: ignore[attr-defined]
    assert {"text_delta", "thinking_delta", "tool_use", "finalize"} <= set(kinds)


@pytest.mark.asyncio
async def test_cli_stream_cb_finalize_carries_elapsed_s(
    attached_pump: UiPump,
) -> None:
    cb = host_io.build_cli_stream_event_cb()
    assert cb is not None
    await cb(
        "finalize",
        num_turns=1,
        cost_usd=0.0,
        usage={},
        elapsed_s=3.5,
    )
    msgs = attached_pump._app.messages  # type: ignore[attr-defined]
    fin = next(
        m
        for m in msgs
        if getattr(getattr(m, "event", None), "kind", "") == "finalize"
    )
    assert fin.event.elapsed_s == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_cli_stream_cb_tool_use_pipes_input_dict(
    attached_pump: UiPump,
) -> None:
    """`tool_use` kwargs carry the raw SDK input dict through to AgentEvent.

    Without this the TUI can only render the tool's name — the formatter
    (tui.tool_format) has no data to work with. Regression guard for
    the Phase-1 observability commit.
    """
    cb = host_io.build_cli_stream_event_cb()
    assert cb is not None
    await cb(
        "tool_use",
        name="Write",
        input={"file_path": "/tmp/x.md", "content": "hi"},
    )
    msgs = attached_pump._app.messages  # type: ignore[attr-defined]
    ev = next(
        m.event for m in msgs
        if getattr(getattr(m, "event", None), "kind", "") == "tool_use"
    )
    assert ev.name == "Write"
    assert ev.tool_input == {"file_path": "/tmp/x.md", "content": "hi"}


@pytest.mark.asyncio
async def test_cli_stream_cb_tool_use_without_input(
    attached_pump: UiPump,
) -> None:
    """Missing or non-dict `input` must degrade to an empty dict, not
    crash the dispatch. Backward-compat with pre-Phase-1 producers."""
    cb = host_io.build_cli_stream_event_cb()
    assert cb is not None
    await cb("tool_use", name="Read")
    await cb("tool_use", name="Read", input=None)
    await cb("tool_use", name="Read", input="not-a-dict")
    msgs = attached_pump._app.messages  # type: ignore[attr-defined]
    tool_events = [
        m.event for m in msgs
        if getattr(getattr(m, "event", None), "kind", "") == "tool_use"
    ]
    assert len(tool_events) == 3
    for ev in tool_events:
        assert ev.tool_input == {}



@pytest.mark.asyncio
async def test_cli_stream_cb_swallows_internal_errors(
    attached_pump: UiPump,
) -> None:
    """A broken kwargs payload must not propagate into the SDK loop."""
    cb = host_io.build_cli_stream_event_cb()
    assert cb is not None
    # ``finalize`` with ``num_turns="not-a-number"`` would raise on int().
    # The callback must swallow and log instead of raising.
    await cb("finalize", num_turns="not-a-number")
    # No assertion — survival is the success criterion.


# ---------------------------------------------------------------------------
# install/uninstall semantics
# ---------------------------------------------------------------------------


def test_install_then_uninstall_round_trip() -> None:
    pump = UiPump()
    host_io.install_pump(pump)
    assert host_io.active_pump() is pump
    host_io.uninstall_pump()
    assert host_io.active_pump() is None


class TestEmitOperatorPlain:
    def test_tui_routes_to_agent_markdown(
        self, attached_pump: UiPump,
    ) -> None:
        host_io.emit_operator_plain("  [wechat] hello")
        msgs = attached_pump._app.messages  # type: ignore[attr-defined]
        kinds = _kinds(msgs)
        assert "markdown" in kinds
        payload = next(
            m.event.text for m in msgs
            if hasattr(m, "event") and getattr(m.event, "kind", "") == "markdown"
        )
        assert "hello" in payload

    def test_line_mode_prints_verbatim(self, line_mode, capsys: pytest.CaptureFixture[str]) -> None:
        host_io.emit_operator_plain("  [wechat] no-prefix-mangling")
        out = capsys.readouterr().out
        assert "no-prefix-mangling" in out
        assert "[wechat] no-prefix-mangling" in out


def test_double_install_warns_and_overwrites(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pump1 = UiPump()
    pump2 = UiPump()
    host_io.install_pump(pump1)
    try:
        with caplog.at_level("WARNING", logger=host_io.log.name):
            host_io.install_pump(pump2)
        assert "install_pump called twice" in caplog.text
        assert host_io.active_pump() is pump2
    finally:
        host_io.uninstall_pump()


# ---------------------------------------------------------------------------
# Status-bar "blocking tool" indicator
# ---------------------------------------------------------------------------


import asyncio as _asyncio  # noqa: E402 — keep classes above import-free


@pytest.fixture
def tool_wait_state(monkeypatch: pytest.MonkeyPatch):
    """Shrink the grace period and reset module state around each test."""
    monkeypatch.setattr(host_io, "_TOOL_WAIT_THRESHOLD_S", 0.02)
    host_io._tool_wait_in_flight.clear()
    host_io._tool_wait_showing = False
    if host_io._tool_wait_pending_task is not None:
        host_io._tool_wait_pending_task.cancel()
        host_io._tool_wait_pending_task = None
    yield
    host_io._tool_wait_in_flight.clear()
    host_io._tool_wait_showing = False
    if host_io._tool_wait_pending_task is not None:
        host_io._tool_wait_pending_task.cancel()
        host_io._tool_wait_pending_task = None


def _tool_wait_events(pump: UiPump) -> list[object]:
    """Collect all tool_wait StatusEvents posted to the pump's app."""
    msgs = pump._app.messages  # type: ignore[attr-defined]
    return [
        m.event for m in msgs
        if getattr(getattr(m, "event", None), "kind", "") == "tool_wait"
    ]


class TestToolWaitStatusBar:
    """The status bar surfaces long-running tools after a grace period.

    Covered:
      * Tool that completes within the grace period never touches the
        status bar (would just flicker).
      * Tool still in flight past the grace period posts
        ``tool_wait`` with non-empty text; matching ``tool_result``
        posts an empty-text ``tool_wait`` (clear sentinel).
      * Concurrent tools show a count. Clearing one while another is
        still in flight updates the bar rather than collapsing it.
      * ``finalize`` clears any leftover state so a stuck spinner
        cannot leak across turns.
    """

    @pytest.mark.asyncio
    async def test_fast_tool_under_threshold_never_posts(
        self, attached_pump: UiPump, tool_wait_state,
    ) -> None:
        cb = host_io.build_cli_stream_event_cb()
        assert cb is not None
        await cb(
            "tool_use",
            id="tu-1",
            name="Read",
            input={"file_path": "/tmp/x.md"},
        )
        # Return well before the 20ms grace period.
        await cb("tool_result", tool_use_id="tu-1", is_error=False)
        # Let the (soon-to-be-cancelled) timer be scheduled/cancelled.
        await _asyncio.sleep(0.04)
        assert _tool_wait_events(attached_pump) == []

    @pytest.mark.asyncio
    async def test_slow_tool_posts_then_clears(
        self, attached_pump: UiPump, tool_wait_state,
    ) -> None:
        cb = host_io.build_cli_stream_event_cb()
        assert cb is not None
        await cb(
            "tool_use",
            id="tu-1",
            name="Bash",
            input={"command": "sleep 10"},
        )
        await _asyncio.sleep(0.05)  # past threshold
        shown = _tool_wait_events(attached_pump)
        assert len(shown) == 1
        assert "Bash" in shown[0].text
        assert "sleep 10" in shown[0].text  # format_tool_summary included args
        assert shown[0].text  # non-empty → "showing" state
        await cb("tool_result", tool_use_id="tu-1", is_error=False)
        cleared = _tool_wait_events(attached_pump)
        assert len(cleared) == 2
        assert cleared[-1].text == ""  # clear sentinel

    @pytest.mark.asyncio
    async def test_concurrent_tools_show_count(
        self, attached_pump: UiPump, tool_wait_state,
    ) -> None:
        cb = host_io.build_cli_stream_event_cb()
        assert cb is not None
        await cb(
            "tool_use", id="a", name="Bash", input={"command": "slow1"},
        )
        await cb(
            "tool_use", id="b", name="Bash", input={"command": "slow2"},
        )
        await _asyncio.sleep(0.05)
        events = _tool_wait_events(attached_pump)
        # Expect: first post after timer fires (2 in flight → count=2),
        # and the second tool_use before threshold didn't post anything.
        # First non-empty post must mention "2 tools in flight".
        assert events, "expected at least one tool_wait post"
        non_empty = [e for e in events if e.text]
        assert non_empty
        assert "2 tools in flight" in non_empty[0].text

        # End one — bar should update to a single-tool message, not clear.
        await cb("tool_result", tool_use_id="a", is_error=False)
        after_end_one = _tool_wait_events(attached_pump)
        assert after_end_one[-1].text  # still non-empty
        assert "2 tools" not in after_end_one[-1].text
        assert "waiting on" in after_end_one[-1].text

        # End the other — now bar should clear.
        await cb("tool_result", tool_use_id="b", is_error=False)
        after_end_both = _tool_wait_events(attached_pump)
        assert after_end_both[-1].text == ""

    @pytest.mark.asyncio
    async def test_finalize_clears_leftover_state(
        self, attached_pump: UiPump, tool_wait_state,
    ) -> None:
        """If the SDK / turn aborts mid-tool we must not leak a spinner."""
        cb = host_io.build_cli_stream_event_cb()
        assert cb is not None
        await cb(
            "tool_use", id="tu-1", name="Bash", input={"command": "x"},
        )
        await _asyncio.sleep(0.05)
        assert any(
            e.text for e in _tool_wait_events(attached_pump)
        ), "spinner should be showing"
        await cb(
            "finalize", num_turns=1, cost_usd=0.0, usage={}, elapsed_s=1.0,
        )
        # Last tool_wait event must be the clear sentinel.
        tw_events = _tool_wait_events(attached_pump)
        assert tw_events[-1].text == ""
        # And module state fully reset.
        assert host_io._tool_wait_in_flight == {}
        assert host_io._tool_wait_showing is False
        assert host_io._tool_wait_pending_task is None

    @pytest.mark.asyncio
    async def test_missing_tool_id_is_tolerated(
        self, attached_pump: UiPump, tool_wait_state,
    ) -> None:
        """A ``tool_use`` without an ``id`` must not crash the callback
        and must not leave orphan state behind."""
        cb = host_io.build_cli_stream_event_cb()
        assert cb is not None
        await cb("tool_use", name="Read", input={"file_path": "/tmp/x"})
        await _asyncio.sleep(0.04)
        assert _tool_wait_events(attached_pump) == []
        assert host_io._tool_wait_in_flight == {}
