"""Codex persistent streaming session.

Mirrors ``pip_agent.streaming_session.StreamingSession`` from the Claude
Code backend — one ``Codex()`` client and ``Thread`` survive across
multiple turns from the same sender.

Lifecycle (matches ``StreamingSessionProtocol``):

    session = CodexStreamingSession(key=..., ...)
    await session.connect()          # starts Codex client + thread
    r1 = await session.run_turn(p1)  # first turn
    r2 = await session.run_turn(p2)  # second turn (same thread)
    ...
    await session.close("idle")      # tears down client
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pip_agent.backends.base import (
    BackendError,
    QueryResult,
    StaleSessionError,
    StreamEventCallback,
)

log = logging.getLogger(__name__)


_STALE_MARKERS = (
    "thread not found",
    "session not found",
    "session expired",
    "unknown thread",
)


class CodexStreamingSession:
    """Persistent codex-python session that outlives a single turn."""

    def __init__(
        self,
        *,
        session_key: str,
        cwd: str | Path,
        system_prompt_append: str,
        sandbox: str = "workspace-write",
        resume_session_id: str | None = None,
    ) -> None:
        self.session_key = session_key
        self.session_id: str = resume_session_id or ""
        self.last_used_ns: int = time.monotonic_ns()
        self.created_ns: int = time.monotonic_ns()
        self.turn_count: int = 0

        self._cwd = str(cwd) if cwd else None
        self._sandbox = sandbox
        self._system_prompt_append = system_prompt_append
        self._client: Any = None
        self._thread: Any = None
        self._closed = False

    async def connect(self) -> None:
        """Start the Codex client and open (or resume) a thread."""
        from pip_agent import _profile

        try:
            from codex import Codex, CodexOptions, ThreadStartOptions
            from codex.protocol import types as proto
        except ImportError as exc:
            raise BackendError(
                f"codex-python not installed: {exc}"
            ) from exc

        async with _profile.span("codex_session.connect"):
            api_key = self._resolve_api_key()
            opts = CodexOptions(api_key=api_key) if api_key else None
            self._client = Codex(opts)

            thread_opts = ThreadStartOptions(
                sandbox=proto.SandboxMode(root=self._sandbox),
                approval_policy=proto.AskForApproval(root="never"),
                cwd=self._cwd,
            )

            if self.session_id:
                try:
                    self._thread = self._client.resume_thread(
                        self.session_id,
                        options=thread_opts,
                    )
                except Exception as exc:
                    msg = str(exc).lower()
                    if any(m in msg for m in _STALE_MARKERS):
                        raise StaleSessionError(str(exc)) from exc
                    raise
            else:
                self._thread = self._client.start_thread(thread_opts)
                self.session_id = self._thread.id

            log.info(
                "Codex session connected: key=%s thread=%s",
                self.session_key,
                self.session_id[:12] if self.session_id else "?",
            )

    async def close(self, reason: str = "idle") -> None:
        """Tear down the client."""
        if self._closed:
            return
        self._closed = True
        log.info(
            "Codex session closing: key=%s reason=%s turns=%d",
            self.session_key,
            reason,
            self.turn_count,
        )
        try:
            if self._client:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass

    async def run_turn(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        sender_id: str,
        peer_id: str,
        stream_text: bool = True,
        account_id: str = "",
        on_stream_event: StreamEventCallback | None = None,
    ) -> QueryResult:
        """Execute a single turn on the persistent thread."""
        from pip_agent import _profile
        from pip_agent.backends.codex_cli.event_translator import translate_event

        if self._closed or self._thread is None:
            raise StaleSessionError("Session is closed or not connected")

        self.last_used_ns = time.monotonic_ns()
        self.turn_count += 1

        prompt_text = (
            prompt
            if isinstance(prompt, str)
            else _blocks_to_text(prompt)
        )

        result = QueryResult()
        state: dict[str, Any] = {}

        try:
            async with _profile.span("codex_session.run_turn"):
                start_ns = time.perf_counter_ns()

                stream = self._thread.run(prompt_text)

                for event in stream:
                    if on_stream_event is not None:
                        await translate_event(
                            event, on_stream_event, state=state,
                        )

                    etype = type(event).__name__
                    if etype == "ItemCompletedNotificationModel":
                        item = event.params.item.root
                        item_type = getattr(item, "type", None)
                        if hasattr(item_type, "root"):
                            item_type = item_type.root
                        if item_type == "agent_message":
                            result.text = getattr(item, "text", "") or ""

                if result.text is None:
                    result.text = state.get("final_text", "")

                elapsed_s = (time.perf_counter_ns() - start_ns) / 1e9
                state["elapsed_s"] = elapsed_s
                result.session_id = self.session_id
                result.num_turns = self.turn_count

                log.info(
                    "Codex turn done: key=%s turn=%d len=%d elapsed=%.1fs",
                    self.session_key,
                    self.turn_count,
                    len(result.text or ""),
                    elapsed_s,
                )

        except Exception as exc:
            msg = str(exc).lower()
            if any(m in msg for m in _STALE_MARKERS):
                raise StaleSessionError(str(exc)) from exc
            log.exception("Codex turn failed: %s", exc)
            result.error = f"{type(exc).__name__}: {exc}"

        return result

    @staticmethod
    def _resolve_api_key() -> str | None:
        import os
        return (
            os.environ.get("CODEX_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or None
        )


def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten content blocks to plain text for Codex."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts) if parts else str(blocks)
