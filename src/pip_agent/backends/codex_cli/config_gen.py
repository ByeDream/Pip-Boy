"""Generate ``config.toml`` MCP server entries for the Codex backend.

Called at host boot to ensure ``~/.codex/config.toml`` includes the
``[mcp_servers.pip]`` section pointing at our STDIO MCP bridge.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _pip_block(workdir: Path | None = None) -> str:
    """Return the TOML snippet for registering Pip-Boy's MCP server.

    Uses forward slashes and TOML literal strings (single-quoted) to
    avoid Windows backslash escape issues.
    """
    python = sys.executable.replace("\\", "/")
    block = (
        "[mcp_servers.pip]\n"
        f"command = '{python}'\n"
        "args = ['-m', 'pip_agent.backends.codex_cli.mcp_bridge']\n"
    )
    if workdir:
        safe = str(workdir).replace("\\", "/")
        block += (
            f"\n[mcp_servers.pip.env]\n"
            f"PIP_WORKDIR = '{safe}'\n"
        )
    return block


def _strip_pip_sections(content: str) -> str:
    """Remove all ``[mcp_servers.pip*]`` sections from *content*."""
    lines = content.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if re.match(r"\[mcp_servers\.pip(?:\.\w+)?\]", line):
            skip = True
            continue
        if skip and line.startswith("["):
            skip = False
        if not skip:
            out.append(line)
    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def ensure_codex_config(workdir: Path | None = None) -> Path:
    """Ensure ``~/.codex/config.toml`` has the correct MCP server entry.

    Idempotent: skips the write when the expected block is already
    present with the current Python executable path.
    """
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"
    codex_dir.mkdir(parents=True, exist_ok=True)

    content = (
        config_path.read_text(encoding="utf-8")
        if config_path.exists() else ""
    )

    block = _pip_block(workdir)
    python = sys.executable.replace("\\", "/")

    needs_update = (
        "[mcp_servers.pip]" not in content
        or python not in content
        or content.count("[mcp_servers.pip]") > 1
    )

    if needs_update:
        content = _strip_pip_sections(content)
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{block}"
        config_path.write_text(content, encoding="utf-8")

    return config_path
