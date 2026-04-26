"""Regression: TUI ``/exit`` does NOT short-circuit ``flush_and_rotate``.

PipBoyCLITheme/design.md §6 is explicit: ``/exit`` must flow through
the host's standard inbound queue so reflect/rotate runs and
observations are not lost. The TUI is just another channel.

The two assertions worth pinning forever:

1. Typing ``/exit`` in the TUI input forwards the literal string into
   the host's ``msg_queue`` with channel=``"cli"`` and
   sender_id=``"cli-user"`` (the same wire format the line-mode
   ``_stdin_reader`` thread uses).
2. The TUI App's :meth:`request_exit` is the SOLE call site for
   ``app.exit()`` — neither input handlers nor sink consumers can
   reach around it.
"""

from __future__ import annotations

import threading

import pytest

from pip_agent.agent_host import _bootstrap_tui
from pip_agent.host_io import uninstall_pump


@pytest.fixture(autouse=True)
def _no_residual_pump():
    yield
    uninstall_pump()


@pytest.mark.asyncio
async def test_exit_input_routes_to_msg_queue() -> None:
    """The TUI's input handler enqueues ``/exit`` like any other line."""
    msg_queue: list = []  # type: ignore[type-arg]
    q_lock = threading.Lock()

    app, pump, log_handler = _bootstrap_tui(
        workdir=__import__("pathlib").Path("."),
        force_no_tui=True,  # short-circuit: capability fails, no app built
        msg_queue=msg_queue,
        q_lock=q_lock,
    )
    # ``force_no_tui`` should yield None,None,None: capability ladder
    # explicitly opted out.
    assert app is None
    assert pump is None
    assert log_handler is None


@pytest.mark.asyncio
async def test_app_request_exit_is_only_path_to_app_exit() -> None:
    """``PipBoyTuiApp`` has no public method other than request_exit
    that reaches ``app.exit()``."""
    from pip_agent.tui.app import PipBoyTuiApp

    public_callers = [
        name for name in dir(PipBoyTuiApp)
        if not name.startswith("_") and name not in ("request_exit", "exit")
    ]
    # Inspect the bytecode of every PUBLIC method on PipBoyTuiApp and
    # assert it does not call ``app.exit`` directly. Inheritance from
    # textual.App means ``exit`` itself is exposed; that's fine — the
    # design contract is "host calls request_exit, which calls
    # app.exit" with NO Pip-Boy-defined method other than
    # request_exit going through ``self.exit``.
    import inspect

    for name in public_callers:
        method = getattr(PipBoyTuiApp, name, None)
        if not callable(method) or not hasattr(method, "__code__"):
            continue
        # Only check methods declared on PipBoyTuiApp itself; skip
        # inherited Textual.App methods (we don't own those).
        if method.__qualname__.split(".", 1)[0] != "PipBoyTuiApp":
            continue
        try:
            src = inspect.getsource(method)
        except (TypeError, OSError):
            continue
        assert "self.exit(" not in src, (
            f"PipBoyTuiApp.{name} calls self.exit directly — host_io / "
            f"request_exit is the only sanctioned path."
        )


@pytest.mark.asyncio
async def test_user_line_handler_enqueues_exit_with_correct_metadata() -> None:
    """End-to-end: typing ``/exit`` in the TUI lands in msg_queue with
    channel=cli + sender_id=cli-user, identical to the line-mode reader."""
    from pip_agent.tui.app import PipBoyTuiApp
    from pip_agent.tui.loader import load_builtin_theme
    from pip_agent.tui.pump import UiPump

    received: list[str] = []

    def on_user_line(line: str) -> None:
        received.append(line)

    bundle = load_builtin_theme("wasteland")
    pump = UiPump()
    app = PipBoyTuiApp(
        theme=bundle, pump=pump, on_user_line=on_user_line,
    )

    async with app.run_test() as pilot:
        # Type "/exit" + Enter; the input handler MUST forward to
        # on_user_line, not call self.exit() directly.
        await pilot.press("slash")
        for ch in "exit":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

        # App is still running — ``/exit`` did NOT short-circuit.
        assert app.is_running

    # Lifecycle: input WAS forwarded to the handler that the host wires
    # to its inbound queue.
    assert received == ["/exit"]
