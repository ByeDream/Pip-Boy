"""Adapter between Claude Code's native JSONL sessions and Pip's reflect prompt.

Claude Code persists every session turn to ``~/.claude/projects/<enc-cwd>/<session_id>.jsonl``.
Each line is a JSON object describing one message (user / assistant / tool) with
schema that is still stabilising upstream. Rather than couple Pip's memory
pipeline to a fragile shape, this module exposes three defensive primitives:

* :func:`iter_transcript` — byte-offset-cursored JSONL line iterator.
* :func:`normalize_line` — best-effort ``(role, text)`` extractor that handles
  multiple observed shapes and gracefully drops lines it doesn't understand.
* :func:`load_formatted` — builds the "ROLE: text" block that ``reflect.py``
  feeds to the LLM, bounded by ``max_chars``, and returns the next byte offset
  for the cursor.
* :func:`locate_session_jsonl` — ad-hoc lookup when we only know the session_id
  (used by the ``reflect`` MCP tool); hook-driven reflect uses
  ``input_data['transcript_path']`` directly and never calls this.

See ``docs/sdk-contract-notes.md`` §6 for the schema rationale.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Keep individual transcript snippets bounded so we don't blow the reflect
# prompt budget on a single turn with a giant tool_result.
_MAX_BLOCK_CHARS = 500


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------


def iter_transcript(
    path: Path,
    start_offset: int = 0,
) -> Iterator[tuple[int, dict]]:
    """Yield ``(new_offset, parsed_line_dict)`` for each JSONL record after the cursor.

    ``new_offset`` is the byte position *after* the yielded line, suitable for
    persisting to ``state["last_reflect_jsonl_offset"]``. Malformed lines are
    skipped (not fatal) so a corrupt mid-file line cannot kill reflection.
    """
    if not path.is_file():
        return
    try:
        # Open in binary mode so ``tell()`` returns real byte offsets even on
        # Windows where text mode CRLF translation shifts positions.
        with path.open("rb") as fh:
            fh.seek(start_offset)
            while True:
                raw = fh.readline()
                if not raw:
                    break
                offset = fh.tell()
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("Skipping malformed JSONL line at offset ~%d", offset)
                    continue
                if isinstance(data, dict):
                    yield offset, data
    except OSError as exc:
        log.warning("Cannot read transcript %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _stringify_content(content: object) -> str:
    """Collapse a message's ``content`` field into a short readable string.

    Accepts the observed shapes:
    * plain string
    * list of blocks — ``text`` / ``thinking`` / ``tool_use`` / ``tool_result``
    * dict block (single-block shorthand)
    """
    if isinstance(content, str):
        return content[:_MAX_BLOCK_CHARS]

    if isinstance(content, dict):
        return _stringify_block(content)

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            rendered = _stringify_block(block) if isinstance(block, dict) else ""
            if rendered:
                parts.append(rendered)
        return " ".join(parts)[: _MAX_BLOCK_CHARS * 2]

    return ""


def _stringify_block(block: dict) -> str:
    btype = block.get("type") or ""
    if btype == "text":
        return str(block.get("text", ""))[:_MAX_BLOCK_CHARS]
    if btype == "thinking":
        # Assistant turns that produced *only* thinking + tool_use would otherwise
        # render as empty text and be dropped by ``normalize_line``. Keep a short
        # summary so reflect can still see the reasoning existed.
        raw = str(block.get("thinking", ""))
        summary = " ".join(raw.split())[:200]
        return f"[thought] {summary}" if summary else ""
    if btype == "tool_use":
        name = block.get("name", "?")
        return f"[tool:{name}]"
    if btype == "tool_result":
        raw = block.get("content", "")
        inner = _stringify_content(raw)
        return f"[tool_result] {inner[:200]}" if inner else "[tool_result]"
    # Unknown block types are silently ignored; reflection can survive them.
    return ""


def normalize_line(line: dict) -> tuple[str, str] | None:
    """Extract ``(role, text)`` from one JSONL record, or ``None`` if irrelevant.

    Known shapes (probed in priority order):

    1. **CC wrapper shape** — ``{"type": "user"|"assistant", "message":
       {"role": ..., "content": ...}, ...}``. This is what the SDK writes
       for normal turns.
    2. **Flat anthropic shape** — ``{"role": "user"|"assistant", "content": ...}``.
    3. **Tool-result record** — ``{"type": "tool_result", ...}`` — treated as
       belonging to the assistant channel so reflect sees the flow.

    Any line that doesn't match these, or whose role isn't user/assistant,
    returns ``None`` and is excluded from the formatted transcript.

    Meta-turn filtering: lines flagged by CC as its own internal artifacts
    (``isMeta``, ``isCompactSummary``, ``isVisibleInTranscriptOnly``) are
    dropped. These records include the ``/compact`` summary turns, the
    ``<local-command-caveat>`` turns, and other UI-only scaffolding — none
    of them are real user speech or assistant output. Letting them into
    reflect causes the summary of prior turns to be re-extracted as a new
    set of observations, double-counting everything the previous reflect
    run already captured.
    """
    if (
        line.get("isMeta") is True
        or line.get("isCompactSummary") is True
        or line.get("isVisibleInTranscriptOnly") is True
    ):
        return None

    msg = line.get("message") if isinstance(line.get("message"), dict) else None

    # Shape 1: CC wrapper.
    if msg and isinstance(msg, dict):
        role = str(msg.get("role") or line.get("type") or "").lower()
        text = _stringify_content(msg.get("content"))
        if role in ("user", "assistant") and text.strip():
            return role, text

    # Shape 2: flat anthropic.
    role_flat = str(line.get("role") or "").lower()
    if role_flat in ("user", "assistant"):
        text = _stringify_content(line.get("content"))
        if text.strip():
            return role_flat, text

    # Shape 3: bare tool_result at top level — show it as assistant-side context.
    if line.get("type") == "tool_result":
        text = _stringify_content(line.get("content"))
        if text.strip():
            return "assistant", f"[tool_result] {text[:200]}"

    return None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_formatted(
    path: Path,
    *,
    start_offset: int = 0,
    max_chars: int = 60000,
) -> tuple[int, str]:
    """Read the transcript from ``start_offset`` and render it for the reflect prompt.

    Returns ``(new_offset, text)`` where ``text`` is a newline-joined
    ``"[ROLE] …"`` block bounded by ``max_chars``, and ``new_offset`` is the
    byte position to persist so the next call only processes newly-appended
    lines. If nothing new is available, text is ``""`` and ``new_offset``
    equals ``start_offset``.
    """
    if not path.is_file():
        return start_offset, ""

    lines: list[str] = []
    total_chars = 0
    last_offset = start_offset

    for offset, record in iter_transcript(path, start_offset):
        last_offset = offset
        parsed = normalize_line(record)
        if not parsed:
            continue
        role, text = parsed
        rendered = f"[{role.upper()}] {text}"
        total_chars += len(rendered)
        if total_chars > max_chars:
            lines.append("[truncated]")
            break
        lines.append(rendered)

    return last_offset, "\n".join(lines)


# ---------------------------------------------------------------------------
# Ad-hoc lookup
# ---------------------------------------------------------------------------


def locate_session_jsonl(
    session_id: str,
    *,
    projects_root: Path | None = None,
) -> Path | None:
    """Return the JSONL file for ``session_id``, or ``None`` if not found.

    Scans every project directory under ``~/.claude/projects`` (or the override
    passed via ``projects_root``). The lookup is by filename, so the cwd
    encoding used by Claude Code is irrelevant — there should only be one
    match across all project folders for a given session id.

    Hook-driven reflect paths should prefer ``input_data['transcript_path']``
    from the hook payload; this helper exists for the ``reflect`` MCP tool and
    for debugging.
    """
    if not session_id:
        return None
    root = projects_root or DEFAULT_PROJECTS_ROOT
    if not root.is_dir():
        return None
    try:
        matches = list(root.glob(f"*/{session_id}.jsonl"))
    except OSError as exc:
        log.warning("Cannot scan %s: %s", root, exc)
        return None
    if not matches:
        return None
    if len(matches) > 1:
        # Extremely unlikely (would mean CC reused a session id across projects),
        # but if it happens, pick the most-recently-modified.
        matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]
