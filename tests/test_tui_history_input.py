"""Behavioural tests for :class:`pip_agent.tui.history_input.HistoryInput`.

The widget is self-contained so the tests host it inside a minimal
Textual app rather than going through ``PipBoyTuiApp``. That keeps the
matrix small and the failure surface obvious — when one of these
breaks, the widget itself is the suspect, not the wider TUI topology.

Coverage:

* ``↑`` walks the list backwards, saves the in-progress draft once.
* ``↓`` past the newest entry restores the saved draft.
* Adjacent duplicates collapse on submit.
* History persists to a file across two App instances when
  ``history_path`` is provided.
* ``Tab`` accepts an inline ``Suggester`` ghost text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.suggester import SuggestFromList

from pip_agent.tui.history_input import HistoryInput


class _HostApp(App[None]):
    """Tiny Textual app that just mounts a single ``HistoryInput``."""

    def __init__(self, **input_kwargs: Any) -> None:
        super().__init__()
        self._input_kwargs = input_kwargs
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield HistoryInput(id="probe", **self._input_kwargs)

    def on_mount(self) -> None:
        self.query_one("#probe", HistoryInput).focus()

    def on_input_submitted(self, event: HistoryInput.Submitted) -> None:
        self.submitted.append(event.value)
        # Mirror the production App: clear the box after each submit so
        # the next ``up`` press has a known starting state (empty draft).
        event.input.value = ""


async def _submit(pilot: Any, text: str) -> None:
    """Type ``text`` and press Enter via the Textual pilot."""
    for ch in text:
        await pilot.press(ch if ch != "/" else "slash")
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_up_walks_backwards_and_saves_draft() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        await _submit(pilot, "alpha")
        await _submit(pilot, "beta")

        widget = app.query_one("#probe", HistoryInput)
        widget.value = "draft-in-progress"
        widget.cursor_position = len(widget.value)

        await pilot.press("up")
        assert widget.value == "beta"

        await pilot.press("up")
        assert widget.value == "alpha"

        # Past the oldest is a no-op — staying on alpha.
        await pilot.press("up")
        assert widget.value == "alpha"

        # Walking forward restores beta then the saved draft.
        await pilot.press("down")
        assert widget.value == "beta"
        await pilot.press("down")
        assert widget.value == "draft-in-progress"


@pytest.mark.asyncio
async def test_down_without_history_navigation_is_noop() -> None:
    """Pressing ``down`` while idle (no prior ``up``) must not blank the box."""
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#probe", HistoryInput)
        widget.value = "typing"
        widget.cursor_position = len(widget.value)

        await pilot.press("down")
        assert widget.value == "typing"


@pytest.mark.asyncio
async def test_adjacent_duplicates_collapse() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        await _submit(pilot, "same")
        await _submit(pilot, "same")
        await _submit(pilot, "different")

        widget = app.query_one("#probe", HistoryInput)
        # Three submits, two distinct entries retained.
        assert widget._history == ["same", "different"]


@pytest.mark.asyncio
async def test_history_persists_across_app_instances(tmp_path: Path) -> None:
    history_file = tmp_path / "tui_history.log"

    first = _HostApp(history_path=history_file)
    async with first.run_test() as pilot:
        await _submit(pilot, "persist-me")
        await _submit(pilot, "and-me-too")

    assert history_file.exists()
    persisted = history_file.read_text(encoding="utf-8").splitlines()
    assert persisted == ["persist-me", "and-me-too"]

    # A fresh App pointed at the same file sees the entries on ``up``.
    second = _HostApp(history_path=history_file)
    async with second.run_test() as pilot:
        widget = second.query_one("#probe", HistoryInput)
        assert widget._history == ["persist-me", "and-me-too"]
        await pilot.press("up")
        assert widget.value == "and-me-too"
        await pilot.press("up")
        assert widget.value == "persist-me"


@pytest.mark.asyncio
async def test_history_limit_truncates_oldest() -> None:
    app = _HostApp(history_limit=3)
    async with app.run_test() as pilot:
        for word in ("a", "b", "c", "d"):
            await _submit(pilot, word)

        widget = app.query_one("#probe", HistoryInput)
        assert widget._history == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_tab_accepts_inline_suggestion() -> None:
    app = _HostApp(
        suggester=SuggestFromList(
            ["/help", "/memory", "/status"], case_sensitive=False,
        ),
    )
    async with app.run_test() as pilot:
        widget = app.query_one("#probe", HistoryInput)
        await pilot.press("slash")
        await pilot.press("m")
        await pilot.pause()
        # Wait for the async suggester to resolve.
        for _ in range(5):
            if widget._suggestion:
                break
            await pilot.pause()
        assert widget._suggestion == "/memory"

        await pilot.press("tab")
        assert widget.value == "/memory"


@pytest.mark.asyncio
async def test_tab_without_suggestion_does_not_clobber_value() -> None:
    """Tab without a suggestion falls through to focus_next, value unchanged."""
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#probe", HistoryInput)
        widget.value = "hello"
        widget.cursor_position = len(widget.value)

        await pilot.press("tab")
        assert widget.value == "hello"
