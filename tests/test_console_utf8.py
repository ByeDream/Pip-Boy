"""Regression tests for the Windows CJK stdin fix.

Symptom the fix addresses: on Windows PowerShell, typing ``你好`` at the
Pip-Boy CLI prompt arrived at the agent as ``??`` because the process-level
console codepage (CP936 / CP437) mangled the bytes before Python could
decode them. See :func:`pip_agent.console_io.force_utf8_console`.

The ``SetConsoleCP`` side of the fix is environment-specific (pre-1809
Windows + non-Windows platforms are no-ops), so we only assert the parts
we can verify deterministically in CI:

* On Windows, the ``ctypes.windll.kernel32`` calls are attempted with
  codepage ``65001``.
* ``sys.stdin`` / ``sys.stdout`` end up as UTF-8 ``TextIOWrapper`` objects
  that round-trip ``你好`` correctly.
* Non-UTF-8 bytes degrade to ``?`` (via ``errors="replace"``) rather than
  crashing the REPL on a ``UnicodeDecodeError``.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from pip_agent.console_io import force_utf8_console


@pytest.fixture
def preserve_std_streams():
    """Snapshot and restore ``sys.stdin`` / ``sys.stdout`` around a test."""
    saved_in, saved_out = sys.stdin, sys.stdout
    try:
        yield
    finally:
        sys.stdin, sys.stdout = saved_in, saved_out


class _TTYTextIOWrapper(io.TextIOWrapper):
    """TextIOWrapper that lies about being a TTY.

    ``force_utf8_console`` deliberately skips the UTF-8 rewrap on non-TTY
    streams (pipes, files, pytest capture) — see the ``isatty()`` guard in
    ``_rewrap_utf8``. Tests need to exercise the real rewrap path, so we
    hand it a wrapper that reports ``isatty() == True``.
    """

    def isatty(self) -> bool:
        return True


def _replace_with_bytes_stream(stream_name: str, data: bytes = b"") -> None:
    """Swap ``sys.<stream>`` for a TTY-reporting TextIOWrapper over a
    ``BytesIO`` seeded with ``data``. The real stdin/stdout on CI may not
    expose a writable ``.buffer``, so we fake it."""
    buf: io.BufferedReader | io.BufferedWriter = (
        io.BufferedReader(io.BytesIO(data))
        if stream_name == "stdin"
        else io.BufferedWriter(io.BytesIO())
    )
    wrapper = _TTYTextIOWrapper(buf, encoding="utf-8")
    setattr(sys, stream_name, wrapper)


class TestForceUtf8Console:
    def test_stdin_roundtrips_cjk(self, preserve_std_streams):
        """你好 encoded as UTF-8 bytes must decode back to 你好."""
        _replace_with_bytes_stream("stdin", "你好\n".encode("utf-8"))
        _replace_with_bytes_stream("stdout")

        force_utf8_console()

        assert sys.stdin.encoding.lower().replace("-", "") == "utf8"
        assert sys.stdin.errors == "replace"
        assert sys.stdin.readline() == "你好\n"

    def test_stdin_survives_legacy_codepage_bytes(self, preserve_std_streams):
        """GBK bytes for 你好 must not crash — they should be replaced with ?."""
        _replace_with_bytes_stream("stdin", "你好".encode("gbk") + b"\n")
        _replace_with_bytes_stream("stdout")

        force_utf8_console()

        line = sys.stdin.readline()
        # 4 GBK bytes, 0 valid UTF-8 sequences — we just need a non-crashing
        # result that the user can at least see garbled. The exact number of
        # replacement chars depends on UTF-8's error-recovery, so just check
        # we got a string and it didn't raise.
        assert isinstance(line, str)
        assert "\n" in line

    def test_stdout_is_utf8_and_line_buffered(self, preserve_std_streams):
        _replace_with_bytes_stream("stdin")
        _replace_with_bytes_stream("stdout")

        force_utf8_console()

        assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
        assert sys.stdout.errors == "replace"
        assert sys.stdout.line_buffering is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only API")
    def test_windows_forces_utf8_codepage(self, preserve_std_streams):
        """On Windows, we must attempt to flip the console codepage to 65001."""
        _replace_with_bytes_stream("stdin")
        _replace_with_bytes_stream("stdout")

        fake_kernel32 = MagicMock()
        fake_windll = MagicMock(kernel32=fake_kernel32)
        with patch("ctypes.windll", fake_windll, create=True):
            force_utf8_console()

        fake_kernel32.SetConsoleCP.assert_called_once_with(65001)
        fake_kernel32.SetConsoleOutputCP.assert_called_once_with(65001)

    def test_non_tty_streams_are_left_untouched(self, preserve_std_streams):
        """pytest capture, pipes, and redirected files must not be rewrapped.

        Rewrapping a captured stream detaches its buffer, which breaks the
        host harness's ``readouterr()`` at session teardown. The ``isatty()``
        gate is what keeps the full test suite able to run this module.
        """
        original_stdin_buf = io.BufferedReader(io.BytesIO(b"noop\n"))
        original_stdin = io.TextIOWrapper(original_stdin_buf, encoding="utf-8")
        original_stdout_buf = io.BufferedWriter(io.BytesIO())
        original_stdout = io.TextIOWrapper(original_stdout_buf, encoding="utf-8")
        sys.stdin, sys.stdout = original_stdin, original_stdout

        force_utf8_console()

        # Same objects — not replaced.
        assert sys.stdin is original_stdin
        assert sys.stdout is original_stdout

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only API")
    def test_windows_codepage_failure_is_swallowed(self, preserve_std_streams):
        """Pre-1809 Windows raises OSError from SetConsoleCP — don't crash."""
        _replace_with_bytes_stream("stdin", "hi\n".encode("utf-8"))
        _replace_with_bytes_stream("stdout")

        fake_kernel32 = MagicMock()
        fake_kernel32.SetConsoleCP.side_effect = OSError("ancient windows")
        fake_windll = MagicMock(kernel32=fake_kernel32)
        with patch("ctypes.windll", fake_windll, create=True):
            force_utf8_console()

        # Wrapper still got installed despite the SetConsoleCP failure.
        assert sys.stdin.readline() == "hi\n"
