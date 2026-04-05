from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic

from pip_agent.config import settings

if TYPE_CHECKING:
    from pip_agent.profiler import Profiler

COMPACT_SCHEMA = {
    "name": "compact",
    "description": (
        "Compress the conversation history to free up context space. "
        "Call this BEFORE a large operation (e.g. reading many files) "
        "if you sense the conversation has been going on for a long time. "
        "The system also compacts automatically when context is large, "
        "so you only need this for proactive cleanup."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Given a conversation transcript "
    "between a user and an AI assistant, produce a concise structured summary. "
    "Preserve:\n"
    "1. The user's original request / goal\n"
    "2. What has been accomplished so far\n"
    "3. Key findings (file paths, function names, concrete data)\n"
    "4. Decisions made and their rationale\n"
    "5. What remains to be done (outstanding todos)\n\n"
    "Be specific — include file paths, variable names, and concrete details "
    "the assistant will need to continue working. "
    "Keep the summary under 1500 words."
)


def _serialize_block(block: object) -> object:
    """Convert an Anthropic SDK content block to a JSON-safe dict."""
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return block


def _serialize_messages(messages: list[dict]) -> str:
    """Serialize the messages list to JSON for transcript saving."""
    safe: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            content = [_serialize_block(b) for b in content]
        safe.append({"role": msg["role"], "content": content})
    return json.dumps(safe, ensure_ascii=False, indent=2, default=str)


def _format_for_summary(messages: list[dict]) -> str:
    """Convert messages into a readable transcript for the summarizer."""
    lines: list[str] = []
    for msg in messages:
        role = msg["role"].upper()
        content = msg.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        text = str(block.get("content", ""))
                        if len(text) > 300:
                            text = text[:300] + "..."
                        parts.append(f"  [tool_result] {text}")
                    elif block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(
                            f"  [tool_use: {block.get('name', '?')}] "
                            f"{json.dumps(block.get('input', {}), default=str)[:200]}"
                        )
                elif hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "type") and block.type == "tool_use":
                    parts.append(
                        f"  [tool_use: {block.name}] "
                        f"{json.dumps(block.input, default=str)[:200]}"
                    )
            lines.append(f"[{role}]\n" + "\n".join(parts))
    return "\n\n".join(lines)


def _find_tool_name(messages: list[dict], tool_use_id: str) -> str:
    """Walk messages to find the tool name matching a tool_use_id."""
    for msg in messages:
        if msg["role"] != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            block_id = (
                block.get("id") if isinstance(block, dict) else getattr(block, "id", None)
            )
            if block_id == tool_use_id:
                return (
                    block.get("name")
                    if isinstance(block, dict)
                    else getattr(block, "name", "unknown")
                )
    return "unknown"


PRESERVE_RESULT_TOOLS = {"read", "task"}


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: serialize to string, divide by 4."""
    text = json.dumps(messages, default=str)
    return len(text) // 4


def micro_compact(messages: list[dict], *, max_age: int | None = None) -> int:
    """Layer 1: replace old tool_result content with short placeholders.

    Operates in-place. Returns the number of replacements made.
    A 'round' is one assistant+user message pair within the list.
    """
    if max_age is None:
        max_age = settings.compact_micro_age

    round_indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user":
            content = msg.get("content")
            if isinstance(content, list) and any(
                (isinstance(b, dict) and b.get("type") == "tool_result") for b in content
            ):
                round_indices.append(i)

    if len(round_indices) <= max_age:
        return 0

    cutoff_indices = set(round_indices[: -max_age])
    replaced = 0

    for idx in cutoff_indices:
        content = messages[idx].get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            current = block.get("content", "")
            if isinstance(current, str) and current.startswith("[Previous:"):
                continue
            tool_name = _find_tool_name(messages, block.get("tool_use_id", ""))
            if tool_name in PRESERVE_RESULT_TOOLS:
                continue
            block["content"] = f"[Previous: used {tool_name}]"
            replaced += 1

    return replaced


def save_transcript(messages: list[dict], directory: Path) -> Path:
    """Save the current messages to a timestamped JSONL file."""
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time())}.json"
    path = directory / filename
    path.write_text(_serialize_messages(messages), encoding="utf-8")
    return path


def summarize_messages(
    client: anthropic.Anthropic,
    messages: list[dict],
    system_prompt: str,
    profiler: Profiler | None = None,
) -> tuple[str, int, int]:
    """Call the LLM to produce a conversation summary.

    Returns (summary_text, input_tokens, output_tokens).
    """
    transcript = _format_for_summary(messages)
    prompt = (
        f"Here is the conversation transcript to summarize:\n\n"
        f"---\n{transcript}\n---\n\n"
        f"The assistant's system prompt was:\n{system_prompt}\n\n"
        f"Produce the summary now."
    )

    if profiler:
        profiler.start("api:compact")

    response = client.messages.create(
        model=settings.model,
        max_tokens=2048,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    usage = response.usage
    if profiler:
        profiler.stop(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    summary = ""
    for block in response.content:
        if hasattr(block, "text"):
            summary += block.text
    return summary, usage.input_tokens, usage.output_tokens


def auto_compact(
    client: anthropic.Anthropic,
    messages: list[dict],
    system_prompt: str,
    transcripts_dir: Path,
    profiler: Profiler | None = None,
) -> str:
    """Layer 2 / Layer 3: save transcript, summarize, replace messages.

    Operates on ``messages`` in-place. Returns the summary text.
    """
    saved = save_transcript(messages, transcripts_dir)
    if settings.verbose:
        print(f"  [compact] transcript saved to {saved}")

    summary, in_tok, out_tok = summarize_messages(
        client, messages, system_prompt, profiler
    )

    messages.clear()
    messages.append({"role": "user", "content": f"<context>\n{summary}\n</context>"})

    if settings.verbose:
        print(
            f"  [compact] conversation compacted "
            f"(summary {out_tok} tokens, was {in_tok} input tokens)"
        )

    return summary
