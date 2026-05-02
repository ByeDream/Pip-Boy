"""Codex CLI backend — stub for ``codex-python`` SDK integration.

Phase 2+ will flesh out this module.  For now it exists so
``get_backend("codex_cli")`` returns a well-typed object that
raises ``NotImplementedError`` on every operation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pip_agent.backends.base import (
    Capability,
    QueryResult,
    StreamEventCallback,
    StreamingSessionProtocol,
)


class CodexBackend:
    """``AgentBackend`` stub for Codex CLI (codex-python SDK)."""

    @property
    def name(self) -> str:
        return "codex_cli"

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
    ) -> QueryResult:
        raise NotImplementedError("Codex backend not yet implemented (Phase 2)")

    async def open_streaming_session(
        self,
        *,
        session_key: str,
        mcp_ctx: Any,
        model_chain: list[str],
        cwd: str | Path,
        system_prompt_append: str,
        resume_session_id: str | None = None,
    ) -> StreamingSessionProtocol:
        raise NotImplementedError("Codex backend not yet implemented (Phase 3)")

    def supports(self, capability: Capability) -> bool:
        return capability in _SUPPORTED

    async def health_check(self) -> tuple[bool, str]:
        try:
            import codex  # noqa: F401
            return True, "codex-python SDK available"
        except ImportError:
            return False, "codex-python not installed (pip install codex-python)"


_SUPPORTED: frozenset[Capability] = frozenset({
    Capability.PERSISTENT_STREAMING,
    Capability.PLUGIN_MARKETPLACE,
    Capability.SLASH_PASSTHROUGH,
    Capability.SESSION_RESUME,
})
