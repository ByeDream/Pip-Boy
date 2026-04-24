"""Streaming-session cache for the Claude Agent SDK subprocess.

Tier 1 of the perf plan (see
``pip-boy_perf_optimization_plan_f63af13d.plan.md``). Wraps a single
``ClaudeSDKClient`` per session key so subsequent turns on the same
session avoid the ~400 ms ``claude.exe`` spawn + control-protocol
handshake tax observed in ``perf-report-new.md``.

Design summary
--------------

* One ``StreamingSession`` per ``session_key``. The session key is the
  same one used by :func:`pip_agent.routing.build_session_key` and by
  the existing ``_session_locks`` map, so per-session serialisation
  carries through without extra coordination.
* Options are frozen at ``connect()`` time. ``system_prompt_append``
  includes memory enrichment, which DOES drift across turns — we
  accept that drift for the life of the cached client and let the
  idle TTL eviction pick up fresh enrichment at the next cold turn.
  McpContext identity fields (``session_id``, ``sender_id``,
  ``peer_id``) ARE mutated per-turn before dispatch, because MCP tool
  handlers read them lazily via attribute access.
* Ephemeral senders (cron / heartbeat) never touch the streaming
  cache. See ``AgentHost._is_ephemeral_sender`` and the dispatch branch
  in ``agent_host._execute_turn``.
* Stale-server-session recovery: if the CC CLI rejects the in-flight
  turn with a "No conversation found" / similar message, we surface
  ``StaleSessionError`` so AgentHost can drop the cached client, wipe
  the persisted ``session_id``, and retry once with a fresh client.
  See ``tier4.2`` in the plan (borrowed from NanoClaw issue #1216).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pip_agent.agent_runner import QueryResult, _build_env
from pip_agent.hooks import build_hooks
from pip_agent.mcp_tools import McpContext, build_mcp_server

log = logging.getLogger(__name__)


# Substrings in an error message that indicate the server-side session
# is gone (the CC control plane lost track of the session id we used to
# resume). Borrowed from NanoClaw issue #1216 and the empirical strings
# emitted by ``claude.exe`` when the user's transcript has been pruned
# or the gateway rotated the session store.
_STALE_SESSION_MARKERS = (
    "no conversation found",
    "session not found",
    "session expired",
    "unknown session",
    "session_id is invalid",
)


class StaleSessionError(RuntimeError):
    """Raised by :meth:`StreamingSession.run_turn` when the CC server lost
    the session id we resumed against. AgentHost handles recovery.
    """


class StreamingSession:
    """One persistent ``ClaudeSDKClient`` wrapper, keyed by session_key.

    Lifecycle:

    1. ``create()`` builds options + MCP server + ClaudeSDKClient and
       calls ``client.connect()``. This pays the ~400 ms subprocess
       spawn + handshake once.
    2. ``run_turn()`` writes the prompt into the already-connected
       client, drains the response via ``receive_response()``, updates
       ``session_id`` from the ``ResultMessage``, bumps ``last_used_ns``.
    3. ``close()`` calls ``client.disconnect()``. Called by the idle
       sweep, by AgentHost on shutdown, and on stale-session recovery.
    """

    def __init__(
        self,
        *,
        session_key: str,
        mcp_ctx: McpContext,
        model: str,
        cwd: Path | str,
        system_prompt_append: str,
        resume_session_id: str | None = None,
    ) -> None:
        self.session_key = session_key
        self._mcp_ctx = mcp_ctx
        self._model = model
        self._cwd = str(cwd)
        self._system_prompt_append = system_prompt_append
        self._resume_session_id = resume_session_id

        self._client: Any = None  # ClaudeSDKClient, imported lazily
        self._connected = False
        self._turn_lock = asyncio.Lock()
        # Invariant: session_id is "" until the first ResultMessage lands.
        self.session_id: str = ""
        self.last_used_ns: int = time.perf_counter_ns()
        self.created_ns: int = time.perf_counter_ns()
        self.turn_count: int = 0
        self._closed = False

    async def connect(self) -> None:
        """Spawn the CC subprocess and run the control-protocol handshake."""
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        from pip_agent import _profile

        mcp_server = build_mcp_server(self._mcp_ctx)
        hooks = build_hooks(memory_store=self._mcp_ctx.memory_store)

        options = ClaudeAgentOptions(
            model=self._model or None,
            cwd=self._cwd,
            resume=self._resume_session_id,
            system_prompt=(
                {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": self._system_prompt_append,
                }
                if self._system_prompt_append
                else None
            ),
            permission_mode="bypassPermissions",
            setting_sources=["project", "user"],
            env=_build_env(),
            mcp_servers={"pip": mcp_server},
            hooks=hooks,
        )

        self._client = ClaudeSDKClient(options=options)
        async with _profile.span(
            "stream.connect",
            session_key=self.session_key,
            resume=bool(self._resume_session_id),
        ):
            # ``connect(None)`` opens with an empty user-message stream,
            # so the subprocess is live and waiting for our first
            # ``client.query(...)`` call. This is the call that pays the
            # one-time 400 ms tax the old per-turn path was eating every
            # turn.
            await self._client.connect()
        self._connected = True
        _profile.event(
            "stream.opened",
            session_key=self.session_key,
            resume=bool(self._resume_session_id),
        )

    async def close(self, reason: str = "idle") -> None:
        """Disconnect the underlying client, idempotent.

        ``reason`` is purely observational — appears in ``stream.closed``
        events so the perf log can distinguish idle eviction vs crash vs
        shutdown. Never raises; a dying subprocess on shutdown must not
        mask the real exit path.
        """
        if self._closed:
            return
        self._closed = True
        from pip_agent import _profile

        _profile.event(
            "stream.closed",
            session_key=self.session_key,
            reason=reason,
            turns=self.turn_count,
            age_ms=round((time.perf_counter_ns() - self.created_ns) / 1e6, 1),
        )
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "stream %s: disconnect raised during %s close: %s",
                self.session_key, reason, exc,
            )
        finally:
            self._client = None
            self._connected = False

    def _looks_stale(self, err_text: str) -> bool:
        """Pattern match for the "server lost our session id" family."""
        low = (err_text or "").lower()
        return any(marker in low for marker in _STALE_SESSION_MARKERS)

    async def run_turn(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        sender_id: str,
        peer_id: str,
        stream_text: bool = True,
        account_id: str = "",
    ) -> QueryResult:
        """Send one user turn into the live subprocess and drain the response.

        Serialises same-session calls via a per-session lock — the
        AgentHost per-session lock would also serialise, but keeping a
        local one here means the StreamingSession is safe to use from
        anywhere (not only the AgentHost path).
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKError,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolUseBlock,
        )

        from pip_agent import _profile

        if self._closed or not self._connected or self._client is None:
            raise RuntimeError(
                f"StreamingSession({self.session_key}) not connected",
            )

        async with self._turn_lock:
            self.turn_count += 1
            turn_idx = self.turn_count
            # Mutate the shared McpContext so MCP tool closures see
            # this turn's identity. Reads are lazy (attribute access at
            # call time), so mutation is observed without any client
            # rebuild.
            self._mcp_ctx.sender_id = sender_id
            self._mcp_ctx.peer_id = peer_id
            self._mcp_ctx.session_id = self.session_id
            self._mcp_ctx.account_id = account_id

            _profile.event(
                "stream.user_pushed",
                session_key=self.session_key,
                turn=turn_idx,
                prompt_kind="str" if isinstance(prompt, str) else "blocks",
            )

            result = QueryResult()
            streaming_line_open = False

            # ClaudeSDKClient.query accepts str (simple) or AsyncIterable
            # (for content-block prompts). Mirror agent_runner's branch.
            if isinstance(prompt, str):
                send_prompt: Any = prompt
            else:
                send_prompt = _single_user_blocks_stream(prompt)

            stream_start_ns = time.perf_counter_ns()
            first_text_seen = False
            tool_count = 0

            async with _profile.span(
                "stream.turn",
                session_key=self.session_key,
                turn=turn_idx,
                prompt_kind="str" if isinstance(prompt, str) else "blocks",
            ):
                try:
                    await self._client.query(send_prompt)

                    async for message in self._client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock) and stream_text:
                                    if not first_text_seen:
                                        _profile.event(
                                            "stream.first_text",
                                            session_key=self.session_key,
                                            turn=turn_idx,
                                            since_stream_ms=round(
                                                (time.perf_counter_ns() - stream_start_ns) / 1e6, 3,
                                            ),
                                            text_len=len(block.text),
                                        )
                                        first_text_seen = True
                                    print(block.text, end="", flush=True)
                                    streaming_line_open = True
                                elif isinstance(block, ToolUseBlock):
                                    tool_count += 1
                                    _profile.event(
                                        "stream.tool_use",
                                        session_key=self.session_key,
                                        turn=turn_idx,
                                        name=block.name,
                                        since_stream_ms=round(
                                            (time.perf_counter_ns() - stream_start_ns) / 1e6, 3,
                                        ),
                                    )
                                    args_preview = str(block.input)[:80]
                                    print(
                                        f"\n  [tool: {block.name} {args_preview}]",
                                        flush=True,
                                    )
                                    streaming_line_open = True

                        elif isinstance(message, SystemMessage):
                            if message.subtype == "init":
                                sid = message.data.get("session_id")
                                # A fresh sid on turn >=2 would indicate a
                                # subprocess respawn we didn't expect —
                                # log it so the regression is visible.
                                if self.session_id and sid and sid != self.session_id:
                                    log.warning(
                                        "stream %s: SystemMessage(init) reports sid %s != cached %s",
                                        self.session_key, sid, self.session_id,
                                    )
                                if sid:
                                    self.session_id = sid
                                _profile.event(
                                    "stream.session_init",
                                    session_key=self.session_key,
                                    turn=turn_idx,
                                    sid=sid,
                                    since_stream_ms=round(
                                        (time.perf_counter_ns() - stream_start_ns) / 1e6, 3,
                                    ),
                                )
                        elif isinstance(message, ResultMessage):
                            result.text = message.result
                            result.session_id = message.session_id
                            result.cost_usd = message.total_cost_usd
                            result.num_turns = message.num_turns
                            if message.session_id:
                                # Canonical CC session id — keep it cached
                                # so downstream AgentHost can persist it and
                                # so MCP tools read the right value on the
                                # next turn.
                                self.session_id = message.session_id
                                self._mcp_ctx.session_id = message.session_id
                            if message.is_error:
                                result.error = message.result
                            if streaming_line_open:
                                print(flush=True)
                                streaming_line_open = False
                            usage = message.usage or {}
                            _profile.event(
                                "stream.result",
                                session_key=self.session_key,
                                turn=turn_idx,
                                turns=message.num_turns,
                                cost_usd=message.total_cost_usd or 0,
                                stop=message.stop_reason,
                                err=message.is_error,
                                tool_calls=tool_count,
                                reply_len=len(message.result or ""),
                                input_tokens=int(usage.get("input_tokens") or 0),
                                output_tokens=int(usage.get("output_tokens") or 0),
                                cache_read=int(usage.get("cache_read_input_tokens") or 0),
                                cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
                            )
                except ClaudeSDKError as exc:
                    if streaming_line_open:
                        print(flush=True)
                    err_text = str(exc)
                    result.error = err_text
                    _profile.event(
                        "stream.sdk_error",
                        session_key=self.session_key,
                        turn=turn_idx,
                        err=err_text[:200],
                    )
                    log.error(
                        "stream %s: SDK error on turn %d: %s",
                        self.session_key, turn_idx, exc,
                    )
                    # Bubble up as stale so AgentHost can rebuild. If it
                    # doesn't look stale we surface as a regular error via
                    # the QueryResult (caller will handle).
                    if self._looks_stale(err_text):
                        raise StaleSessionError(err_text) from exc
                except Exception as exc:  # noqa: BLE001
                    if streaming_line_open:
                        print(flush=True)
                    err_text = str(exc)
                    result.error = err_text
                    _profile.event(
                        "stream.unhandled_error",
                        session_key=self.session_key,
                        turn=turn_idx,
                        err=err_text[:200],
                        err_type=type(exc).__name__,
                    )
                    log.exception(
                        "stream %s: unhandled error on turn %d",
                        self.session_key, turn_idx,
                    )
                    if self._looks_stale(err_text):
                        raise StaleSessionError(err_text) from exc
                finally:
                    self.last_used_ns = time.perf_counter_ns()

            # Soft-error stale detection: some CC builds return the stale
            # marker as a ResultMessage with ``is_error=True`` instead of
            # raising. Translate that into StaleSessionError too so the
            # caller can recover uniformly.
            if result.error and self._looks_stale(result.error):
                raise StaleSessionError(result.error)

            return result


async def _single_user_blocks_stream(
    content: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield exactly one SDK-shaped ``user`` envelope for multimodal input.

    Mirrors ``agent_runner._stream_single_user_message`` but is local
    here to keep streaming_session self-contained. Kept in sync with the
    CC control protocol: ``session_id`` field is empty because actual
    resume is driven by ``options.resume`` at connect() time.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
