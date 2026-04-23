"""Tests for the ``reflect`` MCP tool handler.

The reflect tool has three distinct non-error outcomes that callers need to be
able to tell apart:

1. No ANTHROPIC credentials configured → skipped with an actionable message.
2. LLM ran but produced no observations → explicit "LLM found nothing" text.
3. No new transcript content since the last run → explicit "nothing new" text.

Previously all three collapsed to "Reflection complete: no new observations
found.", which made diagnosing misconfigurations painful.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from pip_agent.mcp_tools import McpContext, _memory_tools
from pip_agent.memory import MemoryStore


def _run(coro):
    return asyncio.run(coro)


def _get_reflect(ctx: McpContext):
    for t in _memory_tools(ctx):
        if t.name == "reflect":
            return t.handler
    raise AssertionError("reflect tool not found")


def _call(handler, args=None) -> str:
    result = _run(handler(args or {}))
    blocks = result.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


@pytest.fixture
def ctx(tmp_path: Path) -> McpContext:
    pip_dir = tmp_path / "agents" / "pip-boy"
    pip_dir.mkdir(parents=True, exist_ok=True)
    ms = MemoryStore(
        agent_dir=pip_dir,
        workspace_pip_dir=pip_dir.parent,
        agent_id="pip-boy",
    )
    return McpContext(memory_store=ms, session_id="sess-test")


class TestReflectPreconditions:
    def test_no_memory_store_returns_error(self, tmp_path):
        c = McpContext(memory_store=None, session_id="x")
        handler = _get_reflect(c)
        result = _run(handler({}))
        assert result.get("is_error") is True

    def test_no_session_id_is_skipped_with_explanation(self, tmp_path):
        pip_dir = tmp_path / "agents" / "pip-boy"
        pip_dir.mkdir(parents=True, exist_ok=True)
        ms = MemoryStore(
            agent_dir=pip_dir,
            workspace_pip_dir=pip_dir.parent,
            agent_id="pip-boy",
        )
        c = McpContext(memory_store=ms, session_id="")
        text = _call(_get_reflect(c))
        assert "Reflection skipped" in text
        assert "session_id" in text

    def test_transcript_not_found_is_skipped_with_explanation(self, ctx):
        with patch(
            "pip_agent.memory.transcript_source.locate_session_jsonl",
            return_value=None,
        ):
            text = _call(_get_reflect(ctx))
        assert "Reflection skipped" in text and "not found" in text


class TestReflectCredentialGap:
    """Regression for the silent "no-credentials == empty-result" collapse."""

    def test_missing_credentials_reports_skip_not_empty(self, ctx, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        with (
            patch(
                "pip_agent.memory.transcript_source.locate_session_jsonl",
                return_value=jsonl,
            ),
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=None,
            ),
        ):
            text = _call(_get_reflect(ctx))

        assert "Reflection skipped" in text
        assert "ANTHROPIC" in text
        # The cursor must NOT have been advanced — next call can retry.
        state = ctx.memory_store.load_state()
        assert "last_reflect_jsonl_offset" not in state


class TestReflectWithLLM:
    def test_llm_produces_observations_writes_them(self, ctx, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        def fake_reflect(path, *, start_offset, agent_id, model, client):
            assert client is not None
            return 99, [{
                "ts": 1.0,
                "text": "user prefers concise output",
                "category": "preference",
                "source": "auto",
            }]

        with (
            patch(
                "pip_agent.memory.transcript_source.locate_session_jsonl",
                return_value=jsonl,
            ),
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.reflect.reflect_from_jsonl",
                side_effect=fake_reflect,
            ),
        ):
            text = _call(_get_reflect(ctx))

        assert "extracted 1 observation" in text
        state = ctx.memory_store.load_state()
        assert state["last_reflect_jsonl_offset"]["sess-test"] == 99

    def test_llm_produced_nothing_but_cursor_advanced(self, ctx, tmp_path):
        # LLM ran cleanly, saw the delta, decided the turns were unremarkable.
        # Message must make that distinct from "no new content".
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        with (
            patch(
                "pip_agent.memory.transcript_source.locate_session_jsonl",
                return_value=jsonl,
            ),
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.reflect.reflect_from_jsonl",
                return_value=(50, []),
            ),
        ):
            text = _call(_get_reflect(ctx))

        assert "no new observations" in text.lower()
        # Cursor advanced.
        state = ctx.memory_store.load_state()
        assert state["last_reflect_jsonl_offset"]["sess-test"] == 50

    def test_no_new_content_since_last_run(self, ctx, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        # Pre-seed the cursor so reflect_from_jsonl would be told start==end.
        ctx.memory_store.save_state({
            "last_reflect_jsonl_offset": {"sess-test": 100},
        })

        with (
            patch(
                "pip_agent.memory.transcript_source.locate_session_jsonl",
                return_value=jsonl,
            ),
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.reflect.reflect_from_jsonl",
                return_value=(100, []),
            ),
        ):
            text = _call(_get_reflect(ctx))

        assert "no new transcript" in text.lower()

    def test_llm_crash_surfaces_as_error(self, ctx, tmp_path):
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        def boom(*a, **kw):
            raise RuntimeError("network down")

        with (
            patch(
                "pip_agent.memory.transcript_source.locate_session_jsonl",
                return_value=jsonl,
            ),
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=object(),
            ),
            patch(
                "pip_agent.memory.reflect.reflect_from_jsonl",
                side_effect=boom,
            ),
        ):
            result = _run(_get_reflect(ctx)({}))

        assert result.get("is_error") is True
        text = "".join(b.get("text", "") for b in result.get("content", []))
        assert "network down" in text


# Credential-resolution / client-building tests live in
# ``tests/test_anthropic_client.py`` — the single source of truth for the
# proxy rule also has the single source of truth for its tests.
