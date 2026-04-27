"""Regression tests for ``WecomStreamRenderer``.

Covers the tool_use-boundary visibility fix: before, text_delta chunks
bracketing a tool call got concatenated into one opaque blob; now a
one-line ``▸ <tool> <args>`` marker is injected and a force-flush
breaks the 300ms throttle so the bubble updates immediately instead
of appearing frozen during a slow tool call.
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
async def test_tool_use_injects_marker_into_body() -> None:
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
    # Final snapshot should contain both narration chunks AND the
    # marker with its formatted args.
    assert ch.finishes, "finalize must push at least one snapshot"
    final_body = ch.finishes[-1][2]
    assert "before tool" in final_body
    assert "after tool" in final_body
    assert "▸ Write" in final_body
    assert "/tmp/x.md" in final_body  # args surfaced via format_tool_summary


@pytest.mark.asyncio
async def test_tool_use_force_flushes_past_rate_limit() -> None:
    """A text_delta that just flushed would normally block the next
    update for 300 ms. The tool_use handler must bypass that throttle
    so the marker appears immediately instead of waiting on the next
    delta (which may never arrive if the tool blocks for seconds)."""
    ch = _FakeChannel()
    r = _make(ch)
    await r.handle_event("text_delta", text="hello")
    updates_after_text = len(ch.updates)
    await r.handle_event("tool_use", name="Bash", input={"command": "ls"})
    # The tool_use call MUST have triggered an immediate flush,
    # even though <300 ms has passed since the text_delta one.
    assert len(ch.updates) > updates_after_text
    assert "▸ Bash" in ch.updates[-1][2]
    assert "ls" in ch.updates[-1][2]


@pytest.mark.asyncio
async def test_tool_use_without_args_renders_bare_marker() -> None:
    """Unknown tool without format_tool_summary support → '▸ Name' only."""
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
    assert "▸ SomeMcpTool" in ch.finishes[-1][2]


@pytest.mark.asyncio
async def test_tool_use_still_increments_footer_count() -> None:
    """The marker-injection change must NOT break the existing footer
    tool counter — both behaviours are additive."""
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
    await r.handle_event("tool_use", name="Read")  # no kwargs.input
    await r.handle_event("tool_use", name="Read", input=None)
    await r.handle_event("tool_use", name="Read", input="not-a-dict")
    await r.handle_event(
        "finalize",
        num_turns=1,
        cost_usd=None,
        usage={},
        elapsed_s=0.0,
    )
    # Three bare markers all landed, no exceptions.
    body = ch.finishes[-1][2]
    assert body.count("▸ Read") == 3


@pytest.mark.asyncio
async def test_multiple_tool_boundaries_stay_visually_separated() -> None:
    """The interleaving of narration + tool markers should preserve
    boundaries — the concatenated body must not collapse back into
    'chunkAchunkB' with no visible break."""
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
    # The three narration chunks are broken up by visible tool markers.
    assert body.index("Now update TCSS for both themes") < body.index("▸ Edit")
    assert body.index("▸ Edit") < body.index("Now update on_agent_message")
    # There is at least one ▸ marker between every pair of narration chunks.
    assert body.count("▸ Edit") == 2
