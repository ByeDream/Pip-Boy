"""Tests for memory pipeline: recall, reflect, consolidate, scheduler."""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pip_agent.memory import MemoryStore
from pip_agent.memory.recall import search_memories, tokenize, temporal_decay
from pip_agent.memory.reflect import reflect, _format_transcript, _load_transcripts
from pip_agent.memory.consolidate import consolidate, distill_axioms
from pip_agent.memory.scheduler import MemoryScheduler


# ---------------------------------------------------------------------------
# recall.py
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_english_words(self):
        tokens = tokenize("Hello World 123")
        assert "hello" in tokens
        assert "world" in tokens
        assert "123" in tokens

    def test_cjk_characters(self):
        tokens = tokenize("测试中文")
        assert "测" in tokens
        assert "试" in tokens

    def test_mixed(self):
        tokens = tokenize("Hello 世界 test")
        assert "hello" in tokens
        assert "世" in tokens
        assert "界" in tokens
        assert "test" in tokens

    def test_empty(self):
        assert tokenize("") == []


class TestTemporalDecay:
    def test_recent_is_near_one(self):
        assert temporal_decay(time.time(), half_life_days=30.0) == pytest.approx(1.0, abs=0.01)

    def test_old_is_less(self):
        thirty_days_ago = time.time() - 30 * 86400
        val = temporal_decay(thirty_days_ago, half_life_days=30.0)
        assert 0.4 < val < 0.6

    def test_future_returns_one(self):
        assert temporal_decay(time.time() + 86400) == 1.0


class TestSearchMemories:
    def test_empty_returns_empty(self):
        assert search_memories("hello", []) == []

    def test_blank_query_returns_empty(self):
        memories = [{"text": "something", "last_reinforced": time.time()}]
        assert search_memories("", memories) == []

    def test_finds_matching_memory(self):
        now = time.time()
        memories = [
            {"text": "The user prefers dark mode", "last_reinforced": now},
            {"text": "The user likes Python", "last_reinforced": now},
            {"text": "The user drinks coffee", "last_reinforced": now},
        ]
        results = search_memories("dark mode preference", memories, top_k=2)
        assert len(results) <= 2
        assert any("dark mode" in r["text"] for r in results)

    def test_results_have_score(self):
        now = time.time()
        memories = [{"text": "test memory content", "last_reinforced": now}]
        results = search_memories("test memory", memories)
        assert len(results) == 1
        assert "score" in results[0]


# ---------------------------------------------------------------------------
# reflect.py
# ---------------------------------------------------------------------------


class TestFormatTranscript:
    def test_basic_messages(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        text = _format_transcript(messages)
        assert "[USER]" in text
        assert "[ASSISTANT]" in text

    def test_list_content(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Hello world"}]},
        ]
        text = _format_transcript(messages)
        assert "Hello world" in text


class TestLoadTranscripts:
    def test_loads_recent_files(self, tmp_path):
        now = int(time.time())
        data = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        (tmp_path / f"{now}.json").write_text(json.dumps(data), encoding="utf-8")
        result = _load_transcripts(tmp_path, "agent-1", since=now - 100)
        assert len(result) == 1
        assert "Hello" in result[0]

    def test_skips_old_files(self, tmp_path):
        data = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]
        (tmp_path / "1000000.json").write_text(json.dumps(data), encoding="utf-8")
        result = _load_transcripts(tmp_path, "agent-1", since=time.time() - 100)
        assert len(result) == 0

    def test_empty_dir(self, tmp_path):
        assert _load_transcripts(tmp_path, "agent-1", since=0) == []


class TestReflect:
    def test_no_transcripts_returns_empty(self, tmp_path):
        client = MagicMock()
        result = reflect(client, tmp_path, "agent-1", time.time())
        assert result == []
        client.messages.create.assert_not_called()

    def test_valid_llm_response(self, tmp_path):
        now = int(time.time())
        data = [
            {"role": "user", "content": "I prefer concise answers"},
            {"role": "assistant", "content": "Understood."},
        ]
        (tmp_path / f"{now}.json").write_text(json.dumps(data), encoding="utf-8")

        observations = [
            {"text": "User prefers concise communication", "category": "communication"},
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(observations))],
        )

        result = reflect(mock_client, tmp_path, "agent-1", now - 100, model="test-model")
        assert len(result) == 1
        assert "concise" in result[0]["text"]
        assert result[0]["source"] == "auto"

    def test_invalid_json_returns_empty(self, tmp_path):
        now = int(time.time())
        data = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "reply"},
        ]
        (tmp_path / f"{now}.json").write_text(json.dumps(data), encoding="utf-8")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="not valid json")],
        )
        result = reflect(mock_client, tmp_path, "agent-1", now - 100, model="test-model")
        assert result == []


# ---------------------------------------------------------------------------
# consolidate.py
# ---------------------------------------------------------------------------


class TestConsolidate:
    def test_empty_input(self):
        client = MagicMock()
        assert consolidate(client, [], [], 1, model="test") == []
        client.messages.create.assert_not_called()

    def test_valid_consolidation(self):
        observations = [{"ts": time.time(), "text": "User values clarity", "category": "value"}]
        existing = []
        updated_memories = [
            {
                "id": "m1",
                "text": "User values clarity",
                "count": 1,
                "category": "value",
                "first_seen": time.time(),
                "last_reinforced": time.time(),
                "contexts": ["value"],
                "total_cycles": 1,
                "stability": 1.0,
                "source": "auto",
            }
        ]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(updated_memories))],
        )

        result = consolidate(mock_client, observations, existing, 1, model="test")
        assert len(result) == 1
        assert result[0]["text"] == "User values clarity"

    def test_llm_failure_returns_existing(self):
        existing = [{"id": "m1", "text": "old", "count": 5}]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API down")

        result = consolidate(mock_client, [{"text": "new"}], existing, 1, model="test")
        assert result == existing


class TestDistillAxioms:
    def test_no_candidates(self):
        client = MagicMock()
        memories = [{"text": "weak", "count": 1, "stability": 0.1}]
        assert distill_axioms(client, memories, model="test") == ""
        client.messages.create.assert_not_called()

    def test_produces_axioms(self):
        memories = [
            {"text": "Values code quality", "count": 10, "stability": 0.8},
        ]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="- Prioritizes code quality over speed")],
        )
        result = distill_axioms(mock_client, memories, model="test")
        assert "code quality" in result


# ---------------------------------------------------------------------------
# scheduler.py
# ---------------------------------------------------------------------------


class TestMemoryScheduler:
    def test_tick_triggers_reflect(self, tmp_path):
        from pip_agent.config import settings

        store = MemoryStore(base_dir=tmp_path, agent_id="test-agent")
        store.save_state({"last_reflect_transcript_ts": 0})

        now = int(time.time())
        transcripts_dir = store.agent_dir / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        data = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        for i in range(settings.reflect_transcript_threshold + 1):
            (transcripts_dir / f"{now + i}.json").write_text(
                json.dumps(data), encoding="utf-8",
            )

        observations = [{"text": "observation one", "category": "decision"}]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(observations))],
        )

        stop = threading.Event()
        sched = MemoryScheduler(
            store, mock_client, transcripts_dir, stop, model="test-model",
        )
        sched._tick()

        state = store.load_state()
        assert state["last_reflect_at"] > 0
        assert state["last_reflect_transcript_ts"] > 0
        assert len(store.load_all_observations()) >= 1

    def test_stop_event_ends_loop(self, tmp_path):
        store = MemoryStore(base_dir=tmp_path, agent_id="test-agent")
        mock_client = MagicMock()
        stop = threading.Event()
        stop.set()

        sched = MemoryScheduler(
            store, mock_client, tmp_path, stop, model="test-model",
        )
        sched.run()
