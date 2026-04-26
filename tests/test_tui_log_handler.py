"""``TuiLogHandler`` forwards every record to the pump's ``log_sink``."""

from __future__ import annotations

import logging

from pip_agent.tui.log_handler import TuiLogHandler
from pip_agent.tui.pump import UiPump


class _RecordingApp:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def post_message(self, msg: object) -> None:
        self.messages.append(msg)


def test_handler_routes_to_pump_log_sink() -> None:
    pump = UiPump()
    app = _RecordingApp()
    pump.attach(app)

    handler = TuiLogHandler(pump, level=logging.INFO)
    rec = logging.LogRecord(
        name="t", level=logging.WARNING, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    handler.emit(rec)
    assert any(getattr(m, "record", None) is rec for m in app.messages)


def test_handler_respects_level_via_logger() -> None:
    """Level filtering applies via the standard ``Logger.callHandlers``
    path. The handler stores the level so loggers can pre-filter; the
    handler itself does not re-check (matches stdlib semantics)."""
    pump = UiPump()
    app = _RecordingApp()
    pump.attach(app)

    handler = TuiLogHandler(pump, level=logging.WARNING)
    logger = logging.getLogger("pip_agent.tests.tui_log_handler")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    logger.debug("hidden")
    logger.warning("visible")

    payloads = [
        m.record.getMessage() for m in app.messages
        if hasattr(m, "record")
    ]
    assert "visible" in payloads
    assert "hidden" not in payloads


def test_handler_does_not_raise_when_pump_detached() -> None:
    pump = UiPump()  # not attached
    handler = TuiLogHandler(pump, level=logging.INFO)
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="boot-time-log", args=(), exc_info=None,
    )
    handler.emit(rec)
    # Buffered into the pump pending attach.
    assert pump.dropped_count == 0
    app = _RecordingApp()
    pump.attach(app)
    assert any(getattr(m, "record", None) is rec for m in app.messages)
