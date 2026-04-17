"""Phase 8: Comprehensive test suite for the refactored memory system.

Covers: reflect (B), consolidate (C), scheduler/Dream (D),
MemoryStore (E), and integration (F).
"""

from __future__ import annotations

import json
import time
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from pip_agent.memory import MemoryStore
from pip_agent.memory.reflect import reflect, _format_transcript, _load_transcripts
from pip_agent.memory.consolidate import (
    consolidate, distill_axioms, _load_sop, MAX_MEMORIES,
)
from pip_agent.config import settings
from pip_agent.memory.scheduler import MemoryScheduler
from pip_agent.memory.utils import extract_json_array


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_llm_response(text: str):
    block = SimpleNamespace(text=text)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


def _write_transcript(transcripts_dir: Path, ts: int, messages: list[dict]) -> Path:
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    fp = transcripts_dir / f"{ts}.json"
    fp.write_text(json.dumps(messages), encoding="utf-8")
    return fp


# ---------------------------------------------------------------------------
# Test Group: extract_json_array utility
# ---------------------------------------------------------------------------

class TestExtractJsonArray:
    def test_plain_json(self):
        assert extract_json_array('[{"a": 1}]') == [{"a": 1}]

    def test_markdown_fenced(self):
        text = '```json\n[{"a": 1}]\n```'
        assert extract_json_array(text) == [{"a": 1}]

    def test_fenced_with_leading_whitespace(self):
        text = '  ```json\n[{"a": 1}]\n```'
        assert extract_json_array(text) == [{"a": 1}]

    def test_non_array_json_falls_back_to_regex(self):
        """Object wrapping an array: regex fallback extracts the inner array."""
        result = extract_json_array('{"obs": [1,2]}')
        assert result == [1, 2]

    def test_pure_object_no_array_returns_none(self):
        assert extract_json_array('{"key": "value"}') is None

    def test_garbage_returns_none(self):
        assert extract_json_array("not json at all") is None

    def test_multiple_fences_extracts_array(self):
        text = 'Some text\n```json\n[1, 2, 3]\n```\nMore text'
        assert extract_json_array(text) == [1, 2, 3]

    def test_empty_array(self):
        assert extract_json_array("[]") == []


# ---------------------------------------------------------------------------
# Test Group B: reflect.py
# ---------------------------------------------------------------------------

class TestReflectTranscriptLoading:
    def test_loads_transcripts_since_timestamp(self, tmp_path):
        tdir = tmp_path / "transcripts"
        base = int(time.time())
        _write_transcript(tdir, base - 100, [
            {"role": "user", "content": "old message"},
            {"role": "assistant", "content": "old reply"},
        ])
        _write_transcript(tdir, base + 100, [
            {"role": "user", "content": "new message"},
            {"role": "assistant", "content": "new reply"},
        ])
        results = _load_transcripts(tdir, "test-agent", base)
        assert len(results) == 1
        assert "new message" in results[0]

    def test_skips_tool_only_transcripts(self, tmp_path):
        tdir = tmp_path / "transcripts"
        ts = int(time.time())
        _write_transcript(tdir, ts, [
            {"role": "user", "content": "query"},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "bash", "input": {}}]},
        ])
        results = _load_transcripts(tdir, "test", 0)
        assert len(results) == 0

    def test_transcript_header_contains_absolute_time(self, tmp_path):
        tdir = tmp_path / "transcripts"
        ts = 1713200000  # fixed epoch
        _write_transcript(tdir, ts, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        results = _load_transcripts(tdir, "test", 0)
        assert len(results) == 1
        assert "UTC" in results[0]
        assert "Transcript at" in results[0]


class TestReflect:
    def test_returns_observations_from_llm(self, tmp_path):
        tdir = tmp_path / "transcripts"
        ts = int(time.time())
        _write_transcript(tdir, ts, [
            {"role": "user", "content": "I prefer simple solutions"},
            {"role": "assistant", "content": "Understood, keeping it simple."},
        ])
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response(
            json.dumps([{"text": "User prefers simplicity", "category": "preference"}])
        )
        obs = reflect(mock_client, tdir, "test", 0, model="test-model")
        assert len(obs) == 1
        assert obs[0]["text"] == "User prefers simplicity"
        assert obs[0]["source"] == "auto"

    def test_handles_markdown_fenced_response(self, tmp_path):
        tdir = tmp_path / "transcripts"
        _write_transcript(tdir, int(time.time()), [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "reply"},
        ])
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response(
            '```json\n[{"text": "obs1", "category": "decision"}]\n```'
        )
        obs = reflect(mock_client, tdir, "test", 0, model="test")
        assert len(obs) == 1

    def test_handles_invalid_json_gracefully(self, tmp_path):
        tdir = tmp_path / "transcripts"
        _write_transcript(tdir, int(time.time()), [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "reply"},
        ])
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response("not json")
        obs = reflect(mock_client, tdir, "test", 0, model="test")
        assert obs == []


# ---------------------------------------------------------------------------
# Test Group C: consolidate.py
# ---------------------------------------------------------------------------

class TestConsolidate:
    def test_preserves_memories_on_empty_llm_response(self):
        """ROB-2: empty array from LLM should not wipe memories."""
        existing = [
            {"id": "a", "text": "mem1", "count": 5, "source": "auto"},
            {"id": "b", "text": "mem2", "count": 3, "source": "auto"},
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response("[]")
        result = consolidate(mock_client, [{"text": "obs"}], existing, 1, model="test")
        assert len(result) == 2  # preserved originals

    def test_preserves_on_drastic_reduction(self):
        existing = [{"id": str(i), "text": f"mem{i}", "count": 5, "source": "auto"} for i in range(10)]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response(
            json.dumps([{"id": "0", "text": "mem0", "count": 6}])
        )
        result = consolidate(mock_client, [{"text": "obs"}], existing, 1, model="test")
        assert len(result) == 10  # preserved originals (>80% drop)

    def test_assigns_uuid_to_new_memories(self):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response(
            json.dumps([{"text": "new mem", "count": 1}])
        )
        result = consolidate(mock_client, [{"text": "obs"}], [], 1, model="test")
        assert len(result) == 1
        assert "id" in result[0]
        assert len(result[0]["id"]) == 12

    def test_handles_llm_failure_gracefully(self):
        existing = [{"id": "a", "text": "mem1", "count": 5}]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")
        result = consolidate(mock_client, [{"text": "obs"}], existing, 1, model="test")
        assert result == existing

    def test_caps_at_max_memories(self):
        big_list = [
            {"id": str(i), "text": f"mem{i}", "count": i} for i in range(250)
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response(
            json.dumps(big_list[:MAX_MEMORIES])
        )
        result = consolidate(mock_client, [], big_list, 1, model="test")
        assert len(result) <= MAX_MEMORIES


class TestDistillAxioms:
    def test_filters_by_count_and_stability(self):
        memories = [
            {"text": "high", "count": 10, "stability": 0.8},
            {"text": "low-count", "count": 2, "stability": 0.9},
            {"text": "low-stability", "count": 10, "stability": 0.1},
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response("- High principle")
        result = distill_axioms(mock_client, memories, model="test")
        assert result == "- High principle"
        call_prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "1 items" in call_prompt

    def test_returns_empty_when_no_candidates(self):
        memories = [{"text": "weak", "count": 1, "stability": 0.1}]
        mock_client = MagicMock()
        result = distill_axioms(mock_client, memories, model="test")
        assert result == ""
        mock_client.messages.create.assert_not_called()


class TestSopLoading:
    def test_sop_file_loads_sections(self):
        sections = _load_sop()
        assert "L1 Reflection Rules" in sections
        assert "L2 Consolidation Rules" in sections
        assert "L3 Axiom Distillation Rules" in sections
        assert "Global Constraints" in sections

    def test_l2_section_mentions_conflict(self):
        sections = _load_sop()
        l2 = sections.get("L2 Consolidation Rules", "")
        assert "CONFLICT" in l2 or "conflict" in l2.lower()


# ---------------------------------------------------------------------------
# Test Group D: scheduler.py (transcript-count trigger + Dream)
# ---------------------------------------------------------------------------

class TestSchedulerReflectTrigger:
    def test_triggers_reflect_when_threshold_reached(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        tdir = tmp_path / "transcripts"
        base_ts = int(time.time())
        for i in range(settings.reflect_transcript_threshold + 1):
            _write_transcript(tdir, base_ts + i, [
                {"role": "user", "content": f"msg {i}"},
                {"role": "assistant", "content": f"reply {i}"},
            ])

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response("[]")
        stop = threading.Event()
        sched = MemoryScheduler(store, mock_client, tdir, stop, model="test")
        sched._tick()
        mock_client.messages.create.assert_called_once()

    def test_does_not_trigger_below_threshold(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        tdir = tmp_path / "transcripts"
        _write_transcript(tdir, int(time.time()), [
            {"role": "user", "content": "msg"},
            {"role": "assistant", "content": "reply"},
        ])

        mock_client = MagicMock()
        stop = threading.Event()
        sched = MemoryScheduler(store, mock_client, tdir, stop, model="test")
        sched._tick()
        mock_client.messages.create.assert_not_called()

    def test_updates_last_reflect_transcript_ts(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        tdir = tmp_path / "transcripts"
        base_ts = int(time.time())
        for i in range(settings.reflect_transcript_threshold + 1):
            _write_transcript(tdir, base_ts + i, [
                {"role": "user", "content": f"msg {i}"},
                {"role": "assistant", "content": f"reply {i}"},
            ])

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response("[]")
        stop = threading.Event()
        sched = MemoryScheduler(store, mock_client, tdir, stop, model="test")
        sched._tick()

        state = store.load_state()
        assert state.get("last_reflect_transcript_ts", 0) > 0


class TestSchedulerTranscriptCleanup:
    def test_removes_old_processed_transcripts(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        tdir = tmp_path / "transcripts"
        now = int(time.time())
        old_ts = now - 8 * 86400  # 8 days ago

        _write_transcript(tdir, old_ts, [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ])
        _write_transcript(tdir, now, [
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new reply"},
        ])

        state = {"last_reflect_transcript_ts": now}
        stop = threading.Event()
        mock_client = MagicMock()
        sched = MemoryScheduler(store, mock_client, tdir, stop, model="test")
        sched._cleanup_transcripts(state, time.time())

        remaining = list(tdir.glob("*.json"))
        assert len(remaining) == 1
        assert remaining[0].stem == str(now)


class TestSchedulerDream:
    def test_dream_does_not_trigger_outside_hour(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        # Write enough observations
        for i in range(settings.dream_min_observations):
            store.write_observations([{"ts": time.time(), "text": f"obs {i}", "category": "test", "source": "auto"}])

        state = {}
        now = time.time()
        stop = threading.Event()
        mock_client = MagicMock()
        sched = MemoryScheduler(store, mock_client, tmp_path / "t", stop, model="test")

        # Patch datetime.fromtimestamp to return a time outside Dream hour
        non_dream_dt = datetime(2026, 4, 15, 14, 0, 0)
        with patch("pip_agent.memory.scheduler.datetime") as mock_dt:
            mock_dt.fromtimestamp.return_value = non_dream_dt
            assert sched._should_dream(state, now) is False

    def test_dream_triggers_at_correct_hour(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        for i in range(settings.dream_min_observations):
            store.write_observations([{"ts": time.time(), "text": f"obs {i}", "category": "test", "source": "auto"}])

        state = {"last_activity_at": time.time() - 3600}
        now = time.time()
        stop = threading.Event()
        mock_client = MagicMock()
        sched = MemoryScheduler(store, mock_client, tmp_path / "t", stop, model="test")

        dream_dt = datetime(2026, 4, 15, settings.dream_hour, 30, 0)
        with patch("pip_agent.memory.scheduler.datetime") as mock_dt:
            mock_dt.fromtimestamp.return_value = dream_dt
            assert sched._should_dream(state, now) is True

    def test_dream_blocked_when_active(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        for i in range(settings.dream_min_observations):
            store.write_observations([{"ts": time.time(), "text": f"obs {i}", "category": "test", "source": "auto"}])

        active = threading.Event()
        active.set()  # agent is active
        state = {}
        stop = threading.Event()
        mock_client = MagicMock()
        sched = MemoryScheduler(store, mock_client, tmp_path / "t", stop, model="test", active_event=active)

        dream_dt = datetime(2026, 4, 15, settings.dream_hour, 30, 0)
        with patch("pip_agent.memory.scheduler.datetime") as mock_dt:
            mock_dt.fromtimestamp.return_value = dream_dt
            assert sched._should_dream(state, time.time()) is False

    def test_dream_clears_observations(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        for i in range(settings.dream_min_observations):
            store.write_observations([{"ts": time.time(), "text": f"obs {i}", "category": "test", "source": "auto"}])
        assert len(store.load_all_observations()) >= settings.dream_min_observations

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response("[]")
        stop = threading.Event()
        sched = MemoryScheduler(store, mock_client, tmp_path / "t", stop, model="test")

        state = store.load_state()
        state["last_reflect_transcript_ts"] = int(time.time())
        sched._run_dream(state, time.time())

        assert len(store.load_all_observations()) == 0
        updated_state = store.load_state()
        assert "last_dream_at" in updated_state
        assert updated_state.get("last_reflect_transcript_ts") == state["last_reflect_transcript_ts"]


# ---------------------------------------------------------------------------
# Test Group E: MemoryStore
# ---------------------------------------------------------------------------

class TestMemoryStoreClearObservations:
    def test_clear_observations(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        store.write_observations([{"ts": 1.0, "text": "obs1", "category": "test", "source": "auto"}])
        store.write_observations([{"ts": 2.0, "text": "obs2", "category": "test", "source": "auto"}])
        assert len(store.load_all_observations()) == 2

        count = store.clear_observations()
        assert count >= 1
        assert store.load_all_observations() == []

    def test_clear_observations_empty_dir(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        count = store.clear_observations()
        assert count == 0


class TestMemoryStoreThreadSafety:
    def test_concurrent_writes_dont_crash(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        errors = []

        def write_obs():
            try:
                for i in range(10):
                    store.write_observations([{"ts": time.time(), "text": f"obs {i}", "category": "test", "source": "auto"}])
            except Exception as e:
                errors.append(e)

        def write_state():
            try:
                for i in range(10):
                    state = store.load_state()
                    state["counter"] = i
                    store.save_state(state)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=write_obs)
        t2 = threading.Thread(target=write_state)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == [], f"Concurrent writes raised: {errors}"


class TestMemoryStoreSearchFallback:
    def test_fallback_to_observations_has_proper_fields(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        store.write_observations([{
            "ts": time.time(),
            "text": "User prefers dark mode",
            "category": "preference",
            "source": "auto",
        }])
        results = store.search("dark mode")
        assert len(results) == 1
        assert "score" in results[0]
        assert "text" in results[0]


class TestMemoryStoreCorruptFiles:
    def test_corrupt_memories_json_returns_empty(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        (store.agent_dir / "memories.json").write_text("NOT JSON", encoding="utf-8")
        assert store.load_memories() == []

    def test_corrupt_observation_line_skipped(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        obs_path = store.agent_dir / "observations" / "2026-04-15.jsonl"
        obs_path.write_text(
            '{"ts": 1.0, "text": "good", "category": "test", "source": "auto"}\n'
            'NOT JSON\n'
            '{"ts": 2.0, "text": "also good", "category": "test", "source": "auto"}\n',
            encoding="utf-8",
        )
        obs = store.load_all_observations()
        assert len(obs) == 2

    def test_corrupt_state_returns_empty(self, tmp_path):
        store = MemoryStore(tmp_path / "agents", "test-agent")
        (store.agent_dir / "state.json").write_text("{bad", encoding="utf-8")
        assert store.load_state() == {}


# ---------------------------------------------------------------------------
# Test Group F: Integration
# ---------------------------------------------------------------------------

class TestReflectToolIntegration:
    def test_reflect_tool_dispatch(self, tmp_path):
        from pip_agent.tool_dispatch import ToolContext, DispatchResult

        store = MemoryStore(tmp_path / "agents", "test-agent")
        tdir = tmp_path / "transcripts"
        _write_transcript(tdir, int(time.time()), [
            {"role": "user", "content": "I like simple code"},
            {"role": "assistant", "content": "OK, keeping it simple."},
        ])

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_llm_response(
            json.dumps([{"text": "User prefers simplicity", "category": "preference"}])
        )

        ctx = ToolContext(
            memory_store=store,
            client=mock_client,
            transcripts_dir=tdir,
            messages=[
                {"role": "user", "content": "current conversation"},
                {"role": "assistant", "content": "current reply"},
            ],
        )

        from pip_agent.tool_dispatch import _handle_reflect
        result = _handle_reflect(ctx, {})
        assert "1 observations" in result.content

        state = store.load_state()
        assert "last_reflect_transcript_ts" in state


class TestTranscriptSaving:
    def test_save_transcript_creates_file(self, tmp_path):
        from pip_agent.compact import save_transcript
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        path = save_transcript(messages, tmp_path)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 2


class TestMemorySearchIntegration:
    def test_memory_search_dispatch(self, tmp_path):
        from pip_agent.tool_dispatch import _handle_memory_search, ToolContext

        store = MemoryStore(tmp_path / "agents", "test-agent")
        store.save_memories([{
            "id": "abc",
            "text": "User prefers dark mode",
            "count": 5,
            "first_seen": time.time(),
            "last_reinforced": time.time(),
            "category": "preference",
            "contexts": ["decision"],
            "total_cycles": 5,
            "stability": 0.8,
            "source": "auto",
        }])

        ctx = ToolContext(memory_store=store)
        result = _handle_memory_search(ctx, {"query": "dark mode"})
        assert "dark mode" in result.content
