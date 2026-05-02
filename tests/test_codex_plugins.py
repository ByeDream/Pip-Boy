"""Tests for ``pip_agent.backends.codex_cli.plugins``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pip_agent.backends.codex_cli.plugins import (
    CodexPluginsCLIError,
    CodexPluginsCLINotFound,
    _bundled_cli,
    health_check,
)


# ---------------------------------------------------------------------------
# _bundled_cli resolution
# ---------------------------------------------------------------------------

def test_bundled_cli_finds_codex():
    """Should find the bundled codex binary since codex-python is installed."""
    cli = _bundled_cli()
    assert cli.exists()
    assert "codex" in cli.name.lower()


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_check_ok():
    ok, msg = await health_check()
    assert ok is True
    assert "Codex CLI found" in msg


@pytest.mark.asyncio
async def test_health_check_missing():
    with patch(
        "pip_agent.backends.codex_cli.plugins._bundled_cli",
        side_effect=CodexPluginsCLINotFound("not found"),
    ):
        ok, msg = await health_check()
        assert ok is False
        assert "not found" in msg


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

def test_cli_error_message():
    err = CodexPluginsCLIError(
        argv=["codex", "plugin", "marketplace", "add", "http://example.com"],
        returncode=1,
        stdout="",
        stderr="Error: bad url",
    )
    assert "exited with code 1" in str(err)
    assert "bad url" in str(err)


def test_cli_not_found_message():
    err = CodexPluginsCLINotFound("codex not installed")
    assert "codex not installed" in str(err)
