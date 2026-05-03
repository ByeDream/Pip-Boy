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

# Per-1M-token pricing (USD). Reasoning tokens are billed as output
# tokens — no separate multiplier. Higher reasoning effort just
# produces more output tokens at the same per-token rate.
#
# Entries are matched longest-key-first so "gpt-5.4-mini" wins over
# "gpt-5.4". Returns ``None`` for unknown models.
_MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    # (input, cached_input, output) per 1M tokens
    "gpt-5.5":       (5.00,   0.50,   30.00),
    "gpt-5.4-pro":   (30.00,  3.00,  180.00),
    "gpt-5.4":       (2.50,   0.25,   15.00),
    "gpt-5.4-mini":  (0.75,   0.075,   4.50),
    "gpt-5.3-codex": (1.75,   0.175,  14.00),
    "gpt-5.2":       (1.75,   0.175,  14.00),
    "o3":            (2.00,   0.50,    8.00),
    "o3-mini":       (0.55,   0.14,    2.20),
    "o4-mini":       (0.55,   0.14,    2.20),
}

_SORTED_PRICING = sorted(
    _MODEL_PRICING.items(), key=lambda kv: len(kv[0]), reverse=True,
)


def estimate_cost_usd(
    model: str | None,
    usage: dict[str, int],
) -> float | None:
    """Estimate cost from token usage and model name.

    Returns ``None`` when pricing is unknown for the model.
    """
    if not model or not usage:
        return None

    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        for key, val in _SORTED_PRICING:
            if key in model:
                pricing = val
                break
    if pricing is None:
        return None

    inp_price, cached_price, out_price = pricing
    input_tok = usage.get("input_tokens", 0)
    cached_tok = usage.get("cached_input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    net_input = max(input_tok - cached_tok, 0)

    cost = (
        net_input * inp_price / 1_000_000
        + cached_tok * cached_price / 1_000_000
        + output_tok * out_price / 1_000_000
    )
    return round(cost, 6)


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
    callback: StreamEventCallback | None,
    *,
    state: dict[str, Any],
) -> None:
    """Translate one SDK event into zero or more Pip-Boy stream events.

    ``state`` is a mutable dict carried across all events in a single
    turn.  Used to track token usage, turn info, etc.  When ``callback``
    is ``None``, state tracking still happens but no events are emitted.

    Callers should pass the same ``state`` dict for the lifetime of a
    ``thread.run()`` iteration.
    """
    etype = type(event).__name__

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        pass

    cb = callback if callback is not None else _noop

    # -- Text delta (real incremental) -----------------------------------
    if etype == "ItemAgentMessageDeltaNotification":
        delta = getattr(event.params, "delta", "") or ""
        if delta:
            state.setdefault("accumulated_text", "")
            state["accumulated_text"] += delta
            await cb("text_delta", text=delta)
        return

    # -- Reasoning / thinking delta --------------------------------------
    if etype in (
        "ItemReasoningTextDeltaNotification",
        "ItemReasoningSummaryTextDeltaNotification",
    ):
        delta = getattr(event.params, "delta", "") or ""
        if delta:
            await cb("thinking_delta", text=delta)
        return

    # -- Item started (tool_use) -----------------------------------------
    if etype == "ItemStartedNotificationModel":
        item = event.params.item.root
        item_type = _get_item_type(item)
        log.debug("codex item_started: type=%s", item_type)

        if item_type in ("command_execution", "commandExecution"):
            cmd = getattr(item, "command", "") or ""
            state["tool_calls"] = state.get("tool_calls", 0) + 1
            await cb(
                "tool_use",
                id=getattr(item, "id", ""),
                name="Bash",
                input={"command": cmd},
            )
        elif item_type in ("file_change", "fileChange"):
            changes = getattr(item, "changes", []) or []
            for change in changes:
                kind = getattr(change, "kind", "update")
                if hasattr(kind, "root"):
                    kind = kind.root
                path = getattr(change, "path", "")
                tool_name = _FILE_KIND_MAP.get(str(kind), "Edit")
                state["tool_calls"] = state.get("tool_calls", 0) + 1
                await cb(
                    "tool_use",
                    id=getattr(item, "id", ""),
                    name=tool_name,
                    input={"path": path, "kind": str(kind)},
                )
        elif item_type in ("mcp_tool_call", "mcpToolCall"):
            tool = getattr(item, "tool", "") or ""
            arguments = getattr(item, "arguments", {}) or {}
            if isinstance(arguments, str):
                import json
                try:
                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    arguments = {"raw": arguments}
            state["tool_calls"] = state.get("tool_calls", 0) + 1
            await cb(
                "tool_use",
                id=getattr(item, "id", ""),
                name=tool,
                input=arguments,
            )
        elif item_type in ("web_search", "webSearch"):
            query = getattr(item, "query", "") or ""
            state["tool_calls"] = state.get("tool_calls", 0) + 1
            await cb(
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

        if item_type in ("command_execution", "commandExecution"):
            exit_code = getattr(item, "exitCode", None)
            is_error = exit_code is not None and exit_code != 0
            await cb(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=is_error,
            )
        elif item_type in ("file_change", "fileChange"):
            await cb(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=_get_status(item) != "completed",
            )
        elif item_type in ("mcp_tool_call", "mcpToolCall"):
            error = getattr(item, "error", None)
            await cb(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=bool(error),
            )
        elif item_type in ("web_search", "webSearch"):
            await cb(
                "tool_result",
                tool_use_id=getattr(item, "id", ""),
                is_error=False,
            )
        elif item_type in ("agent_message", "agentMessage"):
            text = getattr(item, "text", "") or ""
            if text:
                state["final_text"] = text
        return

    # -- Turn plan updated (TodoWrite mapping) ---------------------------
    if etype == "TurnPlanUpdatedNotificationModel":
        plan = getattr(event.params, "plan", None)
        if plan is not None:
            steps = plan if isinstance(plan, list) else (getattr(plan, "steps", []) or [])
            _STATUS_MAP = {"inProgress": "in_progress"}
            todos = []
            for idx, step in enumerate(steps):
                status_obj = getattr(step, "status", None)
                raw_status = str(getattr(status_obj, "root", status_obj) or "pending")
                todos.append({
                    "id": str(idx),
                    "content": str(getattr(step, "step", "") or getattr(step, "title", "") or ""),
                    "status": _STATUS_MAP.get(raw_status, raw_status),
                })
            await cb(
                "tool_use",
                id="plan-update",
                name="TodoWrite",
                input={"todos": todos},
            )
            await cb(
                "tool_result",
                tool_use_id="plan-update",
                is_error=False,
            )
        return

    # -- Token usage update (internal bookkeeping) -----------------------
    # SDK field path: params.tokenUsage.total.{inputTokens,outputTokens,...}
    if etype == "ThreadTokenUsageUpdatedNotificationModel":
        token_usage = getattr(event.params, "tokenUsage", None)
        if token_usage is not None:
            total = getattr(token_usage, "total", None)
            if total is not None:
                state["token_usage"] = {
                    "input_tokens": getattr(total, "inputTokens", 0) or 0,
                    "cached_input_tokens": getattr(total, "cachedInputTokens", 0) or 0,
                    "output_tokens": getattr(total, "outputTokens", 0) or 0,
                    "reasoning_tokens": getattr(total, "reasoningOutputTokens", 0) or 0,
                    "total_tokens": getattr(total, "totalTokens", 0) or 0,
                }
        return

    # -- Turn completed (bookkeeping) -------------------------------------
    # Finalize is emitted by the caller AFTER the stream loop ends, so
    # that post-loop data (tool counts from stream.items, elapsed_s) is
    # available.  We only mark the turn as done in state here.
    if etype == "TurnCompletedNotificationModel":
        state["turn_completed"] = True
        return

    # -- Turn started (bookkeeping only) ---------------------------------
    if etype == "TurnStartedNotificationModel":
        return

    # -- All other events: log and skip ----------------------------------
    log.debug("codex event_translator: unhandled event %s", etype)
