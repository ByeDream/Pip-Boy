"""Cross-backend test matrix (Phase 7).

Parametrizes key behavioral tests across both backends to ensure
contract §1.1 (Claude Code tests green) and §1.2 (consistent
experience) are maintained.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pip_agent.backends import get_backend
from pip_agent.backends.base import (
    BackendError,
    Capability,
    QueryResult,
    StaleSessionError,
    StreamEventCallback,
    StreamingSessionProtocol,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(params=["claude_code", "codex_cli"])
def backend(request: pytest.FixtureRequest) -> Any:
    return get_backend(request.param)


@pytest.fixture(params=["claude_code", "codex_cli"])
def backend_name(request: pytest.FixtureRequest) -> str:
    return request.param


# ---------------------------------------------------------------------------
# run_query contract
# ---------------------------------------------------------------------------


class TestRunQueryContract:
    """Both backends must accept the same kwargs and return QueryResult."""

    @pytest.mark.asyncio
    async def test_run_query_returns_query_result(self, backend_name: str):
        be = get_backend(backend_name)
        mock_result = QueryResult(text="hello", session_id="s1")

        if backend_name == "claude_code":
            with patch(
                "pip_agent.agent_runner.run_query",
                new_callable=AsyncMock,
                return_value=mock_result,
            ):
                result = await be.run_query(
                    "test prompt",
                    mcp_ctx=MagicMock(),
                    model_chain=["t0"],
                    session_id=None,
                    system_prompt_append="",
                    cwd="/tmp",
                    on_stream_event=None,
                )
        else:
            with patch(
                "pip_agent.backends.codex_cli.runner.run_query",
                new_callable=AsyncMock,
                return_value=mock_result,
            ):
                result = await be.run_query(
                    "test prompt",
                    mcp_ctx=MagicMock(),
                    session_id=None,
                    system_prompt_append="",
                    cwd="/tmp",
                    on_stream_event=None,
                )

        assert isinstance(result, QueryResult)
        assert result.text == "hello"
        assert result.session_id == "s1"

    @pytest.mark.asyncio
    async def test_run_query_accepts_string_prompt(self, backend_name: str):
        be = get_backend(backend_name)
        mock_result = QueryResult(text="ok")

        target = (
            "pip_agent.agent_runner.run_query"
            if backend_name == "claude_code"
            else "pip_agent.backends.codex_cli.runner.run_query"
        )
        with patch(target, new_callable=AsyncMock, return_value=mock_result):
            result = await be.run_query(
                "a string prompt",
                mcp_ctx=MagicMock(),
            )
        assert result.text == "ok"

    @pytest.mark.asyncio
    async def test_run_query_accepts_blocks_prompt(self, backend_name: str):
        be = get_backend(backend_name)
        mock_result = QueryResult(text="ok")
        blocks = [{"type": "text", "text": "hello"}]

        target = (
            "pip_agent.agent_runner.run_query"
            if backend_name == "claude_code"
            else "pip_agent.backends.codex_cli.runner.run_query"
        )
        with patch(target, new_callable=AsyncMock, return_value=mock_result):
            result = await be.run_query(
                blocks,
                mcp_ctx=MagicMock(),
            )
        assert result.text == "ok"


# ---------------------------------------------------------------------------
# health_check contract
# ---------------------------------------------------------------------------


class TestHealthCheckContract:
    @pytest.mark.asyncio
    async def test_returns_tuple(self, backend: Any):
        ok, msg = await backend.health_check()
        assert isinstance(ok, bool)
        assert isinstance(msg, str)

    @pytest.mark.asyncio
    async def test_healthy_when_sdk_installed(self, backend: Any):
        ok, msg = await backend.health_check()
        assert ok is True
        assert len(msg) > 0


# ---------------------------------------------------------------------------
# supports() contract
# ---------------------------------------------------------------------------


class TestSupportsContract:
    def test_returns_bool_for_every_capability(self, backend: Any):
        for cap in Capability:
            assert isinstance(backend.supports(cap), bool)

    def test_both_support_persistent_streaming(self, backend: Any):
        assert backend.supports(Capability.PERSISTENT_STREAMING) is True

    def test_both_support_plugin_marketplace(self, backend: Any):
        assert backend.supports(Capability.PLUGIN_MARKETPLACE) is True

    def test_both_support_session_resume(self, backend: Any):
        assert backend.supports(Capability.SESSION_RESUME) is True


# ---------------------------------------------------------------------------
# name property
# ---------------------------------------------------------------------------


class TestNameProperty:
    def test_name_matches_factory_key(self, backend_name: str):
        be = get_backend(backend_name)
        assert be.name == backend_name


# ---------------------------------------------------------------------------
# error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    def test_stale_session_error_is_catchable_as_runtime(self):
        with pytest.raises(RuntimeError):
            raise StaleSessionError("gone")

    def test_backend_error_is_catchable_as_runtime(self):
        with pytest.raises(RuntimeError):
            raise BackendError("generic")

    def test_stale_session_is_backend_error(self):
        assert issubclass(StaleSessionError, BackendError)

    @pytest.mark.asyncio
    async def test_run_query_propagates_exceptions(self, backend_name: str):
        be = get_backend(backend_name)
        target = (
            "pip_agent.agent_runner.run_query"
            if backend_name == "claude_code"
            else "pip_agent.backends.codex_cli.runner.run_query"
        )
        with patch(
            target,
            new_callable=AsyncMock,
            side_effect=BackendError("test error"),
        ):
            with pytest.raises(BackendError, match="test error"):
                await be.run_query("prompt", mcp_ctx=MagicMock())


# ---------------------------------------------------------------------------
# streaming session protocol
# ---------------------------------------------------------------------------


class TestStreamingSessionProtocol:
    def test_protocol_defines_required_attributes(self):
        required = {"session_key", "session_id", "last_used_ns", "created_ns", "turn_count"}
        annotations = getattr(StreamingSessionProtocol, "__annotations__", {})
        assert required.issubset(set(annotations.keys()))

    def test_protocol_defines_required_methods(self):
        required = {"connect", "close", "run_turn"}
        protocol_methods = set()
        for name in dir(StreamingSessionProtocol):
            if not name.startswith("_"):
                protocol_methods.add(name)
        assert required.issubset(protocol_methods)


# ---------------------------------------------------------------------------
# QueryResult cross-backend serialization
# ---------------------------------------------------------------------------


class TestQueryResultCrossBackend:
    def test_default_num_turns_is_zero(self):
        r = QueryResult()
        assert r.num_turns == 0

    def test_error_field_independent_of_text(self):
        r = QueryResult(text="some text", error="but also an error")
        assert r.text == "some text"
        assert r.error == "but also an error"

    def test_cost_usd_optional(self):
        r = QueryResult(text="hello")
        assert r.cost_usd is None
