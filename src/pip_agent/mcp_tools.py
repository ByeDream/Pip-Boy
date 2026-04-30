"""In-process MCP server exposing Pip-Boy's unique capabilities to the SDK.

Most tools the agent uses (Bash, Read, Write, Edit, Glob, Grep, WebSearch,
Task, TodoWrite, Skill, …) are delegated to Claude Code's built-ins.
This server adds capabilities Claude Code does NOT ship — plus a couple
that we deliberately shadow so behaviour stays consistent across direct
and proxied upstream endpoints.

Tools currently exposed:

* Memory: ``memory_search``, ``memory_write``, ``remember_user``,
  ``lookup_user``, ``reflect``
* Cron: ``cron_add``, ``cron_remove``, ``cron_update``, ``cron_list``
* Channel: ``send_file``
* Editor: ``open_file`` (CLI channel only)
* Plugin: ``plugin_list``, ``plugin_search``, ``plugin_install``,
  ``plugin_marketplace_add``, ``plugin_marketplace_list``
  (read + additive only — destructive ops live on the host
  ``/plugin`` slash command)
* Web: ``web_fetch`` and ``web_search`` (canonical implementations;
  Claude Code's built-in ``WebFetch`` / ``WebSearch`` are disabled so
  the namespace is single-source)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

if TYPE_CHECKING:
    from pip_agent.channels import Channel
    from pip_agent.host_scheduler import HostScheduler
    from pip_agent.memory import MemoryStore

log = logging.getLogger(__name__)


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


@dataclass
class McpContext:
    """Runtime context shared by all MCP tool handlers.

    Populated fresh by :class:`AgentHost` before each ``query()`` call so
    handlers see the correct agent/session/channel identity.

    ``user_id`` is the caller's resolved addressbook id — empty when the
    sender is not yet registered (``<user_query user_id="unverified">``).
    It gates ``remember_user``'s create-vs-update branch.
    """

    memory_store: MemoryStore | None = None
    workdir: Path = field(default_factory=Path.cwd)
    session_id: str = ""
    scheduler: HostScheduler | None = None
    channel: Channel | None = None
    peer_id: str = ""
    sender_id: str = ""
    user_id: str = ""
    # WeChat multi-account: picks which bot identity originates outbound
    # sends through ``send_image`` / ``send_file``. Empty for single-
    # identity channels (CLI, WeCom).
    account_id: str = ""
    # Textual TUI app instance when running under the TUI — handlers that
    # spawn TTY-taking subprocesses (``open_file`` launching vim/nano)
    # need this to call ``app.suspend()``. ``Any`` avoids a TUI import
    # cycle at module load; ``None`` in headless / remote-channel runs.
    tui_app: Any | None = None


def build_mcp_server(ctx: McpContext) -> McpSdkServerConfig:
    """Create the in-process MCP server with all Pip-Boy-unique tools."""
    from pip_agent.config import settings

    tools = (
        _memory_tools(ctx)
        + _cron_tools(ctx)
        + _channel_tools(ctx)
        + _editor_tools(ctx)
        + _plugin_tools(ctx)
        + (_web_tools(ctx) if settings.use_custom_web_tools else [])
    )
    # PROFILE — wrap every tool handler so each invocation shows up
    # as its own ``mcp.<name>`` span. Single-site interception means one
    # line to remove during cleanup.
    tools = [_profile_wrap_tool(t) for t in tools]
    return create_sdk_mcp_server("pip", tools=tools)


def _profile_wrap_tool(tool: SdkMcpTool) -> SdkMcpTool:  # PROFILE
    """Wrap an ``SdkMcpTool`` so its handler is instrumented with a span."""
    from pip_agent import _profile

    original = tool.handler
    name = tool.name

    async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
        async with _profile.span(f"mcp.{name}"):
            return await original(args)

    # ``SdkMcpTool`` is a dataclass; build a shallow copy with the new handler.
    from dataclasses import replace

    return replace(tool, handler=wrapped)


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


def _memory_tools(ctx: McpContext) -> list[SdkMcpTool]:

    async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.memory_store is None:
            return _error("Memory store not available.")
        q = args.get("query", "").strip()
        if not q:
            return _error("'query' is required.")
        top_k = int(args.get("top_k", 5))
        results = ctx.memory_store.search(q, top_k=top_k)
        if not results:
            return _text("(no matching memories)")
        lines = [f"- {r.get('text', '')} (score: {r.get('score', 0)})" for r in results]
        return _text("\n".join(lines))

    async def memory_write(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.memory_store is None:
            return _error("Memory store not available.")
        content = args.get("content", "").strip()
        if not content:
            return _error("'content' is required.")
        category = args.get("category", "observation")
        ctx.memory_store.write_single(content, category=category, source="tool")
        return _text(f"Observation recorded ({len(content)} chars).")

    async def remember_user(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.memory_store is None:
            return _error("Memory store not available.")

        ch_name = ctx.channel.name if ctx.channel else "cli"
        sid = ctx.sender_id
        if sid and ch_name and sid.startswith(f"{ch_name}:"):
            sid = sid[len(ch_name) + 1:]

        fields: dict[str, str] = {
            "name": args.get("name", "") or "",
            "call_me": args.get("call_me", "") or "",
            "timezone": args.get("timezone", "") or "",
            "notes": args.get("notes", "") or "",
        }
        target_id_arg = (args.get("user_id") or "").strip()
        current_user_id = ctx.user_id or ""

        # ACL: verified callers can only update their OWN record. They
        # cannot invent new contacts (risk of impersonation) nor
        # rewrite someone else's profile. The errors are surfaced to
        # the model so it learns the constraint instead of silently
        # retrying with the same args.
        if current_user_id:
            if target_id_arg and target_id_arg != current_user_id:
                return _error(
                    "remember_user is only allowed to update your OWN "
                    f"profile ({current_user_id}). To record information "
                    f"about another contact (user_id={target_id_arg}), "
                    "use memory_write instead."
                )
            result = ctx.memory_store.update_contact(
                current_user_id,
                sender_id=sid, channel=ch_name,
                **fields,
            )
            return _text(result)

        # Unverified caller — introduce-yourself handshake. They may
        # only create a fresh contact; targeting an existing user_id
        # would let an anonymous sender hijack that profile's identifiers.
        if target_id_arg:
            return _error(
                "Cannot update an existing contact while unverified. "
                "Omit user_id to create a new entry for the current sender."
            )
        new_id, msg = ctx.memory_store.create_contact(
            sender_id=sid, channel=ch_name, **fields,
        )
        return _text(f"{msg} user_id={new_id}")

    async def lookup_user(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.memory_store is None:
            return _error("Memory store not available.")
        user_id = (args.get("user_id") or "").strip()
        if not user_id:
            return _error("'user_id' is required.")
        content = ctx.memory_store.load_profile_by_id(user_id)
        if content is None:
            return _error(f"No contact with user_id={user_id}.")
        return _text(content)

    async def reflect(args: dict[str, Any]) -> dict[str, Any]:
        """Trigger L1 reflection over the current Claude Code session JSONL.

        Locates the session transcript via ``ctx.session_id`` (populated by
        ``AgentHost`` from the SDK ``SystemMessage(init)``), then runs the
        same delta-cursor reflection loop the ``PreCompact`` hook uses.
        """
        if ctx.memory_store is None:
            return _error("Memory store not available.")
        if not ctx.session_id:
            return _text(
                "Reflection skipped: no active SDK session_id yet. "
                "Run at least one turn with Claude Code first."
            )

        from pip_agent.anthropic_client import build_anthropic_client
        from pip_agent.memory.reflect import reflect_and_persist
        from pip_agent.memory.transcript_source import locate_session_jsonl

        path = locate_session_jsonl(ctx.session_id, prefer_cwd=ctx.workdir)
        if path is None:
            return _text(
                f"Reflection skipped: transcript for session {ctx.session_id[:8]} "
                f"not found under ~/.claude/projects/."
            )

        # Check credentials up front so the response can distinguish
        # "ran + empty" from "skipped for lack of credentials" from "crashed".
        client = build_anthropic_client()
        if client is None:
            return _text(
                "Reflection skipped: no ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN "
                "configured. Set one in `.env` (or as an environment variable) "
                "so reflect can make direct Anthropic calls."
            )

        try:
            start_offset, new_offset, obs_count = reflect_and_persist(
                memory_store=ctx.memory_store,
                session_id=ctx.session_id,
                transcript_path=path,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            return _error(f"Reflection failed: {exc}")

        if obs_count:
            return _text(
                f"Reflection complete: extracted {obs_count} observations."
            )
        if new_offset == start_offset:
            return _text(
                "Reflection complete: no new transcript content since last run."
            )
        return _text(
            "Reflection complete: LLM produced no new observations from the delta."
        )

    return [
        SdkMcpTool(
            name="memory_search",
            description="Search through stored memories and observations about the user.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "top_k": {"type": "integer", "description": "Max results. Default 5."},
                },
                "required": ["query"],
            },
            handler=memory_search,
        ),
        SdkMcpTool(
            name="memory_write",
            description="Store a new observation or note about the user or project.",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The observation to record."},
                    "category": {
                        "type": "string",
                        "description": "Category. Default: observation.",
                    },
                },
                "required": ["content"],
            },
            handler=memory_write,
        ),
        SdkMcpTool(
            name="remember_user",
            description=(
                "Record or update a contact in the workspace-shared "
                "addressbook (<workspace>/.pip/addressbook/<user_id>.md). "
                "This tool is strictly self-directed: "
                "• If the caller is verified (the current <user_query> "
                "carries a user_id), it updates that caller's own "
                "profile only. Passing a different user_id is refused. "
                "• If the caller is unverified, it creates a brand new "
                "contact with a freshly-minted 8-hex user_id and "
                "records the current sender's channel:sender_id. "
                "To note facts ABOUT someone else, use memory_write, "
                "not this tool. All agents share one addressbook."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": (
                            "Optional. Only meaningful for verified callers, "
                            "and must equal your own user_id. Omit to default "
                            "to the current caller. Passing a different id is "
                            "refused."
                        ),
                    },
                    "name": {"type": "string", "description": "The user's real name."},
                    "call_me": {
                        "type": "string",
                        "description": "Preferred name to be called.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone (e.g. 'Asia/Shanghai').",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes (append). English.",
                    },
                },
            },
            handler=remember_user,
        ),
        SdkMcpTool(
            name="lookup_user",
            description=(
                "Read a contact's full profile from the shared "
                "addressbook by user_id. Use this to resolve the "
                "user_id on the current <user_query> into a name, "
                "preferences, timezone, and notes — the addressbook "
                "is NOT auto-injected into context, so call this "
                "whenever you need details beyond the raw id. "
                "Returns the raw markdown profile."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": (
                            "8-hex user_id as seen on <user_query user_id=...>."
                        ),
                    },
                },
                "required": ["user_id"],
            },
            handler=lookup_user,
        ),
        SdkMcpTool(
            name="reflect",
            description=(
                "Trigger a reflection on recent conversation history to consolidate "
                "learnings. Use when a meaningful piece of work is completed."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=reflect,
        ),
    ]


# ---------------------------------------------------------------------------
# Cron tools — scheduled self-messaging via HostScheduler.
# ---------------------------------------------------------------------------


def _cron_tools(ctx: McpContext) -> list[SdkMcpTool]:

    def _require_scheduler() -> str | None:
        if ctx.scheduler is None:
            # McpContext without a scheduler means this tool was
            # invoked outside the running host (unit test / ad-hoc
            # ``reflect`` MCP call). Cron jobs can't fire without one,
            # so refuse explicitly rather than silently accepting a
            # schedule that will never run.
            return "Scheduler not available in this context."
        return None

    async def cron_add(args: dict[str, Any]) -> dict[str, Any]:
        err = _require_scheduler()
        if err:
            return _error(err)
        assert ctx.scheduler is not None
        return _text(
            ctx.scheduler.add_job(
                name=args.get("name", ""),
                schedule_kind=args.get("schedule_kind", ""),
                schedule_config=args.get("schedule_config", {}),
                message=args.get("message", ""),
                channel=ctx.channel.name if ctx.channel else "cli",
                peer_id=ctx.peer_id or "cli-user",
                sender_id=ctx.sender_id,
                agent_id=ctx.memory_store.agent_id if ctx.memory_store else "",
            )
        )

    async def cron_remove(args: dict[str, Any]) -> dict[str, Any]:
        err = _require_scheduler()
        if err:
            return _error(err)
        assert ctx.scheduler is not None
        return _text(ctx.scheduler.remove_job(args.get("job_id", "")))

    async def cron_update(args: dict[str, Any]) -> dict[str, Any]:
        err = _require_scheduler()
        if err:
            return _error(err)
        job_id = args.get("job_id", "")
        if not job_id:
            return _error("'job_id' is required.")
        assert ctx.scheduler is not None
        # Strip ``job_id`` before splatting — it's already passed
        # positionally, and leaving it in would raise
        # "multiple values for argument 'job_id'" the moment anyone
        # called this tool in anger.
        updates = {k: v for k, v in args.items() if k != "job_id"}
        return _text(ctx.scheduler.update_job(job_id, **updates))

    async def cron_list(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.scheduler is None:
            return _text("No scheduled tasks (scheduler not wired).")
        jobs = ctx.scheduler.list_jobs()
        if not jobs:
            return _text("No scheduled tasks.")
        return _text(json.dumps(jobs, indent=2, ensure_ascii=False))

    _SCHED_ENUM = {"type": "string", "enum": ["at", "every", "cron"]}

    return [
        SdkMcpTool(
            name="cron_add",
            description="Create a scheduled background task.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "schedule_kind": _SCHED_ENUM,
                    "schedule_config": {"type": "object"},
                    "message": {"type": "string"},
                },
                "required": ["name", "schedule_kind", "schedule_config", "message"],
            },
            handler=cron_add,
        ),
        SdkMcpTool(
            name="cron_remove",
            description="Remove a scheduled task by ID.",
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            handler=cron_remove,
        ),
        SdkMcpTool(
            name="cron_update",
            description="Modify a scheduled task.",
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "name": {"type": "string"},
                    "schedule_kind": _SCHED_ENUM,
                    "schedule_config": {"type": "object"},
                    "message": {"type": "string"},
                },
                "required": ["job_id"],
            },
            handler=cron_update,
        ),
        SdkMcpTool(
            name="cron_list",
            description="List all scheduled tasks.",
            input_schema={"type": "object", "properties": {}},
            handler=cron_list,
        ),
    ]


# ---------------------------------------------------------------------------
# Channel tools — send_file
#
# Why this is an MCP tool and not a slash command
# -----------------------------------------------
# Slash commands are operator affordances (bind, status, memory, …);
# they live outside the LLM's control loop. ``send_file`` is the
# opposite: the LLM drives it based on a conversational request
# ("can you send me the report?"), using file paths it discovered
# via Read/Glob. Putting it on the tool surface lets the LLM compose
# it naturally with its other file operations.
#
# Why not plumb it through native Claude Code Bash
# ------------------------------------------------
# The channel (WeCom, WeChat) lives inside the Pip-Boy host process
# and its websocket connection. A sub-shell has no way to reach it.
# ---------------------------------------------------------------------------


# 50 MB — the WeCom file-message upper bound is 20 MB per API docs, but
# the host still wants a hard stop well before a naive payload could
# blow out the process heap. 50 MB leaves WeCom to reject oversized
# inputs with its own error code (which we surface) rather than us
# guessing and getting stale when the upstream limit moves.
_SEND_FILE_MAX_BYTES = 50 * 1024 * 1024


def _channel_tools(ctx: McpContext) -> list[SdkMcpTool]:

    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        ch = ctx.channel
        # CLI has no file-send surface by design; returning early with a
        # helpful message is strictly better than letting the LLM retry
        # the tool because the response was empty.
        if ch is None or ch.name == "cli":
            return _error(
                "send_file is only available on messaging channels "
                "(e.g. WeCom). Not available on CLI.",
            )

        raw_path = args.get("path", "")
        if not raw_path or not isinstance(raw_path, str):
            return _error("'path' is required and must be a string.")

        path = Path(raw_path)
        # Relative paths resolve against the agent's workdir rather
        # than Python's CWD so the LLM gets the same scope it sees
        # from Read/Write — consistency is worth more than a tiny
        # bit of flexibility here.
        if not path.is_absolute():
            path = ctx.workdir / path

        # Path containment guard (plan M5 / defence-in-depth).
        # The LLM chooses ``path``; a prompt-injection attempt could
        # try to exfiltrate arbitrary host files via ``/etc/passwd``,
        # ``C:\\Windows\\...`` or ``../../.ssh/id_rsa``. We clamp the
        # whole tool to files that resolve inside the agent's
        # workdir, which is where ``Read`` / ``Write`` also operate.
        # Use ``resolve()`` (not ``absolute()``) so symlinks pointing
        # outside the workdir are also rejected. ``strict=False`` is
        # needed because the file might not exist yet — we check
        # ``is_file()`` separately below, which returns a cleaner
        # error message than ``resolve(strict=True)``.
        try:
            resolved_path = path.resolve(strict=False)
            resolved_workdir = ctx.workdir.resolve(strict=False)
        except OSError as exc:
            return _error(f"Cannot resolve path {raw_path!r}: {exc}")

        try:
            resolved_path.relative_to(resolved_workdir)
        except ValueError:
            return _error(
                "Path escapes workdir. ``send_file`` is restricted "
                "to files inside the agent's workdir for safety.",
            )

        # All downstream operations use the resolved path so symlink
        # tricks (``workdir/foo -> /etc/passwd``) don't open a hole
        # between the containment check and ``read_bytes``.
        path = resolved_path

        if not path.is_file():
            return _error(f"File not found: {path}")

        try:
            size = path.stat().st_size
        except OSError as exc:
            return _error(f"Cannot stat {path}: {exc}")

        if size > _SEND_FILE_MAX_BYTES:
            mb = _SEND_FILE_MAX_BYTES // (1024 * 1024)
            return _error(
                f"File too large ({size} bytes). "
                f"Hard host limit is {mb} MB.",
            )

        peer = ctx.peer_id
        if not peer:
            return _error(
                "No peer_id in context — cannot determine recipient.",
            )

        try:
            file_data = path.read_bytes()
        except OSError as exc:
            return _error(f"Cannot read {path}: {exc}")

        caption = args.get("caption", "") or ""

        # Images get routed through ``send_image`` so the recipient
        # sees an inline preview instead of a filename tile — detection
        # is by magic bytes (not extension) so a .jpg that is really a
        # zip doesn't get mis-routed. Channels that don't override
        # ``send_image`` (e.g. WeChat today) fall back through the
        # ``ok is False`` branch below, which retries via ``send_file``
        # so the LLM still gets delivery instead of a hard failure.
        from pip_agent.channels import _detect_image_mime

        is_image = bool(_detect_image_mime(file_data))

        # ``ch.send_*`` is synchronous but internally dispatches into
        # the channel's own event loop — it's a blocking wait. Offload
        # to a thread so the MCP handler does not stall the SDK event
        # loop (which is also processing streaming assistant output for
        # the same turn).
        # Snapshot ``ctx.account_id`` at call time so the same bot identity
        # that received this turn's inbound is the one that sends. Rebinding
        # ``ctx.account_id`` mid-turn (across concurrent turns in a streaming
        # session) could otherwise race the closure.
        account_id = ctx.account_id

        def _blocking_send_image() -> bool:
            with ch.send_lock:
                return ch.send_image(
                    peer, file_data, caption=caption, account_id=account_id,
                )

        def _blocking_send_file() -> bool:
            with ch.send_lock:
                return ch.send_file(
                    peer, file_data,
                    filename=path.name, caption=caption,
                    account_id=account_id,
                )

        sent_as = "image" if is_image else "file"
        try:
            if is_image:
                ok = await asyncio.to_thread(_blocking_send_image)
                # WeChat + base Channel return False from send_image
                # (no override). Don't let the LLM's "send me that
                # image" intent fail silently on those channels —
                # gracefully downgrade to send_file, which at least
                # delivers the bytes.
                if not ok:
                    sent_as = "file"
                    ok = await asyncio.to_thread(_blocking_send_file)
            else:
                ok = await asyncio.to_thread(_blocking_send_file)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_file crashed for %s", path)
            return _error(f"send_file crashed: {exc}")

        if ok:
            return _text(
                f"Sent {path.name} ({size} bytes) as {sent_as}."
            )
        return _error(f"Channel refused to send {path.name}.")

    return [
        SdkMcpTool(
            name="send_file",
            description=(
                "Send a local file to the current conversation through "
                "the active messaging channel. Reads the file from disk "
                "and delivers it via the channel (e.g. WeCom). Image "
                "files (PNG/JPEG/GIF/WEBP) are auto-detected by magic "
                "bytes and sent as inline images for preview; all "
                "other file types are sent as attachments. Not "
                "available on CLI. Relative paths resolve against the "
                "agent's workdir."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or workdir-relative path to the file."
                        ),
                    },
                    "caption": {
                        "type": "string",
                        "description": (
                            "Optional text sent alongside the file."
                        ),
                    },
                },
                "required": ["path"],
            },
            handler=send_file,
        ),
    ]


# ---------------------------------------------------------------------------
# Editor tools — open_file
#
# Why this is an MCP tool and not a slash command
# -----------------------------------------------
# Same logic as ``send_file``: the LLM drives it based on the shape of
# the conversation ("draft the config for me", "let me edit that plan
# myself"), composing it with paths it got from Read/Write/Glob.
#
# Why CLI-only
# ------------
# Launching ``$VISUAL``/``$EDITOR`` only makes sense when the agent and
# the user share a machine. Over a remote messaging channel (WeCom,
# WeChat) the "editor on the host" would open on the host, not where
# the user is sitting — surprising at best, confusing at worst. The
# symmetric inverse of ``send_file``'s "remote channels only" rule.
#
# Why no timeout
# --------------
# SDK has no tool-response timeout (see ``streaming_session.py`` /
# ``agent_runner.py``); handlers run as long as they need, matching
# Bash contract. If the user walks off mid-edit, the turn sits on the
# editor until they return.
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        # Chunked read so a multi-GB file doesn't blow the heap. The
        # editor path is unlikely to see huge files, but cheap defense.
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Editors that take over the terminal (curses / raw mode). Only these
# need ``App.suspend()`` — GUI editors (notepad, code, subl, gedit …)
# have their own window and must NOT trigger suspend, or the TUI
# vanishes for no reason and confuses the user.
_TTY_EDITORS = frozenset({"vim", "nvim", "vi", "nano", "pico"})


def _needs_tty(argv: list[str]) -> bool:
    """Return True if ``argv[0]`` is a TTY-taking editor.

    ``emacs`` is special — the GUI build needs no TTY, but ``emacs -nw``
    / ``--no-window-system`` / ``-t`` run in the terminal and do.
    """
    name = Path(argv[0]).stem.lower()
    if name in _TTY_EDITORS:
        return True
    if name == "emacs":
        return any(a in ("-nw", "-t", "--no-window-system") for a in argv[1:])
    return False


def _editor_tools(ctx: McpContext) -> list[SdkMcpTool]:

    async def open_file(args: dict[str, Any]) -> dict[str, Any]:
        ch = ctx.channel
        # Inverse of ``send_file``: editor launch only works when the
        # user is physically at the machine running Pip-Boy.
        if ch is not None and ch.name != "cli":
            return _error("open_file is only available on CLI.")

        raw_path = args.get("path", "")
        if not raw_path or not isinstance(raw_path, str):
            return _error("'path' is required and must be a string.")

        create_if_missing = bool(args.get("create_if_missing", False))

        # ``expanduser()`` for ``~``; ``resolve(strict=False)`` for a
        # canonical absolute path that still works when the file does
        # not yet exist (we handle the missing case below).
        try:
            path = Path(raw_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            return _error(f"Cannot resolve path {raw_path!r}: {exc}")

        if not path.exists():
            if create_if_missing:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                except OSError as exc:
                    return _error(
                        f"Failed to create {path}: {exc}"
                    )
            else:
                return _error(
                    f"File does not exist: {path}. "
                    "Pass create_if_missing=true to create it."
                )
        elif path.is_dir():
            return _error(f"Path is a directory, not a file: {path}")

        # Snapshot before. sha256 is the authoritative "did content
        # change?" signal; mtime/size alone lie (some editors touch
        # mtime without changing bytes, some update atomically with
        # matching mtime). Reading twice is cheap.
        stat_before = path.stat()
        size_before = stat_before.st_size
        mtime_before = stat_before.st_mtime
        try:
            hash_before = _sha256_file(path)
        except OSError as exc:
            return _error(f"Cannot read {path}: {exc}")

        # Resolve editor. ``shlex.split`` lets users set
        # ``EDITOR="code --wait"`` or ``VISUAL="vim -p"``.
        editor_env = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor_env:
            try:
                argv = shlex.split(editor_env) + [str(path)]
            except ValueError as exc:
                return _error(
                    f"Malformed $VISUAL/$EDITOR {editor_env!r}: {exc}"
                )
        else:
            default = "notepad" if sys.platform == "win32" else "nano"
            argv = [default, str(path)]

        # Resolve the executable against PATH ourselves. On Windows,
        # ``asyncio.create_subprocess_exec`` calls ``CreateProcess``
        # directly, which does NOT honour ``PATHEXT`` — so a bare
        # ``code`` fails with FileNotFoundError even though the real
        # file is ``code.cmd`` sitting on PATH. ``shutil.which`` does
        # the PATHEXT dance on Windows and is a no-op cost elsewhere.
        resolved = shutil.which(argv[0])
        if resolved is None:
            return _error(
                f"Editor not found: {argv[0]!r}. "
                "Set $VISUAL or $EDITOR to an installed editor."
            )
        argv[0] = resolved

        # Launch. Two rules:
        #
        # 1. Use ``asyncio.create_subprocess_exec`` so the event loop
        #    keeps running while we wait — ``subprocess.run`` in a
        #    worker thread works for the wait itself, but ``suspend()``
        #    mutates Textual driver state (signals, input pump, render
        #    loop) that is only safe from the main event-loop thread.
        #    Off-thread suspend drops the TUI and never recovers.
        #
        # 2. Only enter ``app.suspend()`` for TTY-taking editors
        #    (vim/nano/…). GUI editors (notepad, code) have their own
        #    window; suspending the TUI would just make it disappear
        #    and look like a crash to the user.
        async def _launch() -> int:
            proc = await asyncio.create_subprocess_exec(*argv)
            return await proc.wait()

        tui_app = ctx.tui_app
        want_suspend = (
            tui_app is not None
            and hasattr(tui_app, "suspend")
            and _needs_tty(argv)
        )
        try:
            if want_suspend:
                with tui_app.suspend():
                    returncode = await _launch()
            else:
                returncode = await _launch()
        except FileNotFoundError:
            return _error(
                f"Editor not found: {argv[0]!r}. "
                "Set $VISUAL or $EDITOR to an installed editor."
            )
        except OSError as exc:
            return _error(f"Editor launch failed: {exc}")

        # Snapshot after. File might have been deleted by the editor
        # (some workflows rewrite via rename) — treat "gone" as error.
        if not path.exists():
            return _error(
                f"File disappeared during edit: {path} "
                f"(editor exit={returncode})"
            )

        stat_after = path.stat()
        size_after = stat_after.st_size
        mtime_after = stat_after.st_mtime
        try:
            hash_after = _sha256_file(path)
        except OSError as exc:
            return _error(f"Cannot read {path} after edit: {exc}")

        status = (
            "user_closed_with_modification"
            if hash_after != hash_before
            else "user_closed_without_modification"
        )
        payload = {
            "status": status,
            "path": str(path),
            "size_before": size_before,
            "size_after": size_after,
            "mtime_before": mtime_before,
            "mtime_after": mtime_after,
        }
        return _text(json.dumps(payload))

    return [
        SdkMcpTool(
            name="open_file",
            description=(
                "Open a local text file in the user's editor "
                "($VISUAL/$EDITOR, fallback notepad/nano) and wait "
                "until they close it. Returns a status of "
                "'user_closed_with_modification' or "
                "'user_closed_without_modification' (content-hash "
                "comparison) plus size/mtime before+after.\n\n"
                "Use this when: you've drafted a plan/config/note "
                "and want the user to review and tweak before you "
                "proceed (prefer this over 'please check the file "
                "and tell me what to change'); the user asks to "
                "edit something themselves ('let me fix that', "
                "'open X in my editor'); content is long enough "
                "that in-chat iteration would be tedious (>~50 "
                "lines, dense config, etc.).\n\n"
                "Do NOT use for small edits you can make yourself "
                "with Edit/Write, or files the user hasn't "
                "expressed interest in reviewing.\n\n"
                "After the user closes the editor and status is "
                "'user_closed_with_modification', Read the file to "
                "see what they wrote before continuing.\n\n"
                "Set create_if_missing=true to create an empty file "
                "(with parent directories) when the path doesn't "
                "exist — useful for new drafts. Default false.\n\n"
                "Only available on local CLI channel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or ~-expandable path to the "
                            "text file."
                        ),
                    },
                    "create_if_missing": {
                        "type": "boolean",
                        "description": (
                            "Create empty file (with parent dirs) "
                            "if not exists. Default false."
                        ),
                        "default": False,
                    },
                },
                "required": ["path"],
            },
            handler=open_file,
        ),
    ]


# ---------------------------------------------------------------------------
# Plugin tools — agent self-service for Claude Code plugins / marketplaces
#
# Why only additive operations are surfaced
# -----------------------------------------
# Claude Code's plugin / marketplace state is shared with the user's
# global config (``~/.claude/`` for ``user`` scope, ``<cwd>/.claude/``
# for project / local). Letting the agent ``uninstall`` / ``disable`` /
# ``marketplace remove`` would let a single hostile turn wipe a user's
# carefully curated plugin set. So the agent only gets:
#
#   * read (``plugin_list``, ``plugin_search``, ``plugin_marketplace_list``)
#   * additive write (``plugin_install``, ``plugin_marketplace_add``)
#
# Removal stays a human decision via ``/plugin`` host commands.
# This mirrors the Pip-Boy AGENTS.md principle "CONSTRAIN, SURFACE,
# NEVER COACH": expose the eyes (list/search) and hands (install) the
# agent needs, withhold the destructive ones.
#
# Scope (``user`` / ``project`` / ``local``)
# ------------------------------------------
# The schema offers all three values via a JSON ``enum`` so the SDK
# advertises them to the agent automatically. When the agent picks
# ``project`` or ``local``, the subprocess uses ``ctx.workdir`` so
# the resulting ``.claude/`` files land beside *this* agent's project
# — which is what "this agent installs a plugin for itself" should mean.
# ---------------------------------------------------------------------------


_PLUGIN_SCOPE_ENUM = {
    "type": "string",
    "enum": ["user", "project", "local"],
    "description": (
        "Where to record the change. 'user' = global "
        "(~/.claude/settings.json). 'project' = this agent's "
        "<cwd>/.claude/settings.json (gitable). 'local' = this "
        "agent's <cwd>/.claude/settings.local.json (gitignored). "
        "Defaults to 'user'."
    ),
}


def _plugin_tools(ctx: McpContext) -> list[SdkMcpTool]:

    async def plugin_list_tool(args: dict[str, Any]) -> dict[str, Any]:
        from pip_agent import plugins as plug

        available = bool(args.get("available", False))
        try:
            items = await plug.plugin_list(available=available, cwd=ctx.workdir)
        except plug.PluginsCLINotFound as exc:
            return _error(str(exc))
        except plug.PluginsCLIError as exc:
            return _error(str(exc))
        return _text(json.dumps(items, indent=2, ensure_ascii=False))

    async def plugin_search_tool(args: dict[str, Any]) -> dict[str, Any]:
        from pip_agent import plugins as plug

        query = (args.get("query") or "").strip()
        if not query:
            return _error("'query' is required.")
        try:
            items = await plug.plugin_search(query, cwd=ctx.workdir)
        except plug.PluginsCLINotFound as exc:
            return _error(str(exc))
        except plug.PluginsCLIError as exc:
            return _error(str(exc))
        if not items:
            return _text(
                f"No plugins matched '{query}'. "
                "Use plugin_marketplace_list to see configured sources, "
                "or plugin_marketplace_add to register a new one."
            )
        return _text(json.dumps(items, indent=2, ensure_ascii=False))

    async def plugin_install_tool(args: dict[str, Any]) -> dict[str, Any]:
        from pip_agent import plugins as plug

        spec = (args.get("spec") or "").strip()
        if not spec:
            return _error("'spec' is required (e.g. 'web-search' or 'web-search@anthropic').")
        scope = args.get("scope", "user")
        if scope not in ("user", "project", "local"):
            return _error(
                f"Invalid scope '{scope}'. Valid: user, project, local."
            )
        try:
            out, err, _ = await plug.plugin_install(
                spec, scope=scope, cwd=ctx.workdir,  # type: ignore[arg-type]
            )
        except plug.PluginsCLINotFound as exc:
            return _error(str(exc))
        except plug.PluginsCLIError as exc:
            return _error(str(exc))
        body = (out or err).strip() or f"Installed {spec} (scope={scope})."
        return _text(body)

    async def plugin_marketplace_add_tool(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        from pip_agent import plugins as plug

        source = (args.get("source") or "").strip()
        if not source:
            return _error(
                "'source' is required (gh-repo like 'owner/name', "
                "an https git url, or a local path)."
            )
        scope = args.get("scope", "user")
        if scope not in ("user", "project", "local"):
            return _error(
                f"Invalid scope '{scope}'. Valid: user, project, local."
            )
        try:
            out, err, _ = await plug.marketplace_add(
                source, scope=scope, cwd=ctx.workdir,  # type: ignore[arg-type]
            )
        except plug.PluginsCLINotFound as exc:
            return _error(str(exc))
        except plug.PluginsCLIError as exc:
            return _error(str(exc))
        body = (out or err).strip() or (
            f"Added marketplace {source} (scope={scope})."
        )
        return _text(body)

    async def plugin_marketplace_list_tool(
        _args: dict[str, Any],
    ) -> dict[str, Any]:
        from pip_agent import plugins as plug

        try:
            items = await plug.marketplace_list(cwd=ctx.workdir)
        except plug.PluginsCLINotFound as exc:
            return _error(str(exc))
        except plug.PluginsCLIError as exc:
            return _error(str(exc))
        if not items:
            return _text(
                "No marketplaces configured. Use plugin_marketplace_add "
                "to register one (e.g. 'anthropics/claude-code')."
            )
        return _text(json.dumps(items, indent=2, ensure_ascii=False))

    return [
        SdkMcpTool(
            name="plugin_list",
            description=(
                "List Claude Code plugins. By default returns plugins "
                "currently installed; pass available=true to list every "
                "plugin offered by the configured marketplaces. Each "
                "entry includes name, scope, marketplace source, and "
                "(when installed) enabled state. Use this to check "
                "what capabilities you already have before asking the "
                "user to install something new."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "available": {
                        "type": "boolean",
                        "description": (
                            "When true, return every plugin offered by "
                            "configured marketplaces instead of only "
                            "installed ones. Default false."
                        ),
                    },
                },
            },
            handler=plugin_list_tool,
        ),
        SdkMcpTool(
            name="plugin_search",
            description=(
                "Search marketplace-available plugins by case-insensitive "
                "substring across name, description, and tags. Returns "
                "the same shape as plugin_list(available=true) but "
                "filtered. Use to discover a plugin that delivers a "
                "capability the user is asking for (e.g. 'pdf', "
                "'web search', 'figma')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Substring to match. Empty / missing is "
                            "rejected."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=plugin_search_tool,
        ),
        SdkMcpTool(
            name="plugin_install",
            description=(
                "Install a Claude Code plugin from a configured "
                "marketplace. The plugin's commands, skills, and any "
                "MCP servers it bundles become available on the next "
                "agent turn (no restart needed). Marketplaces must "
                "already be added; if a plugin you want isn't found, "
                "use plugin_marketplace_add first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": (
                            "Plugin name, optionally suffixed with "
                            "'@<marketplace>' to disambiguate when the "
                            "name appears in multiple sources."
                        ),
                    },
                    "scope": _PLUGIN_SCOPE_ENUM,
                },
                "required": ["spec"],
            },
            handler=plugin_install_tool,
        ),
        SdkMcpTool(
            name="plugin_marketplace_add",
            description=(
                "Register a Claude Code plugin marketplace so its "
                "plugins become installable. Source can be a GitHub "
                "'owner/repo' slug, an https git URL, or a local path. "
                "The official catalogue lives at 'anthropics/claude-code'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "GitHub 'owner/repo', https git URL, or "
                            "local filesystem path."
                        ),
                    },
                    "scope": _PLUGIN_SCOPE_ENUM,
                },
                "required": ["source"],
            },
            handler=plugin_marketplace_add_tool,
        ),
        SdkMcpTool(
            name="plugin_marketplace_list",
            description=(
                "List currently-configured plugin marketplaces. Use "
                "before plugin_search / plugin_install to confirm "
                "which sources are reachable."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=plugin_marketplace_list_tool,
        ),
    ]


# ---------------------------------------------------------------------------
# Web tools — canonical ``web_fetch`` / ``web_search`` (shadow CC built-ins)
# ---------------------------------------------------------------------------


def _web_tools(ctx: McpContext) -> list[SdkMcpTool]:
    """Expose ``web_fetch`` and ``web_search`` backed by :mod:`pip_agent.web`.

    ``ctx`` is unused today — kept in the signature for parity with the
    other tool groups so future calls can be scoped to e.g.
    ``ctx.workdir`` for cache files without touching the call site.
    """
    del ctx  # unused

    async def web_fetch(args: dict[str, Any]) -> dict[str, Any]:
        from pip_agent.web import fetch_url

        url = (args.get("url") or "").strip()
        if not url:
            return _error("'url' is required.")
        try:
            max_chars = int(args.get("max_chars", 50_000))
        except (TypeError, ValueError):
            return _error("'max_chars' must be an integer.")
        if max_chars <= 0:
            return _error("'max_chars' must be > 0.")

        result = await fetch_url(url, max_chars=max_chars)
        if not result.get("ok"):
            status = result.get("status")
            status_part = f" (status={status})" if status is not None else ""
            return _error(
                f"web_fetch failed for {result.get('url', url)}"
                f"{status_part}: {result.get('error', 'unknown error')}"
            )

        header_lines = [
            f"URL: {result['url']}",
            f"Status: {result['status']}",
            f"Content-Type: {result.get('content_type') or '?'}",
        ]
        if result.get("truncated"):
            header_lines.append(
                f"(content truncated to {max_chars} chars — request a "
                "larger max_chars or a more specific URL if you need more)"
            )
        body = "\n".join(header_lines) + "\n\n" + (result.get("content") or "")
        return _text(body)

    async def web_search(args: dict[str, Any]) -> dict[str, Any]:
        from pip_agent.web import search_web

        query = (args.get("query") or "").strip()
        if not query:
            return _error("'query' is required.")
        try:
            max_results = int(args.get("max_results", 5))
        except (TypeError, ValueError):
            return _error("'max_results' must be an integer.")
        if max_results <= 0:
            return _error("'max_results' must be > 0.")

        result = await search_web(query, max_results=max_results)
        if not result.get("ok"):
            return _error(
                f"web_search failed: {result.get('error', 'unknown error')}"
            )

        hits = result.get("results") or []
        if not hits:
            return _text(
                f"Provider: {result.get('provider', '?')}\n"
                f"Query: {query}\n\n(no results)"
            )
        lines = [
            f"Provider: {result.get('provider', '?')}",
            f"Query: {query}",
            "",
        ]
        for i, hit in enumerate(hits, start=1):
            title = hit.get("title") or "(untitled)"
            url = hit.get("url") or ""
            snippet = (hit.get("snippet") or "").strip()
            lines.append(f"{i}. {title}")
            if url:
                lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")
        return _text("\n".join(lines).rstrip() + "\n")

    return [
        SdkMcpTool(
            name="web_fetch",
            description=(
                "Fetch a URL and return its main text content. HTML pages "
                "are reduced to article-body markdown; JSON / plain-text / "
                "XML come back verbatim. Follows redirects, 30 s timeout, "
                "5 MB response cap. Returns an error string on non-2xx, "
                "timeout, oversized payload, or transport failure."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "Absolute http(s) URL to fetch."
                        ),
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Maximum characters of content to return. "
                            "Default 50000. The full response is always "
                            "downloaded; this only trims the returned "
                            "extract."
                        ),
                    },
                },
                "required": ["url"],
            },
            handler=web_fetch,
        ),
        SdkMcpTool(
            name="web_search",
            description=(
                "Search the web and return a ranked list of "
                "{title, url, snippet} results. Uses Tavily when "
                "TAVILY_API_KEY is configured (richer, more relevant "
                "results); falls back to DuckDuckGo (free, no key "
                "required) when the key is absent or Tavily errors. "
                "Follow up with web_fetch on the returned URLs when "
                "snippets alone aren't enough."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Maximum number of results. Default 5, "
                            "capped at 20."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=web_search,
        ),
    ]
