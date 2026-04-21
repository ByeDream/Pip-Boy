"""Tests for the ``send_file`` MCP tool handler.

Surface we lock in here:
  * No channel / CLI channel → friendly refusal (not a silent pass).
  * Missing ``path`` / wrong type → input error.
  * Relative path resolves against ``ctx.workdir``.
  * File-not-found → clear error.
  * Over-size → clear error (bytes in message for auditability).
  * Missing ``peer_id`` → clear error.
  * Channel returns ``True`` → success text with size.
  * Channel returns ``False`` → error text.
  * Channel raises → error text (handler must not propagate).
  * Channel ``send_file`` is called under ``send_lock`` with the right
    args (filename, caption, bytes).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pip_agent.mcp_tools import McpContext, _channel_tools


def _run(coro):
    return asyncio.run(coro)


def _get_send_file(ctx: McpContext):
    for t in _channel_tools(ctx):
        if t.name == "send_file":
            return t.handler
    raise AssertionError("send_file tool not found")


def _text_of(result: dict) -> str:
    return "".join(
        b.get("text", "")
        for b in result.get("content", [])
        if b.get("type") == "text"
    )


class _FakeChannel:
    """Minimal Channel stand-in. Counts calls, records args, and supports
    the ``send_lock`` context manager handshake that the real base class
    exposes via ``Channel.send_lock``.
    """

    def __init__(self, *, name: str = "wecom", send_file_returns: bool = True):
        self.name = name
        self.send_file = MagicMock(return_value=send_file_returns)
        # ``Channel.send_lock`` is a plain ``threading.Lock`` (NOT
        # reentrant) in the production code, so mirror that here.
        # Non-reentrancy is what makes the lock-probe test work:
        # ``acquire(blocking=False)`` on an already-held Lock returns
        # False regardless of thread, which is exactly the signal we
        # want.
        self.send_lock = threading.Lock()


def _make_file(path: Path, size: int = 128) -> Path:
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# Channel / CLI gates
# ---------------------------------------------------------------------------


class TestChannelGate:
    def test_no_channel_is_refused(self, tmp_path: Path):
        ctx = McpContext(channel=None, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": "foo.txt"}))
        assert result.get("is_error") is True
        assert "not available on CLI" in _text_of(result).lower() or \
               "only available" in _text_of(result).lower()

    def test_cli_channel_is_refused(self, tmp_path: Path):
        ch = _FakeChannel(name="cli")
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": "foo.txt"}))
        assert result.get("is_error") is True
        ch.send_file.assert_not_called()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_path(self, tmp_path: Path):
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({}))
        assert result.get("is_error") is True
        assert "'path' is required" in _text_of(result)
        ch.send_file.assert_not_called()

    def test_non_string_path(self, tmp_path: Path):
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": 123}))
        assert result.get("is_error") is True
        ch.send_file.assert_not_called()

    def test_file_not_found(self, tmp_path: Path):
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": "nope.txt"}))
        assert result.get("is_error") is True
        assert "not found" in _text_of(result).lower()
        ch.send_file.assert_not_called()

    def test_missing_peer_id(self, tmp_path: Path):
        f = _make_file(tmp_path / "a.txt")
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "peer" in _text_of(result).lower()
        ch.send_file.assert_not_called()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_absolute_path_is_respected(self, tmp_path: Path):
        f = _make_file(tmp_path / "abs.txt", size=32)
        ch = _FakeChannel()
        # workdir is unrelated on purpose — an absolute path must not
        # be prepended.
        ctx = McpContext(
            channel=ch, peer_id="u1", workdir=tmp_path / "other",
        )
        result = _run(_get_send_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is None
        ch.send_file.assert_called_once()

    def test_relative_resolves_against_workdir(self, tmp_path: Path):
        workdir = tmp_path / "wd"
        workdir.mkdir()
        _make_file(workdir / "doc.pdf", size=64)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=workdir)
        result = _run(_get_send_file(ctx)({"path": "doc.pdf"}))
        assert result.get("is_error") is None
        # The bytes shipped must come from the workdir-anchored file.
        (_to, data), kwargs = ch.send_file.call_args[:2], ch.send_file.call_args.kwargs
        assert len(ch.send_file.call_args.args[1]) == 64
        assert kwargs.get("filename") == "doc.pdf"


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------


class TestSizeLimit:
    def test_oversize_is_rejected(self, tmp_path: Path, monkeypatch):
        # Patch the limit down to a manageable size instead of
        # writing 50 MB of zeros.
        from pip_agent import mcp_tools as m
        monkeypatch.setattr(m, "_SEND_FILE_MAX_BYTES", 1024)
        f = _make_file(tmp_path / "big.bin", size=2048)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "too large" in _text_of(result).lower()
        ch.send_file.assert_not_called()


# ---------------------------------------------------------------------------
# Channel dispatch
# ---------------------------------------------------------------------------


class TestChannelDispatch:
    def test_success_returns_size_message(self, tmp_path: Path):
        f = _make_file(tmp_path / "ok.txt", size=42)
        ch = _FakeChannel(send_file_returns=True)
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({
            "path": str(f), "caption": "here you go",
        }))
        assert result.get("is_error") is None
        text = _text_of(result)
        assert "File sent" in text
        assert "ok.txt" in text
        assert "42" in text

    def test_caption_and_filename_are_forwarded(self, tmp_path: Path):
        f = _make_file(tmp_path / "report.pdf", size=100)
        ch = _FakeChannel(send_file_returns=True)
        ctx = McpContext(channel=ch, peer_id="peer-x", workdir=tmp_path)
        _run(_get_send_file(ctx)({
            "path": str(f), "caption": "Q3 results",
        }))
        ch.send_file.assert_called_once()
        args, kwargs = ch.send_file.call_args
        # positional: to, file_data
        assert args[0] == "peer-x"
        assert args[1] == b"x" * 100
        assert kwargs["filename"] == "report.pdf"
        assert kwargs["caption"] == "Q3 results"

    def test_channel_returns_false_is_error(self, tmp_path: Path):
        f = _make_file(tmp_path / "fail.txt")
        ch = _FakeChannel(send_file_returns=False)
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "refused" in _text_of(result).lower() or \
               "failed" in _text_of(result).lower()

    def test_channel_raises_is_contained(self, tmp_path: Path):
        f = _make_file(tmp_path / "boom.txt")
        ch = _FakeChannel()
        ch.send_file.side_effect = RuntimeError("ws disconnect")
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "ws disconnect" in _text_of(result)

    def test_send_runs_under_send_lock(self, tmp_path: Path):
        """The handler must hold ``send_lock`` across the send so
        concurrent senders don't interleave. We verify by having the
        mocked ``send_file`` inspect the lock while it's being called
        — it should already be held.
        """
        f = _make_file(tmp_path / "lock.txt")
        ch = _FakeChannel()
        state: dict[str, Any] = {"locked_during_call": False}

        def _check_locked(*_args, **_kw):
            # Plain ``threading.Lock.acquire(blocking=False)`` on an
            # already-held lock returns False regardless of which
            # thread is probing — exactly the "someone else is holding
            # this" signal we want. Release if we accidentally win.
            got_it = ch.send_lock.acquire(blocking=False)
            state["locked_during_call"] = not got_it
            if got_it:
                ch.send_lock.release()
            return True

        ch.send_file.side_effect = _check_locked
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        _run(_get_send_file(ctx)({"path": str(f)}))
        assert state["locked_during_call"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
