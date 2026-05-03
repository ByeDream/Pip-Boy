"""Tests for ``pip_agent.backends.codex_cli.event_translator``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from pip_agent.backends.codex_cli.event_translator import translate_event


# ---------------------------------------------------------------------------
# Helpers — lightweight fakes for SDK notification objects
# ---------------------------------------------------------------------------

class _FakeRoot:
    """Mimics a discriminated-union .root accessor."""

    def __init__(self, value: str) -> None:
        self.root = value


class _FakeItem:
    """Minimal ThreadItem mock."""

    def __init__(self, *, type: str, **kwargs: Any) -> None:  # noqa: A002
        self.type = _FakeRoot(type)
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeItemWrapper:
    def __init__(self, item: _FakeItem) -> None:
        self.root = item


class _FakeParams:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_event(type_name: str, params: _FakeParams) -> Any:
    """Build a fake notification with ``type(event).__name__ == type_name``."""
    cls = type(type_name, (), {"params": params})
    return cls()


# ---------------------------------------------------------------------------
# text_delta
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_delta():
    cb = AsyncMock()
    ev = _make_event(
        "ItemAgentMessageDeltaNotification",
        _FakeParams(delta="Hello "),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("text_delta", text="Hello ")


@pytest.mark.asyncio
async def test_text_delta_empty_ignored():
    cb = AsyncMock()
    ev = _make_event(
        "ItemAgentMessageDeltaNotification",
        _FakeParams(delta=""),
    )
    await translate_event(ev, cb, state={})
    cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# thinking_delta
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thinking_delta():
    cb = AsyncMock()
    ev = _make_event(
        "ItemReasoningTextDeltaNotification",
        _FakeParams(delta="Hmm..."),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("thinking_delta", text="Hmm...")


@pytest.mark.asyncio
async def test_reasoning_summary_delta():
    cb = AsyncMock()
    ev = _make_event(
        "ItemReasoningSummaryTextDeltaNotification",
        _FakeParams(delta="Summary chunk"),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("thinking_delta", text="Summary chunk")


# ---------------------------------------------------------------------------
# tool_use — command_execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_use_command():
    cb = AsyncMock()
    item = _FakeItem(type="command_execution", id="cmd-1", command="ls -la")
    ev = _make_event(
        "ItemStartedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with(
        "tool_use",
        id="cmd-1",
        name="Bash",
        input={"command": "ls -la"},
    )


# ---------------------------------------------------------------------------
# tool_use — file_change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_use_file_change():
    cb = AsyncMock()
    change = type("Change", (), {"kind": _FakeRoot("add"), "path": "foo.txt"})()
    item = _FakeItem(type="file_change", id="fc-1", changes=[change])
    ev = _make_event(
        "ItemStartedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with(
        "tool_use",
        id="fc-1",
        name="Write",
        input={"path": "foo.txt", "kind": "add"},
    )


# ---------------------------------------------------------------------------
# tool_use — mcp_tool_call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_use_mcp():
    cb = AsyncMock()
    item = _FakeItem(
        type="mcp_tool_call",
        id="mcp-1",
        server="my-server",
        tool="search",
        arguments={"query": "test"},
    )
    ev = _make_event(
        "ItemStartedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with(
        "tool_use",
        id="mcp-1",
        name="search",
        input={"query": "test"},
    )


# ---------------------------------------------------------------------------
# tool_use — web_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_use_web_search():
    cb = AsyncMock()
    item = _FakeItem(type="web_search", id="ws-1", query="python async")
    ev = _make_event(
        "ItemStartedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with(
        "tool_use",
        id="ws-1",
        name="WebSearch",
        input={"query": "python async"},
    )


# ---------------------------------------------------------------------------
# tool_result — command_execution (success / error)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_result_command_success():
    cb = AsyncMock()
    item = _FakeItem(
        type="command_execution",
        id="cmd-1",
        exitCode=0,
        aggregatedOutput="output",
        status=_FakeRoot("completed"),
    )
    ev = _make_event(
        "ItemCompletedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("tool_result", tool_use_id="cmd-1", is_error=False)


@pytest.mark.asyncio
async def test_tool_result_command_error():
    cb = AsyncMock()
    item = _FakeItem(
        type="command_execution",
        id="cmd-2",
        exitCode=1,
        aggregatedOutput="error",
        status=_FakeRoot("completed"),
    )
    ev = _make_event(
        "ItemCompletedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("tool_result", tool_use_id="cmd-2", is_error=True)


# ---------------------------------------------------------------------------
# tool_result — file_change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_result_file_change():
    cb = AsyncMock()
    item = _FakeItem(
        type="file_change",
        id="fc-1",
        changes=[],
        status=_FakeRoot("completed"),
    )
    ev = _make_event(
        "ItemCompletedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("tool_result", tool_use_id="fc-1", is_error=False)


# ---------------------------------------------------------------------------
# tool_result — mcp_tool_call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_result_mcp_success():
    cb = AsyncMock()
    item = _FakeItem(type="mcp_tool_call", id="mcp-1", error=None)
    ev = _make_event(
        "ItemCompletedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("tool_result", tool_use_id="mcp-1", is_error=False)


@pytest.mark.asyncio
async def test_tool_result_mcp_error():
    cb = AsyncMock()
    item = _FakeItem(type="mcp_tool_call", id="mcp-2", error="connection failed")
    ev = _make_event(
        "ItemCompletedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state={})
    cb.assert_awaited_once_with("tool_result", tool_use_id="mcp-2", is_error=True)


# ---------------------------------------------------------------------------
# agent_message completed → final_text capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_message_captures_final_text():
    cb = AsyncMock()
    state: dict[str, Any] = {}
    item = _FakeItem(type="agent_message", id="msg-1", text="Final answer")
    ev = _make_event(
        "ItemCompletedNotificationModel",
        _FakeParams(item=_FakeItemWrapper(item)),
    )
    await translate_event(ev, cb, state=state)
    assert state["final_text"] == "Final answer"
    cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# TurnPlanUpdated → tool_use + tool_result (TodoWrite)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_updated():
    cb = AsyncMock()
    step = type("Step", (), {"title": "Setup env", "status": "pending"})()
    plan = type("Plan", (), {"steps": [step]})()
    ev = _make_event(
        "TurnPlanUpdatedNotificationModel",
        _FakeParams(plan=plan),
    )
    await translate_event(ev, cb, state={})
    assert cb.await_count == 2
    cb.assert_any_await(
        "tool_use",
        id="plan-update",
        name="TodoWrite",
        input={"plan": [{"title": "Setup env", "status": "pending"}]},
    )
    cb.assert_any_await("tool_result", tool_use_id="plan-update", is_error=False)


# ---------------------------------------------------------------------------
# token usage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_usage_tracked():
    cb = AsyncMock()
    state: dict[str, Any] = {}
    total = type("TokenUsageBreakdown", (), {
        "inputTokens": 100,
        "outputTokens": 50,
        "totalTokens": 150,
        "cachedInputTokens": 0,
        "reasoningOutputTokens": 0,
    })()
    token_usage = type("ThreadTokenUsage", (), {
        "total": total,
        "last": None,
        "modelContextWindow": None,
    })()
    ev = _make_event(
        "ThreadTokenUsageUpdatedNotificationModel",
        _FakeParams(tokenUsage=token_usage),
    )
    await translate_event(ev, cb, state=state)
    assert state["token_usage"]["input_tokens"] == 100
    assert state["token_usage"]["output_tokens"] == 50
    assert state["token_usage"]["total_tokens"] == 150
    cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize():
    cb = AsyncMock()
    state: dict[str, Any] = {"final_text": "All done.", "elapsed_s": 1.5}
    ev = _make_event(
        "TurnCompletedNotificationModel",
        _FakeParams(turn=None),
    )
    await translate_event(ev, cb, state=state)
    cb.assert_awaited_once()
    call_args = cb.call_args
    assert call_args[0][0] == "finalize"
    assert call_args[1]["final_text"] == "All done."


# ---------------------------------------------------------------------------
# Unhandled events should not raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unhandled_event_ignored():
    cb = AsyncMock()
    ev = _make_event("SomeUnknownNotification", _FakeParams())
    await translate_event(ev, cb, state={})
    cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# TurnStarted — silent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_started_silent():
    cb = AsyncMock()
    ev = _make_event("TurnStartedNotificationModel", _FakeParams())
    await translate_event(ev, cb, state={})
    cb.assert_not_awaited()
