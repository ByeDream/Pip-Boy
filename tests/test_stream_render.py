"""Regression tests for ``WecomStreamRenderer``.

Covers the stream rendering contract: text_delta and thinking_delta
accumulate into the body, tool_use increments the footer counter and
triggers an immediate flush (no marker injection into the body since
commit 8c2b777), and finalize produces the closing snapshot with the
stats footer.
"""

from __future__ import annotations

from typing import Any

import pytest

from pip_agent.channels.stream_render import WecomStreamRenderer


class _FakeChannel:
    """Minimal Channel stub — records every update / finish call."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, str, str]] = []
        self.finishes: list[tuple[str, str, str]] = []

    def update_stream(
        self, to: str, handle: str, text: str, **kwargs: Any,
    ) -> bool:
        self.updates.append((to, handle, text))
        return True

    def finish_stream(
        self, to: str, handle: str, text: str, **kwargs: Any,
    ) -> bool:
        self.finishes.append((to, handle, text))
        return True


def _make(channel: _FakeChannel) -> WecomStreamRenderer:
    return WecomStreamRenderer(
        channel=channel,  # type: ignore[arg-type]
        to="peer1",
        handle="h1",
        inbound_id="msg1",
    )


@pytest.mark.asyncio
async def test_text_and_tool_use_both_land_in_final_body() -> None:
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("text_delta", text="before tool ")
    await r.handle_event(
        "tool_use",
        name="Write",
        input={"file_path": "/tmp/x.md", "content": "hello"},
    )
    await r.handle_event("text_delta", text=" after tool")
    await r.handle_event(
        "finalize",
        num_turns=1,
        cost_usd=0.001,
        usage={"input_tokens": 100},
        elapsed_s=0.5,
    )
    assert ch.finishes, "finalize must push at least one snapshot"
    final_body = ch.finishes[-1][2]
    assert "before tool" in final_body
    assert "after tool" in final_body
    assert "1 tools" in final_body


@pytest.mark.asyncio
async def test_tool_use_force_flushes_past_rate_limit() -> None:
    """A text_delta that just flushed would normally block the next
    update for 300 ms. The tool_use handler must bypass that throttle
    so the update appears immediately."""
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("text_delta", text="hello")
    updates_after_text = len(ch.updates)
    await r.handle_event("tool_use", name="Bash", input={"command": "ls"})
    assert len(ch.updates) > updates_after_text


@pytest.mark.asyncio
async def test_tool_use_without_args_still_flushes() -> None:
    """Unknown tool triggers a flush even without recognised args."""
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("tool_use", name="SomeMcpTool", input={"foo": 1})
    await r.handle_event(
        "finalize",
        num_turns=1,
        cost_usd=None,
        usage={},
        elapsed_s=0.1,
    )
    assert "1 tools" in ch.finishes[-1][2]


@pytest.mark.asyncio
async def test_tool_use_still_increments_footer_count() -> None:
    """The footer tool counter tracks every tool_use event."""
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("tool_use", name="Read", input={"file_path": "/a"})
    await r.handle_event("tool_use", name="Grep", input={"pattern": "x"})
    await r.handle_event(
        "finalize",
        num_turns=1,
        cost_usd=None,
        usage={},
        elapsed_s=0.0,
    )
    footer = ch.finishes[-1][2]
    assert "2 tools" in footer


@pytest.mark.asyncio
async def test_tool_use_missing_input_does_not_crash() -> None:
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("tool_use", name="Read")
    await r.handle_event("tool_use", name="Read", input=None)
    await r.handle_event("tool_use", name="Read", input="not-a-dict")
    await r.handle_event(
        "finalize",
        num_turns=1,
        cost_usd=None,
        usage={},
        elapsed_s=0.0,
    )
    assert "3 tools" in ch.finishes[-1][2]


@pytest.mark.asyncio
async def test_multiple_tool_boundaries_preserve_narration_order() -> None:
    """Narration chunks interleaved with tool_use events should appear
    in the final body in the order they were emitted."""
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("text_delta", text="Now update TCSS for both themes")
    await r.handle_event(
        "tool_use", name="Edit", input={"file_path": "a.tcss"},
    )
    await r.handle_event("text_delta", text="Now update on_agent_message")
    await r.handle_event(
        "tool_use", name="Edit", input={"file_path": "app.py"},
    )
    await r.handle_event("text_delta", text="Now add split-routing tests")
    await r.handle_event(
        "finalize",
        num_turns=1,
        cost_usd=None,
        usage={},
        elapsed_s=0.0,
    )
    body = ch.finishes[-1][2]
    assert body.index("Now update TCSS") < body.index("Now update on_agent_message")
    assert body.index("Now update on_agent_message") < body.index("Now add split-routing")
    assert "2 tools" in body
