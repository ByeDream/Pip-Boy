"""Translate ``codex-python`` SDK streaming events into Pip-Boy's 5 semantic events.

This is the single-point translation layer for the Codex backend.
Every SDK JSON-RPC notification lands here and is translated into
one of:

    text_delta, thinking_delta, tool_use, tool_result, finalize

TUI, WeChat, and WeCom renderers consume only these 5 events, so
this module is the only place Codex-specific event shapes appear.

Mapping table (contract §3.2):

    SDK event type                          → Pip-Boy event
    ─────────────────────────────────────── ──────────────
    ItemAgentMessageDeltaNotification       → text_delta
    ItemReasoningTextDeltaNotification      → thinking_delta  (if available)
    ItemStartedNotification + cmd/file/mcp  → tool_use
    ItemCommandExecutionOutputDeltaNotif.   → (attached to open tool_use)
    ItemCompletedNotification + cmd         → tool_result
    ItemCompletedNotification + file        → tool_result
    ItemCompletedNotification + mcp         → tool_result
    TurnPlanUpdatedNotification             → tool_use + tool_result (TodoWrite)
    TurnCompletedNotification               → finalize
    ThreadTokenUsageUpdatedNotification     → (internal state)
"""

from __future__ import annotations

import logging
from typing import Any

from pip_agent.backends.base import StreamEventCallback

log = logging.getLogger(__name__)

# Codex item.type → Pip-Boy standard tool name (contract §4)
_TOOL_NAME_MAP: dict[str, str] = {
    "command_execution": "Bash",
}

_FILE_KIND_MAP: dict[str, str] = {
    "add": "Write",
    "update": "Edit",
    "delete": "Write",
}


def _get_item_type(item: Any) -> str:
    """Extract the item type string from a ThreadItem union."""
    t = getattr(item, "type", None)
    if t is None:
        return ""
    return t.root if hasattr(t, "root") else str(t)


def _get_status(item: Any) -> str:
    """Extract item status string."""
    s = getattr(item, "status", None)
    if s is None:
        return ""
    return s.root if hasattr(s, "root") else str(s)


async def translate_event(
    event: Any,
    callback: StreamEventCallback,
    *,
    state: dict[str, Any],
) -> None:
    """Translate one SDK event into zero or more Pip-Boy stream events.

    ``state`` is a mutable dict carried across all events in a single
    turn.  Used to track token usage, turn info, etc.

    Callers should pass the same ``state`` dict for the lifetime of a
    ``thread.run()`` iteration.
    """
    etype = type(event).__name__

    # -- Text delta (real incremental) -----------------------------------
    if etype == "ItemAgentMessageDeltaNotification":
        delta = getattr(event.params, "delta", "") or ""
        if delta:
            await callback("text_delta", text=delta)
        return

    # -- Reasoning / thinking delta --------------------------------------
    if etype in (
        "ItemReasoningTextDeltaNotification",
        "ItemReasoningSummaryTextDeltaNotification",
    ):
        delta = getattr(event.params, "delta", "") or ""
        if delta:
            await callback("thinking_delta", text=delta)
        return

    # -- Item started (tool_use) -----------------------------------------
    if etype == "ItemStartedNotificationModel":
        item = event.params.item.root
        item_type = _get_item_type(item)

        if item_type == "command_execution":
            cmd = getattr(item, "command", "") or ""
            await callback(
                "tool_use",
                id=getattr(item, "id", ""),
                name="Bash",
                input={"command": cmd},
            )
        elif item_type == "file_change":
            changes = getattr(item, "changes", []) or []
            for change in changes:
                kind = getattr(change, "kind", "update")
                if hasattr(kind, "root"):
                    kind = kind.root
                path = getattr(change, "path", "")
                tool_name = _FILE_KIND_MAP.get(str(kind), "Edit")
                await callback(
                    "tool_use",
                    id=getattr(item, "id", ""),
                    name=tool_name,
                    input={"path": path, "kind": str(kind)},
                )
        elif item_type == "mcp_tool_call":
            tool = getattr(item, "tool", "") or ""
            arguments = getattr(item, "arguments", {}) or {}
            if isinstance(arguments, str):
                import json
                try:
                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    arguments = {"raw": arguments}
            await callback(
                "tool_use",
                id=getattr(item, "id", ""),
                name=tool,
                input=arguments,
            )
        elif item_type == "web_search":
            query = getattr(item, "query", "") or ""
            await callback(
                "tool_use",
                id=getattr(item, "id", ""),
                name="WebSearch",
                input={"query": query},
            )
        return

    # -- Command output delta (attached to running tool) -----------------
    if etype == "ItemCommandExecutionOutputDeltaNotification":
        return

    # -- File change output delta ----------------------------------------
    if etype in ("ItemFileChangeOutputDeltaNotification", "FileChangeOutputDeltaNotification"):
        return

    # -- Item completed (tool_result) ------------------------------------
    if etype == "ItemCompletedNotificationModel":
        item = event.params.item.root
        item_type = _get_item_type(item)

        if item_type == "command_execution":
            exit_code = getattr(item, "exitCode", None)
            is_error = exit_code is not None and exit_code != 0
            await callback(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=is_error,
            )
        elif item_type == "file_change":
            await callback(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=_get_status(item) != "completed",
            )
        elif item_type == "mcp_tool_call":
            error = getattr(item, "error", None)
            await callback(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=bool(error),
            )
        elif item_type == "web_search":
            await callback(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=False,
            )
        elif item_type == "agent_message":
            text = getattr(item, "text", "") or ""
            if text:
                state["final_text"] = text
        return

    # -- Turn plan updated (TodoWrite mapping) ---------------------------
    if etype == "TurnPlanUpdatedNotificationModel":
        plan = getattr(event.params, "plan", None)
        if plan is not None:
            steps = getattr(plan, "steps", []) or []
            plan_data = []
            for step in steps:
                plan_data.append({
                    "title": getattr(step, "title", ""),
                    "status": str(getattr(step, "status", "")),
                })
            await callback(
                "tool_use",
                id="plan-update",
                name="TodoWrite",
                input={"plan": plan_data},
            )
            await callback(
                "tool_result",
                tool_use_id="plan-update",
                is_error=False,
            )
        return

    # -- Token usage update (internal bookkeeping) -----------------------
    if etype == "ThreadTokenUsageUpdatedNotificationModel":
        usage = getattr(event.params, "usage", None)
        if usage is not None:
            state["token_usage"] = {
                "input_tokens": getattr(usage, "inputTokens", 0) or 0,
                "output_tokens": getattr(usage, "outputTokens", 0) or 0,
                "total_tokens": getattr(usage, "totalTokens", 0) or 0,
            }
        return

    # -- Turn completed (finalize) ---------------------------------------
    if etype == "TurnCompletedNotificationModel":
        usage = state.get("token_usage", {})
        final_text = state.get("final_text", "")
        cost_usd = None

        await callback(
            "finalize",
            final_text=final_text,
            num_turns=1,
            cost_usd=cost_usd,
            usage=usage,
            elapsed_s=state.get("elapsed_s", 0),
        )
        return

    # -- Turn started (bookkeeping only) ---------------------------------
    if etype == "TurnStartedNotificationModel":
        return

    # -- All other events: log and skip ----------------------------------
    log.debug("codex event_translator: unhandled event type %s", etype)
