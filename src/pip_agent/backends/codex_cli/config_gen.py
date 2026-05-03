"""Generate ``config.toml`` MCP server entries for the Codex backend.

Called during scaffold / setup to ensure ``~/.codex/config.toml``
includes the ``[mcp_servers.pip]`` section pointing at our STDIO
MCP bridge.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CONFIG_VERSION = "2"
_VERSION_MARKER = f"# pip-config-version={_CONFIG_VERSION}"


def pip_mcp_server_toml_block() -> str:
    """Return the TOML snippet for registering Pip-Boy's MCP server.

    Uses forward slashes and TOML literal strings (single-quoted) to
    avoid Windows backslash escape issues in TOML double-quoted strings.
    Includes a version marker comment so stale blocks can be detected.
    """
    python = sys.executable.replace("\\", "/")
    return (
        f"{_VERSION_MARKER}\n"
        "[mcp_servers.pip]\n"
        "type = 'stdio'\n"
        f"command = ['{python}', '-m', "
        "'pip_agent.backends.codex_cli.mcp_bridge']\n"
    )


def _needs_rewrite(content: str, workdir: Path | None = None) -> bool:
    """True if the existing pip MCP block is missing or outdated."""
    if "[mcp_servers.pip]" not in content:
        return True
    if _VERSION_MARKER not in content:
        return True
    python = sys.executable.replace("\\", "/")
    if python not in content:
        return True
    if workdir:
        safe_path = str(workdir).replace("\\", "/")
        if safe_path not in content:
            return True
    return False


def _strip_old_pip_block(content: str) -> str:
    """Remove an existing ``[mcp_servers.pip]`` block and its env sub-table."""
    content = re.sub(
        r"# pip-config-version=\d+\n", "", content,
    )
    content = re.sub(
        r"\[mcp_servers\.pip\]\n(?:[^\[]*?)(?=\n\[|\Z)",
        "", content, flags=re.DOTALL,
    )
    content = re.sub(
        r"\[mcp_servers\.pip\.env\]\n(?:[^\[]*?)(?=\n\[|\Z)",
        "", content, flags=re.DOTALL,
    )
    return content.strip()


def ensure_codex_config(workdir: Path | None = None) -> Path:
    """Ensure ``~/.codex/config.toml`` has an up-to-date MCP server entry.

    Creates or rewrites the ``[mcp_servers.pip]`` block when it is
    missing or carries an older version marker (e.g. one generated with
    broken Windows backslash paths).
    """
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"

    codex_dir.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
    else:
        content = ""

    if _needs_rewrite(content, workdir=workdir):
        content = _strip_old_pip_block(content)
        block = pip_mcp_server_toml_block()

        if workdir:
            safe_path = str(workdir).replace("\\", "/")
            block += (
                f"\n[mcp_servers.pip.env]\n"
                f"PIP_WORKDIR = '{safe_path}'\n"
            )

        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{block}"
        config_path.write_text(content, encoding="utf-8")

    return config_path
