"""Shutdown-contract test for ``agent_host.run_host``.

The M7 fix (``plan H.M7``) moved ``flush_and_rotate`` into ``run_host``'s
``finally`` block so that Ctrl+C, a crashing channel thread, or any other
non-graceful exit still drains the in-memory session map through the
reflect pipeline. Without that, hours of interactive traffic evaporate —
reflect normally only runs on heartbeat / PreCompact / a clean ``/exit``.

This test exercises that ``finally`` branch end-to-end with aggressive
monkeypatching of every heavy setup step in ``run_host`` so we can inject
a ``KeyboardInterrupt`` into the event-loop body and then assert the
shutdown path still called ``flush_and_rotate`` exactly once.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class TestRunHostFlushesOnCtrlC:
    def test_keyboard_interrupt_still_triggers_flush(
        self, tmp_path: Path, monkeypatch,
    ):
        from pip_agent import agent_host as mod

        flush_calls: list[int] = []

        async def _fake_flush(self: Any) -> Any:  # noqa: D401, ANN001
            flush_calls.append(1)
            return mod.FlushSummary(rotated=0, reflected=0, observations=0)

        monkeypatch.setattr(mod.AgentHost, "flush_and_rotate", _fake_flush)

        monkeypatch.setattr(
            "pip_agent.scaffold.ensure_workspace", lambda _p: None,
        )
        monkeypatch.setattr(
            "pip_agent.config.Settings.check_required",
            lambda self: None,
        )
        monkeypatch.setattr(mod, "WORKDIR", tmp_path)
        monkeypatch.setattr(
            mod, "WORKSPACE_PIP_DIR", tmp_path / ".pip",
        )
        monkeypatch.setattr(mod, "BINDINGS_PATH", tmp_path / "bindings.json")
        (tmp_path / ".pip").mkdir(parents=True, exist_ok=True)

        class _StubRegistry:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def list_agents(self) -> list[Any]:
                return []

        class _StubBindingTable:
            def load(self, _p: Path) -> None: ...
            def save(self, _p: Path) -> None: ...
            def remove(self, *a: Any, **kw: Any) -> bool:
                return False
            def add(self, *a: Any, **kw: Any) -> None: ...
            def list_all(self) -> list[Any]:
                return []

        class _StubChannelManager:
            def __init__(self) -> None:
                self._closed = 0
            def register(self, _ch: Any) -> None: ...
            def list_channels(self) -> list[str]:
                return ["cli"]
            def get(self, name: str) -> Any:
                return object() if name == "cli" else None
            def close_all(self) -> None:
                self._closed += 1

        class _StubScheduler:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self._started = False
            def start(self) -> None:
                self._started = True
            def stop(self) -> None: ...

        monkeypatch.setattr(mod, "AgentRegistry", _StubRegistry)
        monkeypatch.setattr(mod, "BindingTable", _StubBindingTable)
        monkeypatch.setattr(mod, "ChannelManager", _StubChannelManager)
        monkeypatch.setattr(mod, "CLIChannel", lambda: object())
        monkeypatch.setattr(mod, "HostScheduler", _StubScheduler)

        # The bug we are guarding against: ``asyncio.run(_run())`` gets
        # killed by Ctrl+C on the user's terminal. We reproduce that by
        # making the FIRST ``asyncio.run`` call raise, and by then
        # letting subsequent calls (for flush_and_rotate) go through.
        real_run = mod.asyncio.run
        call_count = {"n": 0}

        def _fake_asyncio_run(coro: Any) -> Any:  # noqa: D401
            call_count["n"] += 1
            if call_count["n"] == 1:
                coro.close()
                raise KeyboardInterrupt
            return real_run(coro)

        monkeypatch.setattr(mod.asyncio, "run", _fake_asyncio_run)

        mod.run_host()

        assert flush_calls == [1], (
            "run_host must call flush_and_rotate in its finally block "
            "even when the main event loop is killed by KeyboardInterrupt."
        )

    def test_flush_exception_does_not_block_shutdown(
        self, tmp_path: Path, monkeypatch,
    ):
        """A broken reflect must not wedge the host on shutdown.

        We make ``flush_and_rotate`` blow up and then assert ``run_host``
        still returns cleanly (i.e. ``scheduler.stop`` / ``close_all``
        downstream of the try/except still fire) — so the operator gets
        their shell prompt back instead of a hung Python process.
        """
        from pip_agent import agent_host as mod

        async def _bad_flush(self: Any) -> Any:  # noqa: D401, ANN001
            raise RuntimeError("reflect pipeline is on fire")

        monkeypatch.setattr(mod.AgentHost, "flush_and_rotate", _bad_flush)

        monkeypatch.setattr(
            "pip_agent.scaffold.ensure_workspace", lambda _p: None,
        )
        monkeypatch.setattr(
            "pip_agent.config.Settings.check_required",
            lambda self: None,
        )
        monkeypatch.setattr(mod, "WORKDIR", tmp_path)
        monkeypatch.setattr(
            mod, "WORKSPACE_PIP_DIR", tmp_path / ".pip",
        )
        monkeypatch.setattr(mod, "BINDINGS_PATH", tmp_path / "bindings.json")
        (tmp_path / ".pip").mkdir(parents=True, exist_ok=True)

        stop_calls: list[int] = []
        close_calls: list[int] = []

        class _StubRegistry:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def list_agents(self) -> list[Any]:
                return []

        class _StubBindingTable:
            def load(self, _p: Path) -> None: ...
            def save(self, _p: Path) -> None: ...
            def remove(self, *a: Any, **kw: Any) -> bool:
                return False
            def list_all(self) -> list[Any]:
                return []

        class _StubChannelManager:
            def register(self, _ch: Any) -> None: ...
            def list_channels(self) -> list[str]:
                return ["cli"]
            def get(self, name: str) -> Any:
                return object() if name == "cli" else None
            def close_all(self) -> None:
                close_calls.append(1)

        class _StubScheduler:
            def __init__(self, *_a: Any, **_kw: Any) -> None: ...
            def start(self) -> None: ...
            def stop(self) -> None:
                stop_calls.append(1)

        monkeypatch.setattr(mod, "AgentRegistry", _StubRegistry)
        monkeypatch.setattr(mod, "BindingTable", _StubBindingTable)
        monkeypatch.setattr(mod, "ChannelManager", _StubChannelManager)
        monkeypatch.setattr(mod, "CLIChannel", lambda: object())
        monkeypatch.setattr(mod, "HostScheduler", _StubScheduler)

        real_run = mod.asyncio.run
        call_count = {"n": 0}

        def _fake_asyncio_run(coro: Any) -> Any:  # noqa: D401
            call_count["n"] += 1
            if call_count["n"] == 1:
                coro.close()
                raise KeyboardInterrupt
            return real_run(coro)

        monkeypatch.setattr(mod.asyncio, "run", _fake_asyncio_run)

        mod.run_host()

        assert stop_calls == [1], "scheduler.stop never fired — finally wedged"
        assert close_calls == [1], "channel_mgr.close_all never fired — finally wedged"
