from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pip_agent.compact import (
    _session_transcripts,
    auto_compact,
    estimate_tokens,
    micro_compact,
    save_transcript,
    summarize_messages,
)


def _tool_use_block(name: str, tool_input: dict, block_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def _text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_messages_with_tool_rounds(
    n_rounds: int, tool_name: str = "bash"
) -> list[dict]:
    """Build a message list with *n_rounds* of tool-use exchanges."""
    msgs: list[dict] = [{"role": "user", "content": "Do something for me."}]
    for i in range(n_rounds):
        block_id = f"tu_{i}"
        msgs.append({
            "role": "assistant",
            "content": [_tool_use_block(tool_name, {"command": f"cmd_{i}"}, block_id)],
        })
        msgs.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": f"output of cmd_{i} " * 200,
                }
            ],
        })
    return msgs


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_messages(self):
        assert estimate_tokens([]) == 0

    def test_proportional_to_content(self):
        small = [{"role": "user", "content": "hi"}]
        large = [{"role": "user", "content": "x" * 4000}]
        assert estimate_tokens(large) > estimate_tokens(small)

    def test_rough_accuracy(self):
        msgs = [{"role": "user", "content": "a" * 400}]
        tokens = estimate_tokens(msgs)
        assert 80 < tokens < 200


# ---------------------------------------------------------------------------
# micro_compact
# ---------------------------------------------------------------------------


class TestMicroCompact:
    def test_no_replacement_when_fresh(self):
        msgs = _make_messages_with_tool_rounds(2)
        replaced = micro_compact(msgs, max_age=3)
        assert replaced == 0

    def test_replaces_old_tool_results(self):
        msgs = _make_messages_with_tool_rounds(5)
        replaced = micro_compact(msgs, max_age=2)
        assert replaced == 3
        old_content = msgs[2]["content"][0]["content"]
        assert old_content.startswith("[Previous:")
        assert "bash" in old_content

    def test_preserves_recent_tool_results(self):
        msgs = _make_messages_with_tool_rounds(5)
        micro_compact(msgs, max_age=2)
        recent = msgs[-1]["content"][0]["content"]
        assert not recent.startswith("[Previous:")

    def test_idempotent(self):
        msgs = _make_messages_with_tool_rounds(5)
        micro_compact(msgs, max_age=2)
        replaced = micro_compact(msgs, max_age=2)
        assert replaced == 0

    def test_no_tool_results_is_noop(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [_text_block("hi there")]},
        ]
        replaced = micro_compact(msgs, max_age=1)
        assert replaced == 0

    def test_empty_messages(self):
        replaced = micro_compact([], max_age=3)
        assert replaced == 0

    def test_exact_age_boundary(self):
        msgs = _make_messages_with_tool_rounds(3)
        replaced = micro_compact(msgs, max_age=3)
        assert replaced == 0

    def test_one_over_boundary(self):
        msgs = _make_messages_with_tool_rounds(4)
        replaced = micro_compact(msgs, max_age=3)
        assert replaced == 1

    def test_preserves_read_tool_results(self):
        msgs = _make_messages_with_tool_rounds(5, tool_name="read")
        replaced = micro_compact(msgs, max_age=2)
        assert replaced == 0
        for msg in msgs:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        assert not block["content"].startswith("[Previous:")

    def test_preserves_read_but_compacts_others(self):
        msgs: list[dict] = [{"role": "user", "content": "start"}]
        msgs.append({
            "role": "assistant",
            "content": [_tool_use_block("read", {"file_path": "a.py"}, "tu_r")],
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_r", "content": "file content " * 100},
            ],
        })
        msgs.append({
            "role": "assistant",
            "content": [_tool_use_block("bash", {"command": "ls"}, "tu_b")],
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_b", "content": "dir listing " * 100},
            ],
        })
        msgs.append({
            "role": "assistant",
            "content": [_tool_use_block("bash", {"command": "pwd"}, "tu_b2")],
        })
        msgs.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_b2", "content": "/home " * 100}],
        })
        replaced = micro_compact(msgs, max_age=1)
        read_result = msgs[2]["content"][0]["content"]
        bash_result = msgs[4]["content"][0]["content"]
        assert not read_result.startswith("[Previous:")
        assert bash_result.startswith("[Previous:")
        assert replaced == 1


# ---------------------------------------------------------------------------
# save_transcript
# ---------------------------------------------------------------------------


class TestSaveTranscript:
    def setup_method(self):
        _session_transcripts.clear()

    def test_creates_file(self, tmp_path: Path):
        msgs = [{"role": "user", "content": "hello"}]
        path = save_transcript(msgs, tmp_path)
        assert path.exists()
        assert path.suffix == ".json"

    def test_valid_json(self, tmp_path: Path):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [_text_block("hi")]},
        ]
        path = save_transcript(msgs, tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["role"] == "user"

    def test_creates_directory(self, tmp_path: Path):
        target = tmp_path / "nested" / "transcripts"
        msgs = [{"role": "user", "content": "test"}]
        path = save_transcript(msgs, target)
        assert target.is_dir()
        assert path.exists()

    def test_replaces_previous_for_same_session(self, tmp_path: Path):
        import time
        msgs = [{"role": "user", "content": "turn1"}]
        path1 = save_transcript(msgs, tmp_path)
        assert path1.exists()

        time.sleep(1.1)
        msgs.append({"role": "assistant", "content": "reply1"})
        path2 = save_transcript(msgs, tmp_path)

        assert path2.exists()
        assert not path1.exists(), "previous snapshot should be deleted"
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_different_sessions_kept_separate(self, tmp_path: Path):
        import time
        session_a = [{"role": "user", "content": "a"}]
        session_b = [{"role": "user", "content": "b"}]
        path_a = save_transcript(session_a, tmp_path)
        time.sleep(1.1)
        path_b = save_transcript(session_b, tmp_path)

        assert path_a.exists()
        assert path_b.exists()
        assert len(list(tmp_path.glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# summarize_messages
# ---------------------------------------------------------------------------


class TestSummarizeMessages:
    def test_returns_summary_text(self):
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[_text_block("Summary: user asked to read files.")],
            usage=SimpleNamespace(input_tokens=500, output_tokens=50),
        )
        msgs = [{"role": "user", "content": "read my files"}]
        summary, in_tok, out_tok = summarize_messages(client, msgs, "system prompt")
        assert "Summary" in summary
        assert in_tok == 500
        assert out_tok == 50

    def test_calls_profiler(self):
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[_text_block("summary")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )
        profiler = MagicMock()
        summarize_messages(
            client,
            [{"role": "user", "content": "test"}],
            "sys",
            profiler=profiler,
        )
        profiler.start.assert_called_once_with("api:compact")
        profiler.stop.assert_called_once_with(input_tokens=100, output_tokens=20)


# ---------------------------------------------------------------------------
# auto_compact
# ---------------------------------------------------------------------------


class TestAutoCompact:
    def test_replaces_messages_with_summary(self, tmp_path: Path):
        """With tail-preserving compaction, the head is summarized but recent
        turns survive untouched. The new structure is:
            [user(summary), assistant(ack), ...tail...]
        """
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[_text_block("Here is the summary.")],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=80),
        )
        msgs = _make_messages_with_tool_rounds(5)
        original_len = len(msgs)
        assert original_len > 4

        with patch("pip_agent.compact.settings") as mock_settings:
            mock_settings.verbose = False
            auto_compact(client, msgs, "system", tmp_path, model="test")

        assert len(msgs) < original_len
        assert msgs[0]["role"] == "user"
        assert "summary" in msgs[0]["content"].lower()
        assert msgs[1]["role"] == "assistant"
        assert len(msgs) >= 4

    def test_does_not_save_transcript(self, tmp_path: Path):
        """auto_compact no longer saves transcripts — that's agent_loop's job."""
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[_text_block("summary")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )
        msgs = _make_messages_with_tool_rounds(5)

        with patch("pip_agent.compact.settings") as mock_settings:
            mock_settings.verbose = False
            auto_compact(client, msgs, "system", tmp_path, model="test")

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 0

    def test_single_message_skipped(self, tmp_path: Path):
        """Small conversations (<=4 messages) are skipped entirely."""
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[_text_block("nothing to summarize")],
            usage=SimpleNamespace(input_tokens=50, output_tokens=10),
        )
        msgs = [{"role": "user", "content": "hi"}]

        with patch("pip_agent.compact.settings") as mock_settings:
            mock_settings.verbose = False
            auto_compact(client, msgs, "system", tmp_path, model="test")

        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi"
        client.messages.create.assert_not_called()
