"""Backend abstraction layer for Pip-Boy.

Public API:
    Capability, AgentBackend, StreamingSessionProtocol  — protocol types
    QueryResult, StreamEventCallback                    — shared data types
    BackendError and subclasses                         — unified error hierarchy
    get_backend()                                       — resolve the active backend
"""

from pip_agent.backends.base import (
    AgentBackend,
    AuthenticationError,
    BackendError,
    BackendTimeoutError,
    BackendUnavailableError,
    Capability,
    ModelInvalidError,
    QueryResult,
    StaleSessionError,
    StreamEventCallback,
    StreamingSessionProtocol,
)

__all__ = [
    "AgentBackend",
    "AuthenticationError",
    "BackendError",
    "BackendTimeoutError",
    "BackendUnavailableError",
    "Capability",
    "ModelInvalidError",
    "QueryResult",
    "StaleSessionError",
    "StreamEventCallback",
    "StreamingSessionProtocol",
    "get_backend",
]


def get_backend(name: str | None = None) -> AgentBackend:
    """Resolve and return the configured backend instance.

    ``name`` overrides auto-detection from settings; pass ``None``
    (the default) to read ``settings.backend``.
    """
    if name is None:
        try:
            from pip_agent.config import settings
            name = getattr(settings, "backend", "claude_code") or "claude_code"
        except Exception:  # noqa: BLE001
            name = "claude_code"

    if name == "claude_code":
        from pip_agent.backends.claude_code import ClaudeCodeBackend
        return ClaudeCodeBackend()

    if name == "codex_cli":
        from pip_agent.backends.codex_cli import CodexBackend
        return CodexBackend()

    raise ValueError(f"Unknown backend: {name!r}. Valid: 'claude_code', 'codex_cli'.")
