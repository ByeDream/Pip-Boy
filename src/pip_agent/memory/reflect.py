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
from typing import Any

from pip_agent.llm_client import build_background_client
from pip_agent.memory.transcript_source import load_formatted
from pip_agent.models import with_model_fallback
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
# asks the model for ≤5, but prompts are not contracts — a misbehaving model,
# a prompt-injection in the transcript, or a future model update can all
# blow past that. Slice in Python so downstream memory / Dream workload is
# always bounded.
_MAX_OBSERVATIONS_PER_PASS = 5

_REFLECT_SYSTEM_CACHE: str | None = None

# Prompt budget — how many chars of transcript we feed the reflect LLM per
# call. Intentionally conservative so a single overflowing tool_result can't
# push us past the 200K context window.
_MAX_PROMPT_CHARS = 60000

# Tier ``t1`` is hard-coded for reflect (see ``pip_agent.models.TASK_TIER``):
# observation extraction is async / non-interactive, so we pick a mid-cost
# model and degrade through :func:`with_model_fallback` if the head of the
# chain is unavailable. Do NOT introduce a stage-local model constant — every
# Pip-side model selection MUST flow through the tier registry.


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
    client: Any = None,
) -> tuple[int, list[Observation]]:
    """Run L1 reflection over new lines in ``transcript_path``.

    Returns ``(new_offset, observations)``. ``new_offset`` is the byte cursor
    to persist; ``observations`` is the list of extracted observation dicts
    (possibly empty). The transcript is read incrementally from
    ``start_offset``, so repeatedly calling this on a growing file only pays
    for the delta.

    If the transcript has no new reflect-worthy content, returns the advanced
    offset (or the original if nothing was read) with an empty observation
    list. If the LLM call fails, no client is available, or the LLM returns
    invalid JSON, returns ``(start_offset, [])`` — the cursor is NOT advanced
    so the next run can retry. Only a successful LLM call that produces a
    valid JSON array (even an empty one) advances the cursor.
    """
    if not transcript_path or not Path(transcript_path).is_file():
        return start_offset, []

    new_offset, formatted = load_formatted(
        Path(transcript_path),
        start_offset=start_offset,
        max_chars=_MAX_PROMPT_CHARS,
    )
    # Zero-delta cursor guard: if the byte cursor has not moved after
    # ``load_formatted``, there is nothing new in the transcript and
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

    llm = client or build_background_client()
    if llm is None:
        return start_offset, []

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"Current time: {current_time}\n\n"
        f"Here is the active session transcript for agent '{agent_id}':\n\n"
        f"{formatted}\n\n"
        "Extract observations now."
    )

    def _call(model_name: str):
        return llm.messages.create(
            model=model_name,
            max_tokens=1024,
            system=_get_reflect_system(),
            messages=[{"role": "user", "content": prompt}],
        )

    try:
        response = with_model_fallback("t1", _call, label="reflect")
    except Exception:
        log.warning("reflect LLM call failed", exc_info=True)
        raise

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    from pip_agent.memory.utils import extract_json_array
    observations = extract_json_array(text)
    if observations is None:
        # Invalid JSON from the LLM — most often a transient model
        # hiccup (truncation, stray markdown fence, etc.). Keep the
        # cursor at ``start_offset`` so the next reflect pass sees
        # the same delta and can try again; advancing would silently
        # drop whatever observations that chunk was supposed to yield.
        # The LLM call happens only on heartbeat / /exit / PreCompact
        # / ``reflect`` MCP, so retry cost is bounded.
        log.warning("reflect: LLM returned invalid JSON: %.200s", text)
        return start_offset, []

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


# ---------------------------------------------------------------------------
# State-aware wrapper — reflect + persist observations + advance cursor
# ---------------------------------------------------------------------------

OFFSET_STATE_KEY = "last_reflect_jsonl_offset"
"""Key under which per-session byte cursors live in ``state.json``.

Public constant so PreCompact hook, /exit flush, and the reflect MCP tool
can all read/write the same map without any risk of key drift. Schema:
``{session_id: int}``.
"""

PENDING_REFLECT_KEY = "_pending_reflect"
"""Two-phase commit marker for in-flight reflect results.

Before :func:`reflect_and_persist` writes observations to disk it stages
``{session_id, new_offset, observations}`` under this key in
``state.json`` via an atomic save. Only after the append to
``observations/*.jsonl`` succeeds does it clear the marker and advance
the cursor in a second atomic save. Crash recovery: on the next
reflect call, :func:`_drain_pending_reflect` flushes any unfinished
pending bundle before handing control back to the normal pass, so an
interrupt between stage and commit leaves the observations + cursor
exactly where they should be after a clean run.

Residual risk: a crash *during* the observations append can lead to
at-most-once re-append on the next run (append is not itself atomic
on POSIX/NTFS). Dream consolidation now hard-deletes the observations
file on success, so the duplication window is bounded to a single
Dream cycle.
"""


def _drain_pending_reflect(memory_store) -> dict:
    """Finish any half-committed reflect pass left by a prior crash.

    Loads ``state.json``, and if a pending bundle is present:

    1. Appends the staged observations via ``write_observations`` (so
       the data lands in the same daily jsonl a clean run would have
       used).
    2. Advances the per-session offset to the staged ``new_offset``.
    3. Clears the pending marker and saves state atomically.

    Returns the post-drain state dict so the caller can work with a
    single load. If there's nothing pending, state is returned as-is
    (no save happens).
    """
    state = memory_store.load_state()
    pending = state.get(PENDING_REFLECT_KEY)
    if not pending:
        return state

    log.info(
        "reflect: draining pending bundle from prior run "
        "(session=%s obs=%d new_offset=%s)",
        (pending.get("session_id") or "")[:8],
        len(pending.get("observations") or []),
        pending.get("new_offset"),
    )

    observations = pending.get("observations") or []
    if observations:
        try:
            memory_store.write_observations(observations)
        except Exception:  # noqa: BLE001
            # If disk append fails we leave the pending marker alone so
            # the next drain retries; better to risk duplicates than
            # lose data.
            log.exception("reflect: failed to drain pending observations")
            return state

    session_id = pending.get("session_id") or ""
    new_offset = pending.get("new_offset")
    if session_id and isinstance(new_offset, int):
        offsets = dict(state.get(OFFSET_STATE_KEY) or {})
        offsets[session_id] = new_offset
        state[OFFSET_STATE_KEY] = offsets
        state["last_reflect_at"] = time.time()
    state.pop(PENDING_REFLECT_KEY, None)
    memory_store.save_state(state)
    return state


def reflect_and_persist(
    *,
    memory_store,  # MemoryStore — avoid circular import at module load
    session_id: str,
    transcript_path: Path | str,
    client: Any = None,
) -> tuple[int, int, int]:
    """Reflect a session's delta, persist observations, advance the cursor.

    Single entry point shared by three call sites (PreCompact hook, the
    ``reflect`` MCP tool, and ``AgentHost.flush_and_rotate`` on /exit),
    so the state-key name, advance-only cursor semantics, and
    failure-does-not-advance-cursor contract stay in one place.

    Contract:

    * If ``reflect_from_jsonl`` raises or the cursor did not advance,
      nothing is written to the memory store.
    * Observations + cursor commit as a two-phase transaction: stage
      under :data:`PENDING_REFLECT_KEY` (atomic save), append, then
      clear (atomic save). Crash between stage and clear is recovered
      at the start of the next call via
      :func:`_drain_pending_reflect`.
    * State is rewritten only when there is real work (observations
      were produced OR cursor advanced on a no-op pass); a no-delta
      pass is still a no-op (no state churn).

    Returns ``(start_offset, new_offset, obs_count)`` so callers can log
    and report consistently.
    """
    from pip_agent.memory.reflect import reflect_from_jsonl

    state = _drain_pending_reflect(memory_store)
    offsets: dict[str, int] = dict(state.get(OFFSET_STATE_KEY) or {})
    start_offset = int(offsets.get(session_id, 0))

    new_offset, observations = reflect_from_jsonl(
        Path(transcript_path),
        start_offset=start_offset,
        agent_id=memory_store.agent_id,
        client=client,
    )

    if new_offset == start_offset:
        return start_offset, new_offset, len(observations)

    if observations:
        # Phase 1: stage atomically. If we crash after this the drain on
        # the next call will finish the job.
        state[PENDING_REFLECT_KEY] = {
            "session_id": session_id,
            "new_offset": new_offset,
            "observations": list(observations),
        }
        memory_store.save_state(state)

        # Phase 2a: append observations.
        memory_store.write_observations(observations)

        # Phase 2b: clear stage + advance cursor atomically.
        state.pop(PENDING_REFLECT_KEY, None)
        offsets[session_id] = new_offset
        state[OFFSET_STATE_KEY] = offsets
        state["last_reflect_at"] = time.time()
        memory_store.save_state(state)
    else:
        # Delta seen but no observations extracted (LLM said []). No
        # stage needed — just advance the cursor so we don't re-read
        # the same delta. One atomic save.
        offsets[session_id] = new_offset
        state[OFFSET_STATE_KEY] = offsets
        state["last_reflect_at"] = time.time()
        memory_store.save_state(state)

    return start_offset, new_offset, len(observations)
