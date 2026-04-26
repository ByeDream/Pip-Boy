"""``pip-boy doctor`` — read-only environment + TUI self-check.

The command is intentionally small and side-effect free: it never
launches the host, never modifies state, and runs a single capability
ladder probe that does NOT write the workspace's
``tui_capability.log`` (the doctor's lookup is a *report*, not a
fresh observation). This keeps it safe to run from CI or a remote
shell when something goes wrong.

Output is plain text on stdout — no Rich, no Textual, no ANSI — so
the output can be pasted into a bug report verbatim. Sections:

1. **Versions** — Python, ``textual``, ``rich``, ``claude-agent-sdk``.
2. **Locale & console** — locale tuple, codepage (Windows), stdout
   encoding, isatty status.
3. **TUI capability** — the four-stage ladder, with each stage's
   pass/fail and detail line. Forcing ``--no-tui`` short-circuits at
   stage 1 — the doctor mirrors that, so it serves as a quick check
   that the operator's environment hasn't drifted.
4. **Themes** — built-in catalogue, local ``<workspace>/.pip/themes/``
   contributions, override conflicts, and broken-theme issues. The
   active theme (per the precedence chain) is marked with ``*``.
5. **Recent capability log** — last N entries from
   ``<workspace>/.pip/tui_capability.log`` (most recent first), each
   shown as a single line so a streak of failures is visually
   obvious. Returns "(empty)" when the log doesn't exist yet.

Returns 0 always; the doctor's job is to *describe* the environment,
not to gate boot. Detection of "this environment can't run the TUI"
already happens in :mod:`pip_agent.tui.capability` at host boot —
the doctor just surfaces that decision in one screen.
"""

from __future__ import annotations

import io
import json
import locale
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import TextIO

__all__ = ["run_doctor"]


# Number of recent capability log entries the doctor surfaces.
# Twenty is enough to spot a fallback streak after a Windows update
# without hiding the latest successful boot.
_RECENT_LOG_LIMIT: int = 20


def run_doctor(
    *, workdir: Path, force_no_tui: bool = False, out: TextIO | None = None,
) -> int:
    """Render the doctor report. Returns the process exit code.

    ``workdir`` is the workspace root (defaults: ``Path.cwd()``).
    ``force_no_tui`` is exposed mostly for tests and parity with the
    host CLI flag — it controls the capability ladder shown in
    section 3.

    The function never raises; any per-section failure is rendered as
    an inline ``[error]`` line so a half-broken environment still
    produces a usable report.
    """
    sink = out if out is not None else sys.stdout

    _section(sink, "Pip-Boy doctor")
    _print_versions(sink)
    _print_locale(sink)
    _print_capability(sink, force_no_tui=force_no_tui)
    _print_themes(sink, workdir=workdir)
    _print_recent_capability_log(sink, workdir=workdir)
    return 0


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


def _section(out: TextIO, title: str) -> None:
    out.write("\n")
    out.write(title)
    out.write("\n")
    out.write("=" * len(title))
    out.write("\n")


def _kv(out: TextIO, key: str, value: object) -> None:
    out.write(f"  {key:<22} {value}\n")


def _print_versions(out: TextIO) -> None:
    _section(out, "Versions")
    _kv(out, "python", platform.python_version())
    _kv(out, "platform", platform.platform())
    for pkg in ("pip-boy", "textual", "rich", "claude-agent-sdk"):
        try:
            ver = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            ver = "(not installed)"
        _kv(out, pkg, ver)


def _print_locale(out: TextIO) -> None:
    _section(out, "Locale & console")
    try:
        loc = locale.getlocale()
    except Exception as exc:  # noqa: BLE001 — exotic Python builds
        loc = f"[error] {exc}"
    _kv(out, "locale", loc)
    _kv(out, "preferred encoding", locale.getpreferredencoding(False))
    _kv(out, "stdout encoding", getattr(sys.stdout, "encoding", "<unset>"))
    _kv(out, "stdin encoding", getattr(sys.stdin, "encoding", "<unset>"))
    _kv(out, "stdout isatty", _safe_isatty(sys.stdout))
    _kv(out, "stdin isatty", _safe_isatty(sys.stdin))
    if sys.platform == "win32":
        _kv(out, "codepage (in/out)", _windows_codepage())


def _safe_isatty(stream: object) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (ValueError, OSError):
        return False


def _windows_codepage() -> str:
    """Return ``"<in>/<out>"`` from the Win32 console API.

    Wrapped to never raise: the ctypes call returns 0 if the process
    isn't attached to a console (CI runner, redirected pipe), and
    that's surfaced as ``"0/0"`` rather than throwing.
    """
    try:  # pragma: no cover — Windows-specific
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        cp_in = int(kernel32.GetConsoleCP())
        cp_out = int(kernel32.GetConsoleOutputCP())
        return f"{cp_in}/{cp_out}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def _print_capability(out: TextIO, *, force_no_tui: bool) -> None:
    _section(out, "TUI capability ladder")
    try:
        from pip_agent.tui.capability import detect_tui_capability
    except Exception as exc:  # noqa: BLE001
        out.write(f"  [error] failed to import capability module: {exc}\n")
        return
    result = detect_tui_capability(force_no_tui=force_no_tui)
    out.write(f"  overall: {'OK' if result.ok else 'FALLBACK'}")
    out.write(f" (stage={result.stage})\n")
    if result.detail:
        out.write(f"  detail:  {result.detail}\n")
    out.write("  stages:\n")
    for stage, ok, detail in result.checks:
        marker = "PASS" if ok else "FAIL"
        out.write(f"    [{marker}] {stage:<14} {detail}\n")


def _print_themes(out: TextIO, *, workdir: Path) -> None:
    _section(out, "Themes")
    # Theme stack is best-effort: if the import or the discovery walk
    # crashes, we surface it inline and keep going. The doctor is a
    # diagnostic; it must not itself crash on a broken environment.
    try:
        from pip_agent.host_state import (
            HostState,
            resolve_active_theme_name,
        )
        from pip_agent.tui import DEFAULT_THEME_NAME, ThemeManager
    except Exception as exc:  # noqa: BLE001
        out.write(f"  [error] failed to import theme stack: {exc}\n")
        return

    state = HostState(workspace_pip_dir=workdir / ".pip")
    requested = resolve_active_theme_name(
        state=state, default=DEFAULT_THEME_NAME,
    )
    persisted = state.get_theme()
    _kv(out, "active (resolved)", requested)
    _kv(out, "persisted", persisted or "(none)")
    _kv(out, "default", DEFAULT_THEME_NAME)

    mgr = ThemeManager(workdir=workdir)
    try:
        snapshot = mgr.discover()
    except Exception as exc:  # noqa: BLE001
        out.write(f"  [error] discover() raised: {exc}\n")
        return

    bundles = list(snapshot.bundles.values())
    bundles.sort(key=lambda b: (b.source.split(":", 1)[0], b.manifest.name))
    out.write(f"  installed ({len(bundles)}):\n")
    if not bundles:
        out.write("    (none)\n")
    for bundle in bundles:
        origin = bundle.source.split(":", 1)[0]
        marker = " *" if bundle.manifest.name == requested else ""
        truncated = " (art truncated)" if bundle.art_truncated else ""
        out.write(
            f"    [{origin}] {bundle.manifest.name}{marker} — "
            f"{bundle.manifest.display_name} v{bundle.manifest.version}"
            f"{truncated}\n"
        )

    if snapshot.issues:
        out.write(f"  issues ({len(snapshot.issues)}):\n")
        for issue in snapshot.issues:
            head = issue.reason.splitlines()[0] if issue.reason else "(no detail)"
            out.write(
                f"    [{issue.origin}] {issue.path.name} — {head}\n"
            )


def _print_recent_capability_log(out: TextIO, *, workdir: Path) -> None:
    _section(out, f"Recent capability log (last {_RECENT_LOG_LIMIT})")
    log_path = workdir / ".pip" / "tui_capability.log"
    if not log_path.is_file():
        out.write(f"  (empty — {log_path} does not exist)\n")
        return
    try:
        raw = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        out.write(f"  [error] failed to read {log_path}: {exc}\n")
        return
    lines = [ln for ln in raw if ln.strip()]
    recent = lines[-_RECENT_LOG_LIMIT:]
    if not recent:
        out.write("  (empty)\n")
        return
    # Render newest first so a regression streak is visible at the top.
    for line in reversed(recent):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            out.write(f"    [malformed] {line[:120]}\n")
            continue
        ts = entry.get("ts", "?")
        ok = "OK" if entry.get("ok") else "FALLBACK"
        stage = entry.get("stage", "?")
        detail = entry.get("detail", "")
        out.write(f"    {ts}  {ok:<8} stage={stage:<14} {detail}\n")


# ---------------------------------------------------------------------------
# Standalone CLI helper (not the public ``main()`` — that lives in
# :mod:`pip_agent.__main__` and dispatches here based on argv[1]).
# ---------------------------------------------------------------------------


def render_to_string(*, workdir: Path, force_no_tui: bool = False) -> str:
    """Return the doctor report as a string. Convenience for tests."""
    buf = io.StringIO()
    run_doctor(workdir=workdir, force_no_tui=force_no_tui, out=buf)
    return buf.getvalue()
