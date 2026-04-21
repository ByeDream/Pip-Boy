"""Tests for :func:`pip_agent.agent_runner.run_query` prompt handling.

We can't exercise the real SDK subprocess here, but we can verify the
two code paths that Phase 7 introduced:

  * ``str`` prompt flows through unchanged (hot path must not regress).
  * ``list[dict]`` prompt is wrapped in the SDK's expected
    ``AsyncIterable[dict]`` envelope.

Both checks monkey-patch ``claude_agent_sdk.query`` so we can inspect
exactly what the runner hands off.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from typing import Any
from unittest.mock import patch

from pip_agent import agent_runner
from pip_agent.mcp_tools import McpContext


async def _collect_envelopes(prompt_or_iterable: Any) -> list[Any]:
    """Drain whatever the runner passed as ``prompt`` into a list so
    we can assert on it synchronously."""
    if isinstance(prompt_or_iterable, str):
        return [prompt_or_iterable]
    assert isinstance(prompt_or_iterable, AsyncIterable)
    out: list[Any] = []
    async for item in prompt_or_iterable:
        out.append(item)
    return out


def _run(coro):
    return asyncio.run(coro)


class _FakeQuery:
    """Drop-in replacement for ``claude_agent_sdk.query`` that records
    the prompt it saw and returns an empty result stream.

    We DON'T try to emulate the full SDK message lifecycle — the
    runner has its own tests elsewhere for ``ResultMessage`` /
    ``SystemMessage`` parsing. All we need here is to not raise and
    to expose what the runner handed in.
    """

    def __init__(self) -> None:
        self.captured_prompt: Any = None

    def __call__(self, *, prompt, options):  # noqa: D401
        self.captured_prompt = prompt

        async def _empty_stream():
            # Surface at least a ResultMessage so run_query's loop
            # terminates cleanly with num_turns=0.
            from claude_agent_sdk import ResultMessage
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=0,
                session_id="sess-fake",
                total_cost_usd=0.0,
                usage=None,
                result=None,
            )

        return _empty_stream()


class TestStringPromptPassthrough:
    def test_str_prompt_forwarded_verbatim(self, tmp_path):
        fake = _FakeQuery()
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query", fake):
            _run(agent_runner.run_query(
                prompt="hello world",
                mcp_ctx=ctx,
                stream_text=False,
            ))
        # String path must stay a string — wrapping it in an
        # AsyncIterable for no reason would burn an event-loop
        # round-trip per turn.
        assert fake.captured_prompt == "hello world"


class TestBlockListPromptWrapped:
    def test_block_list_wrapped_in_async_iterable(self, tmp_path):
        fake = _FakeQuery()
        ctx = McpContext(workdir=tmp_path)
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "look"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "AAAA",
                },
            },
        ]
        with patch.object(agent_runner, "query", fake):
            _run(agent_runner.run_query(
                prompt=blocks,
                mcp_ctx=ctx,
                stream_text=False,
            ))
        assert isinstance(fake.captured_prompt, AsyncIterable)
        envelopes = _run(_collect_envelopes(fake.captured_prompt))
        assert len(envelopes) == 1
        env = envelopes[0]
        assert env["type"] == "user"
        assert env["parent_tool_use_id"] is None
        # ``session_id`` in the envelope is always empty — actual
        # resumption is driven by ``options.resume``. Matching the
        # SDK's own string-path shape here keeps behaviour uniform.
        assert env["session_id"] == ""
        # Content passes through unchanged, preserving order.
        assert env["message"] == {"role": "user", "content": blocks}

    def test_empty_block_list_still_wraps(self, tmp_path):
        # An empty list would be nonsense input but shouldn't crash —
        # verify the wrapper still yields a well-shaped envelope with
        # empty content so error handling (if any) happens downstream
        # in the SDK, not inside our stream function.
        fake = _FakeQuery()
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query", fake):
            _run(agent_runner.run_query(
                prompt=[],
                mcp_ctx=ctx,
                stream_text=False,
            ))
        envelopes = _run(_collect_envelopes(fake.captured_prompt))
        assert envelopes[0]["message"]["content"] == []
