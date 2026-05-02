"""Shared types for the backend abstraction layer.

Every symbol here is backend-agnostic: renderers, channels, and the host
import them without caring which backend produced the data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Protocol

# -- Capability enum ---------------------------------------------------------

class Capability(Enum):
    """Backend capability flags for explicit feature gating."""

    PRE_COMPACT_HOOK = auto()
    SETTING_SOURCES_THREE_TIER = auto()
    PERSISTENT_STREAMING = auto()
    PLUGIN_MARKETPLACE = auto()
    SLASH_PASSTHROUGH = auto()
    INTERACTIVE_MODALS = auto()
    SESSION_RESUME = auto()


# -- Shared dataclasses ------------------------------------------------------

@dataclass
class QueryResult:
    """Return value from a backend ``run_query`` call."""

    text: str | None = None
    session_id: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    num_turns: int = 0


# Type alias kept intentionally loose (``Callable[..., Awaitable]``) so
# every backend can emit its own kwargs without the callback signature
# having to enumerate them all.  The semantic event names are the
# contract; see ``agent_runner.py`` header comment for the canonical set.
StreamEventCallback = Callable[..., Awaitable[None]]


# -- Error hierarchy ---------------------------------------------------------

class BackendError(RuntimeError):
    """Base for all backend errors.

    Inherits ``RuntimeError`` so existing ``except RuntimeError`` catches
    in host code continue to work during the migration.
    """


class StaleSessionError(BackendError):
    """The server-side session is gone or unrecoverable."""


class ModelInvalidError(BackendError):
    """The requested model name is unusable."""


class AuthenticationError(BackendError):
    """API key invalid, expired, or missing."""


class BackendUnavailableError(BackendError):
    """Backend binary not found or failed to start."""


class BackendTimeoutError(BackendError):
    """Backend response timed out."""


# -- Protocol types ----------------------------------------------------------

class StreamingSessionProtocol(Protocol):
    """Minimal contract a streaming session must satisfy."""

    session_key: str
    session_id: str
    last_used_ns: int
    created_ns: int
    turn_count: int

    async def connect(self) -> None: ...

    async def close(self, reason: str = "idle") -> None: ...

    async def run_turn(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        sender_id: str,
        peer_id: str,
        stream_text: bool = True,
        account_id: str = "",
        on_stream_event: StreamEventCallback | None = None,
    ) -> QueryResult: ...


class AgentBackend(Protocol):
    """The single interface between Host and Backend layers."""

    @property
    def name(self) -> str:
        """Backend identifier: ``'claude_code'`` | ``'codex_cli'``."""
        ...

    async def run_query(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        mcp_ctx: Any,
        model_chain: list[str] | None = None,
        session_id: str | None = None,
        system_prompt_append: str = "",
        cwd: str | Path | None = None,
        stream_text: bool = True,
        on_stream_event: StreamEventCallback | None = None,
    ) -> QueryResult: ...

    async def open_streaming_session(
        self,
        *,
        session_key: str,
        mcp_ctx: Any,
        model_chain: list[str],
        cwd: str | Path,
        system_prompt_append: str,
        resume_session_id: str | None = None,
    ) -> StreamingSessionProtocol: ...

    def supports(self, capability: Capability) -> bool: ...

    async def health_check(self) -> tuple[bool, str]: ...
