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

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, TypeVar

from pip_agent.backends.base import (
    BackendError,
    QueryResult,
    StaleSessionError,
    StreamEventCallback,
)

_T = TypeVar("_T")

_SENTINEL = object()


def _next_or_sentinel(it: Iterator[_T]) -> _T | object:
    """Call ``next(it)`` and return ``_SENTINEL`` on exhaustion.

    ``StopIteration`` cannot propagate out of ``run_in_executor``
    (Python 3.13+ raises ``RuntimeError``), so we convert it to a
    sentinel value here, inside the worker thread.
    """
    try:
        return next(it)
    except StopIteration:
        return _SENTINEL


async def _async_iter(sync_iter: Iterator[_T]) -> AsyncIterator[_T]:
    """Wrap a blocking sync iterator so each ``__next__`` runs in a thread.

    The Codex SDK's ``CodexTurnStream`` is a synchronous iterator whose
    ``__next__`` blocks on network I/O (waiting for the next JSON-RPC
    event from the app-server).  Running it directly in an ``async``
    function starves the asyncio event loop — TUI updates, WebSocket
    keepalives, and all other coroutines are frozen until the entire
    turn finishes.

    This wrapper offloads each ``__next__`` call to the default
    ``ThreadPoolExecutor``, yielding control back to the event loop
    between events.
    """
    loop = asyncio.get_running_loop()
    it = iter(sync_iter)
    while True:
        value = await loop.run_in_executor(None, _next_or_sentinel, it)
        if value is _SENTINEL:
            return
        yield value  # type: ignore[misc]

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
        sandbox: str = "danger-full-access",
        resume_session_id: str | None = None,
        sender_id: str = "",
        peer_id: str = "",
        user_id: str = "",
        account_id: str = "",
        channel_name: str = "",
        agent_id: str = "",
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
        self._user_id = user_id
        self._account_id = account_id
        self._channel_name = channel_name
        self._agent_id = agent_id
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
            from codex import Codex, CodexOptions, ThreadResumeOptions, ThreadStartOptions
            from codex.protocol import types as proto
        except ImportError as exc:
            raise BackendError(
                f"codex-python not installed: {exc}"
            ) from exc

        async with _profile.span("codex_session.connect"):
            api_key, base_url = self._resolve_credentials()
            codex_env = self._build_bridge_env()
            opts_kwargs: dict[str, Any] = {}
            if api_key:
                opts_kwargs["api_key"] = api_key
            if base_url:
                opts_kwargs["base_url"] = base_url
            if codex_env:
                opts_kwargs["env"] = codex_env

            from pip_agent.backends.codex_cli.bridge_env import build_codex_config_override

            config_override = build_codex_config_override(base_url, api_key)
            if config_override is not None:
                opts_kwargs["config"] = config_override

            self._client = Codex(
                CodexOptions(**opts_kwargs) if opts_kwargs else None,
            )
            from pip_agent.backends.codex_cli.turn_options import (
                ensure_experimental_api,
            )

            ensure_experimental_api(self._client)

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
                    resume_opts = ThreadResumeOptions(
                        sandbox=proto.SandboxMode(root=self._sandbox),
                        approval_policy=proto.AskForApproval(root="never"),
                        cwd=self._cwd,
                        model=self._model,
                        developer_instructions=(
                            self._system_prompt_append or None
                        ),
                    )
                    self._thread = self._client.resume_thread(
                        self.session_id,
                        options=resume_opts,
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
                "Codex session connected: key=%s thread=%s cwd=%s transcript=%s",
                self.session_key,
                self.session_id[:12] if self.session_id else "?",
                self._cwd,
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

    def _write_bridge_ctx(
        self,
        sender_id: str,
        peer_id: str,
        account_id: str = "",
    ) -> None:
        """Write per-turn identity to a file the MCP bridge can read.

        The bridge process is long-lived and cannot receive per-session
        env vars after its initial spawn.  This file is the rendezvous
        point for identity context that changes with each conversation.
        """
        from pip_agent.config import WORKDIR

        ctx_path = WORKDIR / ".pip" / "codex_bridge_ctx.json"
        try:
            ctx_path.parent.mkdir(parents=True, exist_ok=True)
            ctx_path.write_text(
                json.dumps({
                    "sender_id": sender_id,
                    "peer_id": peer_id,
                    "user_id": self._user_id,
                    "session_id": self._bridge_session_id,
                    "account_id": account_id,
                    "channel_name": self._channel_name,
                    "agent_id": self._agent_id,
                }),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            log.debug("Failed to write bridge context file", exc_info=True)

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

        self._write_bridge_ctx(sender_id, peer_id, account_id)
        self.last_used_ns = time.monotonic_ns()
        self.turn_count += 1

        prompt_text = (
            prompt
            if isinstance(prompt, str)
            else _blocks_to_text(prompt)
        )
        self._append_transcript("user", prompt_text)

        result = QueryResult()
        state: dict[str, Any] = {"start_ns": time.perf_counter_ns()}

        try:
            async with _profile.span("codex_session.run_turn"):
                start_ns = time.perf_counter_ns()

                from pip_agent.backends.codex_cli.turn_options import (
                    build_turn_options,
                )

                effort_val = self._resolve_reasoning_effort()
                turn_options = build_turn_options(
                    model=self._model,
                    developer_instructions=self._system_prompt_append,
                    effort=effort_val,
                )
                stream = self._thread.run(
                    prompt_text,
                    turn_options,
                )

                async for event in _async_iter(stream):
                    await translate_event(
                        event, on_stream_event, state=state,
                    )

                result.text = state.get("accumulated_text", "") or state.get("final_text", "")

                elapsed_s = (time.perf_counter_ns() - start_ns) / 1e9
                state["elapsed_s"] = elapsed_s
                result.session_id = self.session_id
                result.num_turns = self.turn_count

                if result.text:
                    self._append_transcript("assistant", result.text)

                token_usage = state.get("token_usage", {})
                token_usage["tool_calls"] = state.get("tool_calls", 0)
                if token_usage:
                    self.cumulative_tokens = (
                        token_usage.get("total_tokens", 0)
                    )

                from pip_agent.backends.codex_cli.event_translator import estimate_cost_usd
                cost = estimate_cost_usd(self._model, token_usage)
                result.cost_usd = cost

                if on_stream_event is not None:
                    await on_stream_event(
                        "finalize",
                        final_text=result.text or "",
                        num_turns=self.turn_count,
                        cost_usd=cost,
                        usage=token_usage,
                        elapsed_s=elapsed_s,
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
            user_id=self._user_id,
            account_id=self._account_id,
        )

    @staticmethod
    def _resolve_credentials() -> tuple[str | None, str | None]:
        from pip_agent.backends.codex_cli.bridge_env import resolve_codex_credentials
        return resolve_codex_credentials()

    @staticmethod
    def _resolve_reasoning_effort() -> Any:
        """Read ``codex_reasoning_effort`` from settings and wrap it."""
        from codex.protocol import types as proto

        from pip_agent.config import settings

        _VALID = {"none", "minimal", "low", "medium", "high", "xhigh"}
        raw = (settings.codex_reasoning_effort or "").strip().lower()
        if raw and raw in _VALID:
            return proto.ReasoningEffort(root=raw)  # type: ignore[arg-type]
        return None


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
