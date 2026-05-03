"""Codex CLI backend — ``codex-python`` SDK integration for Pip-Boy.

Implements the ``AgentBackend`` protocol via the Codex persistent-connection
model:

    Codex() → start_thread() → thread.run() → (stream events) → close()

Event translation is delegated to ``event_translator.translate_event``
which maps SDK JSON-RPC notifications into the 5 Pip-Boy semantic events.
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


class CodexBackend:
    """``AgentBackend`` implementation for Codex CLI (codex-python SDK)."""

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
        from pip_agent.backends.codex_cli.runner import run_query as _run

        return await _run(
            prompt,
            mcp_ctx=mcp_ctx,
            model_chain=model_chain,
            session_id=session_id,
            system_prompt_append=system_prompt_append,
            cwd=cwd,
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
        from pip_agent.backends.codex_cli.streaming import CodexStreamingSession

        session = CodexStreamingSession(
            session_key=session_key,
            cwd=cwd,
            system_prompt_append=system_prompt_append,
            model=model_chain[0] if model_chain else None,
            resume_session_id=resume_session_id,
            sender_id=getattr(mcp_ctx, "sender_id", "") or "",
            peer_id=getattr(mcp_ctx, "peer_id", "") or "",
            user_id=getattr(mcp_ctx, "user_id", "") or "",
            account_id=getattr(mcp_ctx, "account_id", "") or "",
            channel_name=getattr(mcp_ctx, "effective_channel_name", "") or "",
        )
        await session.connect()
        return session  # type: ignore[return-value]

    def supports(self, capability: Capability) -> bool:
        return capability in _SUPPORTED

    async def health_check(self) -> tuple[bool, str]:
        try:
            import codex  # noqa: F401

            return True, f"codex-python SDK {codex.__version__} available"
        except ImportError:
            return False, "codex-python not installed (pip install codex-python)"


_SUPPORTED: frozenset[Capability] = frozenset({
    Capability.PERSISTENT_STREAMING,
    Capability.PLUGIN_MARKETPLACE,
    Capability.SLASH_PASSTHROUGH,
    Capability.SESSION_RESUME,
})
