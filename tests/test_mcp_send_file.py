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

    def __init__(
        self, *,
        name: str = "wecom",
        send_file_returns: bool = True,
        send_image_returns: bool = True,
    ):
        self.name = name
        self.send_file = MagicMock(return_value=send_file_returns)
        self.send_image = MagicMock(return_value=send_image_returns)
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
    def test_absolute_path_inside_workdir_is_respected(self, tmp_path: Path):
        # Absolute paths are allowed as long as they resolve inside the
        # agent's workdir. See ``test_absolute_escape_is_rejected`` for
        # the negative case.
        workdir = tmp_path / "wd"
        workdir.mkdir()
        f = _make_file(workdir / "abs.txt", size=32)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=workdir)
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
        kwargs = ch.send_file.call_args.kwargs
        assert len(ch.send_file.call_args.args[1]) == 64
        assert kwargs.get("filename") == "doc.pdf"


# ---------------------------------------------------------------------------
# Path containment (plan M5 — defence in depth)
# ---------------------------------------------------------------------------


class TestPathContainment:
    """``send_file`` is a file-read primitive driven by LLM-controlled
    input, so the path must be clamped to the agent's workdir. Absolute
    paths pointing outside, relative paths escaping via ``..``, and
    symlinks escaping via a link target are all rejected. The LLM
    never gets to read ``/etc/passwd`` or ``C:\\Users\\me\\.ssh\\...``
    through this tool.
    """

    def test_absolute_escape_is_rejected(self, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"secret")
        workdir = tmp_path / "wd"
        workdir.mkdir()
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=workdir)
        result = _run(_get_send_file(ctx)({"path": str(outside)}))
        assert result.get("is_error") is True
        assert "escape" in _text_of(result).lower() or \
               "workdir" in _text_of(result).lower()
        ch.send_file.assert_not_called()

    def test_relative_dotdot_escape_is_rejected(self, tmp_path: Path):
        # Classic path-traversal: relative path that walks out of the
        # workdir. Must be caught by the resolve() + relative_to()
        # check even though ``..`` has no symlink dimension.
        outside = tmp_path / "sibling.txt"
        outside.write_bytes(b"hi")
        workdir = tmp_path / "wd"
        workdir.mkdir()
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=workdir)
        result = _run(_get_send_file(ctx)({"path": "../sibling.txt"}))
        assert result.get("is_error") is True
        assert "escape" in _text_of(result).lower() or \
               "workdir" in _text_of(result).lower()
        ch.send_file.assert_not_called()

    def test_symlink_escape_is_rejected(self, tmp_path: Path):
        # Symlinked file inside the workdir pointing to a file
        # outside: without ``resolve()`` the containment check would
        # let this through. ``os.symlink`` is flaky on Windows
        # (requires admin or developer mode), so skip if the platform
        # refuses to create one — the other two tests already cover
        # the same invariant via different escape vectors.
        import os
        outside = tmp_path / "secret.txt"
        outside.write_bytes(b"password")
        workdir = tmp_path / "wd"
        workdir.mkdir()
        link = workdir / "shortcut.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=workdir)
        result = _run(_get_send_file(ctx)({"path": "shortcut.txt"}))
        assert result.get("is_error") is True
        ch.send_file.assert_not_called()


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
        # Status line declares the actual delivery path so the LLM
        # knows whether the user saw an inline preview or an attachment.
        assert "Sent" in text
        assert "ok.txt" in text
        assert "42" in text
        assert "as file" in text

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

    def test_image_routes_to_send_image(self, tmp_path: Path):
        # PNG magic bytes should trigger the send_image path so the
        # recipient gets an inline preview instead of a file tile.
        png = tmp_path / "pic.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({
            "path": str(png), "caption": "look",
        }))
        assert result.get("is_error") is None
        ch.send_image.assert_called_once()
        ch.send_file.assert_not_called()
        args, kwargs = ch.send_image.call_args
        assert args[0] == "u1"
        assert args[1].startswith(b"\x89PNG")
        assert kwargs.get("caption") == "look"
        assert "as image" in _text_of(result)

    def test_jpeg_routes_to_send_image(self, tmp_path: Path):
        jpg = tmp_path / "photo.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(jpg)}))
        assert result.get("is_error") is None
        ch.send_image.assert_called_once()
        ch.send_file.assert_not_called()

    def test_non_image_stays_on_send_file(self, tmp_path: Path):
        # PDF magic bytes — must NOT trigger the image path, even
        # though the extension could mislead extension-based routing.
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"\x00" * 64)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(pdf)}))
        assert result.get("is_error") is None
        ch.send_file.assert_called_once()
        ch.send_image.assert_not_called()
        assert "as file" in _text_of(result)

    def test_mislabeled_extension_uses_magic_bytes(self, tmp_path: Path):
        # ``foo.pdf`` with PNG magic bytes — detection is by content,
        # not by name, so this still goes as image. Prevents a
        # renamed-file trap.
        f = tmp_path / "fake.pdf"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        ch = _FakeChannel()
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        _run(_get_send_file(ctx)({"path": str(f)}))
        ch.send_image.assert_called_once()
        ch.send_file.assert_not_called()

    def test_send_image_failure_falls_back_to_send_file(self, tmp_path: Path):
        # Channels that don't implement send_image (base Channel,
        # WeChat today) return False. We must not strand the user —
        # degrade to send_file so the bytes at least reach them.
        png = tmp_path / "pic.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        ch = _FakeChannel(send_image_returns=False)
        ctx = McpContext(channel=ch, peer_id="u1", workdir=tmp_path)
        result = _run(_get_send_file(ctx)({"path": str(png)}))
        assert result.get("is_error") is None
        ch.send_image.assert_called_once()
        ch.send_file.assert_called_once()
        assert "as file" in _text_of(result)

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
