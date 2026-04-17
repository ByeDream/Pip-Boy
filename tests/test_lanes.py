from __future__ import annotations

import threading
import time

import pytest

from pip_agent.lanes import CommandQueue, LaneQueue


def test_single_lane_fifo_order():
    lane = LaneQueue("main")
    results: list[int] = []
    futures = []
    for i in range(10):
        def _fn(n: int = i) -> int:
            results.append(n)
            return n

        futures.append(lane.enqueue(_fn))

    for f in futures:
        assert f.result(timeout=5) is not None

    assert results == list(range(10)), "serial lane must preserve FIFO order"


def test_single_lane_serial_execution():
    """max_concurrency=1 guarantees no two tasks overlap in time."""
    lane = LaneQueue("main", max_concurrency=1)
    overlap_lock = threading.Lock()
    active = [0]
    max_seen = [0]

    def _fn() -> None:
        with overlap_lock:
            active[0] += 1
            if active[0] > max_seen[0]:
                max_seen[0] = active[0]
        time.sleep(0.02)
        with overlap_lock:
            active[0] -= 1

    futures = [lane.enqueue(_fn) for _ in range(5)]
    for f in futures:
        f.result(timeout=5)

    assert max_seen[0] == 1, "max_concurrency=1 lane must not run tasks in parallel"


def test_multiple_lanes_run_in_parallel():
    """Different lanes do not block each other."""
    cq = CommandQueue()
    start_barrier = threading.Barrier(3, timeout=5)
    done_event = threading.Event()

    def _fn() -> str:
        start_barrier.wait(timeout=5)
        return "ok"

    futures = [
        cq.enqueue("lane-a", _fn),
        cq.enqueue("lane-b", _fn),
        cq.enqueue("lane-c", _fn),
    ]
    for f in futures:
        assert f.result(timeout=5) == "ok"
    done_event.set()


def test_lane_max_concurrency_allows_parallel():
    lane = LaneQueue("parallel", max_concurrency=3)
    barrier = threading.Barrier(3, timeout=5)

    def _fn() -> None:
        barrier.wait(timeout=5)

    futures = [lane.enqueue(_fn) for _ in range(3)]
    for f in futures:
        f.result(timeout=5)


def test_future_captures_exception():
    lane = LaneQueue("err")

    def _fn() -> None:
        raise RuntimeError("boom")

    future = lane.enqueue(_fn)
    with pytest.raises(RuntimeError, match="boom"):
        future.result(timeout=5)


def test_stats_reflects_queue_state():
    lane = LaneQueue("s", max_concurrency=1)
    gate = threading.Event()

    def _blocker() -> None:
        gate.wait(timeout=5)

    f1 = lane.enqueue(_blocker)
    f2 = lane.enqueue(_blocker)
    time.sleep(0.05)

    st = lane.stats()
    assert st["name"] == "s"
    assert st["active"] == 1
    assert st["queue_depth"] == 1
    assert st["max_concurrency"] == 1

    gate.set()
    f1.result(timeout=5)
    f2.result(timeout=5)
    # _task_done decrements active_count AFTER set_result, so wait briefly
    # for the lane worker to finish bookkeeping.
    deadline = time.monotonic() + 2.0
    while lane.stats()["active"] != 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    st_after = lane.stats()
    assert st_after["active"] == 0
    assert st_after["queue_depth"] == 0


def test_command_queue_lazy_lane_creation():
    cq = CommandQueue()
    assert cq.lane_names() == []

    f = cq.enqueue("new-lane", lambda: 42)
    assert f.result(timeout=5) == 42
    assert "new-lane" in cq.lane_names()


def test_command_queue_aggregate_stats():
    cq = CommandQueue()
    cq.get_or_create_lane("a")
    cq.get_or_create_lane("b")
    snapshot = cq.stats()
    assert set(snapshot.keys()) == {"a", "b"}
    for st in snapshot.values():
        assert st["active"] == 0
        assert st["queue_depth"] == 0


def test_command_queue_lane_busy():
    cq = CommandQueue()
    gate = threading.Event()

    def _blocker() -> None:
        gate.wait(timeout=5)

    assert cq.lane_busy("nope") is False
    f = cq.enqueue("work", _blocker)
    time.sleep(0.05)
    assert cq.lane_busy("work") is True
    gate.set()
    f.result(timeout=5)
    time.sleep(0.05)
    assert cq.lane_busy("work") is False


def test_increasing_max_concurrency_pumps_waiting_tasks():
    lane = LaneQueue("grow", max_concurrency=1)
    gate = threading.Event()

    def _blocker() -> None:
        gate.wait(timeout=5)

    futures = [lane.enqueue(_blocker) for _ in range(3)]
    time.sleep(0.05)
    assert lane.stats()["active"] == 1

    lane.max_concurrency = 3
    time.sleep(0.05)
    assert lane.stats()["active"] == 3

    gate.set()
    for f in futures:
        f.result(timeout=5)
