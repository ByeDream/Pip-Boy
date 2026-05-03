"""Tests for the Codex reflect trigger and transcript capture infrastructure.

Covers:
- CodexStreamingSession transcript JSONL capture
- locate_session_jsonl finding Codex transcripts
- _maybe_codex_reflect turn-count gating
- run_host ensure_codex_config call at boot
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# CodexStreamingSession transcript capture
# ---------------------------------------------------------------------------


class TestTranscriptCapture:
    def _make_session(self, tmp_path: Path) -> "CodexStreamingSession":
        from pip_agent.backends.codex_cli.streaming import CodexStreamingSession

        sess = CodexStreamingSession(
            session_key="test-key",
            cwd=str(tmp_path),
            system_prompt_append="",
        )
        sess.session_id = "test-session-id"
        return sess

    def test_append_transcript_writes_jsonl(self, tmp_path: Path):
        sess = self._make_session(tmp_path)
        jsonl_path = tmp_path / "transcript.jsonl"
        sess._transcript_path = jsonl_path

        sess._append_transcript("user", "hello")
        sess._append_transcript("assistant", "world")

        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"role": "user", "content": "hello"}
        assert json.loads(lines[1]) == {"role": "assistant", "content": "world"}

    def test_append_transcript_skips_empty(self, tmp_path: Path):
        sess = self._make_session(tmp_path)
        jsonl_path = tmp_path / "transcript.jsonl"
        sess._transcript_path = jsonl_path

        sess._append_transcript("user", "  ")
        assert not jsonl_path.exists()

    def test_append_transcript_noop_when_no_path(self, tmp_path: Path):
        sess = self._make_session(tmp_path)
        sess._transcript_path = None
        sess._append_transcript("user", "hello")

    def test_init_transcript_path_creates_dir(self, tmp_path: Path):
        sess = self._make_session(tmp_path)
        with patch("pip_agent.backends.codex_cli.streaming.Path") as _:
            with patch("pip_agent.config.WORKDIR", tmp_path):
                path = sess._init_transcript_path()
                assert path is not None
                assert path.parent.name == "codex_sessions"
                assert path.name == "test-session-id.jsonl"
                assert path.parent.is_dir()

    def test_init_transcript_path_none_without_session_id(self, tmp_path: Path):
        sess = self._make_session(tmp_path)
        sess.session_id = ""
        assert sess._init_transcript_path() is None

    @pytest.mark.asyncio
    async def test_transcript_captures_during_run_turn(self, tmp_path: Path):
        """run_turn writes user + assistant to transcript."""
        sess = self._make_session(tmp_path)
        jsonl_path = tmp_path / "transcript.jsonl"
        sess._transcript_path = jsonl_path
        sess._thread = MagicMock()
        sess._closed = False

        fake_event = MagicMock()
        type(fake_event).__name__ = "ItemCompletedNotificationModel"
        item = MagicMock()
        item.type = "agent_message"
        item.text = "hello back"
        params = MagicMock()
        params.item.root = item
        fake_event.params = params

        sess._thread.run.return_value = [fake_event]

        result = await sess.run_turn("hi there", sender_id="s", peer_id="p")

        assert result.text == "hello back"
        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["role"] == "user"
        assert json.loads(lines[1])["role"] == "assistant"


# ---------------------------------------------------------------------------
# locate_session_jsonl finding Codex transcripts
# ---------------------------------------------------------------------------


class TestLocateCodexTranscript:
    def test_finds_codex_session_jsonl(self, tmp_path: Path):
        from pip_agent.memory.transcript_source import locate_session_jsonl

        codex_dir = tmp_path / ".pip" / "codex_sessions"
        codex_dir.mkdir(parents=True)
        sid = "codex-session-123"
        jsonl = codex_dir / f"{sid}.jsonl"
        jsonl.write_text('{"role":"user","content":"hi"}\n')

        with patch("pip_agent.config.WORKDIR", tmp_path):
            result = locate_session_jsonl(
                sid,
                projects_root=tmp_path / "nonexistent",
            )
        assert result is not None
        assert result == jsonl

    def test_returns_none_when_no_match(self, tmp_path: Path):
        from pip_agent.memory.transcript_source import locate_session_jsonl

        with patch("pip_agent.config.WORKDIR", tmp_path):
            result = locate_session_jsonl(
                "nonexistent-id",
                projects_root=tmp_path / "nonexistent",
            )
        assert result is None


# ---------------------------------------------------------------------------
# _maybe_codex_reflect turn-count gating
# ---------------------------------------------------------------------------


class TestMaybeCodexReflect:
    def _make_host_with_session(
        self, tmp_path: Path, turn_count: int, *, has_transcript: bool = True,
    ):
        host = MagicMock()

        session = MagicMock()
        session.turn_count = turn_count
        session.session_id = "test-sid-1234567890"
        if has_transcript:
            tp = tmp_path / "test.jsonl"
            tp.write_text('{"role":"user","content":"hi"}\n')
            session.transcript_path = tp
        else:
            session.transcript_path = None
        host._streaming_sessions = {"key": session}

        mcp_ctx = MagicMock()
        mcp_ctx.memory_store = MagicMock()

        return host, mcp_ctx

    @pytest.mark.asyncio
    async def test_reflect_fires_at_threshold(self, tmp_path: Path):
        from pip_agent.agent_host import AgentHost

        host, mcp_ctx = self._make_host_with_session(tmp_path, turn_count=10)

        with (
            patch.object(AgentHost, "__init__", return_value=None),
            patch(
                "pip_agent.memory.reflect.reflect_and_persist",
                return_value=(0, 100, 3),
            ) as mock_reflect,
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=MagicMock(),
            ),
        ):
            real_host = AgentHost.__new__(AgentHost)
            real_host._streaming_sessions = host._streaming_sessions
            await real_host._maybe_codex_reflect(
                session_key="key", mcp_ctx=mcp_ctx,
            )
            mock_reflect.assert_called_once()

    @pytest.mark.asyncio
    async def test_reflect_skips_below_threshold(self, tmp_path: Path):
        from pip_agent.agent_host import AgentHost

        host, mcp_ctx = self._make_host_with_session(tmp_path, turn_count=5)

        with (
            patch.object(AgentHost, "__init__", return_value=None),
            patch(
                "pip_agent.memory.reflect.reflect_and_persist",
            ) as mock_reflect,
        ):
            real_host = AgentHost.__new__(AgentHost)
            real_host._streaming_sessions = host._streaming_sessions
            await real_host._maybe_codex_reflect(
                session_key="key", mcp_ctx=mcp_ctx,
            )
            mock_reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflect_skips_without_transcript(self, tmp_path: Path):
        from pip_agent.agent_host import AgentHost

        host, mcp_ctx = self._make_host_with_session(
            tmp_path, turn_count=10, has_transcript=False,
        )

        with (
            patch.object(AgentHost, "__init__", return_value=None),
            patch(
                "pip_agent.memory.reflect.reflect_and_persist",
            ) as mock_reflect,
        ):
            real_host = AgentHost.__new__(AgentHost)
            real_host._streaming_sessions = host._streaming_sessions
            await real_host._maybe_codex_reflect(
                session_key="key", mcp_ctx=mcp_ctx,
            )
            mock_reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflect_skips_no_memory_store(self, tmp_path: Path):
        from pip_agent.agent_host import AgentHost

        host, mcp_ctx = self._make_host_with_session(tmp_path, turn_count=10)
        mcp_ctx.memory_store = None

        with (
            patch.object(AgentHost, "__init__", return_value=None),
            patch(
                "pip_agent.memory.reflect.reflect_and_persist",
            ) as mock_reflect,
        ):
            real_host = AgentHost.__new__(AgentHost)
            real_host._streaming_sessions = host._streaming_sessions
            await real_host._maybe_codex_reflect(
                session_key="key", mcp_ctx=mcp_ctx,
            )
            mock_reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflect_fires_at_multiples(self, tmp_path: Path):
        from pip_agent.agent_host import AgentHost

        host, mcp_ctx = self._make_host_with_session(tmp_path, turn_count=20)

        with (
            patch.object(AgentHost, "__init__", return_value=None),
            patch(
                "pip_agent.memory.reflect.reflect_and_persist",
                return_value=(100, 200, 2),
            ) as mock_reflect,
            patch(
                "pip_agent.anthropic_client.build_anthropic_client",
                return_value=MagicMock(),
            ),
        ):
            real_host = AgentHost.__new__(AgentHost)
            real_host._streaming_sessions = host._streaming_sessions
            await real_host._maybe_codex_reflect(
                session_key="key", mcp_ctx=mcp_ctx,
            )
            mock_reflect.assert_called_once()


# ---------------------------------------------------------------------------
# ensure_codex_config at boot
# ---------------------------------------------------------------------------


class TestEnsureCodexConfigBoot:
    def test_ensure_codex_config_code_in_run_host(self):
        """run_host contains the ensure_codex_config call for codex backend."""
        import inspect
        from pip_agent.agent_host import run_host

        source = inspect.getsource(run_host)
        assert "ensure_codex_config" in source
        assert 'settings.backend == "codex_cli"' in source

    def test_ensure_codex_config_function_callable(self):
        """ensure_codex_config is importable and callable."""
        from pip_agent.backends.codex_cli.config_gen import ensure_codex_config
        assert callable(ensure_codex_config)


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------


class TestTokenUsageTracking:
    def test_cumulative_tokens_initialized(self, tmp_path: Path):
        from pip_agent.backends.codex_cli.streaming import CodexStreamingSession

        sess = CodexStreamingSession(
            session_key="key", cwd=str(tmp_path), system_prompt_append="",
        )
        assert sess.cumulative_tokens == 0

    @pytest.mark.asyncio
    async def test_token_usage_from_state(self, tmp_path: Path):
        from pip_agent.backends.codex_cli.streaming import CodexStreamingSession

        sess = CodexStreamingSession(
            session_key="key", cwd=str(tmp_path), system_prompt_append="",
        )
        sess.session_id = "sid"
        sess._thread = MagicMock()
        sess._closed = False
        sess._transcript_path = None

        token_event = MagicMock()
        type(token_event).__name__ = "ThreadTokenUsageUpdatedNotificationModel"
        total = MagicMock()
        total.inputTokens = 100
        total.outputTokens = 50
        total.totalTokens = 150
        token_usage = MagicMock()
        token_usage.total = total
        token_event.params.tokenUsage = token_usage

        done_event = MagicMock()
        type(done_event).__name__ = "TurnCompletedNotificationModel"
        done_event.params = MagicMock()

        sess._thread.run.return_value = [token_event, done_event]

        await sess.run_turn("test", sender_id="s", peer_id="p")
        assert sess.cumulative_tokens == 150
