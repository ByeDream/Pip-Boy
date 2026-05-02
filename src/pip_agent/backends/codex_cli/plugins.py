"""Plugin CLI adapter for the Codex backend.

Mirrors ``pip_agent.plugins`` but targets the bundled ``codex.exe``
binary instead of ``claude.exe``.  Only marketplace operations are
currently implemented — the Codex CLI's plugin subcommand structure
is different from Claude Code's (no ``--json`` on list/search, only
``marketplace add/upgrade/remove``).

Contract §3.7: plugins are NOT interoperable across backends.  A
plugin installed via Claude Code is invisible to Codex and vice versa.
The host must label plugin output with the active backend name.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from pathlib import Path

from pip_agent import _profile

log = logging.getLogger(__name__)


class CodexPluginsCLINotFound(RuntimeError):
    """Bundled codex binary not found."""


class CodexPluginsCLIError(RuntimeError):
    """Codex plugin subprocess returned a non-zero exit code."""

    def __init__(
        self,
        argv: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        msg = (
            f"`{' '.join(argv)}` exited with code {returncode}: "
            f"{(stderr or stdout).strip()[:400]}"
        )
        super().__init__(msg)


_DEFAULT_TIMEOUT_S = 30.0
_NETWORK_TIMEOUT_S = 180.0


def _bundled_cli() -> Path:
    """Resolve the path to the Codex CLI binary."""
    try:
        from codex._binary import bundled_codex_path

        return bundled_codex_path()
    except Exception:  # noqa: BLE001
        pass

    import shutil

    cli_name = "codex.exe" if platform.system() == "Windows" else "codex"
    sys_cli = shutil.which(cli_name)
    if sys_cli:
        return Path(sys_cli)

    raise CodexPluginsCLINotFound(
        "Codex CLI binary not found. Install codex-python: "
        "pip install codex-python"
    )


async def _run(
    *argv: str,
    cwd: Path | str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> tuple[str, str, int]:
    """Spawn the codex binary with ``argv``."""
    cli = _bundled_cli()
    full = [str(cli), *argv]
    log.debug("codex_plugins: spawning %s (cwd=%s)", full, cwd)

    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    try:
        out_bytes, err_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        finally:
            await proc.wait()
        raise

    stdout = out_bytes.decode("utf-8", errors="replace")
    stderr = err_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, proc.returncode or 0


def _check(argv: list[str], rc: int, stdout: str, stderr: str) -> None:
    if rc != 0:
        raise CodexPluginsCLIError(argv, rc, stdout, stderr)


# ---------------------------------------------------------------------------
# Marketplace operations
# ---------------------------------------------------------------------------


async def marketplace_add(
    url: str,
    *,
    cwd: Path | str | None = None,
) -> str:
    """Add a marketplace from a GitHub URL.

    Returns the stdout text describing what was added.
    """
    async with _profile.span("codex.plugin.marketplace_add"):
        argv = ["plugin", "marketplace", "add", url]
        out, err, rc = await _run(*argv, cwd=cwd, timeout=_NETWORK_TIMEOUT_S)
        _check(argv, rc, out, err)
        return out.strip()


async def marketplace_remove(
    url: str,
    *,
    cwd: Path | str | None = None,
) -> str:
    """Remove a marketplace."""
    async with _profile.span("codex.plugin.marketplace_remove"):
        argv = ["plugin", "marketplace", "remove", url]
        out, err, rc = await _run(*argv, cwd=cwd, timeout=_DEFAULT_TIMEOUT_S)
        _check(argv, rc, out, err)
        return out.strip()


async def marketplace_upgrade(
    *,
    cwd: Path | str | None = None,
) -> str:
    """Upgrade all marketplace plugins."""
    async with _profile.span("codex.plugin.marketplace_upgrade"):
        argv = ["plugin", "marketplace", "upgrade"]
        out, err, rc = await _run(*argv, cwd=cwd, timeout=_NETWORK_TIMEOUT_S)
        _check(argv, rc, out, err)
        return out.strip()


async def health_check() -> tuple[bool, str]:
    """Check if the Codex CLI is available for plugin operations."""
    try:
        cli = _bundled_cli()
        return True, f"Codex CLI found at {cli}"
    except CodexPluginsCLINotFound as exc:
        return False, str(exc)
