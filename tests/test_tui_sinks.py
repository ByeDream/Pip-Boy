"""Locked-contract tests for the TUI sink protocol.

The events flowing into the TUI are part of Pip-Boy's host contract,
so the whitelist of ``kind`` values, the field shape, and the
Null-sink semantics get explicit regression coverage. A theme author
or sink consumer can read these tests to learn what they're allowed
to assume.
"""

from __future__ import annotations

import logging

import pytest

from pip_agent.tui.sinks import (
    AGENT_EVENT_KINDS,
    STATUS_EVENT_KINDS,
    AgentEvent,
    AgentSink,
    LogSink,
    NullAgentSink,
    NullLogSink,
    NullStatusSink,
    StatusEvent,
    StatusSink,
)


class TestAgentEventWhitelist:
    """``AgentEvent.kind`` is locked to a small, named set."""

    def test_locked_set_matches_design_doc(self) -> None:
        # If you grow this set you MUST also grow the design doc + the
        # snapshot baseline; the explicit literal here is the gate.
        assert AGENT_EVENT_KINDS == frozenset(
            {
                "user_input",
                "thinking_delta",
                "text_delta",
                "tool_use",
                "markdown",
                "plain",
                "finalize",
                "error",
            }
        )

    @pytest.mark.parametrize("kind", sorted(AGENT_EVENT_KINDS))
    def test_each_whitelisted_kind_constructs(self, kind: str) -> None:
        AgentEvent(kind=kind)

    def test_unknown_kind_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="not in locked whitelist"):
            AgentEvent(kind="thinking_block")  # almost-correct typo

    def test_event_is_frozen(self) -> None:
        ev = AgentEvent(kind="text_delta", text="hi")
        with pytest.raises((AttributeError, TypeError)):
            ev.text = "mutated"  # type: ignore[misc]


class TestStatusEventWhitelist:
    def test_locked_set_matches_design_doc(self) -> None:
        assert STATUS_EVENT_KINDS == frozenset(
            {
                "banner",
                "channel_ready",
                "channel_lost",
                "scheduler",
                "shutdown",
                "ready",
                "tool_wait",
            }
        )

    @pytest.mark.parametrize("kind", sorted(STATUS_EVENT_KINDS))
    def test_each_whitelisted_kind_constructs(self, kind: str) -> None:
        StatusEvent(kind=kind)

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="not in locked whitelist"):
            StatusEvent(kind="bannar")


class TestNullSinksConformToProtocol:
    """Null sinks are the line-mode default; they must satisfy the
    Protocol classes the TUI App and producers type against."""

    def test_null_agent_sink_is_agent_sink(self) -> None:
        sink: AgentSink = NullAgentSink()
        sink(AgentEvent(kind="text_delta", text="hi"))
        assert isinstance(sink, AgentSink)

    def test_null_log_sink_is_log_sink(self) -> None:
        sink: LogSink = NullLogSink()
        sink(
            logging.LogRecord(
                name="t", level=logging.INFO, pathname="", lineno=0,
                msg="m", args=(), exc_info=None,
            )
        )
        assert isinstance(sink, LogSink)

    def test_null_status_sink_is_status_sink(self) -> None:
        sink: StatusSink = NullStatusSink()
        sink(StatusEvent(kind="banner", text="hi"))
        assert isinstance(sink, StatusSink)
