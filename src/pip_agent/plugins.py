"""Async wrappers around the bundled Claude Code CLI's plugin subcommands.

The Claude Agent SDK ships a full ``claude.exe`` (or ``claude``) binary
inside ``claude_agent_sdk/_bundled/``. That binary owns the canonical
plugin / marketplace state under ``~/.claude/`` and ``<cwd>/.claude/``;
we never re-implement it. This module just calls the binary as a
subprocess with ``--json`` where the CLI supports it, and surfaces the
result so host commands and MCP tools can render it.

Why a separate module
---------------------
Two surfaces (``host_commands`` and ``mcp_tools``) want the same
operations. Keeping the subprocess plumbing in one place means:

* a single point to update if the CLI flags drift between SDK versions,
* one place to bolt on profiling spans, timeouts, and shell-injection
  hardening (we always pass ``argv`` lists, never strings),
* a single unit-test seam (every public coroutine just await-shells
  out).

Scope (``user`` / ``project`` / ``local``)
------------------------------------------
The CLI accepts ``-s {user|project|local}`` on ``plugin install``,
``plugin marketplace add``, and ``plugin enable`` / ``plugin disable``.
The wrappers expose ``scope`` as a typed kwarg (``Scope`` literal). When
the caller passes ``project`` or ``local`` the resulting ``.claude/``
state is written under whatever ``cwd`` the subprocess inherits — so the
caller MUST pass ``cwd`` set to the agent's effective workdir for those
scopes. Pip-Boy 's [src/pip_agent/agent_runner.py:204](src/pip_agent/agent_runner.py)
loads all three settings sources, so plugins installed at any scope are
visible to the next ``query()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
from pathlib import Path
from typing import Literal

import claude_agent_sdk

from pip_agent import _profile

log = logging.getLogger(__name__)


Scope = Literal["user", "project", "local"]
"""Legal values for ``--scope`` on plugin / marketplace subcommands."""


# Two-tier timeout policy. Reads against on-disk state (``list``,
# ``search``, ``enable``/``disable``, ``uninstall``) finish in well
# under 5 s on any sane host; capping them at 30 s gives an obvious
# stuck-subprocess signal without affecting the happy path.
#
# Network-bound operations (``marketplace add``, ``marketplace
# update``, ``plugin install``) git-clone a repo and frequently run
# ``npm`` / ``uv`` to fetch the plugin's runtime deps. On a corporate
# proxy or first-cold-cache npm fetch this can comfortably take a
# minute or two; the previous 30 s cap killed Exa's install at
# ~28 s on a normal connection. 180 s is the empirical "would have
# succeeded if we'd just waited" threshold from the install logs;
# operators with worse networks override via ``PLUGIN_NETWORK_TIMEOUT_SEC``.
_DEFAULT_TIMEOUT_S = 30.0
_NETWORK_TIMEOUT_S_FALLBACK = 180.0


def _network_timeout() -> float:
    """Resolve the timeout for git-clone / dep-install style operations.

    Reads :attr:`pip_agent.config.Settings.plugin_network_timeout_sec`
    so deployments behind a slow proxy can dial it up without code
    changes. Falls back to :data:`_NETWORK_TIMEOUT_S_FALLBACK` if the
    settings module fails to import (e.g. during isolated unit tests
    that monkeypatch the import graph).
    """
    try:
        from pip_agent.config import settings
        return float(settings.plugin_network_timeout_sec)
    except Exception:  # noqa: BLE001
        return _NETWORK_TIMEOUT_S_FALLBACK


class PluginsCLINotFound(RuntimeError):
    """Raised when neither the SDK-bundled nor the system ``claude`` CLI
    binary can be located.

    Pip-Boy's runtime always has the bundled binary (it's how
    ``claude_agent_sdk.query`` itself works), so seeing this in
    practice means the SDK install is broken; the dependent host
    command / MCP tool surfaces the message verbatim.
    """


class PluginsCLIError(RuntimeError):
    """Subprocess returned a non-zero exit code.

    Carries ``returncode``, ``stdout``, ``stderr`` so callers can
    surface diagnostic detail without re-running the subprocess.
    """

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


# ---------------------------------------------------------------------------
# Bundled CLI resolution
# ---------------------------------------------------------------------------


def _bundled_cli() -> Path:
    """Resolve the path to the Claude Code CLI binary.

    Mirrors
    :meth:`claude_agent_sdk._internal.transport.subprocess_cli.SubprocessCLITransport._find_cli`
    so we don't depend on a private internal symbol: prefer the binary
    bundled with the SDK package, fall back to ``shutil.which("claude")``
    for editable installs / containers that strip ``_bundled``.
    """
    sdk_root = Path(claude_agent_sdk.__file__).resolve().parent
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = sdk_root / "_bundled" / cli_name
    if bundled.is_file():
        return bundled
    sys_cli = shutil.which("claude")
    if sys_cli:
        return Path(sys_cli)
    raise PluginsCLINotFound(
        "Claude Code CLI not found. Looked for "
        f"{bundled} (SDK-bundled) and `claude` on PATH. "
        "Reinstall claude-agent-sdk or install the standalone CLI."
    )


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------


async def _run(
    *argv: str,
    cwd: Path | str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> tuple[str, str, int]:
    """Spawn the bundled CLI with ``argv`` and capture (stdout, stderr, rc).

    ``argv`` MUST be a flat list of arguments; never pass a single
    space-joined string — that would re-introduce shell-injection on
    inputs the user / agent controls (e.g. plugin specs, marketplace
    URLs). On timeout the child is killed and a descriptive
    :class:`asyncio.TimeoutError` propagates.
    """
    cli = _bundled_cli()
    full = [str(cli), *argv]
    log.debug("plugins: spawning %s (cwd=%s)", full, cwd)

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
        raise PluginsCLIError(argv, rc, stdout, stderr)


def _parse_json(stdout: str) -> list[dict] | dict:
    """Parse ``--json`` output, returning ``[]`` for empty output.

    The CLI emits a blank line when the result set is empty (e.g.
    ``plugin marketplace list --json`` with no marketplaces configured)
    rather than ``[]``. Treating empty stdout as ``[]`` lets call sites
    iterate without an extra null check.
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("plugins: failed to parse JSON: %s | raw=%r", exc, text[:400])
        raise


# ---------------------------------------------------------------------------
# Marketplace operations
# ---------------------------------------------------------------------------


async def marketplace_add(
    source: str,
    *,
    scope: Scope = "user",
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Register a marketplace.

    ``source`` accepts a GitHub ``owner/repo`` slug, an HTTPS git URL,
    or a local path — the CLI distinguishes them itself.
    """
    await ensure_bootstrap_once()
    async with _profile.span("plugins.marketplace_add"):
        argv = ["plugin", "marketplace", "add", source, "--scope", scope]
        out, err, rc = await _run(*argv, cwd=cwd, timeout=_network_timeout())
        _check(argv, rc, out, err)
        return out, err, rc


async def marketplace_list(
    *,
    cwd: Path | str | None = None,
) -> list[dict]:
    """List configured marketplaces (parsed JSON)."""
    await ensure_bootstrap_once()
    async with _profile.span("plugins.marketplace_list"):
        argv = ["plugin", "marketplace", "list", "--json"]
        out, err, rc = await _run(*argv, cwd=cwd)
        _check(argv, rc, out, err)
        result = _parse_json(out)
        return result if isinstance(result, list) else [result]


async def marketplace_remove(
    name: str,
    *,
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Remove a previously-registered marketplace by name."""
    await ensure_bootstrap_once()
    async with _profile.span("plugins.marketplace_remove"):
        argv = ["plugin", "marketplace", "remove", name]
        out, err, rc = await _run(*argv, cwd=cwd)
        _check(argv, rc, out, err)
        return out, err, rc


async def marketplace_update(
    name: str | None = None,
    *,
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Refresh marketplace metadata for ``name`` (or all if omitted)."""
    await ensure_bootstrap_once()
    async with _profile.span("plugins.marketplace_update"):
        argv = ["plugin", "marketplace", "update"]
        if name:
            argv.append(name)
        out, err, rc = await _run(*argv, cwd=cwd, timeout=_network_timeout())
        _check(argv, rc, out, err)
        return out, err, rc


# ---------------------------------------------------------------------------
# Plugin operations
# ---------------------------------------------------------------------------


async def plugin_install(
    spec: str,
    *,
    scope: Scope = "user",
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Install a plugin.

    ``spec`` is either ``<name>`` (resolved against the configured
    marketplaces) or ``<name>@<marketplace>`` to disambiguate when the
    same name appears in multiple sources. The CLI itself prints a
    helpful error when ``spec`` is ambiguous, so we just pass it through.
    """
    await ensure_bootstrap_once()
    async with _profile.span("plugins.install"):
        argv = ["plugin", "install", spec, "-s", scope]
        out, err, rc = await _run(*argv, cwd=cwd, timeout=_network_timeout())
        _check(argv, rc, out, err)
        return out, err, rc


async def plugin_uninstall(
    name: str,
    *,
    scope: Scope | None = None,
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Uninstall ``name``. ``scope=None`` lets the CLI auto-detect."""
    await ensure_bootstrap_once()
    async with _profile.span("plugins.uninstall"):
        argv = ["plugin", "uninstall", name]
        if scope:
            argv += ["-s", scope]
        out, err, rc = await _run(*argv, cwd=cwd)
        _check(argv, rc, out, err)
        return out, err, rc


async def plugin_list(
    available: bool = False,
    *,
    cwd: Path | str | None = None,
) -> list[dict]:
    """List installed plugins, or all marketplace-discoverable plugins.

    ``available=True`` aggregates entries from every configured
    marketplace; this is the data source ``plugin_search`` filters
    locally.

    Output shapes the CLI uses (observed on Claude Code 2.1.97):

    * ``plugin list --json`` → flat array of installed plugin records.
    * ``plugin list --available --json`` → object
      ``{"installed": [...], "available": [...]}``. ``installed`` is
      whatever ``plugin list --json`` would have returned;
      ``available`` is the full catalogue with one record per plugin
      across every configured marketplace.

    We normalise both into a flat ``list[dict]`` keyed by the field the
    caller asked about. This is the seam ``plugin_search`` and the host
    formatters consume, so changing the shape downstream would be a
    bigger refactor than picking the right field here.
    """
    await ensure_bootstrap_once()
    async with _profile.span("plugins.list"):
        argv = ["plugin", "list"]
        if available:
            argv.append("--available")
        argv.append("--json")
        out, err, rc = await _run(*argv, cwd=cwd)
        _check(argv, rc, out, err)
        result = _parse_json(out)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            field = "available" if available else "installed"
            items = result.get(field, [])
            return list(items) if isinstance(items, list) else []
        # Defensive: unexpected scalar — surface nothing rather than
        # poisoning the formatter with a non-dict ``item``.
        return []


async def plugin_enable(
    name: str,
    *,
    scope: Scope | None = None,
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Re-enable a previously-disabled plugin."""
    await ensure_bootstrap_once()
    async with _profile.span("plugins.enable"):
        argv = ["plugin", "enable", name]
        if scope:
            argv += ["-s", scope]
        out, err, rc = await _run(*argv, cwd=cwd)
        _check(argv, rc, out, err)
        return out, err, rc


async def plugin_disable(
    name: str,
    *,
    scope: Scope | None = None,
    cwd: Path | str | None = None,
) -> tuple[str, str, int]:
    """Disable a plugin without uninstalling it."""
    await ensure_bootstrap_once()
    async with _profile.span("plugins.disable"):
        argv = ["plugin", "disable", name]
        if scope:
            argv += ["-s", scope]
        out, err, rc = await _run(*argv, cwd=cwd)
        _check(argv, rc, out, err)
        return out, err, rc


# ---------------------------------------------------------------------------
# Sync bridge for host_commands
# ---------------------------------------------------------------------------
#
# The host slash-command dispatcher (``host_commands.dispatch_command``)
# is synchronous, but it is invoked from inside an already-running
# event loop (``AgentHost.process_inbound``). That rules out
# ``asyncio.run`` (would raise "cannot be called from a running event
# loop"). We spawn a one-shot worker thread with its own loop instead;
# the calling thread blocks on ``join()`` while the subprocess runs,
# but Pip-Boy already accepts that latency for ``/subagent`` (rmtree)
# and ``/wechat`` (controller bootstrap).


def run_sync(coro):  # type: ignore[no-untyped-def]
    """Run ``coro`` to completion from synchronous code.

    Safe to call from threads that already have an event loop running.
    Used by host slash commands; MCP tool handlers are already async
    and should ``await`` the wrappers directly.
    """
    import threading

    box: dict[str, object] = {}

    def _runner() -> None:
        try:
            box["ok"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]  # type: ignore[misc]
    return box["ok"]


# ---------------------------------------------------------------------------
# Lazy marketplace bootstrap
# ---------------------------------------------------------------------------
#
# Previously ``run_host`` synchronously called ``ensure_marketplaces`` at
# boot — a ~3 s ``claude.exe plugin marketplace list --json`` spawn paid
# on every launch even though the result is almost always "already
# configured". Sessions that never touch plugins were still billed for
# it. We now defer the check to the first public plugins coroutine that
# actually needs marketplace state (any of ``marketplace_*``,
# ``plugin_*``). Flag + lock ensure exactly-once per process; setting
# the flag *before* awaiting ``ensure_marketplaces`` also breaks the
# recursion where the bootstrap itself calls ``marketplace_list`` /
# ``marketplace_add``.

_bootstrap_done = False
_bootstrap_lock: asyncio.Lock | None = None


async def ensure_bootstrap_once() -> None:
    """Run the configured marketplace bootstrap at most once per process.

    Idempotent, safe to call from any plugins coroutine. Failures are
    swallowed (``ensure_marketplaces`` already logs them) and the flag
    is still flipped so a transient network issue does not cause every
    subsequent plugin operation to re-spawn the subprocess.
    """
    global _bootstrap_done, _bootstrap_lock
    if _bootstrap_done:
        return
    if _bootstrap_lock is None:
        _bootstrap_lock = asyncio.Lock()
    async with _bootstrap_lock:
        if _bootstrap_done:
            return
        # Flip *before* awaiting so ``ensure_marketplaces``'s internal
        # calls to ``marketplace_list`` / ``marketplace_add`` hit the
        # cheap True fast-path instead of deadlocking on this same lock.
        _bootstrap_done = True
        try:
            from pip_agent.config import settings
            csv = (settings.bootstrap_marketplaces or "").strip()
        except Exception:  # noqa: BLE001
            return
        specs = [chunk for chunk in csv.split(",") if chunk.strip()]
        if not specs:
            return
        try:
            added = await ensure_marketplaces(specs)
        except Exception as exc:  # noqa: BLE001
            log.warning("plugins: marketplace bootstrap aborted: %s", exc)
            return
        if added:
            log.info(
                "plugins: bootstrapped %d marketplace(s): %s",
                len(added),
                ", ".join(added),
            )


def reset_bootstrap_for_test() -> None:
    """Test-only: clear the one-shot gate so each test starts clean."""
    global _bootstrap_done, _bootstrap_lock
    _bootstrap_done = False
    _bootstrap_lock = None


async def ensure_marketplaces(
    specs: list[str] | tuple[str, ...],
    *,
    cwd: Path | str | None = None,
) -> list[str]:
    """Idempotently register a fixed set of marketplaces.

    Used at host cold-start to bootstrap an opinionated default catalogue
    (see :attr:`pip_agent.config.Settings.bootstrap_marketplaces`) so a
    fresh Pip-Boy install can immediately ``/plugin search exa`` without
    a manual ``marketplace add``.

    Behaviour:

    * Each spec is normalised: empty / whitespace-only entries are dropped.
    * The current marketplace list is fetched once. Specs whose
      ``owner/repo`` matches an existing entry's ``repo`` field are
      skipped — no subprocess spawn, no network.
    * Specs that don't match (or aren't ``owner/repo`` shape, e.g. URLs
      or local paths) trigger a ``marketplace add`` at user scope. The
      CLI itself is idempotent — duplicate adds exit 0 with an
      "already on disk" message — so this is safe even if our local
      diff misses an alias.
    * Failures (network down, malformed spec, CLI not found) are logged
      at WARNING and swallowed: the caller's startup continues.
      ``PluginsCLINotFound`` is also caught here, so an SDK install
      missing the bundled binary doesn't take the host down.

    Returns the list of specs we actually attempted to add (i.e. were
    not already present), so callers can log a single concise summary.
    """
    cleaned = [s.strip() for s in specs if s and s.strip()]
    if not cleaned:
        return []

    try:
        existing = await marketplace_list(cwd=cwd)
    except (PluginsCLIError, PluginsCLINotFound, OSError) as exc:
        log.warning(
            "plugins: skipping marketplace bootstrap (list failed): %s",
            exc,
        )
        return []

    known_repos = {
        str(item.get("repo")).strip()
        for item in existing
        if isinstance(item, dict) and item.get("repo")
    }

    added: list[str] = []
    for spec in cleaned:
        is_owner_repo = (
            "/" in spec
            and ":" not in spec
            and " " not in spec
            and spec.count("/") == 1
        )
        if is_owner_repo and spec in known_repos:
            log.debug("plugins: marketplace %r already configured", spec)
            continue
        try:
            await marketplace_add(spec, scope="user", cwd=cwd)
            added.append(spec)
            log.info("plugins: bootstrapped marketplace %r", spec)
        except (PluginsCLIError, PluginsCLINotFound, OSError) as exc:
            log.warning(
                "plugins: marketplace bootstrap %r failed: %s", spec, exc,
            )
    return added


async def plugin_search(
    query: str,
    *,
    cwd: Path | str | None = None,
) -> list[dict]:
    """Filter the marketplace catalogue locally by case-insensitive substring.

    The CLI does not expose a ``plugin search`` subcommand — the
    interactive TUI provides one, but we run headless. Pulling
    ``plugin list --available --json`` once and grepping client-side
    keeps the surface simple and offline-friendly between marketplace
    updates.
    """
    async with _profile.span("plugins.search"):
        items = await plugin_list(available=True, cwd=cwd)
        if not query:
            return items
        q = query.casefold()

        def _matches(item: dict) -> bool:
            haystack: list[str] = []
            for field_name in ("name", "id", "description", "summary"):
                val = item.get(field_name)
                if isinstance(val, str):
                    haystack.append(val)
            tags = item.get("tags") or item.get("keywords") or []
            if isinstance(tags, list):
                haystack.extend(str(t) for t in tags)
            return any(q in s.casefold() for s in haystack)

        return [it for it in items if _matches(it)]
