from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic

from pip_agent.config import settings
from pip_agent.routing import DEFAULT_COMPACT_MICRO_AGE, DEFAULT_MODEL
from pip_agent.tools import COMPACT_SCHEMA  # noqa: F401 — re-exported

if TYPE_CHECKING:
    from pip_agent.profiler import Profiler

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
    "Keep the summary under 1500 words.\n\n"
    "Write the summary in the same language the conversation was conducted in."
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
        if "role" not in msg:
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = [_serialize_block(b) for b in content]
        safe.append({"role": msg["role"], "content": content})
    return json.dumps(safe, ensure_ascii=False, indent=2, default=str)


def _format_for_summary(messages: list[dict]) -> str:
    """Convert messages into a readable transcript for the summarizer."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
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
                    elif block.get("type") == "image":
                        parts.append("[Image attached]")
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


PRESERVE_RESULT_TOOLS = {"read"}
PRESERVE_RESULT_PREFIXES = ("task_",)

OVERSIZED_TOOL_RESULT_CHARS = 20_000


_IMAGE_TOKEN_ESTIMATE = 1600  # Anthropic bills images by pixel area, not base64 size


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: serialize to string, divide by 4.

    Image blocks are counted as a fixed ~1600 tokens each (Anthropic's
    average for a medium image) instead of the raw base64 string which
    would massively over-count and trigger premature compaction.
    """
    image_count = 0

    def _strip_images(obj: object) -> object:
        nonlocal image_count
        if isinstance(obj, dict):
            if obj.get("type") == "image":
                image_count += 1
                return {"type": "image", "source": "(stripped)"}
            return {k: _strip_images(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_strip_images(item) for item in obj]
        return obj

    stripped = _strip_images(messages)
    text = json.dumps(stripped, default=str)
    return len(text) // 4 + image_count * _IMAGE_TOKEN_ESTIMATE


def truncate_oversized_tool_results(
    messages: list[dict],
    max_chars: int = OVERSIZED_TOOL_RESULT_CHARS,
) -> int:
    """Size-based truncation of tool_result content blocks.

    Unlike `micro_compact` which folds old results by age, this pass only
    touches results whose content exceeds `max_chars`. Short results are
    preserved unchanged. Intended as Stage 1 of overflow recovery.

    Operates in-place. Returns the number of truncations made.
    """
    replaced = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if isinstance(body, str) and len(body) > max_chars:
                original_len = len(body)
                block["content"] = (
                    body[:max_chars]
                    + f"\n\n[... truncated ({original_len} chars total, "
                    f"showing first {max_chars}) ...]"
                )
                replaced += 1
            elif isinstance(body, list):
                for sub in body:
                    if not isinstance(sub, dict):
                        continue
                    if sub.get("type") == "text":
                        text = sub.get("text", "")
                        if isinstance(text, str) and len(text) > max_chars:
                            original_len = len(text)
                            sub["text"] = (
                                text[:max_chars]
                                + f"\n\n[... truncated ({original_len} chars total, "
                                f"showing first {max_chars}) ...]"
                            )
                            replaced += 1
    if replaced and settings.verbose:
        print(
            f"  [truncate_tool_results] truncated {replaced} oversized "
            f"block(s) (>{max_chars} chars)"
        )
    return replaced


def micro_compact(messages: list[dict], *, max_age: int | None = None) -> int:
    """Layer 1: replace old tool_result content with short placeholders.

    Operates in-place. Returns the number of replacements made.
    A 'round' is one assistant+user message pair within the list.
    """
    if max_age is None:
        max_age = DEFAULT_COMPACT_MICRO_AGE

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
            if tool_name in PRESERVE_RESULT_TOOLS or tool_name.startswith(PRESERVE_RESULT_PREFIXES):
                continue
            block["content"] = f"[Previous: used {tool_name}]"
            replaced += 1

    if replaced and settings.verbose:
        print(f"  [micro_compact] replaced {replaced} old tool_result(s), "
              f"kept last {max_age} rounds")

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
    model: str = "",
) -> tuple[str, int, int]:
    """Call the LLM to produce a conversation summary.

    Returns (summary_text, input_tokens, output_tokens).
    """
    if not model:
        model = DEFAULT_MODEL
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
        model=model,
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


def _tail_keep_count(total: int, ratio: float = 0.2, minimum: int = 4) -> int:
    """Compute how many tail messages to preserve."""
    return max(minimum, int(total * ratio))


def auto_compact(
    client: anthropic.Anthropic,
    messages: list[dict],
    system_prompt: str,
    transcripts_dir: Path,
    profiler: Profiler | None = None,
    *,
    model: str = "",
) -> str:
    """Compact older messages via LLM summary while preserving the tail.

    Summarizes the first ~50% of messages into a synthetic user/assistant
    pair at the head, then appends the preserved tail (~20%, min 4). The
    tail alignment is fixed up so it starts with a `user` role, keeping
    the Anthropic alternation contract intact.

    Transcript saving is handled independently by the agent_loop. If the
    summary LLM call fails, the old portion is dropped and only the tail
    is retained -- a last-ditch safety net.
    """
    total = len(messages)
    if total <= 4:
        return ""

    keep_count = _tail_keep_count(total)
    compress_count = max(2, int(total * 0.5))
    compress_count = min(compress_count, total - keep_count)
    if compress_count < 2:
        return ""

    while compress_count < total and messages[compress_count]["role"] != "user":
        compress_count += 1
    if compress_count >= total:
        return ""

    old_messages = messages[:compress_count]
    tail = messages[compress_count:]
    if not tail:
        return ""

    try:
        summary, in_tok, out_tok = summarize_messages(
            client, old_messages, system_prompt, profiler, model=model,
        )
    except Exception as exc:
        if settings.verbose:
            print(f"  [compact] summary failed ({exc}); dropping old messages")
        messages[:] = tail
        return ""

    compacted: list[dict] = [
        {"role": "user", "content": f"<context>\n{summary}\n</context>"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Understood. Continuing from the context above."},
            ],
        },
    ]
    compacted.extend(tail)
    messages[:] = compacted

    if settings.verbose:
        print(
            f"  [compact] compacted {len(old_messages)} old msg(s) -> summary "
            f"({out_tok} out tokens, was {in_tok} in tokens); kept {len(tail)} tail msg(s)"
        )

    return summary


def emergency_compact(
    client: anthropic.Anthropic,
    messages: list[dict],
    system_prompt: str,
    transcripts_dir: Path | None = None,
    profiler: Profiler | None = None,
    *,
    model: str = "",
) -> str:
    """Aggressive compaction for overflow recovery.

    Stage 1: aggressive micro_compact (max_age=1) — fold nearly all old tool
    results to placeholders regardless of the normal age threshold.
    Stage 2: truncate_oversized_tool_results — trim any remaining huge blocks.
    Stage 3: auto_compact — LLM-summarize the first half, keep the tail.

    Each stage mutates `messages` in place. Returns the summary text from
    Stage 3 (or empty string if Stage 3 was skipped / failed).
    """
    if settings.verbose:
        print("  [emergency_compact] running three-stage overflow recovery")
    micro_compact(messages, max_age=1)
    truncate_oversized_tool_results(messages)
    if transcripts_dir is None:
        transcripts_dir = Path(".")
    return auto_compact(
        client, messages, system_prompt, transcripts_dir, profiler, model=model,
    )
