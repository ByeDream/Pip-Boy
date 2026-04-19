"""SDK hook callbacks for Pip-Boy.

Hooks are invoked by the Claude Agent SDK at specific lifecycle points.
They bridge SDK events to Pip-Boy's host-side services (memory, profiler,
transcript archiving).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import HookMatcher

if TYPE_CHECKING:
    from pip_agent.memory import MemoryStore
    from pip_agent.profiler import Profiler

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PreCompact — archive transcript before context compaction
# ---------------------------------------------------------------------------


def _pre_compact_hook(
    transcripts_dir: Path,
    agent_name: str = "Pip-Boy",
):
    """Return a hook callback that archives the transcript to markdown."""

    async def _callback(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        transcript_path = input_data.get("transcript_path", "")
        session_id = input_data.get("session_id", "")

        if not transcript_path:
            log.debug("PreCompact: no transcript_path, skipping")
            return {}

        tp = Path(transcript_path)
        if not tp.exists():
            log.debug("PreCompact: transcript file not found: %s", tp)
            return {}

        try:
            content = tp.read_text("utf-8")
            messages = _parse_transcript(content)
            if not messages:
                return {}

            conversations_dir = transcripts_dir / "conversations"
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = time.strftime("%Y-%m-%d")
            slug = _session_slug(session_id)
            filename = f"{date}-{slug}.md"
            dest = conversations_dir / filename

            markdown = _format_transcript_md(messages, agent_name)
            dest.write_text(markdown, "utf-8")
            log.info("PreCompact: archived transcript to %s", dest)
        except Exception as exc:
            log.warning("PreCompact: archival failed: %s", exc)

        return {}

    return _callback


def _parse_transcript(content: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "user":
            msg = entry.get("message", {})
            text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
            if not text and isinstance(msg.get("content"), list):
                text = "".join(
                    c.get("text", "") for c in msg["content"]
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            if text:
                messages.append({"role": "user", "content": text})
        elif entry.get("type") == "assistant":
            msg = entry.get("message", {})
            parts = msg.get("content", [])
            if isinstance(parts, list):
                text = "".join(
                    c.get("text", "") for c in parts
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            else:
                text = str(parts)
            if text:
                messages.append({"role": "assistant", "content": text})
    return messages


def _session_slug(session_id: str) -> str:
    if session_id:
        return session_id[:12]
    return time.strftime("%H%M")


def _format_transcript_md(
    messages: list[dict[str, str]],
    agent_name: str,
) -> str:
    lines = [
        "# Conversation",
        "",
        f"Archived: {time.strftime('%b %d, %I:%M %p')}",
        "",
        "---",
        "",
    ]
    for msg in messages:
        sender = "User" if msg["role"] == "user" else agent_name
        content = msg["content"]
        if len(content) > 2000:
            content = content[:2000] + "..."
        lines.append(f"**{sender}**: {content}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stop — write memory observations when the agent stops
# ---------------------------------------------------------------------------


def _stop_hook(memory_store: MemoryStore | None):
    """Return a hook callback that writes observations on agent stop."""

    async def _callback(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if memory_store is None:
            return {}

        try:
            state = memory_store.load_state()
            state["last_activity_at"] = time.time()
            memory_store.save_state(state)
        except Exception as exc:
            log.warning("Stop hook: failed to update state: %s", exc)

        return {}

    return _callback


# ---------------------------------------------------------------------------
# PreToolUse / PostToolUse — profiler integration
# ---------------------------------------------------------------------------


_tool_start_times: dict[str, float] = {}


def _pre_tool_use_hook(profiler: Profiler | None):
    """Start a profiler timer when a tool begins execution."""

    async def _callback(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if profiler is None or not tool_use_id:
            return {}

        tool_name = input_data.get("tool_name", "unknown")
        _tool_start_times[tool_use_id] = time.monotonic()
        profiler.start(f"tool:{tool_name}")
        return {}

    return _callback


def _post_tool_use_hook(profiler: Profiler | None):
    """Stop the profiler timer when a tool finishes."""

    async def _callback(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if profiler is None or not tool_use_id:
            return {}

        start = _tool_start_times.pop(tool_use_id, None)
        tool_name = input_data.get("tool_name", "unknown")
        if start is not None:
            elapsed_ms = (time.monotonic() - start) * 1000
            profiler.record(f"tool:{tool_name}", elapsed_ms)
        profiler.stop()
        return {}

    return _callback


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_hooks(
    *,
    transcripts_dir: Path | None = None,
    memory_store: MemoryStore | None = None,
    profiler: Profiler | None = None,
    agent_name: str = "Pip-Boy",
) -> dict[str, list[HookMatcher]]:
    """Build the hooks dict for ``ClaudeAgentOptions.hooks``.

    Each hook event maps to a list of ``HookMatcher`` objects. The SDK
    dispatches matching hooks to our async callbacks.
    """
    hooks: dict[str, list[HookMatcher]] = {}

    if transcripts_dir is not None:
        hooks["PreCompact"] = [
            HookMatcher(hooks=[_pre_compact_hook(transcripts_dir, agent_name)]),
        ]

    hooks["Stop"] = [
        HookMatcher(hooks=[_stop_hook(memory_store)]),
    ]

    if profiler is not None:
        hooks["PreToolUse"] = [
            HookMatcher(hooks=[_pre_tool_use_hook(profiler)]),
        ]
        hooks["PostToolUse"] = [
            HookMatcher(hooks=[_post_tool_use_hook(profiler)]),
        ]

    return hooks
