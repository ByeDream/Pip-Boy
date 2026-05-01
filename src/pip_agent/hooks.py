"""SDK hook callbacks for Pip-Boy.

Two events from Claude Code's lifecycle feed Pip's memory pipeline:

* ``PreCompact`` — Claude Code is about to compact the session context (either
  automatically or via ``/compact``). This is Pip's primary trigger for L1
  reflection: we read the session JSONL from ``input_data['transcript_path']``
  and run :func:`pip_agent.memory.reflect.reflect_from_jsonl` over the delta
  since the last cursor.
* ``Stop`` — the agent just finished a turn. We stamp ``last_activity_at`` so
  operational UIs (``/status``) can tell when the agent last did work.

All other lifecycle events (``PreToolUse`` / ``PostToolUse`` / ``SubagentStop``
/ …) are left to Claude Code. Pip-Boy deliberately does not wrap tool
execution; profiling and permission gating are CC's responsibility.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import HookMatcher

if TYPE_CHECKING:
    from pip_agent.memory import MemoryStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PreCompact — drives reflection
# ---------------------------------------------------------------------------


def _pre_compact_hook(memory_store: MemoryStore | None):
    """Return a hook callback that runs reflect over the session JSONL.

    Execution flow:

    1. Stamp ``last_pre_compact_at`` / ``last_pre_compact_session_id`` in state
       so we can tell reflection fired even if the LLM call is skipped.
    2. Look up the per-session byte cursor in ``state[_OFFSET_KEY]``.
    3. Call :func:`reflect_from_jsonl` on the delta.
    4. Persist new observations and advance the cursor. The cursor is
       advanced whenever the LLM call returned a well-formed JSON array,
       even an empty one — an empty ``[]`` is the model's explicit
       "no high-signal observations in this delta" signal, and
       re-reading the same bytes to ask again would just burn tokens.
       The cursor is preserved only when the LLM call itself failed or
       returned malformed JSON so the next run can retry the same delta.

    All errors are swallowed with a ``log.warning`` so Claude Code's compact
    never aborts because of Pip's bookkeeping.
    """

    async def _callback(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        # PROFILE
        from pip_agent import _profile

        async with _profile.span(
            "hook.pre_compact",
            trigger=input_data.get("trigger", "?"),
        ):
            if memory_store is None:
                return {}

            transcript_path = str(input_data.get("transcript_path") or "")
            session_id = str(input_data.get("session_id") or "")

            try:
                state = memory_store.load_state()
                state["last_pre_compact_at"] = time.time()
                if session_id:
                    state["last_pre_compact_session_id"] = session_id
                if transcript_path:
                    state["last_pre_compact_transcript"] = transcript_path
                memory_store.save_state(state)
            except Exception as exc:  # noqa: BLE001
                log.warning("PreCompact: failed to stamp memory state: %s", exc)
                return {}

            if not transcript_path or not session_id:
                log.info(
                    "PreCompact: missing transcript_path or session_id; skipping reflect"
                )
                return {}

            path = Path(transcript_path)
            if not path.is_file():
                log.info("PreCompact: transcript file missing: %s", path)
                return {}

            try:
                from pip_agent.anthropic_client import build_anthropic_client
                from pip_agent.memory.reflect import reflect_and_persist

                client = build_anthropic_client()
                if client is None:
                    log.info(
                        "PreCompact: reflect skipped for session=%s — "
                        "no ANTHROPIC_API_KEY configured",
                        session_id[:8],
                    )
                    return {}

                async with _profile.span(  # PROFILE
                    "hook.pre_compact.reflect", session=session_id[:8]
                ):
                    start_offset, new_offset, obs_count = reflect_and_persist(
                        memory_store=memory_store,
                        session_id=session_id,
                        transcript_path=path,
                        client=client,
                    )
                log.info(
                    "PreCompact: reflect session=%s obs=%d offset=%d→%d trigger=%s",
                    session_id[:8], obs_count, start_offset, new_offset,
                    input_data.get("trigger", "?"),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("PreCompact: reflect failed: %s", exc)

            return {}

    return _callback


# ---------------------------------------------------------------------------
# Stop — lightweight state stamp
# ---------------------------------------------------------------------------


def _stop_hook(memory_store: MemoryStore | None):
    async def _callback(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        # PROFILE
        from pip_agent import _profile

        async with _profile.span("hook.stop"):
            if memory_store is None:
                return {}
            try:
                state = memory_store.load_state()
                state["last_activity_at"] = time.time()
                memory_store.save_state(state)
            except Exception as exc:  # noqa: BLE001
                log.warning("Stop hook: failed to update state: %s", exc)
            return {}

    return _callback


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_hooks(
    *,
    memory_store: MemoryStore | None = None,
) -> dict[str, list[HookMatcher]]:
    """Build the ``hooks`` dict for :class:`ClaudeAgentOptions`.

    Only ``PreCompact`` and ``Stop`` are registered. All other SDK events are
    intentionally left to Claude Code's native handling.
    """
    return {
        "PreCompact": [HookMatcher(hooks=[_pre_compact_hook(memory_store)])],
        "Stop": [HookMatcher(hooks=[_stop_hook(memory_store)])],
    }
