"""Tests for ``pip_agent.backends.codex_cli`` backend wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pip_agent.backends.base import Capability


# ---------------------------------------------------------------------------
# get_backend() factory
# ---------------------------------------------------------------------------

def test_get_backend_codex():
    from pip_agent.backends import get_backend
    from pip_agent.backends.codex_cli import CodexBackend

    be = get_backend("codex_cli")
    assert isinstance(be, CodexBackend)
    assert be.name == "codex_cli"


def test_get_backend_claude_code():
    from pip_agent.backends import get_backend
    from pip_agent.backends.claude_code import ClaudeCodeBackend

    be = get_backend("claude_code")
    assert isinstance(be, ClaudeCodeBackend)
    assert be.name == "claude_code"


def test_get_backend_unknown():
    from pip_agent.backends import get_backend

    with pytest.raises(ValueError, match="Unknown backend"):
        get_backend("nonexistent")


# ---------------------------------------------------------------------------
# CodexBackend capabilities
# ---------------------------------------------------------------------------

def test_codex_capabilities():
    from pip_agent.backends.codex_cli import CodexBackend

    be = CodexBackend()
    assert be.supports(Capability.PERSISTENT_STREAMING)
    assert be.supports(Capability.SESSION_RESUME)
    assert not be.supports(Capability.PRE_COMPACT_HOOK)
    assert not be.supports(Capability.SETTING_SOURCES_THREE_TIER)


# ---------------------------------------------------------------------------
# CodexBackend health_check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_health_check_installed():
    from pip_agent.backends.codex_cli import CodexBackend

    be = CodexBackend()
    ok, msg = await be.health_check()
    assert ok is True
    assert "codex-python" in msg


# ---------------------------------------------------------------------------
# CodexBackend.run_query delegates to runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_run_query_delegates():
    from pip_agent.backends.codex_cli import CodexBackend
    from pip_agent.backends.base import QueryResult

    fake_result = QueryResult(text="hello from codex")

    with patch(
        "pip_agent.backends.codex_cli.runner.run_query",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_run:
        be = CodexBackend()
        result = await be.run_query(
            "test prompt",
            mcp_ctx=None,
            cwd="/tmp",
        )
        assert result.text == "hello from codex"
        mock_run.assert_awaited_once()


# ---------------------------------------------------------------------------
# CodexBackend.open_streaming_session delegates to streaming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_open_streaming_session_delegates():
    from pip_agent.backends.codex_cli import CodexBackend

    with patch(
        "pip_agent.backends.codex_cli.streaming.CodexStreamingSession",
    ) as mock_cls:
        mock_session = MagicMock()
        mock_session.connect = AsyncMock()
        mock_cls.return_value = mock_session

        be = CodexBackend()
        result = await be.open_streaming_session(
            session_key="test-key",
            mcp_ctx=None,
            model_chain=["gpt-4"],
            cwd="/tmp",
            system_prompt_append="",
        )
        assert result is mock_session
        mock_session.connect.assert_awaited_once()


# ---------------------------------------------------------------------------
# settings.backend field exists
# ---------------------------------------------------------------------------

def test_settings_backend_field():
    from pip_agent.config import Settings

    assert "backend" in Settings.model_fields
    s = Settings(backend="codex_cli")
    assert s.backend == "codex_cli"
