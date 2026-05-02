"""Tests for backend-aware dispatch in AgentHost and host_commands.

Verifies that:
- AgentHost stores a backend instance from get_backend()
- /status includes the active backend name
- /help includes a Backend section
- /plugin gates unsupported operations for codex_cli
- /plugin marketplace routes to the correct adapter per backend
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from pip_agent.backends.base import Capability
from pip_agent.channels import InboundMessage
from pip_agent.host_commands import (
    CommandContext,
    CommandResult,
    dispatch_command,
)
from pip_agent.routing import AgentRegistry, BindingTable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cli_inbound(text: str) -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_id="cli-user",
        channel="cli",
        peer_id="cli-user",
    )


def _build_ctx(text: str, tmp_path: Path) -> CommandContext:
    workspace = tmp_path / "workspace"
    (workspace / ".pip").mkdir(parents=True, exist_ok=True)
    registry = AgentRegistry(workspace)
    return CommandContext(
        inbound=_cli_inbound(text),
        registry=registry,
        bindings=BindingTable(),
        bindings_path=workspace / ".pip" / "bindings.json",
    )


# ---------------------------------------------------------------------------
# AgentHost._backend init
# ---------------------------------------------------------------------------


class TestAgentHostBackendInit:
    def test_default_backend_is_claude_code(self):
        with mock.patch("pip_agent.agent_host.get_backend") as mock_get:
            mock_backend = mock.MagicMock()
            mock_backend.name = "claude_code"
            mock_get.return_value = mock_backend

            from pip_agent.agent_host import AgentHost
            from pip_agent.channels import ChannelManager

            registry = mock.MagicMock(spec=AgentRegistry)
            binding_table = mock.MagicMock(spec=BindingTable)
            channel_mgr = mock.MagicMock(spec=ChannelManager)

            with mock.patch("pip_agent.agent_host._load_sessions", return_value={}):
                host = AgentHost(
                    registry=registry,
                    binding_table=binding_table,
                    channel_mgr=channel_mgr,
                )

            assert host._backend is mock_backend
            mock_get.assert_called_once()


# ---------------------------------------------------------------------------
# /status backend display
# ---------------------------------------------------------------------------


class TestStatusBackendDisplay:
    def test_status_shows_claude_code(self, tmp_path: Path):
        ctx = _build_ctx("/status", tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "Backend: claude_code" in (result.response or "")

    def test_status_shows_codex_cli(self, tmp_path: Path):
        with mock.patch("pip_agent.config.settings") as mock_settings:
            mock_settings.backend = "codex_cli"
            ctx = _build_ctx("/status", tmp_path)
            result = dispatch_command(ctx)
            assert result.handled
            assert "Backend: codex_cli" in (result.response or "")


# ---------------------------------------------------------------------------
# /help backend section
# ---------------------------------------------------------------------------


class TestHelpBackendSection:
    def test_help_shows_backend_section(self, tmp_path: Path):
        ctx = _build_ctx("/help", tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "## Backend" in (result.response or "")
        assert "claude_code" in (result.response or "")

    def test_help_shows_codex_backend(self, tmp_path: Path):
        with mock.patch("pip_agent.config.settings") as mock_settings:
            mock_settings.backend = "codex_cli"
            ctx = _build_ctx("/help", tmp_path)
            result = dispatch_command(ctx)
            assert result.handled
            assert "codex_cli" in (result.response or "")


# ---------------------------------------------------------------------------
# /plugin backend gating
# ---------------------------------------------------------------------------


class TestPluginBackendGating:
    @pytest.mark.parametrize("sub", [
        "list", "search", "install", "uninstall", "enable", "disable",
    ])
    def test_codex_blocks_unsupported_plugin_ops(self, sub, tmp_path: Path):
        with mock.patch("pip_agent.config.settings") as mock_settings:
            mock_settings.backend = "codex_cli"
            ctx = _build_ctx(f"/plugin {sub} test-arg", tmp_path)
            result = dispatch_command(ctx)
            assert result.handled
            assert "not available" in (result.response or "")
            assert "Codex" in (result.response or "")

    @pytest.mark.parametrize("sub", [
        "list", "search", "install", "uninstall", "enable", "disable",
    ])
    def test_claude_allows_all_plugin_ops(self, sub, tmp_path: Path):
        """Claude Code backend should not block any /plugin operations
        at the gating level (the underlying plugin CLI may still fail,
        but the gate itself should pass through)."""
        ctx = _build_ctx(f"/plugin {sub}", tmp_path)
        result = dispatch_command(ctx)
        assert result.handled
        assert "not available" not in (result.response or "")


# ---------------------------------------------------------------------------
# /plugin marketplace backend routing
# ---------------------------------------------------------------------------


class TestPluginMarketplaceRouting:
    def test_claude_marketplace_list_uses_claude_plugins(self, tmp_path: Path):
        with mock.patch("pip_agent.plugins.run_sync") as mock_run:
            mock_run.return_value = []
            ctx = _build_ctx("/plugin marketplace list", tmp_path)
            result = dispatch_command(ctx)
            assert result.handled
            mock_run.assert_called_once()

    def test_codex_marketplace_add_routes_to_codex_adapter(self, tmp_path: Path):
        with mock.patch("pip_agent.config.settings") as mock_settings:
            mock_settings.backend = "codex_cli"
            with mock.patch("pip_agent.plugins.run_sync") as mock_run:
                mock_run.return_value = "Added."
                ctx = _build_ctx(
                    "/plugin marketplace add https://example.com/repo",
                    tmp_path,
                )
                result = dispatch_command(ctx)
                assert result.handled
                mock_run.assert_called_once()

    def test_codex_marketplace_list_blocked(self, tmp_path: Path):
        with mock.patch("pip_agent.config.settings") as mock_settings:
            mock_settings.backend = "codex_cli"
            ctx = _build_ctx("/plugin marketplace list", tmp_path)
            result = dispatch_command(ctx)
            assert result.handled
            assert "not available" in (result.response or "")


# ---------------------------------------------------------------------------
# Capability enum completeness
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    def test_claude_code_supports_all_capabilities(self):
        from pip_agent.backends.claude_code import ClaudeCodeBackend

        backend = ClaudeCodeBackend()
        for cap in Capability:
            assert backend.supports(cap), f"ClaudeCodeBackend missing {cap}"

    def test_codex_cli_does_not_support_pre_compact_hook(self):
        from pip_agent.backends.codex_cli import CodexBackend

        backend = CodexBackend()
        assert not backend.supports(Capability.PRE_COMPACT_HOOK)
        assert not backend.supports(Capability.SETTING_SOURCES_THREE_TIER)
        assert not backend.supports(Capability.INTERACTIVE_MODALS)

    def test_codex_cli_supports_core_capabilities(self):
        from pip_agent.backends.codex_cli import CodexBackend

        backend = CodexBackend()
        assert backend.supports(Capability.PERSISTENT_STREAMING)
        assert backend.supports(Capability.PLUGIN_MARKETPLACE)
        assert backend.supports(Capability.SLASH_PASSTHROUGH)
        assert backend.supports(Capability.SESSION_RESUME)
