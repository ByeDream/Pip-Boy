"""Integration tests for BackgroundScheduler + CommandQueue lane isolation.

These cover the tier-2 concurrency refactor's acceptance criteria that can
be exercised without a live LLM:

* Slow jobs do not starve faster jobs on different lanes.
* Same-lane jobs serialize in FIFO order.
* ``BackgroundScheduler.stop()`` cleans up without hanging.
* ``CommandQueue.stats()`` is wired through scheduler status.
"""

from __future__ import annotations

import threading
import time

from pip_agent.lanes import CommandQueue
from pip_agent.scheduler import BackgroundJob, BackgroundScheduler


class _RecordingJob(BackgroundJob):
    """Fires exactly once per tick while ``_due`` is set; records start/stop."""

    def __init__(self, name: str, lane_name: str, *, sleep_s: float = 0.0) -> None:
        self.name = name
        self.lane_name = lane_name
        self.sleep_s = sleep_s
        self._due = threading.Event()
        self._due.set()
        self.start_events: list[float] = []
        self.finish_events: list[float] = []
        self._lock = threading.Lock()

    def arm(self) -> None:
        self._due.set()

    def disarm(self) -> None:
        self._due.clear()

    def should_run(self, now: float) -> tuple[bool, str]:
        return (self._due.is_set(), "armed" if self._due.is_set() else "idle")

    def execute(self, now: float, output_queue, queue_lock) -> None:
        t = time.time()
        with self._lock:
            self.start_events.append(t)
        self._due.clear()  # one-shot until re-armed
        if self.sleep_s > 0:
            time.sleep(self.sleep_s)
        with self._lock:
            self.finish_events.append(time.time())


def _wait(pred, timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def test_jobs_on_different_lanes_run_in_parallel():
    """A slow 'heartbeat' lane must not block a fast 'reflect' lane."""
    cq = CommandQueue()
    stop = threading.Event()
    sched = BackgroundScheduler(cq, stop)

    slow = _RecordingJob("heartbeat", "heartbeat", sleep_s=0.3)
    fast = _RecordingJob("reflect", "reflect", sleep_s=0.0)
    sched.register(slow)
    sched.register(fast)

    # Dispatch both without starting the polling thread so we can control timing.
    sched._dispatch(slow, time.time())
    sched._dispatch(fast, time.time())

    assert _wait(lambda: fast.finish_events), "fast job should finish quickly"
    # Fast finishes before slow finishes — proves true parallelism.
    slow_finished_at_fast_done = len(slow.finish_events)
    assert slow_finished_at_fast_done == 0, (
        "slow job on separate lane should still be running while fast job finished"
    )
    assert _wait(lambda: slow.finish_events, timeout=2.0)
    stop.set()


def test_same_lane_serializes_fifo():
    """Two jobs on the same lane run one after another."""
    cq = CommandQueue()
    stop = threading.Event()
    sched = BackgroundScheduler(cq, stop)

    first = _RecordingJob("first", "shared", sleep_s=0.2)
    second = _RecordingJob("second", "shared", sleep_s=0.0)
    sched.register(first)
    sched.register(second)

    sched._dispatch(first, time.time())
    sched._dispatch(second, time.time())

    assert _wait(lambda: len(second.finish_events) == 1, timeout=2.0)
    assert first.finish_events[0] <= second.start_events[0] + 1e-3, (
        "second must start only after first completed (FIFO serial execution)"
    )
    stop.set()


def test_busy_lane_skips_redundant_dispatch():
    """_tick does not double-dispatch a job whose lane is already busy."""
    cq = CommandQueue()
    stop = threading.Event()
    sched = BackgroundScheduler(cq, stop)
    job = _RecordingJob("slow", "heartbeat", sleep_s=0.3)
    sched.register(job)

    sched._dispatch(job, time.time())
    # Lane is busy; _tick must not enqueue a second run.
    job.arm()
    sched._tick()

    assert _wait(lambda: len(job.finish_events) == 1, timeout=2.0)
    time.sleep(0.1)
    assert len(job.start_events) == 1, (
        "busy lane should have gated the second dispatch"
    )
    stop.set()


def test_stop_cleanly_shuts_down_polling_thread():
    cq = CommandQueue()
    stop = threading.Event()
    sched = BackgroundScheduler(cq, stop)
    sched.register(_RecordingJob("noop", "noop"))
    sched.start()
    assert sched._thread is not None and sched._thread.is_alive()
    sched.stop()
    assert sched._thread is None
    assert stop.is_set()


def test_status_exposes_lane_stats():
    cq = CommandQueue()
    stop = threading.Event()
    sched = BackgroundScheduler(cq, stop)
    sched.register(_RecordingJob("a", "lane-a"))
    sched.register(_RecordingJob("b", "lane-b"))

    info = sched.status()
    assert info["job_count"] == 2
    assert set(info["lanes"].keys()) == {"lane-a", "lane-b"}
    for st in info["lanes"].values():
        assert {"name", "queue_depth", "active", "max_concurrency"} <= set(st.keys())
    stop.set()
