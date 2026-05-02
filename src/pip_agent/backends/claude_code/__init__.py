"""Claude Code backend — wraps ``claude_agent_sdk`` for Pip-Boy.

This is the original (and default) backend.  The implementation lives
in ``pip_agent.agent_runner`` and ``pip_agent.streaming_session`` (the
pre-existing modules).  This class provides the ``AgentBackend``
protocol interface that the host layer can program against
backend-agnostically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pip_agent.backends.base import (
    Capability,
    QueryResult,
    StreamEventCallback,
    StreamingSessionProtocol,
)

log = logging.getLogger(__name__)


class ClaudeCodeBackend:
    """``AgentBackend`` implementation for Claude Code (claude_agent_sdk)."""

    @property
    def name(self) -> str:
        return "claude_code"

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
        from pip_agent.agent_runner import run_query as _run_query

        return await _run_query(
            prompt,
            mcp_ctx=mcp_ctx,
            model_chain=model_chain,
            session_id=session_id,
            system_prompt_append=system_prompt_append,
            cwd=cwd,
            stream_text=stream_text,
            on_stream_event=on_stream_event,
        )

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
        from pip_agent.streaming_session import StreamingSession

        session = StreamingSession(
            session_key=session_key,
            mcp_ctx=mcp_ctx,
            model_chain=model_chain,
            cwd=cwd,
            system_prompt_append=system_prompt_append,
            resume_session_id=resume_session_id,
        )
        await session.connect()
        return session

    def supports(self, capability: Capability) -> bool:
        return capability in _SUPPORTED

    async def health_check(self) -> tuple[bool, str]:
        try:
            import claude_agent_sdk  # noqa: F401

            return True, "claude_agent_sdk available"
        except Exception as exc:  # noqa: BLE001
            return False, f"claude_agent_sdk unavailable: {exc}"


_SUPPORTED: frozenset[Capability] = frozenset({
    Capability.PRE_COMPACT_HOOK,
    Capability.SETTING_SOURCES_THREE_TIER,
    Capability.PERSISTENT_STREAMING,
    Capability.PLUGIN_MARKETPLACE,
    Capability.SLASH_PASSTHROUGH,
    Capability.INTERACTIVE_MODALS,
    Capability.SESSION_RESUME,
})
