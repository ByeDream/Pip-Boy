"""Background daemon thread for scheduling memory pipeline tasks.

Reflect (L1) is triggered by transcript count.
Dream (L2 Consolidate + L3 Axioms) runs at a configured hour when the system
is inactive and enough observations have accumulated.
Old transcripts are cleaned up after reflection.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic
    from pathlib import Path
    from pip_agent.memory import MemoryStore

log = logging.getLogger(__name__)

POLL_INTERVAL = 60


class MemoryScheduler:
    """Daemon scheduler for the memory pipeline.

    Runs in a background thread.  Each tick checks whether a reflection or
    Dream cycle is due based on transcript counts, time-of-day, and activity.
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        client: anthropic.Anthropic,
        transcripts_dir: Path,
        stop_event: threading.Event,
        *,
        model: str = "",
        active_event: threading.Event | None = None,
    ) -> None:
        self.store = memory_store
        self.client = client
        self.transcripts_dir = transcripts_dir
        self.stop_event = stop_event
        self.model = model
        self.active_event = active_event

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop.  Meant to be run as a daemon thread target."""
        log.debug("MemoryScheduler started for agent %s", self.store.agent_id)
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("MemoryScheduler tick error")
            self.stop_event.wait(POLL_INTERVAL)
        log.debug("MemoryScheduler stopped for agent %s", self.store.agent_id)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        from pip_agent.config import settings

        state = self.store.load_state()
        now = time.time()

        # --- L1: Reflect when enough new transcripts have accumulated ---
        new_count = self._count_new_transcripts(state)
        if new_count >= settings.reflect_transcript_threshold:
            self._run_reflect(state, now)
            self._cleanup_transcripts(state, now)

        # --- Dream: Consolidate + Axioms ---
        if self._should_dream(state, now):
            self._run_dream(state, now)

    # ------------------------------------------------------------------
    # Transcript counting
    # ------------------------------------------------------------------

    def _count_new_transcripts(self, state: dict) -> int:
        if not self.transcripts_dir.is_dir():
            return 0
        last_ts = state.get("last_reflect_transcript_ts", 0)
        count = 0
        for fp in self.transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts > last_ts:
                count += 1
        return count

    def _latest_transcript_ts(self) -> int:
        """Return the highest timestamp among transcript files, or 0."""
        if not self.transcripts_dir.is_dir():
            return 0
        latest = 0
        for fp in self.transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts > latest:
                latest = ts
        return latest

    # ------------------------------------------------------------------
    # L1: Reflect
    # ------------------------------------------------------------------

    def _run_reflect(self, state: dict, now: float) -> None:
        from pip_agent.memory.reflect import reflect

        since = state.get("last_reflect_transcript_ts", 0)

        observations = reflect(
            self.client,
            self.transcripts_dir,
            self.store.agent_id,
            since,
            model=self.model,
        )

        if observations:
            self.store.write_observations(observations)
            log.info(
                "L1 reflect: %d observations for agent %s",
                len(observations), self.store.agent_id,
            )

        latest = self._latest_transcript_ts()
        if latest > 0:
            state["last_reflect_transcript_ts"] = latest
        state["last_reflect_at"] = now
        self.store.save_state(state)

    # ------------------------------------------------------------------
    # Transcript cleanup
    # ------------------------------------------------------------------

    def _cleanup_transcripts(self, state: dict, now: float) -> None:
        """Remove transcripts that are old AND already reflected upon."""
        if not self.transcripts_dir.is_dir():
            return
        from pip_agent.config import settings
        cutoff = now - settings.transcript_retention_days * 86400
        last_reflected_ts = state.get("last_reflect_transcript_ts", 0)
        removed = 0
        for fp in self.transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts < cutoff and ts <= last_reflected_ts:
                fp.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info("Transcript cleanup: removed %d old files for agent %s",
                     removed, self.store.agent_id)

    # ------------------------------------------------------------------
    # Dream: Consolidate (L2) + Axioms (L3)
    # ------------------------------------------------------------------

    def _should_dream(self, state: dict, now: float) -> bool:
        from pip_agent.config import settings

        local_now = datetime.fromtimestamp(now)

        if local_now.hour != settings.dream_hour:
            return False

        last_dream = state.get("last_dream_at", 0)
        if last_dream > 0:
            last_dream_date = datetime.fromtimestamp(last_dream).date()
            if last_dream_date == local_now.date():
                return False

        obs_count = len(self.store.load_all_observations())
        if obs_count < settings.dream_min_observations:
            return False

        if self.active_event is not None and self.active_event.is_set():
            return False
        last_activity = state.get("last_activity_at", 0)
        if last_activity > 0 and (now - last_activity) < settings.dream_inactive_minutes * 60:
            return False

        return True

    def _run_dream(self, state: dict, now: float) -> None:
        from pip_agent.memory.consolidate import consolidate, distill_axioms

        observations = self.store.load_all_observations()
        memories = self.store.load_memories()
        cycle = state.get("consolidate_cycle", 0) + 1

        updated = consolidate(
            self.client,
            observations,
            memories,
            cycle,
            model=self.model,
        )
        self.store.save_memories(updated)

        axioms_text = distill_axioms(self.client, updated, model=self.model)
        if axioms_text:
            self.store.save_axioms(axioms_text)

        cleared = self.store.clear_observations()

        state["last_dream_at"] = now
        state["consolidate_cycle"] = cycle
        self.store.save_state(state)

        log.info(
            "Dream complete: %d memories, axioms=%s, cleared %d obs files for agent %s",
            len(updated), bool(axioms_text), cleared, self.store.agent_id,
        )
