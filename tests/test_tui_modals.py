"""Unit tests for the interactive tool modals.

Covers the pure-Python helpers (format_ask_answers) and the behavioural
contract for PipBoyTuiApp when it observes a tool_use event for
AskUserQuestion or ExitPlanMode:

* The modal is pushed only when tool_input has the expected shape.
* The dismissed result is forwarded to ``on_user_line``.
* Cancellation (None dismiss) does NOT forward anything.

Textual's ``run_test`` pilot is used so push_screen actually attaches
the modal to the screen stack — we don't drive key events, just
verify the screen class.
"""

from __future__ import annotations

import pytest

from pip_agent.tui.app import PipBoyTuiApp
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.modals import AskUserModal, PlanReviewModal, format_ask_answers
from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent

# ---------------------------------------------------------------------------
# format_ask_answers — pure helper
# ---------------------------------------------------------------------------


def test_format_answers_empty_list() -> None:
    assert format_ask_answers([]) == ""


def test_format_answers_single_no_header() -> None:
    assert format_ask_answers([("", "red")]) == "red"


def test_format_answers_single_with_header() -> None:
    assert format_ask_answers([("Color", "red")]) == "Color: red"


def test_format_answers_multiple() -> None:
    out = format_ask_answers([("Color", "red"), ("Size", "S")])
    assert out == "Color: red | Size: S"


def test_format_answers_skips_header_when_empty_mixed() -> None:
    out = format_ask_answers([("Color", "red"), ("", "S")])
    assert "Color: red" in out
    assert "S" in out


# ---------------------------------------------------------------------------
# Modal construction (no mount — just __init__)
# ---------------------------------------------------------------------------


def test_ask_modal_filters_non_dict_questions() -> None:
    # Robustness: a malformed tool_input mixture shouldn't crash init.
    modal = AskUserModal([None, {"question": "real?"}, "string"])  # type: ignore[list-item]
    assert len(modal._questions) == 1
    assert modal._questions[0]["question"] == "real?"


def test_plan_modal_empty_plan_fallback() -> None:
    modal = PlanReviewModal("")
    assert "empty plan" in modal._plan_text


# ---------------------------------------------------------------------------
# App → modal trigger path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_askuserquestion_tool_use_pushes_modal() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(
            kind="tool_use", name="AskUserQuestion",
            tool_input={"questions": [{"question": "?", "options": [{"label": "a"}]}]},
        ))
        await pilot.pause()
        # The modal should now be on top of the screen stack.
        assert isinstance(app.screen, AskUserModal)


@pytest.mark.asyncio
async def test_askuserquestion_empty_questions_does_not_push() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(
            kind="tool_use", name="AskUserQuestion",
            tool_input={"questions": []},
        ))
        await pilot.pause()
        # No modal — main screen still on top.
        assert not isinstance(app.screen, AskUserModal)


@pytest.mark.asyncio
async def test_exitplanmode_tool_use_pushes_modal() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(
            kind="tool_use", name="ExitPlanMode",
            tool_input={"plan": "# Plan\n1. step"},
        ))
        await pilot.pause()
        assert isinstance(app.screen, PlanReviewModal)


@pytest.mark.asyncio
async def test_exitplanmode_empty_plan_does_not_push() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(
            kind="tool_use", name="ExitPlanMode",
            tool_input={"plan": "   "},  # whitespace-only = empty
        ))
        await pilot.pause()
        assert not isinstance(app.screen, PlanReviewModal)


@pytest.mark.asyncio
async def test_unrelated_tool_use_does_not_push_modal() -> None:
    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump)
    async with app.run_test() as pilot:
        pump.agent_sink(AgentEvent(
            kind="tool_use", name="Bash",
            tool_input={"command": "ls"},
        ))
        await pilot.pause()
        assert not isinstance(app.screen, (AskUserModal, PlanReviewModal))


# ---------------------------------------------------------------------------
# Modal dismiss → _forward_modal_result plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_result_forwards_to_on_user_line() -> None:
    received: list[str] = []

    def handler(line: str) -> None:
        received.append(line)

    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump, on_user_line=handler)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._forward_modal_result("approve")
        await pilot.pause()
    assert received == ["approve"]


@pytest.mark.asyncio
async def test_modal_result_none_does_not_forward() -> None:
    """Esc (dismiss None) must not queue a user turn."""
    received: list[str] = []

    def handler(line: str) -> None:
        received.append(line)

    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump, on_user_line=handler)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._forward_modal_result(None)
        await pilot.pause()
    assert received == []


@pytest.mark.asyncio
async def test_modal_result_empty_string_does_not_forward() -> None:
    """An empty / whitespace-only answer shouldn't waste an agent turn."""
    received: list[str] = []

    def handler(line: str) -> None:
        received.append(line)

    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(theme=bundle, pump=pump, on_user_line=handler)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._forward_modal_result("   ")
        await pilot.pause()
    assert received == []
