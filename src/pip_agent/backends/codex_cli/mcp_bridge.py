"""STDIO MCP server bridging Pip-Boy's tools to the Codex SDK.

The Codex app-server spawns MCP servers as child processes and
communicates over stdin/stdout using the standard MCP JSON-RPC
protocol.  This module reuses the exact same tool definitions and
handler functions from ``pip_agent.mcp_tools`` — no schema or logic
duplication.

Registration: add the following to ``~/.codex/config.toml``::

    [mcp_servers.pip]
    type = "stdio"
    command = ["python", "-m", "pip_agent.backends.codex_cli.mcp_bridge"]

The server is started by the Codex app-server on demand and torn down
when the Codex client closes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _read_bridge_ctx(pip_dir: Path) -> dict[str, str]:
    """Read per-turn identity from the context file written by the host.

    The host's ``CodexStreamingSession`` writes this file before each
    turn so the long-lived bridge process always sees fresh identity
    data — env vars alone are stale after the initial spawn.
    """
    ctx_path = pip_dir / "codex_bridge_ctx.json"
    try:
        if ctx_path.is_file():
            return json.loads(ctx_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.debug("Failed to read bridge context file", exc_info=True)
    return {}


def _build_mcp_ctx() -> Any:
    """Construct a ``McpContext`` for tool handlers in the STDIO subprocess.

    The STDIO MCP bridge runs as a child process of the Codex app-server,
    so it cannot share the host's live in-process state (channel objects,
    TUI app, asyncio-based scheduler).  We reconstruct what we can from
    disk and a context file written by the host before each turn:

    * ``memory_store`` — loaded from ``.pip/`` (full read/write)
    * ``workdir`` — from ``PIP_WORKDIR`` env var
    * ``sender_id`` / ``peer_id`` / ``user_id`` / ``session_id`` /
      ``account_id`` — from ``.pip/codex_bridge_ctx.json`` (primary)
      with env-var fallback for backwards compatibility
    Limitations (documented in dual-backend-contract.md §3.6):
    * ``channel`` / ``tui_app`` are ``None`` — tools that require a live
      channel (``send_file``) will report unavailability
    * ``scheduler`` is ``None`` — ``HostScheduler`` requires a live
      registry, message queue, and threading primitives that do not exist
      in this child process.  Cron tools will report "scheduler not
      wired" which is accurate for the bridge context.
    """
    from pip_agent.mcp_tools import McpContext

    workdir = Path(os.environ.get("PIP_WORKDIR", os.getcwd()))
    pip_dir = workdir / ".pip"

    memory_store = None
    try:
        from pip_agent.memory import MemoryStore

        if pip_dir.exists():
            memory_store = MemoryStore(
                agent_dir=pip_dir,
                workspace_pip_dir=pip_dir,
            )
    except Exception:  # noqa: BLE001
        log.warning("MCP bridge: failed to init MemoryStore", exc_info=True)

    bridge_ctx = _read_bridge_ctx(pip_dir)

    sender_id = bridge_ctx.get("sender_id") or os.environ.get("PIP_SENDER_ID", "")
    peer_id = bridge_ctx.get("peer_id") or os.environ.get("PIP_PEER_ID", "")
    user_id = bridge_ctx.get("user_id") or os.environ.get("PIP_USER_ID", "")
    session_id = bridge_ctx.get("session_id") or os.environ.get("PIP_SESSION_ID", "")
    account_id = bridge_ctx.get("account_id") or os.environ.get("PIP_ACCOUNT_ID", "")

    if not user_id and memory_store and sender_id:
        try:
            profile = memory_store.find_profile_by_sender(
                channel="cli", sender_id=sender_id,
            )
            if profile:
                user_id = memory_store.extract_user_id(profile)
        except Exception:  # noqa: BLE001
            pass

    return McpContext(
        memory_store=memory_store,
        workdir=workdir,
        scheduler=None,
        sender_id=sender_id,
        peer_id=peer_id,
        user_id=user_id,
        session_id=session_id,
        account_id=account_id,
    )


def _refresh_ctx_identity(ctx: Any) -> None:
    """Re-read identity fields from the bridge context file.

    Called before every tool invocation so the long-lived bridge
    process picks up identity changes between turns.
    """
    workdir = Path(os.environ.get("PIP_WORKDIR", os.getcwd()))
    bridge_ctx = _read_bridge_ctx(workdir / ".pip")
    if not bridge_ctx:
        return
    for field in ("sender_id", "peer_id", "user_id", "session_id", "account_id"):
        val = bridge_ctx.get(field)
        if val and hasattr(ctx, field):
            setattr(ctx, field, val)


def _collect_sdk_tools(ctx: Any) -> list[Any]:
    """Collect all SdkMcpTool definitions from mcp_tools.py."""
    from pip_agent.config import settings
    from pip_agent.mcp_tools import (
        _channel_tools,
        _cron_tools,
        _editor_tools,
        _memory_tools,
        _plugin_tools,
        _web_tools,
    )

    tools = (
        _memory_tools(ctx)
        + _cron_tools(ctx)
        + _channel_tools(ctx)
        + _editor_tools(ctx)
        + _plugin_tools(ctx)
        + (_web_tools(ctx) if settings.use_custom_web_tools else [])
    )
    return tools


def _run_server() -> None:
    """Entry point: build and run the STDIO MCP server."""
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    ctx = _build_mcp_ctx()
    sdk_tools = _collect_sdk_tools(ctx)

    tool_map: dict[str, Any] = {}
    mcp_tools: list[types.Tool] = []

    for sdk_tool in sdk_tools:
        schema = sdk_tool.input_schema
        if isinstance(schema, type):
            schema = {"type": "object", "properties": {}}

        mcp_tool = types.Tool(
            name=sdk_tool.name,
            description=sdk_tool.description or "",
            inputSchema=schema,
        )
        mcp_tools.append(mcp_tool)
        tool_map[sdk_tool.name] = sdk_tool.handler

    server = Server("pip")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return mcp_tools

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        _refresh_ctx_identity(ctx)

        handler = tool_map.get(name)
        if handler is None:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

        try:
            result = await handler(arguments or {})
        except Exception as exc:
            log.exception("MCP tool %s failed", name)
            return [types.TextContent(type="text", text=f"Error: {exc}")]

        content_list = result.get("content", [])
        out: list[types.TextContent | types.ImageContent | types.EmbeddedResource] = []
        for item in content_list:
            if item.get("type") == "text":
                out.append(types.TextContent(type="text", text=item.get("text", "")))
            elif item.get("type") == "image":
                out.append(types.ImageContent(
                    type="image",
                    data=item.get("data", ""),
                    mimeType=item.get("mimeType", "image/png"),
                ))
        return out or [types.TextContent(type="text", text=json.dumps(result))]

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_main())


if __name__ == "__main__":
    _run_server()
