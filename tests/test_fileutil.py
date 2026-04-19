"""Tests for pip_agent.fileutil — atomic_write and chunk_message."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from pip_agent.fileutil import CHANNEL_LIMITS, atomic_write, chunk_message

# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_basic_write(self, tmp_path: Path):
        p = tmp_path / "test.json"
        atomic_write(p, '{"key": "value"}')
        assert p.read_text(encoding="utf-8") == '{"key": "value"}'

    def test_overwrites_existing(self, tmp_path: Path):
        p = tmp_path / "test.json"
        p.write_text("old", encoding="utf-8")
        atomic_write(p, "new")
        assert p.read_text(encoding="utf-8") == "new"

    def test_creates_parent_dirs(self, tmp_path: Path):
        p = tmp_path / "sub" / "deep" / "file.txt"
        atomic_write(p, "hello")
        assert p.read_text(encoding="utf-8") == "hello"

    def test_no_leftover_tmp_on_success(self, tmp_path: Path):
        p = tmp_path / "test.json"
        atomic_write(p, "data")
        tmps = list(tmp_path.glob(".tmp.*"))
        assert tmps == []

    def test_no_leftover_tmp_on_error(self, tmp_path: Path):
        """If the write raises, the tmp file should be cleaned up."""
        p = tmp_path / "test.json"

        class WriteError(Exception):
            pass

        original_replace = os.replace

        def bad_replace(src, dst):
            raise WriteError("boom")

        try:
            os.replace = bad_replace  # type: ignore[assignment]
            with pytest.raises(WriteError):
                atomic_write(p, "data")
        finally:
            os.replace = original_replace  # type: ignore[assignment]

        assert not p.exists()
        tmps = list(tmp_path.glob(".tmp.*"))
        assert tmps == []

    def test_unicode_content(self, tmp_path: Path):
        p = tmp_path / "unicode.txt"
        atomic_write(p, "你好世界 🌍")
        assert p.read_text(encoding="utf-8") == "你好世界 🌍"

    def test_sequential_writes_from_threads(self, tmp_path: Path):
        """Multiple threads writing sequentially — final value is the last write."""
        p = tmp_path / "shared.txt"

        def writer(value: str) -> None:
            atomic_write(p, value)

        for i in range(4):
            t = threading.Thread(target=writer, args=(f"v{i}",))
            t.start()
            t.join()

        content = p.read_text(encoding="utf-8")
        assert content == "v3"
        tmps = list(tmp_path.glob(".tmp.*"))
        assert tmps == []


# ---------------------------------------------------------------------------
# chunk_message
# ---------------------------------------------------------------------------

class TestChunkMessage:
    def test_empty_text(self):
        assert chunk_message("") == []

    def test_short_text_no_split(self):
        assert chunk_message("hello world") == ["hello world"]

    def test_within_limit_returns_single(self):
        text = "A" * 2000
        assert chunk_message(text, "wechat") == [text]

    def test_splits_on_paragraph_boundary(self):
        part1 = "A" * 1800
        part2 = "B" * 500
        text = part1 + "\n\n" + part2
        chunks = chunk_message(text, "wechat")
        assert len(chunks) == 2
        assert chunks[0] == part1
        assert chunks[1] == part2

    def test_splits_on_newline(self):
        part1 = "C" * 1900
        part2 = "D" * 500
        text = part1 + "\n" + part2
        chunks = chunk_message(text, "wechat")
        assert len(chunks) == 2

    def test_hard_split_no_boundary(self):
        text = "X" * 4500
        chunks = chunk_message(text, "wechat")
        assert len(chunks) >= 3
        assert all(len(c) <= CHANNEL_LIMITS["wechat"] for c in chunks)
        assert "".join(chunks) == text

    def test_respects_channel_limit(self):
        text = "Y" * 5000
        wechat_chunks = chunk_message(text, "wechat")
        default_chunks = chunk_message(text, "default")
        assert all(len(c) <= 2000 for c in wechat_chunks)
        assert all(len(c) <= 4096 for c in default_chunks)
        assert len(wechat_chunks) > len(default_chunks)

    def test_unknown_channel_uses_default(self):
        text = "Z" * 5000
        chunks = chunk_message(text, "unknown_platform")
        assert all(len(c) <= CHANNEL_LIMITS["default"] for c in chunks)

    def test_preserves_all_content(self):
        paragraphs = [f"Paragraph {i}: {'W' * 300}" for i in range(10)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_message(text, "wechat")
        reassembled = "\n\n".join(chunks)
        for p in paragraphs:
            assert p in reassembled
