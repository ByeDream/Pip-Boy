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

Transcript capture
------------------
Each turn appends user + assistant lines to a JSONL file alongside the
session_id so the reflect pipeline (``memory/transcript_source.py``) can
read them with the same ``iter_transcript`` + ``normalize_line`` logic
it uses for Claude Code sessions.  Lines use the flat
``{"role": "...", "content": "..."}`` shape that ``normalize_line``
already handles (Shape 2).
"""

from __future__ import annotations

import json
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
        model: str | None = None,
        sandbox: str = "workspace-write",
        resume_session_id: str | None = None,
        sender_id: str = "",
        peer_id: str = "",
        account_id: str = "",
    ) -> None:
        self.session_key = session_key
        self.session_id: str = resume_session_id or ""
        self._bridge_session_id: str = self._resolve_bridge_session_id(
            resume_session_id,
        )
        self.last_used_ns: int = time.monotonic_ns()
        self.created_ns: int = time.monotonic_ns()
        self.turn_count: int = 0
        self.cumulative_tokens: int = 0

        self._cwd = str(cwd) if cwd else None
        self._sandbox = sandbox
        self._system_prompt_append = system_prompt_append
        self._model = model
        self._sender_id = sender_id
        self._peer_id = peer_id
        self._account_id = account_id
        self._client: Any = None
        self._thread: Any = None
        self._closed = False
        self._transcript_path: Path | None = None

    @property
    def reflect_session_id(self) -> str:
        """Session id used by transcript lookup and reflect cursors."""
        return self._bridge_session_id

    @classmethod
    def _resolve_bridge_session_id(
        cls, resume_session_id: str | None,
    ) -> str:
        """Return the stable session id visible to MCP reflect tools."""
        if resume_session_id:
            return (
                cls._load_bridge_session_id(resume_session_id)
                or resume_session_id
            )

        import uuid

        return uuid.uuid4().hex

    @staticmethod
    def _codex_sessions_dir() -> Path:
        from pip_agent.config import WORKDIR

        return WORKDIR / ".pip" / "codex_sessions"

    @classmethod
    def _load_bridge_session_id(cls, session_id: str) -> str:
        """Load the reflect id previously assigned to a Codex thread."""
        try:
            alias_path = cls._codex_sessions_dir() / f"{session_id}.bridge"
            if not alias_path.is_file():
                return ""
            bridge_id = alias_path.read_text(encoding="utf-8").strip()
            if not bridge_id or Path(bridge_id).name != bridge_id:
                return ""
            return bridge_id
        except Exception:  # noqa: BLE001
            log.debug("Failed to load Codex bridge session alias", exc_info=True)
            return ""

    def _write_bridge_session_alias(self) -> None:
        """Persist Codex thread id to reflect id mapping for resume."""
        if (
            not self.session_id
            or not self.reflect_session_id
            or self.session_id == self.reflect_session_id
        ):
            return
        try:
            sessions_dir = self._codex_sessions_dir()
            sessions_dir.mkdir(parents=True, exist_ok=True)
            alias_path = sessions_dir / f"{self.session_id}.bridge"
            alias_path.write_text(self.reflect_session_id, encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.debug("Failed to write Codex bridge session alias", exc_info=True)

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
            codex_env = self._build_bridge_env()
            opts_kwargs: dict[str, Any] = {}
            if api_key:
                opts_kwargs["api_key"] = api_key
            if codex_env:
                opts_kwargs["env"] = codex_env
            self._client = Codex(
                CodexOptions(**opts_kwargs) if opts_kwargs else None,
            )

            thread_opts = ThreadStartOptions(
                sandbox=proto.SandboxMode(root=self._sandbox),
                approval_policy=proto.AskForApproval(root="never"),
                cwd=self._cwd,
                model=self._model,
                developer_instructions=(
                    self._system_prompt_append or None
                ),
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

            self._write_bridge_session_alias()
            self._transcript_path = self._init_transcript_path()
            log.info(
                "Codex session connected: key=%s thread=%s transcript=%s",
                self.session_key,
                self.session_id[:12] if self.session_id else "?",
                self._transcript_path,
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
        self._append_transcript("user", prompt_text)

        result = QueryResult()
        state: dict[str, Any] = {}

        try:
            async with _profile.span("codex_session.run_turn"):
                start_ns = time.perf_counter_ns()

                stream = self._thread.run(prompt_text)

                for event in stream:
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

                if result.text:
                    self._append_transcript("assistant", result.text)

                token_usage = state.get("token_usage", {})
                if token_usage:
                    self.cumulative_tokens = (
                        token_usage.get("total_tokens", 0)
                    )

                log.info(
                    "Codex turn done: key=%s turn=%d len=%d elapsed=%.1fs tokens=%d",
                    self.session_key,
                    self.turn_count,
                    len(result.text or ""),
                    elapsed_s,
                    self.cumulative_tokens,
                )

        except Exception as exc:
            msg = str(exc).lower()
            if any(m in msg for m in _STALE_MARKERS):
                raise StaleSessionError(str(exc)) from exc
            log.exception("Codex turn failed: %s", exc)
            result.error = f"{type(exc).__name__}: {exc}"

        return result

    @property
    def transcript_path(self) -> Path | None:
        """Path to the JSONL transcript file, or None if not available."""
        return self._transcript_path

    def _init_transcript_path(self) -> Path | None:
        """Create the JSONL transcript file for this session.

        Stored in the same ``~/.claude/projects/`` tree that
        ``transcript_source.locate_session_jsonl`` scans, so the
        existing reflect pipeline finds them without changes.
        """
        if not self.reflect_session_id:
            return None
        try:
            sessions_dir = self._codex_sessions_dir()
            sessions_dir.mkdir(parents=True, exist_ok=True)
            return sessions_dir / f"{self.reflect_session_id}.jsonl"
        except Exception:  # noqa: BLE001
            log.debug("Failed to init transcript path", exc_info=True)
            return None

    def _append_transcript(self, role: str, text: str) -> None:
        """Append one JSONL line in the flat shape transcript_source expects."""
        if not self._transcript_path or not text.strip():
            return
        try:
            line = json.dumps(
                {"role": role, "content": text},
                ensure_ascii=False,
            )
            with self._transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            log.debug("transcript append failed", exc_info=True)

    def _build_bridge_env(self) -> dict[str, str]:
        """Build env dict passed to ``CodexOptions(env=...)``.

        Uses ``_bridge_session_id`` (pre-generated in ``__init__``) so
        ``PIP_SESSION_ID`` is always available — even for brand-new
        sessions where the SDK thread ID is not yet assigned.
        """
        from pip_agent.backends.codex_cli.bridge_env import build_bridge_env

        return build_bridge_env(
            session_id=self._bridge_session_id,
            sender_id=self._sender_id,
            peer_id=self._peer_id,
            account_id=self._account_id,
        )

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
