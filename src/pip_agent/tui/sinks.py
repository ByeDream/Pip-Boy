"""Locked sink protocol: the only way producer code talks to the TUI.

Three channels, three event shapes — all framework-owned, intentionally
narrow:

* ``agent_sink`` — model dialog: streamed text deltas, thinking deltas,
  tool-use traces, finalized turn footer, errors, and the user's own
  echoed input. The single output for "what the agent is saying".
* ``log_sink`` — stdlib :class:`logging.LogRecord` records routed to the
  TUI's ``#app-log`` pane. The single output for "what the host is
  doing under the hood".
* ``status_sink`` — small banner / channel-ready / scheduler-tick
  status updates that belong in a dedicated bar widget.

The event ``kind`` whitelists are LOCKED for v1. Adding a new kind is
a breaking change for theme authors and for the snapshot test baseline,
so any future addition must come with explicit migration notes.

Theme code, sink consumers, and producer code all operate against these
dataclasses. ``__post_init__`` validates ``kind`` so a typo at the
producer side fails at the producer's call site, not silently in the
UI thread.

There are no methods on the events; they're plain data. The UI pump
(:mod:`pip_agent.tui.pump`) is the only piece allowed to know how to
turn an event into a Textual message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "AGENT_EVENT_KINDS",
    "STATUS_EVENT_KINDS",
    "AgentEvent",
    "AgentSink",
    "LogSink",
    "NullAgentSink",
    "NullLogSink",
    "NullStatusSink",
    "StatusEvent",
    "StatusSink",
]


# ---------------------------------------------------------------------------
# Event-kind whitelists (LOCKED — v1 contract)
# ---------------------------------------------------------------------------

AGENT_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "user_input",       # the user's own typed line, echoed for transcript
        "thinking_delta",   # partial extended-thinking text from the SDK
        "text_delta",       # partial assistant reply text
        "tool_use",         # one-line "[tool: name args]" trace
        "markdown",         # whole markdown block (LLM / banner replies)
        "plain",            # preformatted plain text (e.g. /help — no markup)
        "finalize",         # turn-finished footer (turns/cost/usage/elapsed)
        "error",            # turn errored — render the message inline
    }
)

STATUS_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "banner",                # one-shot welcome banner at boot
        "channel_ready",         # "[+] Channel registered: cli"
        "channel_lost",          # connectivity blip on a remote channel
        "scheduler",             # cron / heartbeat tick announcement
        "shutdown",              # "powering down" with reflect summary
        "ready",                 # "type and press Enter; /exit to quit"
    }
)


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Single event flowing into the agent pane.

    Field usage by ``kind``:

    * ``user_input``        — ``text``
    * ``thinking_delta``    — ``text``
    * ``text_delta``        — ``text``
    * ``tool_use``          — ``name`` (+ optional ``tool_input`` dict
                               with raw SDK tool-call arguments; the
                               renderer formats per-tool from this)
    * ``markdown``          — ``text`` (full markdown block)
    * ``plain``             — ``text`` (verbatim plain block; no markup)
    * ``finalize``          — ``num_turns``, ``cost_usd``, ``usage``,
                               ``elapsed_s`` (wall seconds for this turn)
    * ``error``             — ``text``

    Unused fields are left at their default. Sinks must NOT rely on
    fields that aren't documented for the given kind — that contract
    keeps producers and consumers from coupling on accidents.
    """

    kind: str
    text: str = ""
    name: str = ""
    num_turns: int = 0
    cost_usd: float | None = None
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    tool_input: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in AGENT_EVENT_KINDS:
            raise ValueError(
                f"AgentEvent.kind={self.kind!r} not in locked whitelist "
                f"{sorted(AGENT_EVENT_KINDS)}"
            )


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """Single event flowing into the status bar / banner area.

    ``fields`` is an open-ended dict for kind-specific structured data
    (e.g. ``{"channel": "cli"}`` for ``channel_ready``). The status
    widget formats them according to the active theme; producers only
    have to fill the dict.
    """

    kind: str
    text: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in STATUS_EVENT_KINDS:
            raise ValueError(
                f"StatusEvent.kind={self.kind!r} not in locked whitelist "
                f"{sorted(STATUS_EVENT_KINDS)}"
            )


# ---------------------------------------------------------------------------
# Sink protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentSink(Protocol):
    """Callable contract for agent-pane events. Must be thread-safe."""

    def __call__(self, event: AgentEvent) -> None:
        ...


@runtime_checkable
class LogSink(Protocol):
    """Callable contract for stdlib log records. Must be thread-safe."""

    def __call__(self, record: logging.LogRecord) -> None:
        ...


@runtime_checkable
class StatusSink(Protocol):
    """Callable contract for status-bar events. Must be thread-safe."""

    def __call__(self, event: StatusEvent) -> None:
        ...


# ---------------------------------------------------------------------------
# Null implementations (for line mode + tests)
# ---------------------------------------------------------------------------


class NullAgentSink:
    """No-op sink. Drops every event. Used in line mode and unit tests."""

    def __call__(self, event: AgentEvent) -> None:  # noqa: D401
        return None


class NullLogSink:
    """No-op log sink. Drops every record. Used in line mode and unit tests."""

    def __call__(self, record: logging.LogRecord) -> None:
        return None


class NullStatusSink:
    """No-op status sink. Drops every event. Used in line mode and unit tests."""

    def __call__(self, event: StatusEvent) -> None:
        return None
