from __future__ import annotations

import time
import threading

from pip_agent.background import BackgroundTaskManager, NOTIFICATION_TRUNCATE


def _echo(tool_input: dict) -> str:
    return tool_input.get("output", "(no output)")


def _slow(tool_input: dict) -> str:
    time.sleep(tool_input.get("delay", 0.1))
    return "done"


def _failing(tool_input: dict) -> str:
    raise RuntimeError("boom")


class TestSpawnAndDrain:
    def test_basic(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo hello", _echo, {"output": "hello"})
        time.sleep(0.05)
        notifications = mgr.drain()
        assert len(notifications) == 1
        n = notifications[0]
        assert n.task_id == "t1"
        assert n.status == "completed"
        assert n.result == "hello"
        assert n.elapsed_ms >= 0

    def test_drain_clears_queue(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo hi", _echo, {"output": "hi"})
        time.sleep(0.05)
        first = mgr.drain()
        second = mgr.drain()
        assert len(first) == 1
        assert len(second) == 0


class TestDrainEmpty:
    def test_returns_empty_list(self):
        mgr = BackgroundTaskManager()
        assert mgr.drain() == []


class TestHasPending:
    def test_true_while_running(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "sleep", _slow, {"delay": 0.3})
        assert mgr.has_pending() is True

    def test_false_after_completion(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo", _echo, {"output": "x"})
        time.sleep(0.05)
        assert mgr.has_pending() is False


class TestCheckSpecificTask:
    def test_returns_full_output(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo hello", _echo, {"output": "full output here"})
        time.sleep(0.05)
        result = mgr.check("t1")
        assert "[completed]" in result
        assert "full output here" in result

    def test_running_task(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "sleep", _slow, {"delay": 0.5})
        result = mgr.check("t1")
        assert "[running]" in result

    def test_unknown_task(self):
        mgr = BackgroundTaskManager()
        result = mgr.check("nonexistent")
        assert "Unknown task" in result


class TestCheckAllTasks:
    def test_lists_all(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo a", _echo, {"output": "a"})
        mgr.spawn("t2", "echo b", _echo, {"output": "b"})
        time.sleep(0.05)
        result = mgr.check()
        assert "t1:" in result
        assert "t2:" in result

    def test_no_tasks(self):
        mgr = BackgroundTaskManager()
        result = mgr.check()
        assert "No background tasks" in result


class TestConcurrentTasks:
    def test_multiple_spawns(self):
        mgr = BackgroundTaskManager()
        for i in range(5):
            mgr.spawn(f"t{i}", f"echo {i}", _echo, {"output": str(i)})
        time.sleep(0.1)
        notifications = mgr.drain()
        assert len(notifications) == 5
        task_ids = {n.task_id for n in notifications}
        assert task_ids == {f"t{i}" for i in range(5)}


class TestExceptionInWorker:
    def test_error_captured(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "fail", _failing, {})
        time.sleep(0.05)
        notifications = mgr.drain()
        assert len(notifications) == 1
        n = notifications[0]
        assert n.status == "error"
        assert "boom" in n.result

    def test_error_in_tasks_dict(self):
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "fail", _failing, {})
        time.sleep(0.05)
        result = mgr.check("t1")
        assert "[error]" in result
        assert "boom" in result


class TestNotificationTruncation:
    def test_long_output_truncated_in_notification(self):
        long_output = "x" * 2000
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo long", _echo, {"output": long_output})
        time.sleep(0.05)
        notifications = mgr.drain()
        assert len(notifications) == 1
        n = notifications[0]
        assert len(n.result) < len(long_output)
        assert "truncated" in n.result

    def test_full_output_in_tasks_dict(self):
        long_output = "x" * 2000
        mgr = BackgroundTaskManager()
        mgr.spawn("t1", "echo long", _echo, {"output": long_output})
        time.sleep(0.05)
        result = mgr.check("t1")
        assert long_output in result
