"""UTF-8 console setup for Windows.

Single responsibility: make ``sys.stdin`` / ``sys.stdout`` survive CJK input
on Windows PowerShell. Lives in its own module (rather than ``agent_host``)
so the CLI entrypoint can call it *before* ``logging.basicConfig`` runs —
``basicConfig`` captures the current ``sys.stdout`` into a ``StreamHandler``,
and if we detach stdout later the handler ends up with a dead wrapper and
every ``log.info`` raises ``ValueError: underlying buffer has been detached``.

Call order in ``__main__.main()``:

1. :func:`force_utf8_console` — swap stdin/stdout for UTF-8 wrappers.
2. :func:`_configure_logging`     — install handlers on the *new* stdout.
3. :func:`run_host`                — agent loop; stdin reads CJK correctly.
"""

from __future__ import annotations

import io
import sys

__all__ = ["force_utf8_console"]


def force_utf8_console() -> None:
    """Make stdin/stdout survive CJK input on Windows PowerShell.

    Symptom without this: a user types ``你好`` at the CLI prompt and Pip-Boy
    sees ``??``. The bytes get mangled upstream of Python — the Windows
    console codepage defaults to the system locale (``chcp 936`` on Chinese
    Windows, ``437`` on vanilla en-US), and anything that can't round-trip
    through that codepage is replaced with ``?`` before Python's
    ``WindowsConsoleIO`` ever sees it. ``sys.stdin.reconfigure(encoding=...)``
    is useless here because it only changes how the byte stream is decoded,
    not what bytes arrive.

    Fix:

    1. On Windows, flip the process's console codepage to 65001 (UTF-8) via
       ``SetConsoleCP`` / ``SetConsoleOutputCP``. This is Microsoft's
       documented opt-out from legacy codepages and is safe on Windows 10
       1809+ (no-op / silent failure on older builds).
    2. Rewrap ``sys.stdin`` / ``sys.stdout`` as explicit UTF-8 with
       ``errors="replace"`` so any residual byte-level mismatch surfaces as
       a visible ``?`` rather than a ``UnicodeDecodeError`` crash mid-REPL.

    MUST be called before :mod:`logging` is configured — ``basicConfig``
    binds a ``StreamHandler`` to the *current* ``sys.stdout`` reference, and
    we're about to ``detach()`` that stream out from under it.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            # Pre-1809 Windows or sandboxed env — fall through to the
            # TextIOWrapper rewrap, which at least keeps us from crashing
            # on mixed-encoding bytes.
            pass

    try:
        sys.stdout.flush()
    except Exception:  # pragma: no cover — flush is best-effort
        pass

    # ``detach()`` is the correct way to swap the text layer without
    # closing the byte stream. Wrapping ``sys.stdin.buffer`` directly
    # leaves the old wrapper holding a reference; when it gets GC'd its
    # finalizer closes the shared buffer and the *new* wrapper raises
    # ``ValueError: I/O operation on closed file`` on the first read.
    #
    # Some execution environments (pytest capture, redirected pipes in tools
    # like tox, notebook frontends) replace stdio with non-standard objects
    # that don't support ``detach()`` — in that case just skip the rewrap.
    # The Windows codepage flip above is the important part; the rewrap is
    # only a belt-and-suspenders guard against mixed-encoding byte streams.
    sys.stdout = _rewrap_utf8(sys.stdout, line_buffering=True)
    sys.stdin = _rewrap_utf8(sys.stdin, line_buffering=False)


def _rewrap_utf8(stream: object, *, line_buffering: bool) -> object:
    """Return a UTF-8 TextIOWrapper over ``stream``, or ``stream`` unchanged
    when rewrapping isn't safe.

    Gated on ``isatty()`` intentionally: the whole point of this rewrap is
    to normalize what the Windows console gives us, and anyone reading from
    a pipe / file / pytest's capture layer either already set their own
    encoding or needs us to stay out of the way. Rewrapping a captured
    stream specifically broke pytest's ``readouterr()`` — their ``tmpfile``
    is detachable, but once detached pytest's own ``seek()`` on it fails.
    """
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return stream
    try:
        if not isatty():
            return stream
    except (ValueError, OSError):
        return stream

    detach = getattr(stream, "detach", None)
    if detach is None:
        return stream
    try:
        raw = detach()
    except (io.UnsupportedOperation, ValueError, AttributeError):
        return stream
    return io.TextIOWrapper(
        raw,
        encoding="utf-8",
        errors="replace",
        line_buffering=line_buffering,
    )
