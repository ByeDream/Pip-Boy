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

from pip_agent.agent_runner import (
    QueryResult,
    StreamEventCallback,
    _build_env,
    _builtin_disallowed_tools,
    _emit_stream_event_deltas,
    _enrich_with_stderr,
    _StderrBuffer,
)
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
        model_chain: list[str],
        cwd: Path | str,
        system_prompt_append: str,
        resume_session_id: str | None = None,
    ) -> None:
        self.session_key = session_key
        self._mcp_ctx = mcp_ctx
        # Ordered tier candidates. ``connect()`` walks them in order and
        # pins ``self._model`` to the first that connects. Empty list
        # means "let CC pick its default".
        self._model_chain: list[str] = list(model_chain) if model_chain else [""]
        self._model: str = ""
        self._cwd = str(cwd)
        self._system_prompt_append = system_prompt_append
        self._resume_session_id = resume_session_id

        self._client: Any = None  # ClaudeSDKClient, imported lazily
        self._connected = False
        self._turn_lock = asyncio.Lock()
        # Bound stderr capture for the current subprocess. Re-allocated
        # by every ``connect()`` (each call spawns a fresh claude.exe)
        # and reset at the start of every turn so a turn's failure
        # carries only that turn's stderr context.
        self._stderr_buf: _StderrBuffer | None = None
        # Invariant: session_id is "" until the first ResultMessage lands.
        self.session_id: str = ""
        self.last_used_ns: int = time.perf_counter_ns()
        self.created_ns: int = time.perf_counter_ns()
        self.turn_count: int = 0
        self._closed = False

    async def connect(self) -> None:
        """Spawn the CC subprocess and run the control-protocol handshake.

        Walks ``model_chain`` in order: a model-invalid error on connect
        (e.g. the chain head returns ``404 model_not_found`` from the
        proxy) closes the doomed client and retries with the next
        candidate. Anything else (auth, network, payload) re-raises
        immediately because no model substitution would help.
        """
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        from pip_agent import _profile
        from pip_agent.models import is_model_invalid_error

        last_exc: BaseException | None = None
        for idx, candidate in enumerate(self._model_chain):
            mcp_server = build_mcp_server(self._mcp_ctx)
            hooks = build_hooks(memory_store=self._mcp_ctx.memory_store)
            stderr_buf = _StderrBuffer()

            options = ClaudeAgentOptions(
                model=candidate or None,
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
                disallowed_tools=_builtin_disallowed_tools(),
                mcp_servers={"pip": mcp_server},
                hooks=hooks,
                # See :class:`pip_agent.agent_runner._StderrBuffer` —
                # without this, gateway errors arrive as the SDK's
                # ``"Check stderr output for details"`` placeholder.
                stderr=stderr_buf,
                # Always on in cached sessions: option is frozen at
                # ``connect()`` time, but a single connected client
                # serves both progressive-reply channels (WeCom) and
                # plain-old-text channels in arbitrary order. The
                # extra ``StreamEvent`` envelopes are cheap to ignore
                # when no per-turn ``on_stream_event`` is supplied,
                # and reconnecting just to flip the flag would burn
                # the ~400 ms tax this whole class exists to avoid.
                include_partial_messages=True,
                # Adaptive extended thinking. Same rationale as
                # ``include_partial_messages``: frozen at connect
                # time and the model decides per turn whether to
                # actually emit thinking blocks, so the cost is
                # only paid for turns that need it. Without this
                # option the SDK leaves ``thinking`` unset and no
                # ``thinking_delta`` events ever cross the wire.
                thinking={"type": "adaptive"},
            )

            client = ClaudeSDKClient(options=options)
            try:
                async with _profile.span(
                    "stream.connect",
                    session_key=self.session_key,
                    resume=bool(self._resume_session_id),
                    candidate_idx=idx + 1,
                    candidates=len(self._model_chain),
                ):
                    # ``connect(None)`` opens with an empty user-message
                    # stream, so the subprocess is live and waiting for
                    # our first ``client.query(...)`` call. This is the
                    # call that pays the one-time 400 ms tax the old
                    # per-turn path was eating every turn.
                    await client.connect()
            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                # Best-effort cleanup of the half-built client; we don't
                # want a doomed subprocess lingering while we try the
                # next candidate.
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                if (
                    len(self._model_chain) > 1
                    and idx < len(self._model_chain) - 1
                    and is_model_invalid_error(exc)
                ):
                    log.warning(
                        "stream %s: candidate %d/%d (%s) rejected as "
                        "invalid model; falling back to next tier (%s)",
                        self.session_key, idx + 1, len(self._model_chain),
                        candidate, exc,
                    )
                    _profile.event(
                        "stream.model_fallback",
                        session_key=self.session_key,
                        idx=idx + 1,
                        total=len(self._model_chain),
                        model=candidate,
                        err=str(exc)[:200],
                    )
                    continue
                raise

            self._client = client
            self._model = candidate
            self._connected = True
            self._stderr_buf = stderr_buf
            _profile.event(
                "stream.opened",
                session_key=self.session_key,
                resume=bool(self._resume_session_id),
                model=candidate,
            )
            return

        assert last_exc is not None
        raise last_exc

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
            self._stderr_buf = None

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
        on_stream_event: StreamEventCallback | None = None,
    ) -> QueryResult:
        """Send one user turn into the live subprocess and drain the response.

        Serialises same-session calls via a per-session lock — the
        AgentHost per-session lock would also serialise, but keeping a
        local one here means the StreamingSession is safe to use from
        anywhere (not only the AgentHost path).

        Run-time fallback: ``claude.exe`` does not contact the API on
        ``connect()`` — it only validates the model when the first user
        message goes through. So a bad ``MODEL_T*`` always surfaces here,
        not in :meth:`connect`. When the failure matches
        :func:`pip_agent.models.is_model_invalid_error` and the tier
        chain has remaining candidates, we tear down the dead client,
        reconnect with the next candidate, and replay the same prompt.
        Limited to one full sweep of the chain per turn — second-failure
        modes (auth, network, quota) propagate via ``QueryResult.error``.
        """
        from pip_agent.models import is_model_invalid_error

        if self._closed or not self._connected or self._client is None:
            raise RuntimeError(
                f"StreamingSession({self.session_key}) not connected",
            )

        async with self._turn_lock:
            # Worst case the chain head + every fallback is bad; cap
            # the loop at chain length so a misconfigured proxy can't
            # spin us forever.
            for _attempt in range(max(len(self._model_chain), 1)):
                result = await self._attempt_turn(
                    prompt,
                    sender_id=sender_id,
                    peer_id=peer_id,
                    stream_text=stream_text,
                    account_id=account_id,
                    on_stream_event=on_stream_event,
                )
                if not result.error:
                    return result
                if not is_model_invalid_error(RuntimeError(result.error)):
                    # Stale-session handling stays the same; non-model
                    # errors bubble up via the QueryResult.
                    if self._looks_stale(result.error):
                        raise StaleSessionError(result.error)
                    return result
                # Model rejected at runtime. Try the next candidate
                # if the chain has one; otherwise surface the error.
                if not await self._reconnect_after_invalid_model(result.error):
                    return result
            return result

    async def _attempt_turn(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        sender_id: str,
        peer_id: str,
        stream_text: bool,
        account_id: str,
        on_stream_event: StreamEventCallback | None = None,
    ) -> QueryResult:
        """One pass at sending the prompt and draining the response.

        Extracted from :meth:`run_turn` so the model-invalid retry loop
        can replay the same prompt against a freshly-reconnected client
        without re-acquiring ``_turn_lock``. Lock semantics: the caller
        already holds ``_turn_lock``; this body must not re-acquire it.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKError,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            ThinkingBlock,
            ToolUseBlock,
        )

        from pip_agent import _profile

        use_stream_events = on_stream_event is not None

        self.turn_count += 1
        turn_idx = self.turn_count
        # Mutate the shared McpContext so MCP tool closures see this
        # turn's identity. Reads are lazy (attribute access at call
        # time), so mutation is observed without any client rebuild.
        self._mcp_ctx.sender_id = sender_id
        self._mcp_ctx.peer_id = peer_id
        self._mcp_ctx.session_id = self.session_id
        self._mcp_ctx.account_id = account_id
        # Each turn's stderr is bounded to that turn — drop any context
        # carried over from idle / earlier turns so a failure here
        # surfaces only the relevant gateway lines.
        if self._stderr_buf is not None:
            self._stderr_buf.reset()

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
                    if isinstance(message, StreamEvent) and use_stream_events:
                        # Forward fine-grained text/thinking deltas to
                        # the renderer; whole-block events from
                        # ``AssistantMessage`` would arrive too late
                        # for the typewriter effect.
                        await _emit_stream_event_deltas(
                            message, on_stream_event,
                        )
                        continue

                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
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
                                # Console mirror only when there's no
                                # consumer — otherwise the delta path
                                # already emitted character-by-character.
                                if stream_text and not use_stream_events:
                                    print(block.text, end="", flush=True)
                                    streaming_line_open = True
                            elif isinstance(block, ThinkingBlock) and not use_stream_events:
                                # Fallback: forward whole thinking
                                # blocks when partial messages weren't
                                # subscribed to. With ``use_stream_events``
                                # the deltas already covered the body.
                                if on_stream_event is not None:
                                    await on_stream_event(
                                        "thinking_delta", text=block.thinking,
                                    )
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
                                if on_stream_event is not None:
                                    await on_stream_event(
                                        "tool_use",
                                        id=block.id,
                                        name=block.name,
                                        input=block.input,
                                    )

                    elif isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            sid = message.data.get("session_id")
                            # A fresh sid on turn >=2 would indicate a
                            # subprocess respawn we didn't expect — log
                            # it so the regression is visible.
                            if self.session_id and sid and sid != self.session_id:
                                log.warning(
                                    "stream %s: SystemMessage(init) reports sid %s != cached %s",
                                    self.session_key, sid, self.session_id,
                                )
                            if sid:
                                self.session_id = sid
                            from pip_agent import sdk_caps
                            sdk_caps.record(message.data.get("slash_commands"))
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
                        if on_stream_event is not None:
                            elapsed_s = (
                                time.perf_counter_ns() - stream_start_ns
                            ) / 1e9
                            await on_stream_event(
                                "finalize",
                                final_text=message.result,
                                num_turns=message.num_turns,
                                cost_usd=message.total_cost_usd,
                                usage=usage,
                                elapsed_s=elapsed_s,
                            )
            except ClaudeSDKError as exc:
                if streaming_line_open:
                    print(flush=True)
                captured = self._stderr_buf.text() if self._stderr_buf else ""
                err_text = _enrich_with_stderr(str(exc), captured)
                result.error = err_text
                _profile.event(
                    "stream.sdk_error",
                    session_key=self.session_key,
                    turn=turn_idx,
                    err=err_text[:200],
                    stderr_chars=len(captured),
                )
                log.error(
                    "stream %s: SDK error on turn %d: %s",
                    self.session_key, turn_idx, err_text,
                )
            except Exception as exc:  # noqa: BLE001
                if streaming_line_open:
                    print(flush=True)
                captured = self._stderr_buf.text() if self._stderr_buf else ""
                err_text = _enrich_with_stderr(str(exc), captured)
                result.error = err_text
                _profile.event(
                    "stream.unhandled_error",
                    session_key=self.session_key,
                    turn=turn_idx,
                    err=err_text[:200],
                    err_type=type(exc).__name__,
                    stderr_chars=len(captured),
                )
                log.exception(
                    "stream %s: unhandled error on turn %d",
                    self.session_key, turn_idx,
                )
            finally:
                self.last_used_ns = time.perf_counter_ns()

        return result

    async def _reconnect_after_invalid_model(self, err_text: str) -> bool:
        """Drop the current client and reconnect with the next chain candidate.

        Returns ``True`` when a fresh client is live and ready for a
        retry, ``False`` when the chain is exhausted (caller should
        surface the original error). The current model is removed from
        the chain so a future retry never re-tries the known-bad name.
        Resume metadata is dropped because the failed turn never reached
        the model — there's no conversation state to preserve.
        """
        from pip_agent import _profile

        try:
            cur_idx = self._model_chain.index(self._model)
        except ValueError:
            return False

        remaining = list(self._model_chain[cur_idx + 1:])
        if not remaining:
            log.warning(
                "stream %s: model %s rejected and tier chain exhausted (%s)",
                self.session_key, self._model, err_text[:160],
            )
            return False

        log.warning(
            "stream %s: model %s rejected (%s); reconnecting with next "
            "tier candidate %s",
            self.session_key, self._model, err_text[:160], remaining[0],
        )
        _profile.event(
            "stream.runtime_model_fallback",
            session_key=self.session_key,
            failed_model=self._model,
            next_model=remaining[0],
            err=err_text[:200],
        )

        # Tear down the dead client. Best-effort — the subprocess might
        # already be in a half-broken state, and we don't want a stale
        # disconnect error to mask the runtime fallback.
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "stream %s: ignoring disconnect error during model "
                    "fallback: %s", self.session_key, exc,
                )
        self._client = None
        self._connected = False

        # Reset the chain to start at the next candidate, drop any
        # session-resume metadata (the failed turn was never accepted by
        # the upstream so there's nothing to resume), and reconnect.
        self._model_chain = remaining
        self._resume_session_id = None
        self.session_id = ""

        await self.connect()
        return True


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
