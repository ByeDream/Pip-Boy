"""TUI capability detection — four-stage ladder, structured fallback log.

The TUI is the *default* mode from v0.5+, but only when the terminal
can actually host it. The ladder runs every host boot:

1. ``user_optout``  — the operator passed ``--no-tui``.
2. ``tty``          — both ``sys.stdin`` and ``sys.stdout`` are TTYs.
3. ``driver``       — ``textual`` (and its platform driver) imports
                      cleanly; on Windows we additionally probe that the
                      win32 driver constructor doesn't raise.
4. ``encoding``     — ``sys.stdout.encoding`` is a UTF-8 alias. Anything
                      else mangles CJK and box-drawing glyphs.

The first failing stage wins; remaining stages are not run. Result is
written as a single JSONL line to ``<workspace>/.pip/tui_capability.log``
so an operator (and ``pip-boy doctor`` later) can see *why* the host fell
back to line mode without having to re-read the host's logs.

Design.md §5 explicitly forbids reviving the old ``cli_layout`` boolean
config — the world model here is "TUI is the default, fall back only on
clear evidence we can't render". That's what this module is for.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

__all__ = [
    "CapabilityResult",
    "detect_tui_capability",
    "write_capability_log",
]


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------

# Stage identifiers (LOCKED — referenced by tests, doctor output, log
# parsers). Adding new stages is a contract change.
STAGE_USER_OPTOUT = "user_optout"
STAGE_TTY = "tty"
STAGE_DRIVER = "driver"
STAGE_ENCODING = "encoding"
STAGE_READY = "ready"


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """Outcome of one capability ladder run.

    ``ok`` is true only when every stage passed; ``stage`` then equals
    ``"ready"``. On failure ``stage`` is the FIRST stage that failed
    and ``detail`` carries a one-line operator-readable explanation
    (no traceback — the writer logs those at DEBUG separately).

    ``checks`` records the result of every stage that was actually run,
    in order, so ``pip-boy doctor`` can show the full ladder rather
    than just the first failure.
    """

    ok: bool
    stage: str
    detail: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def to_json(self) -> str:
        """JSON line for ``tui_capability.log``."""
        payload = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **asdict(self),
        }
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Per-stage probes
# ---------------------------------------------------------------------------


def _probe_tty() -> tuple[bool, str]:
    """Stage 2: stdin AND stdout report ``isatty()``.

    Either being a pipe (CI, ``pip-boy < input.txt``, captured by
    pytest) is enough to disqualify the TUI: the input box wouldn't
    receive keystrokes and the renderer would scribble escape codes
    into a log file.
    """
    try:
        stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        stdout_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    except (ValueError, OSError) as exc:
        return False, f"isatty raised: {exc}"
    if not stdin_tty:
        return False, "sys.stdin is not a TTY"
    if not stdout_tty:
        return False, "sys.stdout is not a TTY"
    return True, "stdin+stdout are TTY"


def _probe_driver() -> tuple[bool, str]:
    """Stage 3: textual imports and resolves a platform driver.

    We do NOT instantiate the driver here — full instantiation needs
    a running App and would race with the actual launch. We just
    confirm the import chain works, which catches the common failure
    mode: a stale ``textual`` install whose Windows driver was pinned
    to an incompatible version (design.md §4).
    """
    try:
        import textual  # noqa: F401
        from textual.app import App  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — surface ImportError detail
        return False, f"textual import failed: {exc}"

    if sys.platform == "win32":
        try:
            from textual.drivers.windows_driver import (  # noqa: F401
                WindowsDriver,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"windows driver import failed: {exc}"

    return True, "textual driver importable"


def _probe_encoding() -> tuple[bool, str]:
    """Stage 4: ``sys.stdout.encoding`` is a UTF-8 alias.

    The console_io rewrap (:func:`pip_agent.console_io.force_utf8_console`)
    runs before this probe in ``__main__.main``, so on Windows we expect
    a fresh UTF-8 ``TextIOWrapper``. If the rewrap was skipped (non-TTY)
    we already failed stage 2, so reaching this stage with a non-UTF-8
    encoding indicates an exotic terminal and we'd rather fail clearly
    than render mojibake.
    """
    enc = getattr(sys.stdout, "encoding", "") or ""
    norm = enc.lower().replace("-", "")
    if norm in {"utf8", "utf8mb4"}:
        return True, f"stdout encoding={enc}"
    return False, f"stdout encoding={enc or '<unset>'} not utf-8"


# Probe registry kept module-level so tests can monkeypatch one stage in
# isolation. Order matters: ladder stops at the first failure.
_PROBES: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    (STAGE_TTY, _probe_tty),
    (STAGE_DRIVER, _probe_driver),
    (STAGE_ENCODING, _probe_encoding),
]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def detect_tui_capability(*, force_no_tui: bool = False) -> CapabilityResult:
    """Run the four-stage capability ladder and return the verdict.

    ``force_no_tui`` is the operator's explicit ``--no-tui`` flag; when
    true the ladder short-circuits at stage 1 with a clean
    ``user_optout`` result so the fallback log shows *why* TUI didn't
    start (an operator looking at it later sees "operator opted out",
    not "we don't know why").
    """
    checks: list[tuple[str, bool, str]] = []

    if force_no_tui:
        checks.append((STAGE_USER_OPTOUT, False, "operator passed --no-tui"))
        return CapabilityResult(
            ok=False,
            stage=STAGE_USER_OPTOUT,
            detail="operator passed --no-tui",
            checks=checks,
        )

    for stage, probe in _PROBES:
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 — never let a probe crash boot
            ok, detail = False, f"probe raised: {exc}"
        checks.append((stage, ok, detail))
        if not ok:
            return CapabilityResult(
                ok=False, stage=stage, detail=detail, checks=checks
            )

    checks.append((STAGE_READY, True, "all stages passed"))
    return CapabilityResult(
        ok=True, stage=STAGE_READY, detail="all stages passed", checks=checks
    )


def write_capability_log(workdir: Path, result: CapabilityResult) -> None:
    """Append one JSONL record describing the ladder outcome.

    Writes to ``<workdir>/.pip/tui_capability.log``. Best-effort: a
    write failure (read-only filesystem, full disk) is logged at
    WARNING but never propagated — the host must still boot.

    The log is intentionally append-only and small (one line per boot).
    Pruning is the operator's call; ``pip-boy doctor`` will summarise
    the most recent N entries.
    """
    target_dir = workdir / ".pip"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with (target_dir / "tui_capability.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(result.to_json() + "\n")
    except OSError as exc:
        log.warning("Failed to write tui_capability.log: %s", exc)
