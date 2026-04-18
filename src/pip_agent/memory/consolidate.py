"""L2 Consolidation + L3 Axiom distillation.

L2: merge observations into memories — reinforce, create, decay, forget,
    resolve conflicts.
L3: promote high-stability memories into judgment principles (axioms.md).

Detailed rules are loaded from ``sops/memory_pipeline_sop.md``.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import anthropic

from pip_agent.types import Memory, Observation

log = logging.getLogger(__name__)

MAX_MEMORIES = 200
PROMOTE_COUNT = 5
PROMOTE_STABILITY = 0.5

# ---------------------------------------------------------------------------
# SOP-driven prompts (loaded once at import time)
# ---------------------------------------------------------------------------

_SOP_PATH = Path(__file__).resolve().parent / "sops" / "memory_pipeline_sop.md"
_SOP_SECTIONS: dict[str, str] = {}


def _load_sop() -> dict[str, str]:
    """Parse the SOP markdown into sections keyed by ``## <heading>``."""
    global _SOP_SECTIONS
    if _SOP_SECTIONS:
        return _SOP_SECTIONS
    try:
        raw = _SOP_PATH.read_text(encoding="utf-8")
    except OSError:
        log.warning("SOP file not found at %s, using fallback prompts", _SOP_PATH)
        return {}
    current_key = ""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            if current_key:
                _SOP_SECTIONS[current_key] = "\n".join(lines).strip()
            current_key = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    if current_key:
        _SOP_SECTIONS[current_key] = "\n".join(lines).strip()
    return _SOP_SECTIONS


def _get_consolidate_system() -> str:
    sop = _load_sop()
    l2_rules = sop.get("L2 Consolidation Rules", "")
    global_rules = sop.get("Global Constraints", "")
    if l2_rules:
        return (
            "You are a memory consolidation engine. Given a list of existing memories "
            "and new observations, produce an updated memory list.\n\n"
            f"{l2_rules}\n\n"
            f"{global_rules}\n\n"
            "Output a JSON array of memory objects with these fields:\n"
            '  {"id": "...", "text": "...", "count": N, "category": "...", '
            '"first_seen": epoch, "last_reinforced": epoch, '
            '"contexts": ["ctx1", "ctx2"], "total_cycles": N, '
            '"stability": 0.0-1.0, "source": "auto"|"user"}\n\n'
            "Return ONLY the JSON array, no markdown fences or extra text."
        )
    return (
        "You are a memory consolidation engine. Given a list of existing memories "
        "and new observations, produce an updated memory list.\n\n"
        "Rules:\n"
        "- If a new observation matches an existing memory semantically, REINFORCE it "
        "(increment count, update last_reinforced, add context_type to contexts).\n"
        "- If a new observation is novel, CREATE a new memory (count=1).\n"
        "- Existing memories NOT reinforced by any observation: DECAY (count -= 1).\n"
        "- Memories with count <= 0 are FORGOTTEN (remove them).\n"
        "- When two memories contradict, the one with higher count wins; "
        "equal count: newer last_reinforced wins. Loser is removed.\n"
        "- Calculate stability = unique_contexts / total_cycles for each memory.\n\n"
        "Output a JSON array of memory objects with these fields:\n"
        '  {"id": "...", "text": "...", "count": N, "category": "...", '
        '"first_seen": epoch, "last_reinforced": epoch, '
        '"contexts": ["ctx1", "ctx2"], "total_cycles": N, '
        '"stability": 0.0-1.0, "source": "auto"|"user"}\n\n'
        "Write all text in English.\n"
        "Return ONLY the JSON array, no markdown fences or extra text."
    )


def _get_axiom_system() -> str:
    sop = _load_sop()
    l3_rules = sop.get("L3 Axiom Distillation Rules", "")
    global_rules = sop.get("Global Constraints", "")
    if l3_rules:
        return (
            "You are a judgment principle distiller. Given a list of high-stability "
            "behavioral memories about a user, distill them into concise judgment "
            "principles (axioms).\n\n"
            f"{l3_rules}\n\n"
            f"{global_rules}"
        )
    return (
        "You are a judgment principle distiller. Given a list of high-stability "
        "behavioral memories about a user, distill them into concise judgment "
        "principles (axioms).\n\n"
        "Each principle should describe HOW the user thinks or decides, not WHO "
        "they are. Focus on decision heuristics, quality standards, and cognitive "
        "patterns that are stable across contexts.\n\n"
        "Output as a markdown list. Each item is one principle, 1-2 sentences.\n"
        "Write all output in English.\n"
        "Return ONLY the markdown list, no extra text or headers."
    )


def consolidate(
    client: anthropic.Anthropic,
    observations: list[Observation],
    memories: list[Memory],
    cycle_count: int,
    *,
    model: str = "",
) -> list[Memory]:
    """L2: merge observations into memories. Returns updated memory list."""
    from pip_agent.routing import DEFAULT_MODEL
    if not model:
        model = DEFAULT_MODEL

    if not observations and not memories:
        return []

    if len(memories) > MAX_MEMORIES:
        memories = sorted(memories, key=lambda m: m.get("count", 0), reverse=True)[:MAX_MEMORIES]

    mem_summary = json.dumps(memories, ensure_ascii=False, default=str)
    obs_summary = json.dumps(observations, ensure_ascii=False, default=str)

    if len(mem_summary) > 40000:
        mem_summary = mem_summary[:40000] + "..."
    if len(obs_summary) > 20000:
        obs_summary = obs_summary[:20000] + "..."

    prompt = (
        f"Current memories ({len(memories)} items):\n{mem_summary}\n\n"
        f"New observations ({len(observations)} items):\n{obs_summary}\n\n"
        f"Current consolidation cycle: {cycle_count}\n"
        "Produce the updated memory list now."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_get_consolidate_system(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("consolidate LLM call failed: %s", exc)
        return memories

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    from pip_agent.memory.utils import extract_json_array
    updated = extract_json_array(text)
    if updated is None:
        log.warning("consolidate: LLM returned invalid JSON, keeping existing memories")
        return memories

    # ROB-2: guard against LLM returning empty or drastically reduced list
    if memories and not updated:
        log.warning(
            "consolidate: LLM returned empty array with %d existing memories,"
            " preserving originals",
            len(memories),
        )
        return memories
    if memories and len(updated) < len(memories) * 0.2:
        log.warning(
            "consolidate: LLM shrank memories from %d to %d (>80%% drop), preserving originals",
            len(memories), len(updated),
        )
        return memories

    cleaned = []
    for mem in updated:
        if not isinstance(mem, dict):
            continue
        if not mem.get("id"):
            mem["id"] = uuid.uuid4().hex[:12]
        cleaned.append(mem)

    return cleaned


def distill_axioms(
    client: anthropic.Anthropic,
    memories: list[Memory],
    *,
    model: str = "",
) -> str:
    """L3: distill high-stability memories into judgment principles.

    Returns markdown text for axioms.md, or empty string if nothing qualifies.
    """
    from pip_agent.routing import DEFAULT_MODEL
    if not model:
        model = DEFAULT_MODEL

    candidates = [
        m for m in memories
        if m.get("count", 0) >= PROMOTE_COUNT
        and m.get("stability", 0) >= PROMOTE_STABILITY
    ]
    if not candidates:
        return ""

    summary = json.dumps(candidates, ensure_ascii=False, default=str)
    if len(summary) > 30000:
        summary = summary[:30000] + "..."

    prompt = (
        f"High-stability memories ({len(candidates)} items):\n{summary}\n\n"
        "Distill these into judgment principles now."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_get_axiom_system(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("distill_axioms LLM call failed: %s", exc)
        return ""

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    return text.strip()
