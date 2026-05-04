"""Tests for :func:`pip_agent.agent_runner.run_query` prompt handling
and streaming-message dispatch.

We can't exercise the real SDK subprocess here, but we can verify the
two prompt-shaping code paths that Phase 7 introduced:

  * ``str`` prompt flows through unchanged (hot path must not regress).
  * ``list[dict]`` prompt is wrapped in the SDK's expected
    ``AsyncIterable[dict]`` envelope.

And the three streaming-message handlers that populate ``QueryResult``:

  * ``AssistantMessage`` — text blocks stream to stdout, tool-use blocks
    emit an always-visible ``[tool: …]`` trace (UX contract, not debug).
  * ``SystemMessage(init)`` — captures ``session_id`` before any
    ``ResultMessage`` arrives so CC crashes mid-turn still resume.
  * ``ResultMessage`` — populates text / cost / turns / error and
    closes the streaming line so the "Done: …" log record doesn't
    glue onto the last ``TextBlock``.
  * ``ClaudeSDKError`` — surfaces as ``result.error`` without
    tearing the whole host down.

Monkey-patches ``claude_agent_sdk.query`` so we can drive arbitrary
message sequences without a subprocess.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

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
    the prompt + options it saw and returns an empty result stream.

    We DON'T try to emulate the full SDK message lifecycle — the
    runner has its own tests elsewhere for ``ResultMessage`` /
    ``SystemMessage`` parsing. All we need here is to not raise and
    to expose what the runner handed in.
    """

    def __init__(self) -> None:
        self.captured_prompt: Any = None
        self.captured_options: Any = None

    def __call__(self, *, prompt, options):  # noqa: D401
        self.captured_prompt = prompt
        self.captured_options = options

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

    def test_no_custom_tool_whitelist_is_passed(self, tmp_path):
        """H6 regression guard: the Host must not narrow CC's tool set.

        Options default ``allowed_tools=[]``; the SDK only emits the
        ``--allowedTools`` CLI flag when the list is truthy. If the
        whitelist ever sneaks back in, CC silently loses any tool
        added after the last time that list was edited. Assert that
        ``allowed_tools`` is either empty or absent on the options
        we hand the SDK. MCP tools (``mcp__pip__*``) are wired via
        ``mcp_servers`` and never need listing in ``allowed_tools``.
        """
        fake = _FakeQuery()
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query", fake):
            _run(agent_runner.run_query(
                prompt="ping",
                mcp_ctx=ctx,
                stream_text=False,
            ))
        allowed = getattr(fake.captured_options, "allowed_tools", [])
        assert not allowed, (
            "Host should not customise CC's tool whitelist; "
            "CC picks the default set when allowed_tools is empty."
        )
        # MCP wiring must still reach the subprocess — losing this would
        # silently break every ``mcp__pip__*`` tool.
        assert "pip" in fake.captured_options.mcp_servers

    def test_web_builtins_are_disallowed_so_pip_versions_own_namespace(
        self, tmp_path, monkeypatch,
    ):
        """Built-in ``WebFetch`` and ``WebSearch`` must both be removed
        from the model's option set so the agent uses the Pip-Boy
        implementations (``mcp__pip__web_fetch`` /
        ``mcp__pip__web_search`` in :mod:`pip_agent.web`) instead of
        the bundled beta-gated server-side tools — the corporate
        gateway rejects the experimental-betas header those require.
        """
        from pip_agent.config import settings
        monkeypatch.setattr(settings, "use_custom_web_tools", True)

        fake = _FakeQuery()
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query", fake):
            _run(agent_runner.run_query(
                prompt="ping",
                mcp_ctx=ctx,
                stream_text=False,
            ))
        disallowed = list(
            getattr(fake.captured_options, "disallowed_tools", []) or [],
        )
        assert "WebFetch" in disallowed
        assert "WebSearch" in disallowed
        # And the helper function matches what got wired through
        # — the seam other call sites import.
        assert "WebFetch" in agent_runner._builtin_disallowed_tools()
        assert "WebSearch" in agent_runner._builtin_disallowed_tools()

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


# ---------------------------------------------------------------------------
# Streaming-message dispatch
# ---------------------------------------------------------------------------


def _fake_query_yielding(messages: list[Any]):
    """Build a drop-in for ``claude_agent_sdk.query`` that streams the
    given message sequence. Unlike :class:`_FakeQuery` this doesn't
    inject its own ``ResultMessage`` — tests pass the full sequence so
    we can verify what happens without one too."""

    def _call(*, prompt, options):
        async def _stream():
            for m in messages:
                yield m
        return _stream()

    return _call


def _result(num_turns=1, session_id="sess-final", cost=0.0125,
            text="done", is_error=False):
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session_id,
        total_cost_usd=cost,
        usage=None,
        result=text,
    )


class TestAssistantMessageStreaming:
    def test_text_blocks_stream_to_stdout(self, tmp_path, capsys):
        messages = [
            AssistantMessage(
                model="claude-test",
                content=[TextBlock(text="Hello "), TextBlock(text="world")],
            ),
            _result(),
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            out = _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=True,
            ))
        captured = capsys.readouterr().out
        # Both text blocks must appear, in order, on stdout.
        assert "Hello " in captured and "world" in captured
        # ``ResultMessage`` drove the final state.
        assert out.num_turns == 1

    def test_text_blocks_suppressed_when_stream_text_false(
        self, tmp_path, capsys,
    ):
        messages = [
            AssistantMessage(
                model="claude-test",
                content=[TextBlock(text="SHOULD NOT APPEAR")],
            ),
            _result(),
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=False,
            ))
        assert "SHOULD NOT APPEAR" not in capsys.readouterr().out

    def test_tool_use_blocks_always_emit_trace(self, tmp_path, capsys):
        # Tool traces are a UX contract — they must fire even when
        # ``stream_text=False`` (cron / heartbeat context). A silent
        # 30-second tool-chain cannot be distinguished from a crash.
        messages = [
            AssistantMessage(
                model="claude-test",
                content=[ToolUseBlock(id="t1", name="Bash",
                                      input={"command": "ls"})],
            ),
            _result(),
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=False,
            ))
        captured = capsys.readouterr().out
        assert "[tool: Bash" in captured


class TestSystemMessageInit:
    def test_init_captures_session_id_before_result(self, tmp_path):
        # The ``SystemMessage(init)`` path is what lets Pip-Boy recover
        # a session id even when the turn crashes before producing a
        # ``ResultMessage``. Verify it writes to ``result.session_id``.
        messages = [
            SystemMessage(subtype="init",
                          data={"session_id": "sess-from-init"}),
            # Deliberately NO ResultMessage — simulates a mid-turn hang
            # whose stream ran out. The runner should still return with
            # the session id the init message carried.
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            out = _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=False,
            ))
        assert out.session_id == "sess-from-init"
        # ``num_turns`` stays at its dataclass default — we never got a
        # ``ResultMessage`` to update it, and that's the signal the
        # caller needs to treat this as a partial / crashed turn.
        assert out.num_turns == 0


class TestResultMessageFinalises:
    def test_result_populates_all_fields(self, tmp_path):
        messages = [
            _result(num_turns=3, session_id="sess-r",
                    cost=0.0420, text="final-answer"),
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            out = _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=False,
            ))
        assert out.text == "final-answer"
        assert out.session_id == "sess-r"
        assert out.num_turns == 3
        assert out.cost_usd == 0.0420
        # No ``is_error`` → ``error`` stays None.
        assert out.error is None

    def test_is_error_sets_error_field(self, tmp_path):
        messages = [
            _result(is_error=True, text="turn limit exceeded"),
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            out = _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=False,
            ))
        assert out.error == "turn limit exceeded"
        # ``text`` still reflects what the SDK sent — callers that
        # want the post-error text (e.g. the partial answer before a
        # safety filter kicked in) should not need to re-parse.
        assert out.text == "turn limit exceeded"

    def test_result_closes_streaming_line(self, tmp_path, capsys):
        # Regression test for the "Done: …" log record gluing onto the
        # last ``TextBlock``. After ``ResultMessage`` processes, the
        # runner prints a bare newline iff a streaming line was open.
        messages = [
            AssistantMessage(
                model="claude-test",
                content=[TextBlock(text="streamed")],
            ),
            _result(),
        ]
        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query",
                          _fake_query_yielding(messages)):
            _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=True,
            ))
        out = capsys.readouterr().out
        # The newline after ``streamed`` is the close-the-line flush —
        # without it, the log record would glue to the same line.
        assert out.endswith("\n")


class TestStderrBuffer:
    """``_StderrBuffer`` is the stderr-capture sink we hand to
    ``ClaudeAgentOptions.stderr``. Without it, ``ProcessError`` arrives
    with the SDK's literal placeholder ``"Check stderr output for
    details"`` and the real gateway error (e.g. ``API Error: 400 ...``)
    is silently dropped. These tests lock the buffer's accumulation,
    bounding, and reset semantics."""

    def test_appends_lines_in_order(self):
        buf = agent_runner._StderrBuffer()
        buf("first line")
        buf("second line")
        assert buf.text() == "first line\nsecond line"

    def test_reset_clears_lines_and_chars(self):
        buf = agent_runner._StderrBuffer()
        buf("noise from a prior turn")
        buf.reset()
        assert buf.text() == ""
        # After reset the budget is fully restored.
        buf("fresh")
        assert buf.text() == "fresh"

    def test_line_cap_drops_overflow(self, monkeypatch):
        # Set a tiny cap so the test stays cheap and obvious.
        monkeypatch.setattr(agent_runner._StderrBuffer, "_MAX_LINES", 3)
        buf = agent_runner._StderrBuffer()
        for i in range(10):
            buf(f"line-{i}")
        # First 3 kept, rest silently dropped — the API-error JSON is
        # always near the start so this bound is safe.
        assert buf.text().splitlines() == ["line-0", "line-1", "line-2"]

    def test_char_cap_truncates_then_drops(self, monkeypatch):
        monkeypatch.setattr(agent_runner._StderrBuffer, "_MAX_TOTAL_CHARS", 12)
        buf = agent_runner._StderrBuffer()
        buf("12345")            # 5 chars
        buf("67890abcdef")      # would push past 12 → truncated to 7
        buf("ignored")           # budget exhausted
        # The second line is truncated to fit the remaining budget; the
        # third line is dropped entirely. Joining adds 1 separator so
        # final text length is 5 + 1 + 7 = 13.
        assert buf.text() == "12345\n67890ab"


class TestEnrichWithStderr:
    """Verify that captured stderr replaces the SDK's placeholder
    instead of being concatenated next to it. The proxy's real error
    body MUST be in the user-facing error text — without this fix,
    ``is_model_invalid_error`` only ever sees the placeholder string."""

    def test_replaces_placeholder_when_present(self):
        # ``ProcessError`` formats the placeholder as a multi-line
        # tail. Captured stderr should land in its place.
        err = (
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )
        captured = (
            'API Error: 400 {"type":"error","error":'
            '{"type":"invalid_request_error","message":"模型不存在"}}'
        )
        out = agent_runner._enrich_with_stderr(err, captured)
        assert "Check stderr output for details" not in out
        assert "模型不存在" in out
        assert "Command failed with exit code 1" in out

    def test_appends_when_no_placeholder(self):
        out = agent_runner._enrich_with_stderr(
            "transport closed",
            "warning: noisy line\nfatal: something",
        )
        assert out.startswith("transport closed")
        assert "fatal: something" in out

    def test_passthrough_when_capture_empty(self):
        # No captured stderr → keep the original error verbatim so we
        # don't pollute logs with empty " | stderr: " trailers.
        original = "Command failed with exit code 1"
        assert agent_runner._enrich_with_stderr(original, "") == original


class TestClaudeSDKError:
    def test_sdk_error_becomes_result_error_without_raising(self, tmp_path):
        def _raising_query(*, prompt, options):
            async def _stream():
                # Yield one message then blow up — verifies the error
                # handler catches mid-stream crashes, not just "query
                # raised before yielding anything".
                yield AssistantMessage(
                    model="claude-test",
                    content=[TextBlock(text="partial")],
                )
                raise ClaudeSDKError("transport closed")
            return _stream()

        ctx = McpContext(workdir=tmp_path)
        with patch.object(agent_runner, "query", _raising_query):
            out = _run(agent_runner.run_query(
                prompt="hi", mcp_ctx=ctx, stream_text=False,
            ))
        assert out.error is not None
        assert "transport closed" in out.error
        # A crash mid-stream before ``ResultMessage`` means turns stay
        # 0 — that's correct and lets the caller distinguish "turn
        # completed with error status" from "turn crashed".
        assert out.num_turns == 0
