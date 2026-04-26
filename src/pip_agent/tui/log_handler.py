"""``logging.Handler`` that forwards records into a :class:`UiPump`.

Installed by ``run_host`` only when TUI mode is active. Replaces the
stdout :class:`logging.StreamHandler` that
:func:`pip_agent.__main__._configure_logging` installs at boot — left
in place, that handler would scribble escape codes onto the TUI
canvas and corrupt rendering.

The handler is intentionally trivial: it does no formatting (the App
formats via :class:`pip_agent.tui.app.PipBoyTuiApp._format_log_record`)
and never blocks. ``emit`` defers to the pump, which itself defers to
``app.post_message`` — the only API path documented as safe from
arbitrary threads.
"""

from __future__ import annotations

import logging

from pip_agent.tui.pump import UiPump

__all__ = ["TuiLogHandler"]


class TuiLogHandler(logging.Handler):
    """Pushes :class:`logging.LogRecord` objects through ``UiPump.log_sink``."""

    def __init__(self, pump: UiPump, *, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self._pump = pump

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._pump.log_sink(record)
        except Exception:  # pragma: no cover — pump itself swallows
            self.handleError(record)
