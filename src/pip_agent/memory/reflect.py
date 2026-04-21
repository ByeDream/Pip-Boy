"""L1 Observer: extract behavioral observations from the active session JSONL.

Pip's reflect stage reads Claude Code's native per-session JSONL log (see
``memory/transcript_source.py`` for the path + schema contract) and asks an
LLM to extract two kinds of observations:

* **User behavior** — decision patterns, judgment frameworks, values,
  recurring preferences.
* **Objective experience** — non-obvious technical lessons, API constraints,
  reusable solution patterns.

The reflect prompt and JSON-array output contract are preserved from the old
transcript-based implementation; only the data source changed (Phase 4.5).
Callers advance a ``state["last_reflect_jsonl_offset"][session_id]`` byte
cursor so each run only sees newly-appended lines.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from pip_agent.anthropic_client import build_anthropic_client, default_direct_sdk_model
from pip_agent.memory.transcript_source import load_formatted
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
    "Output AT MOST 5 observations. Fewer high-signal ones are strictly "
    "better than many low-signal ones — if you are borderline on the fifth, "
    "leave it out. If there is nothing meaningful in the transcript at all, "
    "output [].\n"
    "Output all observations in English, regardless of the transcript language.\n"
    "Return ONLY the JSON array, no markdown fences or extra text."
)

# Hard cap on observations returned from a single reflect pass. The prompt
# asks the model for ≤5 (see Q1 in ``docs/sdk-contract-notes.md`` §11) but
# prompts are not contracts — a misbehaving model, a prompt-injection in
# the transcript, or a future model update can all blow past that. Slice
# in Python so downstream memory / Dream workload is always bounded.
_MAX_OBSERVATIONS_PER_PASS = 5

_REFLECT_SYSTEM_CACHE: str | None = None

# Prompt budget — how many chars of transcript we feed the reflect LLM per
# call. Intentionally conservative so a single overflowing tool_result can't
# push us past the 200K context window.
_MAX_PROMPT_CHARS = 60000

# No module-level DEFAULT_REFLECT_MODEL — the shared rule in
# ``anthropic_client.default_direct_sdk_model()`` governs. Forking a model
# constant here caused a regression where ``claude-sonnet-4-5`` was sent to
# proxies that only whitelisted the agent's own configured model.


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


# ---------------------------------------------------------------------------
# Reflect
# ---------------------------------------------------------------------------


def reflect_from_jsonl(
    transcript_path: Path,
    *,
    start_offset: int = 0,
    agent_id: str,
    model: str = "",
    client: anthropic.Anthropic | None = None,
) -> tuple[int, list[Observation]]:
    """Run L1 reflection over new lines in ``transcript_path``.

    Returns ``(new_offset, observations)``. ``new_offset`` is the byte cursor
    to persist; ``observations`` is the list of extracted observation dicts
    (possibly empty). The transcript is read incrementally from
    ``start_offset``, so repeatedly calling this on a growing file only pays
    for the delta.

    If the transcript has no new reflect-worthy content, returns the advanced
    offset (or the original if nothing was read) with an empty observation
    list. If the LLM call fails or no client is available, returns
    ``(start_offset, [])`` — the cursor is NOT advanced so the next run can
    retry.
    """
    if not transcript_path or not Path(transcript_path).is_file():
        return start_offset, []

    new_offset, formatted = load_formatted(
        Path(transcript_path),
        start_offset=start_offset,
        max_chars=_MAX_PROMPT_CHARS,
    )
    # Q7 cursor guard (docs/sdk-contract-notes.md §11.3): if the byte
    # cursor has not moved, there is nothing new in the transcript and
    # the LLM call would burn a cold start to produce ``[]``. The
    # ``not formatted.strip()`` check below catches a superset of this
    # (e.g. cursor advanced but the delta was pure system-init chrome),
    # but pinning the ``new_offset == start_offset`` case to its own
    # explicit branch means a future refactor of ``load_formatted``
    # cannot silently regress the zero-delta-zero-LLM guarantee.
    if new_offset == start_offset:
        return start_offset, []
    if not formatted.strip():
        return new_offset, []

    llm = client or build_anthropic_client()
    if llm is None:
        return start_offset, []

    if not model:
        model = default_direct_sdk_model()

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"Current time: {current_time}\n\n"
        f"Here is the active session transcript for agent '{agent_id}':\n\n"
        f"{formatted}\n\n"
        "Extract observations now."
    )

    try:
        response = llm.messages.create(
            model=model,
            max_tokens=1024,
            system=_get_reflect_system(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("reflect LLM call failed: %s", exc)
        return start_offset, []

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    from pip_agent.memory.utils import extract_json_array
    observations = extract_json_array(text)
    if observations is None:
        log.warning("reflect: LLM returned invalid JSON: %.200s", text)
        return new_offset, []

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
        if len(valid) >= _MAX_OBSERVATIONS_PER_PASS:
            # Defense-in-depth cap matching the Q1 prompt contract.
            # If the LLM ignored "≤5" and dumped 12, we take the first 5
            # (preserving the LLM's own ordering, which the prompt framed
            # as highest-signal first).
            break
    return new_offset, valid
