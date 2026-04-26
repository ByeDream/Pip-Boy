"""Tests for :class:`pip_agent.tui.pump.UiPump`.

These are the regressions for the worst lesson from the PipBoyCLITheme
research stash (design.md §2): a producer thread must NEVER raise into
the App, and the App must NEVER block when a producer fires from its
own loop thread. The pump is the one place that owns those invariants.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

from pip_agent.tui.pump import UiPump
from pip_agent.tui.sinks import AgentEvent, StatusEvent


class _RecordingApp:
    """Minimal stand-in for ``textual.app.App.post_message``.

    Records every (msg_class, payload) so tests can assert what the
    pump pushed, without spinning up an actual Textual event loop.
    Thread-safe to mirror the real App's contract.
    """

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self._lock = threading.Lock()

    def post_message(self, message: Any) -> None:
        with self._lock:
            self.messages.append(message)


class TestBufferedBeforeAttach:
    """Events fired before ``attach`` are queued and flushed in order."""

    def test_agent_events_buffer_then_flush_in_order(self) -> None:
        pump = UiPump()
        pump.agent_sink(AgentEvent(kind="text_delta", text="a"))
        pump.agent_sink(AgentEvent(kind="text_delta", text="b"))
        pump.agent_sink(AgentEvent(kind="text_delta", text="c"))
        app = _RecordingApp()
        pump.attach(app)
        texts = [m.event.text for m in app.messages]
        assert texts == ["a", "b", "c"]

    def test_mixed_sinks_keep_arrival_order(self) -> None:
        pump = UiPump()
        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        pump.status_sink(StatusEvent(kind="banner", text="boot"))
        pump.log_sink(rec)
        pump.agent_sink(AgentEvent(kind="text_delta", text="hi"))

        app = _RecordingApp()
        pump.attach(app)

        kinds = [type(m).__name__ for m in app.messages]
        assert kinds == ["StatusMessage", "LogMessage", "AgentMessage"]

    def test_buffer_is_bounded(self) -> None:
        pump = UiPump()
        # Force a tiny buffer so the test runs fast.
        pump.BUFFER_LIMIT = 4
        for i in range(10):
            pump.agent_sink(AgentEvent(kind="text_delta", text=str(i)))
        assert pump.dropped_count == 6

        app = _RecordingApp()
        pump.attach(app)
        # The MOST recent events survive; the oldest are dropped.
        texts = [m.event.text for m in app.messages]
        assert texts == ["6", "7", "8", "9"]


class TestAttachedDispatch:
    def test_agent_event_routes_to_agent_message(self) -> None:
        pump = UiPump()
        app = _RecordingApp()
        pump.attach(app)
        pump.agent_sink(AgentEvent(kind="markdown", text="**hi**"))
        assert len(app.messages) == 1
        assert type(app.messages[0]).__name__ == "AgentMessage"
        assert app.messages[0].event.text == "**hi**"

    def test_log_record_routes_to_log_message(self) -> None:
        pump = UiPump()
        app = _RecordingApp()
        pump.attach(app)
        rec = logging.LogRecord(
            name="t", level=logging.WARNING, pathname="", lineno=0,
            msg="oops", args=(), exc_info=None,
        )
        pump.log_sink(rec)
        assert type(app.messages[0]).__name__ == "LogMessage"
        assert app.messages[0].record is rec

    def test_status_event_routes_to_status_message(self) -> None:
        pump = UiPump()
        app = _RecordingApp()
        pump.attach(app)
        pump.status_sink(StatusEvent(kind="ready", text="go"))
        assert type(app.messages[0]).__name__ == "StatusMessage"


class TestAppThreadSafety:
    """Design.md §2 regression: a producer firing from the App's own
    thread must NOT raise. The pump promises this by always going
    through ``post_message`` (never ``call_from_thread``)."""

    def test_post_from_app_loop_thread_does_not_raise(self) -> None:
        pump = UiPump()
        app = _RecordingApp()
        pump.attach(app)

        # Simulate a logger.info that fires synchronously from inside
        # the App's loop thread — the pump must not blow up.
        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="from-app-thread", args=(), exc_info=None,
        )
        pump.log_sink(rec)
        assert len(app.messages) == 1

    def test_concurrent_producers_do_not_lose_events(self) -> None:
        pump = UiPump()
        app = _RecordingApp()
        pump.attach(app)

        N = 200

        def fire(prefix: str) -> None:
            for i in range(N):
                pump.agent_sink(
                    AgentEvent(kind="text_delta", text=f"{prefix}{i}")
                )

        threads = [
            threading.Thread(target=fire, args=("a-",)),
            threading.Thread(target=fire, args=("b-",)),
            threading.Thread(target=fire, args=("c-",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(app.messages) == 3 * N

    def test_pump_swallows_app_post_message_failures(self) -> None:
        """A broken App must not crash a producer thread."""
        pump = UiPump()

        class _BrokenApp:
            def post_message(self, _msg: Any) -> None:
                raise RuntimeError("App is dead")

        pump.attach(_BrokenApp())
        # Must not raise — design.md §2 contract.
        pump.agent_sink(AgentEvent(kind="text_delta", text="hi"))


class TestDetach:
    def test_detach_drops_buffer_and_app(self) -> None:
        pump = UiPump()
        pump.agent_sink(AgentEvent(kind="text_delta", text="x"))
        assert pump.is_attached is False

        app = _RecordingApp()
        pump.attach(app)
        assert pump.is_attached is True

        pump.detach()
        assert pump.is_attached is False

        # Posts after detach buffer again, NOT to the previous app.
        pump.agent_sink(AgentEvent(kind="text_delta", text="y"))
        assert len(app.messages) == 1  # only the pre-detach flush

    def test_double_attach_replays_buffer_once(self) -> None:
        pump = UiPump()
        pump.agent_sink(AgentEvent(kind="text_delta", text="x"))

        app1 = _RecordingApp()
        pump.attach(app1)
        assert len(app1.messages) == 1

        app2 = _RecordingApp()
        pump.attach(app2)
        # Buffer was flushed by attach #1; attach #2 sees nothing
        # buffered — events fired *between* the two attaches go to
        # whichever app was bound at the time.
        assert len(app2.messages) == 0


@pytest.fixture(autouse=True)
def _ensure_messages_module_importable():
    """Force-load messages module so lazy import paths are exercised."""
    from pip_agent.tui import messages  # noqa: F401
    yield
