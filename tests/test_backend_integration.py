"""Integration tests for the dual-backend architecture.

Verifies that the backend abstraction layer works correctly as a whole:
factory resolution, protocol conformance, capability gating, error mapping.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pip_agent.backends import get_backend
from pip_agent.backends.base import (
    AgentBackend,
    AuthenticationError,
    BackendError,
    BackendTimeoutError,
    BackendUnavailableError,
    Capability,
    ModelInvalidError,
    QueryResult,
    StaleSessionError,
    StreamingSessionProtocol,
)


# ---------------------------------------------------------------------------
# Factory smoke tests
# ---------------------------------------------------------------------------

class TestGetBackend:
    def test_claude_code_default(self):
        be = get_backend()
        assert be.name == "claude_code"

    def test_claude_code_explicit(self):
        be = get_backend("claude_code")
        assert be.name == "claude_code"

    def test_codex_cli(self):
        be = get_backend("codex_cli")
        assert be.name == "codex_cli"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend("nonexistent")

    def test_settings_override(self):
        with patch("pip_agent.config.settings") as mock_settings:
            mock_settings.backend = "codex_cli"
            be = get_backend()
            assert be.name == "codex_cli"


# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_claude_code_all_capabilities(self):
        be = get_backend("claude_code")
        assert be.supports(Capability.PRE_COMPACT_HOOK)
        assert be.supports(Capability.SETTING_SOURCES_THREE_TIER)
        assert be.supports(Capability.PERSISTENT_STREAMING)
        assert be.supports(Capability.PLUGIN_MARKETPLACE)
        assert be.supports(Capability.SLASH_PASSTHROUGH)
        assert be.supports(Capability.INTERACTIVE_MODALS)
        assert be.supports(Capability.SESSION_RESUME)

    def test_codex_supported(self):
        be = get_backend("codex_cli")
        assert be.supports(Capability.PERSISTENT_STREAMING)
        assert be.supports(Capability.PLUGIN_MARKETPLACE)
        assert be.supports(Capability.SLASH_PASSTHROUGH)
        assert be.supports(Capability.SESSION_RESUME)

    def test_codex_not_supported(self):
        be = get_backend("codex_cli")
        assert not be.supports(Capability.PRE_COMPACT_HOOK)
        assert not be.supports(Capability.SETTING_SOURCES_THREE_TIER)
        assert not be.supports(Capability.INTERACTIVE_MODALS)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:
    def test_backend_error_is_runtime_error(self):
        assert issubclass(BackendError, RuntimeError)

    def test_stale_session_error(self):
        err = StaleSessionError("session gone")
        assert isinstance(err, BackendError)
        assert isinstance(err, RuntimeError)
        assert "session gone" in str(err)

    def test_model_invalid_error(self):
        err = ModelInvalidError("bad model")
        assert isinstance(err, BackendError)

    def test_auth_error(self):
        err = AuthenticationError("expired key")
        assert isinstance(err, BackendError)

    def test_unavailable_error(self):
        err = BackendUnavailableError("not installed")
        assert isinstance(err, BackendError)

    def test_timeout_error(self):
        err = BackendTimeoutError("took too long")
        assert isinstance(err, BackendError)

    def test_catch_as_runtime_error(self):
        """Existing 'except RuntimeError' catches must still work."""
        with pytest.raises(RuntimeError):
            raise StaleSessionError("gone")


# ---------------------------------------------------------------------------
# QueryResult dataclass
# ---------------------------------------------------------------------------

class TestQueryResult:
    def test_defaults(self):
        r = QueryResult()
        assert r.text is None
        assert r.session_id is None
        assert r.error is None
        assert r.cost_usd is None
        assert r.num_turns == 0

    def test_populated(self):
        r = QueryResult(
            text="hello",
            session_id="abc-123",
            cost_usd=0.05,
            num_turns=3,
        )
        assert r.text == "hello"
        assert r.session_id == "abc-123"
        assert r.cost_usd == 0.05
        assert r.num_turns == 3


# ---------------------------------------------------------------------------
# Capability enum completeness
# ---------------------------------------------------------------------------

class TestCapabilityEnum:
    def test_all_seven_defined(self):
        names = {c.name for c in Capability}
        expected = {
            "PRE_COMPACT_HOOK",
            "SETTING_SOURCES_THREE_TIER",
            "PERSISTENT_STREAMING",
            "PLUGIN_MARKETPLACE",
            "SLASH_PASSTHROUGH",
            "INTERACTIVE_MODALS",
            "SESSION_RESUME",
        }
        assert names == expected

    def test_enum_values_unique(self):
        values = [c.value for c in Capability]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

class TestHealthChecks:
    @pytest.mark.asyncio
    async def test_claude_code_health(self):
        be = get_backend("claude_code")
        ok, msg = await be.health_check()
        assert ok is True

    @pytest.mark.asyncio
    async def test_codex_health(self):
        be = get_backend("codex_cli")
        ok, msg = await be.health_check()
        assert ok is True
        assert "codex-python" in msg


# ---------------------------------------------------------------------------
# models.is_model_invalid_error with Codex types
# ---------------------------------------------------------------------------

class TestModelErrorDetection:
    def test_codex_model_invalid(self):
        from pip_agent.models import is_model_invalid_error

        class ModelInvalidErrorFake(Exception):
            pass

        err = ModelInvalidErrorFake("model 'gpt-5' not found")
        assert is_model_invalid_error(err) is True

    def test_codex_auth_not_model(self):
        from pip_agent.models import is_model_invalid_error

        class CodexAuthError(Exception):
            pass

        err = CodexAuthError("API key invalid")
        assert is_model_invalid_error(err) is False

    def test_codex_rate_limit_not_model(self):
        from pip_agent.models import is_model_invalid_error

        class RateLimitError(Exception):
            pass

        err = RateLimitError("too many requests")
        assert is_model_invalid_error(err) is False


# ---------------------------------------------------------------------------
# Backend protocol conformance check
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    """Verify both backends expose the same interface."""

    @pytest.fixture(params=["claude_code", "codex_cli"])
    def backend(self, request: pytest.FixtureRequest) -> Any:
        return get_backend(request.param)

    def test_has_name(self, backend: Any):
        assert isinstance(backend.name, str)
        assert backend.name in ("claude_code", "codex_cli")

    def test_has_run_query(self, backend: Any):
        assert hasattr(backend, "run_query")
        assert callable(backend.run_query)

    def test_has_open_streaming_session(self, backend: Any):
        assert hasattr(backend, "open_streaming_session")
        assert callable(backend.open_streaming_session)

    def test_has_supports(self, backend: Any):
        assert hasattr(backend, "supports")
        assert callable(backend.supports)

    def test_has_health_check(self, backend: Any):
        assert hasattr(backend, "health_check")
        assert callable(backend.health_check)

    def test_supports_returns_bool(self, backend: Any):
        for cap in Capability:
            result = backend.supports(cap)
            assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# config.Settings.backend field
# ---------------------------------------------------------------------------

class TestSettingsBackend:
    def test_default_is_claude_code(self):
        from pip_agent.config import Settings

        s = Settings()
        assert s.backend == "claude_code"

    def test_codex_cli_value(self):
        from pip_agent.config import Settings

        s = Settings(backend="codex_cli")
        assert s.backend == "codex_cli"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("BACKEND", "codex_cli")
        from pip_agent.config import Settings

        s = Settings()
        assert s.backend == "codex_cli"
