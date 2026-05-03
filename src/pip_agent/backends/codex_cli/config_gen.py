"""Generate ``config.toml`` MCP server entries for the Codex backend.

Called at host boot to ensure:

1. The project directory is trusted in ``~/.codex/config.toml``
   (prompts the user interactively if not).
2. The project-local ``.codex/config.toml`` has a ``[mcp_servers.pip]``
   entry pointing at our STDIO MCP bridge.

MCP registration lives in project-local config so it does not pollute
the global config or affect other Codex clients.
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


# -- Project trust ----------------------------------------------------------

def _global_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _read_global_config() -> str:
    path = _global_config_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _normalise_path_key(p: str) -> str:
    """Lowercase and forward-slash normalise for TOML key comparison."""
    return p.replace("\\", "/").lower().rstrip("/")


def is_project_trusted(project_dir: Path) -> bool:
    """Check whether *project_dir* is marked trusted in global config."""
    content = _read_global_config()
    norm = _normalise_path_key(str(project_dir))

    for match in re.finditer(
        r"""\[projects\.['"](.*?)['"]\]""", content,
    ):
        entry_path = _normalise_path_key(match.group(1))
        if entry_path == norm:
            after = content[match.end():]
            next_section = after.find("\n[")
            block = after[:next_section] if next_section != -1 else after
            if re.search(
                r"""trust_level\s*=\s*['"]trusted['"]""", block,
            ):
                return True
    return False


def add_project_trust(project_dir: Path) -> None:
    """Append a ``[projects.'<path>'] trust_level = "trusted"`` entry.

    The path is written in its OS-native form (backslashes on Windows)
    so Codex CLI recognises it and does not add a duplicate entry.
    """
    config_path = _global_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    content = _read_global_config()
    native = str(project_dir).lower()

    entry = f"\n[projects.'{native}']\ntrust_level = \"trusted\"\n"
    content = content.rstrip("\n") + "\n" + entry

    config_path.write_text(content, encoding="utf-8")


def ensure_project_trusted(project_dir: Path) -> bool:
    """Ensure *project_dir* is trusted; prompt interactively if needed.

    Returns ``True`` if the project is (now) trusted, ``False`` if the
    user declined.  Only called for the ``codex_cli`` backend.
    """
    if is_project_trusted(project_dir):
        return True

    norm = str(project_dir).replace("\\", "/")
    print(
        f"\nCodex requires project trust to load local MCP config.\n"
        f"Project: {norm}\n"
    )
    try:
        answer = input("Trust this project? [y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer in ("", "y", "yes"):
        add_project_trust(project_dir)
        print("Project trusted.\n")
        return True

    return False


# -- Project-local MCP config ----------------------------------------------

def ensure_codex_config(workdir: Path | None = None) -> Path:
    """Ensure project-local ``.codex/config.toml`` has the MCP server entry.

    Writes to ``<workdir>/.codex/config.toml`` (project-scoped, requires
    the project to be trusted in the global config).  Does NOT touch the
    global ``~/.codex/config.toml`` — only ``ensure_project_trusted``
    and ``cleanup_global_mcp`` write there.

    Idempotent: skips the write when the expected block is already
    present with the current Python executable path.
    """
    if workdir is None:
        workdir = Path.cwd()

    codex_dir = workdir / ".codex"
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


def cleanup_global_mcp() -> None:
    """Remove any ``[mcp_servers.pip*]`` sections from the global config.

    Called once at boot to clean up entries left by older Pip-Boy
    versions that wrote MCP registration to the global config.
    """
    config_path = _global_config_path()
    if not config_path.exists():
        return

    content = config_path.read_text(encoding="utf-8")
    if "[mcp_servers.pip]" not in content:
        return

    cleaned = _strip_pip_sections(content)
    if cleaned != content.strip():
        if cleaned and not cleaned.endswith("\n"):
            cleaned += "\n"
        config_path.write_text(cleaned, encoding="utf-8")
