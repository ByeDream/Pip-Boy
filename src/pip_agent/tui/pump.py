"""Thread-safe single entry point between producer threads and the Textual App.

This module exists to enforce one architectural rule from the Pip-Boy
TUI design.md: **no producer code is allowed to assume which thread it
runs on**. Logging handlers, channel inbound threads, scheduler ticks,
and the agent runner can each fire events from arbitrary threads,
including the Textual App's own event-loop thread (which is the case
that ``call_from_thread`` blows up on with
``RuntimeError: must run in a different thread from the app``).

The ``UiPump`` solution:

1. Producers call ``pump.agent_sink(event)`` /
   ``pump.log_sink(record)`` / ``pump.status_sink(event)``.
2. The pump wraps the payload in a Textual ``Message`` subclass and
   posts it via ``app.post_message`` — the one Textual API documented
   as safe from any thread, App-thread included.
3. The Textual App declares ``on_*_message`` handlers for the message
   subclasses; those handlers run on the App thread, where every
   widget mutation is naturally safe.

When no app is attached yet (during host boot, before the TUI mounts),
events are appended to a bounded buffer. ``attach`` flushes the buffer
in arrival order so banner / scaffold logs aren't lost. ``detach``
clears the buffer to prevent a teardown race from posting into a torn
down App.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from pip_agent.tui.sinks import AgentEvent, StatusEvent

if TYPE_CHECKING:  # pragma: no cover — type-checking only
    from textual.app import App

__all__ = ["UiPump"]

# Bounded so a runaway producer (e.g. a tight log loop fired before the
# TUI mounts) cannot OOM the host. 4096 is comfortably above the largest
# burst we observed during boot (banner + scaffold + ~30 channel/scheduler
# init records); past the cap the *oldest* events are dropped so the most
# recent state is what the user sees once the App attaches.
_BUFFER_LIMIT = 4096


# Sentinel used to tag buffered entries by sink kind. Cheaper than a
# dataclass per entry given how hot this path is during boot.
_AGENT = "agent"
_LOG = "log"
_STATUS = "status"


class UiPump:
    """Thread-safe sink fan-in for a single :class:`PipBoyTuiApp`.

    Lifecycle:

    * Construct early (during host boot) so banner/scaffold producers
      have a place to push events to before the App exists.
    * Call :meth:`attach` once the Textual App is mounted; buffered
      events flush in arrival order onto the App's message queue.
    * Call :meth:`detach` during shutdown so any late-arriving event
      from a worker thread doesn't land on a disposed App.

    Producer-side API (:meth:`agent_sink`, :meth:`log_sink`,
    :meth:`status_sink`) is intentionally callable — they conform to
    the :class:`pip_agent.tui.sinks.AgentSink` / ``LogSink`` /
    ``StatusSink`` Protocols so callers can type-annotate against the
    Protocol and a ``Null*Sink`` is interchangeable in line mode.
    """

    BUFFER_LIMIT: int = _BUFFER_LIMIT

    def __init__(self) -> None:
        self._app: "App[Any] | None" = None
        # ``RLock`` because ``attach`` flushes inside the lock and the
        # message-construction path may re-enter ``_post`` if a Message
        # subclass constructor logs (it shouldn't, but a defensive
        # re-entrant lock keeps the code from deadlocking on a future
        # accident).
        self._lock = threading.RLock()
        self._pending: list[tuple[str, Any]] = []
        self._dropped: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach(self, app: "App[Any]") -> None:
        """Bind a Textual App and flush any buffered events.

        Idempotent on re-attach: a second ``attach`` with the same app
        is a no-op; with a different app the old buffer is replayed
        against the new one (``detach`` should run between the two in
        well-behaved code, but we don't rely on it).
        """
        with self._lock:
            self._app = app
            buffered = self._pending
            self._pending = []

        for kind, payload in buffered:
            self._post_to_app(app, kind, payload)

    def detach(self) -> None:
        """Disconnect from the App and discard the buffer.

        Call from teardown so a worker thread that fires an event
        between "App.run() returned" and "process exit" doesn't post
        onto a torn-down message queue.
        """
        with self._lock:
            self._app = None
            self._pending.clear()

    @property
    def is_attached(self) -> bool:
        """True iff an App is currently bound. Used by host code to decide
        whether to take the TUI path or fall back to ``print``."""
        with self._lock:
            return self._app is not None

    @property
    def dropped_count(self) -> int:
        """Number of events dropped because the buffer was full prior to
        attach. Surfaced via ``pip-boy doctor`` in Phase C."""
        with self._lock:
            return self._dropped

    # ------------------------------------------------------------------
    # Sink methods (Protocol-compatible callables)
    # ------------------------------------------------------------------

    def agent_sink(self, event: AgentEvent) -> None:
        """Push an :class:`AgentEvent` onto the agent pane."""
        self._post(_AGENT, event)

    def log_sink(self, record: logging.LogRecord) -> None:
        """Push a stdlib log record onto the app-log pane."""
        self._post(_LOG, record)

    def status_sink(self, event: StatusEvent) -> None:
        """Push a :class:`StatusEvent` onto the status bar."""
        self._post(_STATUS, event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _post(self, kind: str, payload: Any) -> None:
        """Buffer or post one event, holding the lock minimally.

        We capture ``app`` under the lock and release it before doing
        the actual ``post_message`` so a slow ``post_message`` call
        cannot serialize unrelated producers behind itself.
        """
        with self._lock:
            app = self._app
            if app is None:
                if len(self._pending) >= self.BUFFER_LIMIT:
                    # Drop oldest first — preserve recency. The dropped
                    # count surfaces via ``doctor`` so an operator can
                    # spot a producer that's hammering the buffer.
                    self._pending.pop(0)
                    self._dropped += 1
                self._pending.append((kind, payload))
                return

        self._post_to_app(app, kind, payload)

    @staticmethod
    def _post_to_app(app: "App[Any]", kind: str, payload: Any) -> None:
        """Wrap ``payload`` in the appropriate Textual ``Message`` and post.

        Lazy import of :mod:`pip_agent.tui.messages`: that module pulls
        in :mod:`textual.message`, which we don't want to require for
        line-mode boots. The first sink call after attach pays the
        ~5 ms import cost once; everything thereafter hits the cached
        module.

        Any failure here is swallowed — a UI failure must NEVER raise
        into a producer thread (logger, stream-event handler, scheduler
        tick). Worst case the user loses one rendered line; best case
        the next call succeeds and rendering catches up.
        """
        try:
            from pip_agent.tui.messages import (
                AgentMessage,
                LogMessage,
                StatusMessage,
            )

            if kind == _AGENT:
                app.post_message(AgentMessage(payload))
            elif kind == _LOG:
                app.post_message(LogMessage(payload))
            elif kind == _STATUS:
                app.post_message(StatusMessage(payload))
            else:  # pragma: no cover — kind is enum-like, only set above
                pass
        except Exception:
            # Swallow on purpose. A user-facing failure mode here would
            # be a producer thread crash, which design.md §2 calls out
            # as the worst possible TUI integration bug.
            pass
