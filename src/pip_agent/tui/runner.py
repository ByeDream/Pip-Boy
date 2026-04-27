"""Staged TUI startup — capability ladder, theme load, App boot.

Phase A's runner exposes a single function, :func:`launch_tui`, that
returns ``None`` after the App's event loop terminates and ``False``
if any startup stage fails. Callers (the host's ``run_host``) use the
boolean return value to decide whether to fall back to line mode.

Each startup stage logs its outcome to ``<workspace>/.pip/tui_capability.log``
so an operator can see *why* TUI didn't start without scraping the
host's main log. Stages, in order:

1. **Capability detection** — :func:`detect_tui_capability`. On
   failure: log + return False (line mode fallback).
2. **Theme loading** — Phase A only loads the builtin ``wasteland``
   theme. A theme-load failure is a developer bug (the theme ships
   with the package), so we re-raise; CI catches it.
3. **App construction & run** — wraps ``App.run()`` in a try/except
   so a Textual driver init error degrades cleanly. Phase A.3 will
   replace the bare ``app.run()`` with the host-aware integration.

The runner does NOT touch ``sys.stdout`` / ``sys.stdin`` itself —
that's :func:`pip_agent.console_io.force_utf8_console`'s job, and the
runner explicitly *trusts* its result. Reapplying the rewrap from
here would reintroduce the dunder-stream drift bug from design.md §1.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from pip_agent.tui.app import PipBoyTuiApp, SnapshotProvider, UserLineHandler
from pip_agent.tui.capability import (
    CapabilityResult,
    detect_tui_capability,
    write_capability_log,
)
from pip_agent.tui.loader import load_builtin_theme
from pip_agent.tui.pump import UiPump

log = logging.getLogger(__name__)

__all__ = [
    "TuiBootResult",
    "build_app",
    "launch_tui",
]


class TuiBootResult:
    """Outcome of one :func:`launch_tui` call.

    Field invariants:

    * ``ok=True`` ⇒ the App ran to completion (clean ``app.exit()``).
    * ``ok=False`` ⇒ either capability check failed or App boot raised.
    * ``capability`` is always populated; the host can use it to
      decide which fallback message to print.
    """

    __slots__ = ("ok", "capability", "error")

    def __init__(
        self,
        *,
        ok: bool,
        capability: CapabilityResult,
        error: BaseException | None = None,
    ) -> None:
        self.ok = ok
        self.capability = capability
        self.error = error


def build_app(
    *,
    theme_name: str = "wasteland",
    pump: UiPump | None = None,
    on_user_line: UserLineHandler | None = None,
    art_anim_interval: float = 3.0,
    initial_side_snapshot: dict[str, str] | None = None,
    snapshot_provider: SnapshotProvider | None = None,
) -> tuple[PipBoyTuiApp, UiPump]:
    """Build (but do NOT run) a :class:`PipBoyTuiApp`.

    Separated from :func:`launch_tui` so unit tests can construct the
    App without going through the capability ladder. The returned
    pump is the *same* one passed in (or a freshly created one if
    none was provided), so callers can wire producers against it
    before calling ``app.run()``.
    """
    bundle = load_builtin_theme(theme_name)
    if pump is None:
        pump = UiPump()
    app = PipBoyTuiApp(
        theme=bundle, pump=pump, on_user_line=on_user_line,
        art_anim_interval=art_anim_interval,
        initial_side_snapshot=initial_side_snapshot,
        snapshot_provider=snapshot_provider,
    )
    return app, pump


def launch_tui(
    *,
    workdir: Path,
    force_no_tui: bool = False,
    theme_name: str = "wasteland",
    pump: UiPump | None = None,
    on_user_line: UserLineHandler | None = None,
    pre_run_hook: Callable[[PipBoyTuiApp], Awaitable[None] | None] | None = None,
) -> TuiBootResult:
    """Run the capability ladder, build the App, run its loop.

    Returns a :class:`TuiBootResult` so callers know:

    * whether to print a "falling back to line mode" notice;
    * what to write into the capability log (``write_capability_log``
      is called inside this function so the caller doesn't have to).

    ``pre_run_hook`` runs after the App is constructed but before
    ``app.run()`` — the host wires its log handler attachment there
    so log records emitted during the App's mount phase reach the
    pump (rather than racing the App's own logging configuration).
    """
    capability = detect_tui_capability(force_no_tui=force_no_tui)
    write_capability_log(workdir, capability)
    if not capability.ok:
        log.info(
            "TUI capability check failed at stage=%s: %s — line mode fallback.",
            capability.stage, capability.detail,
        )
        return TuiBootResult(ok=False, capability=capability)

    try:
        app, _pump = build_app(
            theme_name=theme_name, pump=pump, on_user_line=on_user_line,
        )
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        log.error("TUI build failed: %s", exc, exc_info=True)
        return TuiBootResult(ok=False, capability=capability, error=exc)

    if pre_run_hook is not None:
        try:
            result = pre_run_hook(app)
            if result is not None and hasattr(result, "__await__"):
                # Caller is responsible for its own awaitables. We do
                # not run them here because launch_tui owns the event
                # loop via app.run(); awaiting now would block the
                # boot sequence.
                pass
        except Exception:  # noqa: BLE001
            log.exception("pre_run_hook raised; continuing.")

    try:
        app.run()
    except Exception as exc:  # noqa: BLE001
        log.error("TUI run failed: %s", exc, exc_info=True)
        return TuiBootResult(ok=False, capability=capability, error=exc)
    return TuiBootResult(ok=True, capability=capability)
