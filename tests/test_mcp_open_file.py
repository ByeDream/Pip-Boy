"""Tests for the ``open_file`` MCP tool handler.

Surface we lock in here:
  * Non-CLI channel → friendly refusal.
  * Missing / non-string ``path`` → input error.
  * Path doesn't exist + ``create_if_missing=False`` → error.
  * Path doesn't exist + ``create_if_missing=True`` → creates file
    (with parents) and invokes editor.
  * Path is a directory → error.
  * ``$EDITOR="code --wait"`` → ``shlex.split`` parses args correctly
    (assert argv the handler passed to ``create_subprocess_exec``).
  * No env var → platform default (``notepad`` on Windows, ``nano``
    elsewhere).
  * Editor binary missing → ``FileNotFoundError`` → clean error text.
  * Content hash unchanged → ``user_closed_without_modification``
    even if mtime was touched.
  * Content hash changed → ``user_closed_with_modification`` with
    updated size/mtime.
  * File deleted during edit → error.
  * ``tui_app.suspend()`` is entered ONLY for TTY editors (vim, nano,
    emacs -nw, …) — never for GUI editors (notepad, code, …), since
    those have their own window.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pip_agent.mcp_tools import McpContext, _editor_tools


def _run(coro):
    return asyncio.run(coro)


def _get_open_file(ctx: McpContext):
    for t in _editor_tools(ctx):
        if t.name == "open_file":
            return t.handler
    raise AssertionError("open_file tool not found")


def _text_of(result: dict) -> str:
    return "".join(
        b.get("text", "")
        for b in result.get("content", [])
        if b.get("type") == "text"
    )


class _FakeChannel:
    def __init__(self, name: str = "cli") -> None:
        self.name = name


class _FakeSuspend:
    """Context manager stand-in for ``App.suspend()``."""
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False


class _FakeApp:
    def __init__(self) -> None:
        self._ctx = _FakeSuspend()

    def suspend(self):
        return self._ctx


def _exec_mock(rc: int = 0, side_effect=None):
    """Mock for ``asyncio.create_subprocess_exec``.

    When awaited it yields a process whose ``wait()`` returns ``rc``.
    ``side_effect`` (optional callable) is invoked with ``argv`` before
    the process is returned — tests use it to mutate / delete the
    target file mid-"edit".
    """
    async def _call(*argv, **_kwargs):
        if side_effect is not None:
            side_effect(*argv)
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=rc)
        return proc

    return MagicMock(side_effect=_call)


def _patch_exec(**kwargs):
    """Shorthand: patch create_subprocess_exec with an _exec_mock."""
    return patch(
        "pip_agent.mcp_tools.asyncio.create_subprocess_exec",
        _exec_mock(**kwargs),
    )


@pytest.fixture(autouse=True)
def _clear_editor_env(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with a clean $VISUAL/$EDITOR — tests opt-in."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


# ---------------------------------------------------------------------------
# Channel gate
# ---------------------------------------------------------------------------


class TestChannelGate:
    def test_remote_channel_is_refused(self, tmp_path: Path):
        ch = _FakeChannel(name="wecom")
        ctx = McpContext(channel=ch, workdir=tmp_path)
        result = _run(_get_open_file(ctx)({"path": str(tmp_path / "x.txt")}))
        assert result.get("is_error") is True
        assert "only available on cli" in _text_of(result).lower()

    def test_no_channel_is_allowed(self, tmp_path: Path):
        # ``channel=None`` is the CLI bootstrap state in Pip-Boy.
        # We treat it as CLI, not as a refusal.
        f = tmp_path / "x.txt"
        f.write_text("hi")
        ctx = McpContext(channel=None, workdir=tmp_path)
        with _patch_exec():
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is None

    def test_cli_channel_is_allowed(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        ch = _FakeChannel(name="cli")
        ctx = McpContext(channel=ch, workdir=tmp_path)
        with _patch_exec():
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_path(self, tmp_path: Path):
        ctx = McpContext(workdir=tmp_path)
        result = _run(_get_open_file(ctx)({}))
        assert result.get("is_error") is True
        assert "'path' is required" in _text_of(result)

    def test_non_string_path(self, tmp_path: Path):
        ctx = McpContext(workdir=tmp_path)
        result = _run(_get_open_file(ctx)({"path": 123}))
        assert result.get("is_error") is True

    def test_path_is_directory(self, tmp_path: Path):
        ctx = McpContext(workdir=tmp_path)
        result = _run(_get_open_file(ctx)({"path": str(tmp_path)}))
        assert result.get("is_error") is True
        assert "directory" in _text_of(result).lower()


# ---------------------------------------------------------------------------
# Missing path / create_if_missing
# ---------------------------------------------------------------------------


class TestCreateIfMissing:
    def test_missing_without_flag_errors(self, tmp_path: Path):
        ctx = McpContext(workdir=tmp_path)
        result = _run(_get_open_file(ctx)(
            {"path": str(tmp_path / "nope.md")}
        ))
        assert result.get("is_error") is True
        assert "does not exist" in _text_of(result).lower()
        assert "create_if_missing" in _text_of(result)

    def test_missing_with_flag_creates_file(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "new.md"
        assert not target.exists()
        ctx = McpContext(workdir=tmp_path)
        with _patch_exec():
            result = _run(_get_open_file(ctx)({
                "path": str(target),
                "create_if_missing": True,
            }))
        assert result.get("is_error") is None
        assert target.exists()
        assert target.read_text() == ""

    def test_tilde_expansion(self, tmp_path: Path, monkeypatch):
        # Point ~ at tmp_path so the test is hermetic.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        ctx = McpContext(workdir=tmp_path)
        with _patch_exec():
            result = _run(_get_open_file(ctx)({
                "path": "~/drafts/note.md",
                "create_if_missing": True,
            }))
        assert result.get("is_error") is None
        assert (tmp_path / "drafts" / "note.md").exists()


# ---------------------------------------------------------------------------
# Editor resolution
# ---------------------------------------------------------------------------


class TestEditorResolution:
    def test_visual_takes_precedence_over_editor(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("VISUAL", "vim")
        monkeypatch.setenv("EDITOR", "nano")
        ctx = McpContext(workdir=tmp_path)
        exec_mock = _exec_mock()
        with patch(
            "pip_agent.mcp_tools.asyncio.create_subprocess_exec", exec_mock
        ):
            _run(_get_open_file(ctx)({"path": str(f)}))
        argv = list(exec_mock.call_args.args)
        assert argv[0] == "vim"
        assert argv[-1] == str(f)

    def test_editor_with_args_is_split(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "code --wait")
        ctx = McpContext(workdir=tmp_path)
        exec_mock = _exec_mock()
        with patch(
            "pip_agent.mcp_tools.asyncio.create_subprocess_exec", exec_mock
        ):
            _run(_get_open_file(ctx)({"path": str(f)}))
        argv = list(exec_mock.call_args.args)
        assert argv == ["code", "--wait", str(f)]

    def test_no_env_uses_platform_default(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        ctx = McpContext(workdir=tmp_path)
        exec_mock = _exec_mock()
        with patch(
            "pip_agent.mcp_tools.asyncio.create_subprocess_exec", exec_mock
        ):
            _run(_get_open_file(ctx)({"path": str(f)}))
        argv = list(exec_mock.call_args.args)
        # Don't assume platform — just require it's one of the two known
        # fallbacks. Narrows if wrong default sneaks in.
        assert argv[0] in ("notepad", "nano")

    def test_editor_not_found_returns_error(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-xyz")
        ctx = McpContext(workdir=tmp_path)
        with patch(
            "pip_agent.mcp_tools.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("no"),
        ):
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "not found" in _text_of(result).lower()

    def test_malformed_editor_env_returns_error(
        self, tmp_path: Path, monkeypatch
    ):
        # Unclosed quote — shlex.split raises ValueError.
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", 'vim "unterminated')
        ctx = McpContext(workdir=tmp_path)
        result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "malformed" in _text_of(result).lower()


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


class TestChangeDetection:
    def test_unchanged_when_editor_doesnt_touch_file(
        self, tmp_path: Path
    ):
        f = tmp_path / "x.txt"
        f.write_text("original")
        ctx = McpContext(workdir=tmp_path)
        with _patch_exec():
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is None
        payload = json.loads(_text_of(result))
        assert payload["status"] == "user_closed_without_modification"
        assert payload["size_before"] == payload["size_after"] == 8

    def test_changed_when_content_differs(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("original")
        ctx = McpContext(workdir=tmp_path)

        def _edit(*_argv):
            f.write_text("modified content")

        with _patch_exec(side_effect=_edit):
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is None
        payload = json.loads(_text_of(result))
        assert payload["status"] == "user_closed_with_modification"
        assert payload["size_before"] == 8
        assert payload["size_after"] == len("modified content")

    def test_mtime_touch_without_content_change_is_unchanged(
        self, tmp_path: Path
    ):
        """sha256 guard: some editors rewrite the file byte-identical
        but bump mtime. We must not report
        ``user_closed_with_modification`` in that case."""
        f = tmp_path / "x.txt"
        f.write_text("same bytes")
        ctx = McpContext(workdir=tmp_path)

        def _touch_only(*_argv):
            # Rewrite identical content but force a newer mtime.
            f.write_text("same bytes")
            new_mtime = f.stat().st_mtime + 10
            os.utime(f, (new_mtime, new_mtime))

        with _patch_exec(side_effect=_touch_only):
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        payload = json.loads(_text_of(result))
        assert payload["status"] == "user_closed_without_modification"
        assert payload["mtime_after"] > payload["mtime_before"]

    def test_file_deleted_during_edit_is_error(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("original")
        ctx = McpContext(workdir=tmp_path)

        def _delete(*_argv):
            f.unlink()

        with _patch_exec(side_effect=_delete):
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is True
        assert "disappeared" in _text_of(result).lower()


# ---------------------------------------------------------------------------
# TUI suspend integration
#
# suspend() takes over the terminal and must only run for editors that
# actually need the TTY. GUI editors (notepad, code, subl) have their
# own window — suspending the TUI for them would make it disappear for
# no reason and look like a crash.
# ---------------------------------------------------------------------------


class TestTuiSuspend:
    def test_suspend_entered_for_vim(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "vim")
        app = _FakeApp()
        ctx = McpContext(workdir=tmp_path, tui_app=app)
        with _patch_exec():
            _run(_get_open_file(ctx)({"path": str(f)}))
        assert app._ctx.entered == 1
        assert app._ctx.exited == 1

    def test_suspend_entered_for_nano(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "nano")
        app = _FakeApp()
        ctx = McpContext(workdir=tmp_path, tui_app=app)
        with _patch_exec():
            _run(_get_open_file(ctx)({"path": str(f)}))
        assert app._ctx.entered == 1

    def test_suspend_entered_for_emacs_nw(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "emacs -nw")
        app = _FakeApp()
        ctx = McpContext(workdir=tmp_path, tui_app=app)
        with _patch_exec():
            _run(_get_open_file(ctx)({"path": str(f)}))
        assert app._ctx.entered == 1

    def test_suspend_skipped_for_notepad(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "notepad")
        app = _FakeApp()
        ctx = McpContext(workdir=tmp_path, tui_app=app)
        with _patch_exec():
            _run(_get_open_file(ctx)({"path": str(f)}))
        assert app._ctx.entered == 0

    def test_suspend_skipped_for_vscode(
        self, tmp_path: Path, monkeypatch
    ):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "code --wait")
        app = _FakeApp()
        ctx = McpContext(workdir=tmp_path, tui_app=app)
        with _patch_exec():
            _run(_get_open_file(ctx)({"path": str(f)}))
        assert app._ctx.entered == 0

    def test_suspend_skipped_for_emacs_gui(
        self, tmp_path: Path, monkeypatch
    ):
        # Bare ``emacs`` (no -nw) is the GUI build.
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "emacs")
        app = _FakeApp()
        ctx = McpContext(workdir=tmp_path, tui_app=app)
        with _patch_exec():
            _run(_get_open_file(ctx)({"path": str(f)}))
        assert app._ctx.entered == 0

    def test_no_tui_means_no_suspend(self, tmp_path: Path, monkeypatch):
        f = tmp_path / "x.txt"
        f.write_text("hi")
        monkeypatch.setenv("EDITOR", "vim")  # would suspend if tui present
        ctx = McpContext(workdir=tmp_path, tui_app=None)
        with _patch_exec():
            result = _run(_get_open_file(ctx)({"path": str(f)}))
        assert result.get("is_error") is None
