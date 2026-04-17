"""Named FIFO lane queues for concurrent task execution.

A *lane* is a named FIFO queue that runs up to ``max_concurrency`` tasks at a
time. Tasks are ordinary callables; each ``enqueue()`` call returns a
``concurrent.futures.Future`` that resolves when the task finishes. The pump
loop is self-driving: when a task completes it checks whether more work can
start, so no external scheduler thread is required.

Compared with the tutorial prototype this module omits generation tracking
and ``wait_for_idle`` -- Pip-Boy does not yet need restart-safe pumping or
blocking drain. Both can be added later without changing the public API.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)


class LaneQueue:
    """Named FIFO queue that runs up to ``max_concurrency`` tasks in parallel.

    Each enqueued callable runs in its own daemon thread. Results are
    delivered through ``concurrent.futures.Future``. The queue is self-pumping:
    every completion triggers ``_pump()`` so pending items start as soon as
    capacity frees up.
    """

    def __init__(self, name: str, max_concurrency: int = 1) -> None:
        self.name = name
        self._max_concurrency = max(1, max_concurrency)
        self._deque: deque[tuple[Callable[[], Any], concurrent.futures.Future]] = deque()
        self._condition = threading.Condition()
        self._active_count = 0

    @property
    def max_concurrency(self) -> int:
        with self._condition:
            return self._max_concurrency

    @max_concurrency.setter
    def max_concurrency(self, value: int) -> None:
        new_max = max(1, int(value))
        with self._condition:
            self._max_concurrency = new_max
            self._pump()
            self._condition.notify_all()

    def enqueue(self, fn: Callable[[], Any]) -> concurrent.futures.Future:
        """Append ``fn`` to the queue and return a Future for its result."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._condition:
            self._deque.append((fn, future))
            self._pump()
        return future

    def _pump(self) -> None:
        """Start as many pending tasks as capacity allows.

        Caller must hold ``self._condition``.
        """
        while self._active_count < self._max_concurrency and self._deque:
            fn, future = self._deque.popleft()
            self._active_count += 1
            t = threading.Thread(
                target=self._run_task,
                args=(fn, future),
                daemon=True,
                name=f"lane-{self.name}",
            )
            t.start()

    def _run_task(
        self,
        fn: Callable[[], Any],
        future: concurrent.futures.Future,
    ) -> None:
        try:
            result = fn()
            future.set_result(result)
        except BaseException as exc:  # noqa: BLE001 -- propagate anything to the Future
            future.set_exception(exc)
            if not isinstance(exc, Exception):
                # Re-raise KeyboardInterrupt / SystemExit after reporting
                raise
        finally:
            self._task_done()

    def _task_done(self) -> None:
        with self._condition:
            self._active_count -= 1
            self._pump()
            self._condition.notify_all()

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                "name": self.name,
                "queue_depth": len(self._deque),
                "active": self._active_count,
                "max_concurrency": self._max_concurrency,
            }


class CommandQueue:
    """Central dispatcher that routes callables to named ``LaneQueue`` instances.

    Lanes are created lazily on first use. Each lane is independent, so a slow
    task on one lane never blocks another.
    """

    def __init__(self) -> None:
        self._lanes: dict[str, LaneQueue] = {}
        self._lock = threading.Lock()

    def get_or_create_lane(
        self,
        name: str,
        max_concurrency: int = 1,
    ) -> LaneQueue:
        with self._lock:
            lane = self._lanes.get(name)
            if lane is None:
                lane = LaneQueue(name, max_concurrency)
                self._lanes[name] = lane
            return lane

    def enqueue(
        self,
        lane_name: str,
        fn: Callable[[], Any],
        *,
        max_concurrency: int = 1,
    ) -> concurrent.futures.Future:
        """Route ``fn`` to the named lane (creating it if needed)."""
        lane = self.get_or_create_lane(lane_name, max_concurrency=max_concurrency)
        return lane.enqueue(fn)

    def lane_names(self) -> list[str]:
        with self._lock:
            return list(self._lanes.keys())

    def stats(self) -> dict[str, dict[str, Any]]:
        """Return ``{lane_name: lane.stats()}`` for every known lane."""
        with self._lock:
            lanes = list(self._lanes.values())
        return {lane.name: lane.stats() for lane in lanes}

    def lane_busy(self, lane_name: str) -> bool:
        """Return True if the lane currently has running or queued work."""
        with self._lock:
            lane = self._lanes.get(lane_name)
        if lane is None:
            return False
        st = lane.stats()
        return st["active"] > 0 or st["queue_depth"] > 0
