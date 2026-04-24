"""Unit tests for the WeChat poll-loop idle backoff contract.

``wechat_poll_loop`` runs in a daemon thread in production. We test the
new idle-backoff behaviour by driving the loop through a handful of
iterations with a stub channel and a stub ``stop`` event whose
``wait()`` we record.

Covered:

* Empty-poll path: ``stop.wait(settings.wechat_poll_idle_sec)`` fires
  exactly once per idle iteration.
* Non-empty path: ``wait`` is NOT called (loops immediately for active
  conversations).
* Transition empty → non-empty → empty: no wait between the empty
  result and the active one; wait resumes on the next empty.
* ``wechat_poll_idle_sec = 0``: no wait at all (pre-Tier-2 hot-loop
  behaviour, useful for users who want maximum latency).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from unittest.mock import patch

import pytest

from pip_agent.channels.base import InboundMessage
from pip_agent.channels.wechat import wechat_poll_loop


class _StubStop:
    """Drop-in for ``threading.Event`` that records every ``wait`` call.

    The real loop uses ``stop.wait(seconds)`` both as the sleep primitive
    and as the shutdown-interrupt check. We stop the loop by flipping
    ``_set_after`` iterations, then reporting ``is_set() == True`` so the
    loop exits its while-condition cleanly.
    """

    def __init__(self, stop_after_polls: int) -> None:
        self._poll_count = 0
        self._stop_after = stop_after_polls
        self._is_set = False
        self.wait_calls: list[float] = []

    def tick_poll(self) -> None:
        """Called by the stub channel after each ``poll()``."""
        self._poll_count += 1
        if self._poll_count >= self._stop_after:
            self._is_set = True

    def is_set(self) -> bool:
        return self._is_set

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(float(timeout) if timeout is not None else -1.0)
        # Wait returns True when the event is set — in prod that means
        # "we've been asked to stop". Mirror that so the loop's post-wait
        # ``if stop.is_set()`` branches correctly.
        return self._is_set


class _StubAccount:
    is_logged_in = True


class _StubChannel:
    """Minimal ``WeChatChannel`` surface the poll loop touches."""

    def __init__(
        self,
        *,
        stop: _StubStop,
        poll_results: Callable[[int], list[InboundMessage]],
    ) -> None:
        self._stop = stop
        self._poll_results = poll_results
        self._call_ix = 0
        self._account = _StubAccount()

    def get_account(self, _account_id: str) -> _StubAccount:
        return self._account

    def poll(self, _account_id: str) -> list[InboundMessage]:
        msgs = self._poll_results(self._call_ix)
        self._call_ix += 1
        # Tell stop how many polls we've done so it can raise its flag
        # when the test's bound is hit.
        self._stop.tick_poll()
        return msgs


@pytest.fixture
def _fake_settings():
    """Patch the module-level ``settings`` the loop imports lazily.

    The loop does ``from pip_agent.config import settings`` inside the
    function body, so we patch the attribute on the live settings
    instance (``settings.wechat_poll_idle_sec``).
    """
    from pip_agent.config import settings as live

    original = live.wechat_poll_idle_sec
    yield live
    live.wechat_poll_idle_sec = original


def _drive(
    poll_results: Callable[[int], list[InboundMessage]],
    stop_after_polls: int,
) -> tuple[_StubStop, list[InboundMessage]]:
    stop = _StubStop(stop_after_polls=stop_after_polls)
    queue: list[InboundMessage] = []
    lock = threading.Lock()
    channel = _StubChannel(stop=stop, poll_results=poll_results)
    # No Polling print in tests — it's harmless but avoids stdout noise
    # with ``pytest -s``.
    with patch("builtins.print"):
        wechat_poll_loop(
            channel, "bot-a", queue, lock, stop,  # type: ignore[arg-type]
        )
    return stop, queue


def test_empty_poll_triggers_single_idle_wait(_fake_settings) -> None:
    """One empty poll → one ``stop.wait(idle_sec)`` with configured value."""
    _fake_settings.wechat_poll_idle_sec = 0.25
    stop, queue = _drive(poll_results=lambda i: [], stop_after_polls=1)
    # Exactly one wait, at the configured idle value.
    assert stop.wait_calls == [0.25]
    assert queue == []


def test_non_empty_poll_does_not_wait(_fake_settings) -> None:
    """A poll with messages loops immediately — zero wait calls."""
    _fake_settings.wechat_poll_idle_sec = 0.25
    msg = InboundMessage(
        text="hi", sender_id="u1", channel="wechat", peer_id="u1",
    )
    stop, queue = _drive(
        poll_results=lambda i: [msg],
        stop_after_polls=1,
    )
    assert stop.wait_calls == []
    assert queue == [msg]


def test_mixed_sequence_backs_off_only_on_empty(_fake_settings) -> None:
    """Empty → active → empty: wait only after the empty polls."""
    _fake_settings.wechat_poll_idle_sec = 0.5
    msg = InboundMessage(
        text="hi", sender_id="u1", channel="wechat", peer_id="u1",
    )

    def _results(i: int) -> list[InboundMessage]:
        return {0: [], 1: [msg], 2: []}[i]

    stop, queue = _drive(poll_results=_results, stop_after_polls=3)
    # Waits fire on polls 0 and 2 (both empty), NOT on poll 1 (active).
    assert stop.wait_calls == [0.5, 0.5]
    assert queue == [msg]


def test_idle_zero_disables_backoff(_fake_settings) -> None:
    """``wechat_poll_idle_sec = 0`` restores the pre-backoff hot loop."""
    _fake_settings.wechat_poll_idle_sec = 0
    stop, queue = _drive(poll_results=lambda i: [], stop_after_polls=3)
    # No waits at all — every empty poll loops immediately.
    assert stop.wait_calls == []
    assert queue == []
