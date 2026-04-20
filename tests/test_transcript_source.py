"""Unit tests for ``memory.transcript_source`` — JSONL → role/content adapter."""

from __future__ import annotations

import json
from pathlib import Path

from pip_agent.memory.transcript_source import (
    iter_transcript,
    load_formatted,
    locate_session_jsonl,
    normalize_line,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


class TestNormalizeLine:
    def test_cc_wrapper_shape_user(self):
        rec = {"type": "user", "message": {"role": "user", "content": "hello"}}
        assert normalize_line(rec) == ("user", "hello")

    def test_cc_wrapper_shape_assistant_list(self):
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
        }
        assert normalize_line(rec) == ("assistant", "hi")

    def test_tool_use_block_renders_marker(self):
        rec = {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "running"},
                    {"type": "tool_use", "name": "Bash"},
                ],
            }
        }
        role, text = normalize_line(rec)
        assert role == "assistant"
        assert "running" in text
        assert "[tool:Bash]" in text

    def test_thinking_only_assistant_preserves_summary(self):
        # Observed in real CC JSONL: heartbeat-style replies carry a single
        # thinking block and no text. Previously these were dropped silently.
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "thinking",
                    "thinking": "This is a heartbeat check. Let me do the basic checks.",
                    "signature": "sig-ignored",
                }],
            },
        }
        result = normalize_line(rec)
        assert result is not None
        role, text = result
        assert role == "assistant"
        assert text.startswith("[thought]")
        assert "heartbeat check" in text

    def test_thinking_summary_collapses_whitespace_and_truncates(self):
        long_thought = "word " * 200  # > 200 chars once joined
        rec = {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "line1\n\nline2\t" + long_thought}],
        }
        role, text = normalize_line(rec)
        assert role == "assistant"
        assert "\n" not in text and "\t" not in text
        # 200-char cap on the thought body (plus the "[thought] " prefix).
        assert len(text) <= len("[thought] ") + 200

    def test_thinking_with_trailing_text_keeps_both(self):
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "pondering"},
                    {"type": "text", "text": "HEARTBEAT_OK"},
                ],
            },
        }
        role, text = normalize_line(rec)
        assert role == "assistant"
        assert "[thought] pondering" in text
        assert "HEARTBEAT_OK" in text

    def test_flat_anthropic_shape(self):
        rec = {"role": "user", "content": "plain text"}
        assert normalize_line(rec) == ("user", "plain text")

    def test_unknown_role_returns_none(self):
        assert normalize_line({"role": "system", "content": "x"}) is None

    def test_empty_content_returns_none(self):
        assert normalize_line({"role": "user", "content": ""}) is None

    def test_unrecognised_shape_returns_none(self):
        assert normalize_line({"foo": "bar"}) is None

    def test_compact_summary_turn_is_dropped(self):
        """CC writes its own ``/compact`` summaries into the JSONL as user
        turns with ``isCompactSummary: true``. Feeding them to reflect causes
        the previous conversation's content to be re-extracted as new
        observations — double-counting. Must return None.
        """
        rec = {
            "type": "user",
            "isCompactSummary": True,
            "message": {
                "role": "user",
                "content": "This session is being continued ... Summary: user is the developer ...",
            },
        }
        assert normalize_line(rec) is None

    def test_meta_turn_is_dropped(self):
        """``isMeta: true`` covers ``<local-command-caveat>`` / system-echoed
        slash-command turns. They're CC UI scaffolding, not real dialogue.
        """
        rec = {
            "type": "user",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": "<local-command-caveat>...</local-command-caveat>",
            },
        }
        assert normalize_line(rec) is None

    def test_visible_in_transcript_only_is_dropped(self):
        """Another CC-internal flag for turns shown in the UI transcript but
        not part of the real message stream.
        """
        rec = {
            "type": "user",
            "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": "internal scaffolding"},
        }
        assert normalize_line(rec) is None

    def test_real_user_turn_without_flags_still_passes(self):
        """Sanity: the filter must NOT over-match on ordinary turns."""
        rec = {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
        }
        assert normalize_line(rec) == ("user", "hello")


class TestIterTranscript:
    def test_yields_lines_with_offsets(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ])
        results = list(iter_transcript(path))
        assert len(results) == 2
        assert results[0][1]["content"] == "one"
        assert results[1][1]["content"] == "two"
        # Offsets are monotonically increasing.
        assert results[0][0] < results[1][0]

    def test_start_offset_skips_earlier_lines(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        first_offset = next(iter_transcript(path))[0]
        rest = list(iter_transcript(path, start_offset=first_offset))
        assert [r[1]["content"] for r in rest] == ["two", "three"]

    def test_malformed_lines_skipped(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            '{"role": "user", "content": "ok"}\n'
            "not json\n"
            '{"role": "assistant", "content": "also ok"}\n',
            encoding="utf-8",
        )
        results = list(iter_transcript(path))
        assert len(results) == 2
        assert results[0][1]["content"] == "ok"
        assert results[1][1]["content"] == "also ok"

    def test_missing_file_yields_nothing(self, tmp_path: Path):
        assert list(iter_transcript(tmp_path / "missing.jsonl")) == []


class TestLoadFormatted:
    def test_renders_role_prefix(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        offset, text = load_formatted(path)
        assert "[USER] hi" in text
        assert "[ASSISTANT] hello" in text
        assert offset > 0

    def test_delta_reads_only_new_lines(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [{"role": "user", "content": "first"}])
        offset1, text1 = load_formatted(path)
        assert "first" in text1

        # Append a new line and re-read from offset1.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "assistant", "content": "second"}) + "\n")
        offset2, text2 = load_formatted(path, start_offset=offset1)
        assert "second" in text2
        assert "first" not in text2
        assert offset2 > offset1

    def test_max_chars_truncates(self, tmp_path: Path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
        ])
        _, text = load_formatted(path, max_chars=100)
        assert "[truncated]" in text


class TestLocateSessionJsonl:
    def test_returns_match_when_present(self, tmp_path: Path):
        root = tmp_path / "projects"
        project = root / "dash-some-cwd"
        project.mkdir(parents=True)
        target = project / "abc123.jsonl"
        target.write_text("{}\n", encoding="utf-8")

        found = locate_session_jsonl("abc123", projects_root=root)
        assert found == target

    def test_returns_none_when_missing(self, tmp_path: Path):
        assert locate_session_jsonl("nosuchsession", projects_root=tmp_path) is None

    def test_empty_session_id_returns_none(self, tmp_path: Path):
        assert locate_session_jsonl("", projects_root=tmp_path) is None

    def test_picks_newest_when_duplicated(self, tmp_path: Path):
        root = tmp_path / "projects"
        p1 = root / "a"
        p2 = root / "b"
        p1.mkdir(parents=True)
        p2.mkdir(parents=True)
        old = p1 / "dup.jsonl"
        new = p2 / "dup.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        new.write_text("{}\n", encoding="utf-8")
        import os
        import time
        os.utime(old, (time.time() - 1000, time.time() - 1000))

        found = locate_session_jsonl("dup", projects_root=root)
        assert found == new
