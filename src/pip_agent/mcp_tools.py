"""In-process MCP server exposing Pip-Boy's unique capabilities to the SDK.

Only tools that Claude Code does NOT provide natively are exposed here.
Everything else (Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch,
Task, TodoWrite, Skill, …) is delegated to Claude Code's built-ins.

Tools currently exposed:

* Memory: ``memory_search``, ``memory_write``, ``remember_user``, ``reflect``
* Cron: ``cron_add``, ``cron_remove``, ``cron_update``, ``cron_list``
* Channel: ``send_file``
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
    """

    memory_store: MemoryStore | None = None
    workdir: Path = field(default_factory=Path.cwd)
    model: str = ""
    session_id: str = ""
    scheduler: HostScheduler | None = None
    channel: Channel | None = None
    peer_id: str = ""
    sender_id: str = ""


def build_mcp_server(ctx: McpContext) -> McpSdkServerConfig:
    """Create the in-process MCP server with all Pip-Boy-unique tools."""
    tools = _memory_tools(ctx) + _cron_tools(ctx) + _channel_tools(ctx)
    return create_sdk_mcp_server("pip", tools=tools)


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
        sid = args.get("sender_id") or ctx.sender_id
        if sid and ch_name and sid.startswith(f"{ch_name}:"):
            sid = sid[len(ch_name) + 1:]
        result = ctx.memory_store.update_user_profile(
            sender_id=sid, channel=ch_name,
            name=args.get("name", ""),
            call_me=args.get("call_me", ""),
            timezone=args.get("timezone", ""),
            notes=args.get("notes", ""),
        )
        return _text(result)

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

        path = locate_session_jsonl(ctx.session_id)
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
                model=ctx.model,
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
                "Remember or update a user's identity. Use when an unverified "
                "user reveals who they are, or to update a verified user's info."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sender_id": {
                        "type": "string",
                        "description": "Raw sender_id without channel prefix.",
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
# Cron tools — wiring to HostScheduler lands in Phase 5.
# ---------------------------------------------------------------------------


def _cron_tools(ctx: McpContext) -> list[SdkMcpTool]:

    def _require_scheduler() -> str | None:
        if ctx.scheduler is None:
            return "Scheduler not wired (pending Phase 5)."
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
        return _text(ctx.scheduler.update_job(job_id, **args))

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

        # ``ch.send_file`` is synchronous but internally dispatches into
        # the channel's own event loop — it's a blocking wait. Offload
        # to a thread so the MCP handler does not stall the SDK event
        # loop (which is also processing streaming assistant output for
        # the same turn).
        def _blocking_send() -> bool:
            with ch.send_lock:
                return ch.send_file(
                    peer, file_data,
                    filename=path.name, caption=caption,
                )

        try:
            ok = await asyncio.to_thread(_blocking_send)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_file crashed for %s", path)
            return _error(f"send_file crashed: {exc}")

        if ok:
            return _text(f"File sent: {path.name} ({size} bytes)")
        return _error(f"Channel refused to send {path.name}.")

    return [
        SdkMcpTool(
            name="send_file",
            description=(
                "Send a local file to the current conversation through "
                "the active messaging channel. Reads the file from disk "
                "and delivers it via the channel (e.g. WeCom). Not "
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
