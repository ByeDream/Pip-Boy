from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable

MAX_RESULT_CHARS = 50_000
NOTIFICATION_TRUNCATE = 500


@dataclass
class Notification:
    task_id: str
    status: str
    command: str
    result: str
    elapsed_ms: float


class BackgroundTaskManager:
    """Manage background shell tasks on daemon threads with a notification queue."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._notifications: list[Notification] = []
        self._lock = threading.Lock()

    def spawn(
        self,
        task_id: str,
        command: str,
        fn: Callable[[dict], str],
        tool_input: dict,
    ) -> None:
        self._tasks[task_id] = {
            "status": "running",
            "command": command[:80],
            "result": None,
            "elapsed_ms": 0.0,
        }
        thread = threading.Thread(
            target=self._execute,
            args=(task_id, fn, tool_input),
            daemon=True,
        )
        thread.start()

    def _execute(
        self,
        task_id: str,
        fn: Callable[[dict], str],
        tool_input: dict,
    ) -> None:
        t0 = time.perf_counter()
        try:
            result = fn(tool_input)
            status = "completed"
        except Exception as e:
            result = f"Error: {e}"
            status = "error"
        elapsed_ms = (time.perf_counter() - t0) * 1000

        result = (result or "(no output)")[:MAX_RESULT_CHARS]

        task = self._tasks[task_id]
        task["status"] = status
        task["result"] = result
        task["elapsed_ms"] = elapsed_ms

        truncated = result[:NOTIFICATION_TRUNCATE]
        if len(result) > NOTIFICATION_TRUNCATE:
            truncated += "... [truncated, use check_background for full output]"

        with self._lock:
            self._notifications.append(
                Notification(
                    task_id=task_id,
                    status=status,
                    command=task["command"],
                    result=truncated,
                    elapsed_ms=elapsed_ms,
                )
            )

    def drain(self) -> list[Notification]:
        with self._lock:
            notifications = list(self._notifications)
            self._notifications.clear()
        return notifications

    def check(self, task_id: str | None = None) -> str:
        if task_id is not None:
            task = self._tasks.get(task_id)
            if not task:
                return f"Unknown task: {task_id}"
            return (
                f"[{task['status']}] {task['command']}\n"
                f"{task.get('result') or '(running)'}"
            )
        if not self._tasks:
            return "No background tasks."
        lines = []
        for tid, task in self._tasks.items():
            lines.append(f"{tid}: [{task['status']}] {task['command']}")
        return "\n".join(lines)

    def has_pending(self) -> bool:
        return any(t["status"] == "running" for t in self._tasks.values())
