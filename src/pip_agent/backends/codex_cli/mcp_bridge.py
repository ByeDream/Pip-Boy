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


def _build_mcp_ctx() -> Any:
    """Construct a ``McpContext`` for tool handlers in the STDIO subprocess.

    The STDIO MCP bridge runs as a child process of the Codex app-server,
    so it cannot share the host's live in-process state (channel objects,
    TUI app, asyncio-based scheduler).  We reconstruct what we can from
    disk and environment variables:

    * ``memory_store`` — loaded from ``.pip/`` (full read/write)
    * ``workdir`` — from ``PIP_WORKDIR`` env var
    * ``sender_id`` / ``peer_id`` / ``session_id`` / ``account_id`` —
      from env vars set by ``ensure_codex_config``
    * ``scheduler`` — read-only snapshot from ``cron.json``

    Limitations (documented in dual-backend-contract.md §3.6):
    * ``channel`` / ``tui_app`` are ``None`` — tools that require a live
      channel (``send_file``) will report unavailability
    * ``scheduler`` is a snapshot, not the live instance — cron mutations
      are picked up on next host restart
    """
    from pip_agent.mcp_tools import McpContext

    workdir = Path(os.environ.get("PIP_WORKDIR", os.getcwd()))
    pip_dir = workdir / ".pip"

    memory_store = None
    try:
        from pip_agent.memory import MemoryStore

        if pip_dir.exists():
            memory_store = MemoryStore(pip_dir)
    except Exception:  # noqa: BLE001
        pass

    scheduler = None
    try:
        from pip_agent.host_scheduler import HostScheduler

        if memory_store is not None:
            scheduler = HostScheduler.__new__(HostScheduler)
            scheduler._jobs = {}
            scheduler._store_path = pip_dir / "cron.json"
            if scheduler._store_path.exists():
                scheduler._load()
    except Exception:  # noqa: BLE001
        pass

    return McpContext(
        memory_store=memory_store,
        workdir=workdir,
        scheduler=scheduler,
        sender_id=os.environ.get("PIP_SENDER_ID", ""),
        peer_id=os.environ.get("PIP_PEER_ID", ""),
        session_id=os.environ.get("PIP_SESSION_ID", ""),
        account_id=os.environ.get("PIP_ACCOUNT_ID", ""),
    )


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
