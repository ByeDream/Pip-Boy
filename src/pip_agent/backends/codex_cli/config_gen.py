"""Generate ``config.toml`` MCP server entries for the Codex backend.

Called during scaffold / setup to ensure ``~/.codex/config.toml``
includes the ``[mcp_servers.pip]`` section pointing at our STDIO
MCP bridge.
"""

from __future__ import annotations

import sys
from pathlib import Path


def pip_mcp_server_toml_block() -> str:
    """Return the TOML snippet for registering Pip-Boy's MCP server.

    Uses forward slashes and TOML literal strings (single-quoted) to
    avoid Windows backslash escape issues in TOML double-quoted strings.
    """
    python = sys.executable.replace("\\", "/")
    return (
        "[mcp_servers.pip]\n"
        "type = 'stdio'\n"
        f"command = ['{python}', '-m', "
        "'pip_agent.backends.codex_cli.mcp_bridge']\n"
    )


def ensure_codex_config(workdir: Path | None = None) -> Path:
    """Ensure ``~/.codex/config.toml`` includes our MCP server entry.

    Returns the path to the config file.  Creates the file and
    directory if they don't exist.  Appends the MCP block if the
    ``[mcp_servers.pip]`` key is missing.
    """
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"

    codex_dir.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
    else:
        content = ""

    if "[mcp_servers.pip]" not in content:
        block = pip_mcp_server_toml_block()

        if workdir:
            safe_path = str(workdir).replace("\\", "/")
            env_block = (
                f"\n[mcp_servers.pip.env]\n"
                f"PIP_WORKDIR = '{safe_path}'\n"
            )
            block += env_block

        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{block}"
        config_path.write_text(content, encoding="utf-8")

    return config_path
