"""Tests for ``pip_agent.backends.codex_cli.mcp_bridge``."""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# config_gen tests
# ---------------------------------------------------------------------------

def test_pip_block():
    from pip_agent.backends.codex_cli.config_gen import _pip_block

    block = _pip_block()
    assert "[mcp_servers.pip]" in block
    assert "command = " in block
    assert "args = " in block
    assert "pip_agent.backends.codex_cli.mcp_bridge" in block


def test_ensure_codex_config_creates_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pip_agent.backends.codex_cli.config_gen import ensure_codex_config

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    result = ensure_codex_config()
    assert result.exists()
    content = result.read_text(encoding="utf-8")
    assert "[mcp_servers.pip]" in content
    assert "command = " in content


def test_ensure_codex_config_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pip_agent.backends.codex_cli.config_gen import ensure_codex_config

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    ensure_codex_config()
    ensure_codex_config()

    content = (fake_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert content.count("[mcp_servers.pip]") == 1


def test_ensure_codex_config_with_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pip_agent.backends.codex_cli.config_gen import ensure_codex_config

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    workdir = tmp_path / "workspace"
    ensure_codex_config(workdir=workdir)

    content = (fake_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "PIP_WORKDIR" in content
    assert str(workdir).replace("\\", "/") in content


def test_ensure_codex_config_preserves_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pip_agent.backends.codex_cli.config_gen import ensure_codex_config

    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir(parents=True)
    config = codex_dir / "config.toml"
    config.write_text('model_provider = "openai"\n', encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    ensure_codex_config()
    content = config.read_text(encoding="utf-8")
    assert 'model_provider = "openai"' in content
    assert "[mcp_servers.pip]" in content


# ---------------------------------------------------------------------------
# mcp_bridge tool collection
# ---------------------------------------------------------------------------

def test_collect_sdk_tools():
    """Verify we can collect tools without crashing."""
    from pip_agent.mcp_tools import McpContext

    ctx = McpContext()
    from pip_agent.backends.codex_cli.mcp_bridge import _collect_sdk_tools

    tools = _collect_sdk_tools(ctx)
    assert len(tools) > 0

    names = {t.name for t in tools}
    assert "memory_search" in names
    assert "memory_write" in names
    assert "cron_add" in names


def test_collect_sdk_tools_have_handlers():
    """Every tool must have a callable handler."""
    from pip_agent.mcp_tools import McpContext

    ctx = McpContext()
    from pip_agent.backends.codex_cli.mcp_bridge import _collect_sdk_tools

    tools = _collect_sdk_tools(ctx)
    for tool in tools:
        assert callable(tool.handler), f"Tool {tool.name} handler is not callable"


def test_collect_sdk_tools_have_schemas():
    """Every tool must have a dict-typed input_schema."""
    from pip_agent.mcp_tools import McpContext

    ctx = McpContext()
    from pip_agent.backends.codex_cli.mcp_bridge import _collect_sdk_tools

    tools = _collect_sdk_tools(ctx)
    for tool in tools:
        assert isinstance(tool.input_schema, dict), (
            f"Tool {tool.name} input_schema is {type(tool.input_schema)}, expected dict"
        )


# ---------------------------------------------------------------------------
# mcp_bridge._build_mcp_ctx
# ---------------------------------------------------------------------------

def test_build_mcp_ctx_returns_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from pip_agent.backends.codex_cli.mcp_bridge import _build_mcp_ctx

    monkeypatch.setenv("PIP_WORKDIR", str(tmp_path))
    ctx = _build_mcp_ctx()
    assert ctx.workdir == tmp_path
