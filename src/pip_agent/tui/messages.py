"""Textual ``Message`` subclasses that the UI pump posts onto the App.

This module is the single place where ``textual.message.Message`` is
imported. Keeping the import here (rather than in :mod:`pip_agent.tui.pump`
or :mod:`pip_agent.tui.sinks`) lets non-TUI code paths — line mode boots,
``--version``, ``pip-boy doctor`` early plumbing — avoid the textual
import altogether.

The wrappers are deliberately thin: each carries the locked event
dataclass / log record from :mod:`pip_agent.tui.sinks` and nothing else.
The App declares ``on_<message_class_name>`` handlers to render them.

Why subclasses rather than a single generic ``UiMessage`` with a kind
field: Textual's message dispatch routes by class, so giving each sink
its own subclass means handlers don't have to switch internally and the
type checker can verify each handler takes the right payload.
"""

from __future__ import annotations

import logging

from textual.message import Message

from pip_agent.tui.sinks import AgentEvent, StatusEvent

__all__ = ["AgentMessage", "LogMessage", "StatusMessage"]


class AgentMessage(Message):
    """Carries an :class:`AgentEvent` to the agent-pane handler."""

    def __init__(self, event: AgentEvent) -> None:
        self.event = event
        super().__init__()


class LogMessage(Message):
    """Carries a stdlib :class:`logging.LogRecord` to the app-log handler."""

    def __init__(self, record: logging.LogRecord) -> None:
        self.record = record
        super().__init__()


class StatusMessage(Message):
    """Carries a :class:`StatusEvent` to the status-bar handler."""

    def __init__(self, event: StatusEvent) -> None:
        self.event = event
        super().__init__()
