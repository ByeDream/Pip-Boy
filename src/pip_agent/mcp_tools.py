"""In-process MCP server exposing Pip-Boy's unique capabilities to the Claude Agent SDK.

Only tools that the SDK does NOT provide natively are exposed here.
Basic file/shell/web tools (Bash, Read, Write, Edit, Glob, Grep, WebSearch,
WebFetch) are handled by the SDK's built-in tool implementations.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

if TYPE_CHECKING:
    import anthropic

    from pip_agent.background import BackgroundTaskManager
    from pip_agent.channels import Channel
    from pip_agent.memory import MemoryStore
    from pip_agent.profiler import Profiler
    from pip_agent.scheduler import BackgroundScheduler
    from pip_agent.worktree import WorktreeManager

log = logging.getLogger(__name__)


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


@dataclass
class McpContext:
    """Mutable runtime context shared by all MCP tool handlers.

    Updated by the host layer before each ``query()`` call to reflect
    the effective agent, session, and channel.
    """

    memory_store: MemoryStore | None = None
    worktree_manager: WorktreeManager | None = None
    bg_manager: BackgroundTaskManager | None = None
    profiler: Profiler | None = None
    client: anthropic.Anthropic | None = None
    workdir: Path = field(default_factory=Path.cwd)
    model: str = ""
    transcripts_dir: Path | None = None
    scheduler: BackgroundScheduler | None = None
    channel: Channel | None = None
    peer_id: str = ""
    sender_id: str = ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_mcp_server(ctx: McpContext) -> McpSdkServerConfig:
    """Create an in-process MCP server with all Pip-Boy-unique tools.

    The returned config is passed to ``ClaudeAgentOptions.mcp_servers``.
    Tool handlers close over *ctx* so state changes are visible immediately.
    """
    tools = (
        _memory_tools(ctx)
        + _cron_tools(ctx)
    )
    return create_sdk_mcp_server("pip", tools=tools)


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


def _memory_tools(ctx: McpContext) -> list[SdkMcpTool]:

    async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.memory_store is None:
            return _error("Memory store not available.")
        query = args.get("query", "").strip()
        if not query:
            return _error("'query' is required.")
        top_k = int(args.get("top_k", 5))
        results = ctx.memory_store.search(query, top_k=top_k)
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
        if ctx.memory_store is None or ctx.client is None:
            return _error("Reflection not available.")
        from pip_agent.memory.reflect import reflect as do_reflect

        if ctx.transcripts_dir is not None:
            state = ctx.memory_store.load_state()
            since = state.get("last_reflect_transcript_ts", 0)
            observations = do_reflect(
                ctx.client, ctx.transcripts_dir, ctx.memory_store.agent_id,
                since, model=ctx.model,
            )
            if observations:
                ctx.memory_store.write_observations(observations)
            latest = 0
            if ctx.transcripts_dir.is_dir():
                for fp in ctx.transcripts_dir.glob("*.json"):
                    try:
                        ts = int(fp.stem)
                    except ValueError:
                        continue
                    if ts > latest:
                        latest = ts
            if latest > 0:
                state["last_reflect_transcript_ts"] = latest
            state["last_reflect_at"] = time.time()
            ctx.memory_store.save_state(state)
            if observations:
                return _text(f"Reflection complete: extracted {len(observations)} observations.")
        return _text("Reflection complete: no new observations found.")

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
                    "name": {
                        "type": "string",
                        "description": "The user's real name.",
                    },
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
# Cron tools
# ---------------------------------------------------------------------------


def _cron_tools(ctx: McpContext) -> list[SdkMcpTool]:

    def _cs():
        if ctx.scheduler is None:
            return None
        return ctx.scheduler.get_cron_service()

    async def cron_add(args: dict[str, Any]) -> dict[str, Any]:
        cs = _cs()
        if cs is None:
            return _error("Cron service not available.")
        result = cs.add_job(
            name=args.get("name", ""),
            schedule_kind=args.get("schedule_kind", ""),
            schedule_config=args.get("schedule_config", {}),
            message=args.get("message", ""),
            channel=ctx.channel.name if ctx.channel else "cli",
            peer_id=ctx.peer_id or "cli-user",
            sender_id=ctx.sender_id,
            agent_id=ctx.memory_store.agent_id if ctx.memory_store else "",
        )
        return _text(result)

    async def cron_remove(args: dict[str, Any]) -> dict[str, Any]:
        cs = _cs()
        if cs is None:
            return _error("Cron service not available.")
        return _text(cs.remove_job(args.get("job_id", "")))

    async def cron_update(args: dict[str, Any]) -> dict[str, Any]:
        cs = _cs()
        if cs is None:
            return _error("Cron service not available.")
        job_id = args.get("job_id", "")
        if not job_id:
            return _error("'job_id' is required.")
        return _text(cs.update_job(job_id, **args))

    async def cron_list(args: dict[str, Any]) -> dict[str, Any]:
        cs = _cs()
        if cs is None:
            return _text("No scheduled tasks.")
        jobs = cs.list_jobs()
        if not jobs:
            return _text("No scheduled tasks.")
        return _text(json.dumps(jobs, indent=2, ensure_ascii=False))

    _SCHED_ENUM = {
        "type": "string", "enum": ["at", "every", "cron"],
    }

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
                "required": [
                    "name", "schedule_kind",
                    "schedule_config", "message",
                ],
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
