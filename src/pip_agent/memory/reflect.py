"""L1 Observer: extract behavioral observations from recent transcripts.

Reads saved transcript JSON files, formats them, and asks an LLM to identify
decision patterns, judgment frameworks, and recurring preferences — focusing
on HOW the user thinks rather than WHAT was done.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from pip_agent.types import Observation

log = logging.getLogger(__name__)

_REFLECT_SYSTEM_BASE = (
    "You are an analyst reviewing conversation transcripts between a user and "
    "an AI assistant. Extract two kinds of observations:\n\n"
    "1. **User behavior** — decision patterns, judgment frameworks, values, "
    "communication style, recurring preferences, and cognitive heuristics.\n"
    "2. **Objective experience** — technical lessons learned during the work, "
    "non-obvious tool/API constraints, and reusable solution patterns.\n\n"
    "For user behavior, focus on HOW the user thinks and decides.\n"
    "For objective experience, focus on insights that are non-obvious and "
    "would be valuable to recall in future work. Do NOT record trivial facts "
    "that are easily looked up, or implementation details tied to a single "
    "file or line of code.\n\n"
    "Each transcript header shows its absolute timestamp. When the conversation "
    "contains relative time references (e.g. 'yesterday', 'last week'), convert "
    "them to absolute dates based on the transcript timestamp and use absolute "
    "dates in your observations.\n\n"
    "Output a JSON array of observation objects. Each object has:\n"
    '  {"text": "...", "category": "<category>"}\n\n'
    "Categories:\n"
    "  User behavior: decision, judgment, communication, value, preference\n"
    "  Objective experience: lesson, knowledge, pattern\n\n"
    "Examples:\n"
    '  GOOD: {"text": "User prefers env vars + pydantic-settings '
    'over per-agent YAML", "category": "decision"}\n'
    '  GOOD: {"text": "pydantic-settings ignores .env unless '
    'model_config sets env_file", "category": "lesson"}\n'
    '  GOOD: {"text": "WeChat access_token expires after 2h; '
    'must be cached server-side", "category": "knowledge"}\n'
    '  BAD:  {"text": "Fixed bug on line 42", '
    '"category": "lesson"} -- too specific\n\n'
    "Output 3-10 observations. If there is nothing meaningful, output [].\n"
    "Output all observations in English, regardless of the transcript language.\n"
    "Return ONLY the JSON array, no markdown fences or extra text."
)

_REFLECT_SYSTEM_CACHE: str | None = None


def _get_reflect_system() -> str:
    global _REFLECT_SYSTEM_CACHE
    if _REFLECT_SYSTEM_CACHE is not None:
        return _REFLECT_SYSTEM_CACHE

    from pip_agent.memory.consolidate import _load_sop
    sop = _load_sop()
    l1_rules = sop.get("L1 Reflection Rules", "")
    if l1_rules:
        _REFLECT_SYSTEM_CACHE = (
            _REFLECT_SYSTEM_BASE + "\n\n"
            "Detailed guidelines:\n\n" + l1_rules
        )
    else:
        _REFLECT_SYSTEM_CACHE = _REFLECT_SYSTEM_BASE
    return _REFLECT_SYSTEM_CACHE

MAX_TRANSCRIPTS = 50
MAX_TRANSCRIPT_CHARS = 3000


def _format_transcript(messages: list[dict]) -> str:
    """Simplified transcript formatter for reflection (not reusing compact's)."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "?").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"[{role}] {content[:500]}")
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", "")[:500])
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}]")
            if parts:
                lines.append(f"[{role}] " + " ".join(parts))
    return "\n".join(lines)


def _load_transcripts(
    transcripts_dir: Path,
    agent_id: str,
    since: float,
) -> list[str]:
    """Load and format transcripts for the given agent since a timestamp."""
    if not transcripts_dir.is_dir():
        return []

    files = sorted(transcripts_dir.glob("*.json"), reverse=True)
    formatted: list[str] = []

    for fp in files[:MAX_TRANSCRIPTS * 2]:
        try:
            ts = int(fp.stem)
        except ValueError:
            continue
        if ts < since:
            continue

        try:
            messages = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(messages, list):
            continue

        text = _format_transcript(messages)
        if not text.strip():
            continue

        has_agent_content = False
        for m in messages:
            if m.get("role") != "assistant":
                continue
            c = m.get("content", "")
            if isinstance(c, str) and c.strip():
                has_agent_content = True
                break
            if isinstance(c, list) and any(
                isinstance(b, dict) and b.get("text", "").strip()
                for b in c
            ):
                has_agent_content = True
                break
        if not has_agent_content:
            continue

        if len(text) > MAX_TRANSCRIPT_CHARS:
            text = text[:MAX_TRANSCRIPT_CHARS] + "\n[truncated]"
        abs_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        formatted.append(f"--- Transcript at {abs_time} ---\n{text}")

        if len(formatted) >= MAX_TRANSCRIPTS:
            break

    return formatted


def reflect(
    client: anthropic.Anthropic,
    transcripts_dir: Path,
    agent_id: str,
    since: float,
    *,
    model: str = "",
) -> list[Observation]:
    """Run L1 reflection on recent transcripts.

    Returns list of observation dicts: [{text, category}, ...].
    """
    from pip_agent.config import settings
    if not model:
        model = settings.model

    transcripts = _load_transcripts(transcripts_dir, agent_id, since)
    if not transcripts:
        log.debug("reflect: no transcripts since %s for agent %s", since, agent_id)
        return []

    combined = "\n\n".join(transcripts)
    if len(combined) > 60000:
        combined = combined[:60000] + "\n[truncated]"

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"Current time: {current_time}\n\n"
        f"Here are recent conversation transcripts for agent '{agent_id}':\n\n"
        f"{combined}\n\n"
        "Extract behavioral observations about the user now."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_get_reflect_system(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("reflect LLM call failed: %s", exc)
        return []

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    from pip_agent.memory.utils import extract_json_array
    observations = extract_json_array(text)
    if observations is None:
        log.warning("reflect: LLM returned invalid JSON: %.200s", text)
        return []

    now = time.time()
    valid: list[Observation] = []
    for obs in observations:
        if isinstance(obs, dict) and obs.get("text"):
            valid.append({
                "ts": now,
                "text": str(obs["text"]),
                "category": str(obs.get("category", "observation")),
                "source": "auto",
            })
    return valid
