"""SDK-native agent runner: wraps ``claude_agent_sdk.query()`` for Pip-Boy.

The SDK manages the full agent loop — tool dispatch, context compaction, and
session persistence — while Pip-Boy's unique capabilities are exposed via an
in-process MCP server (see ``mcp_tools.py``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from pip_agent import sdk_caps
from pip_agent.hooks import build_hooks
from pip_agent.mcp_tools import McpContext, build_mcp_server

# Type alias for the per-turn streaming callback. Callers receive a
# small set of semantic event names so the runner can swap its source
# between ``StreamEvent`` (partial deltas) and ``AssistantMessage``
# (whole blocks) without breaking the contract:
#
# * ``thinking_delta``  — kwargs: ``text`` (raw partial)
# * ``text_delta``      — kwargs: ``text``
# * ``tool_use``        — kwargs: ``id`` (tool_use_id), ``name``,
#                          ``input`` (raw tool-call argument dict, as
#                          sent by the SDK)
# * ``tool_result``     — kwargs: ``tool_use_id``, ``is_error`` (the
#                          matching result for a prior ``tool_use``;
#                          emitted from the subsequent ``UserMessage``)
# * ``finalize``        — kwargs: ``final_text``, ``num_turns``,
#                          ``cost_usd``, ``usage``, ``elapsed_s``
#                          (wall seconds from stream open to result)
#
# ``await``ed inline with the SDK message loop, so a slow callback
# directly throttles delta consumption — keep handlers lean.
StreamEventCallback = Callable[..., Awaitable[None]]

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Return value from :func:`run_query`."""

    text: str | None = None
    session_id: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    num_turns: int = 0


# No hardcoded allowed-tools list: the Host deliberately does NOT
# customise CC's tool surface. The SDK treats an empty ``allowed_tools``
# (the default) as "use the CLI's built-in default set", and the
# ``mcp_servers`` wiring registers our ``mcp__pip__*`` tools alongside
# those automatically. Maintaining a whitelist here meant CC silently
# losing access to any tool CC itself added after this list was last
# edited — a clear violation of "Pip is a host, not a CC policy
# author". See H6 in ``code_review_plan_133b34c7.plan.md``.


def _build_env() -> dict[str, str]:
    """Collect env vars to forward to the Claude Code CLI subprocess.

    Credential resolution + the proxy rule live in
    ``pip_agent.anthropic_client.resolve_anthropic_credential`` — this
    function just translates the resolved credential into the env var names
    the CC CLI expects. DO NOT duplicate the proxy rule here; if you need to
    change how bearer vs. x-api-key is decided, change it in one place.

    Pip-Boy does not forward any search or tool-specific keys — those are
    handled by Claude Code's own config.

    ``CLAUDE_CODE_DISABLE_CRON=1`` kills CC's native ``CronCreate`` /
    ``CronList`` / ``CronDelete`` tools (and the ``/loop`` skill that fronts
    them). They are fundamentally incompatible with our architecture: CC's
    cron scheduler lives in the ``claude.exe`` subprocess, which we spawn
    fresh for each ``run_query`` and kill on ``end_turn``. A task scheduled
    via ``CronCreate`` has no process to tick it between turns, so it
    silently never fires — an "API that lies to the model". Pip-Boy's own
    scheduler (``host_scheduler.py``) is durable, multi-channel, and lives
    in the long-running host process, so we are intentionally the only cron
    provider the agent sees.
    """
    from pip_agent.anthropic_client import resolve_anthropic_credential

    env: dict[str, str] = {
        "CLAUDE_CODE_DISABLE_CRON": "1",
    }
    cred = resolve_anthropic_credential()
    if cred is not None:
        if cred.bearer:
            env["ANTHROPIC_AUTH_TOKEN"] = cred.token
        else:
            env["ANTHROPIC_API_KEY"] = cred.token
        if cred.base_url:
            env["ANTHROPIC_BASE_URL"] = cred.base_url
            # Experimental betas are rejected by most corporate proxies.
            env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    return env


# Built-in Claude Code tools we deliberately shadow with our own
# ``mcp__pip__*`` implementation. Listed here so the SDK strips them
# from the model's option set — without that, the agent sees two
# tools doing nearly the same thing and picks arbitrarily, splitting
# trace logs and confusing failure modes.
#
# Currently shadowed:
#   * ``WebFetch``  → ``mcp__pip__web_fetch``  (see :mod:`pip_agent.web`)
#   * ``WebSearch`` → ``mcp__pip__web_search`` (Tavily → DDG fallback)
#
# Both are disabled because the corporate-proxy gateway Pip-Boy is
# pointed at rejects the experimental-betas header Claude Code's
# server-side web tools require. Shipping in-process replacements
# sidesteps that entirely.
_BUILTIN_DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")


class _StderrBuffer:
    """Bounded line-buffer for ``claude.exe`` stderr capture.

    The SDK only pipes stderr when ``ClaudeAgentOptions.stderr`` is
    set; otherwise ``ProcessError`` arrives with the literal
    placeholder string ``"Check stderr output for details"`` and the
    real gateway message (``API Error: 400 ... 模型不存在``) is gone.
    Pass an instance as the ``stderr`` option, then call :meth:`text`
    after the attempt to recover the captured output.

    Bounded on purpose: a misbehaving proxy could emit megabytes of
    error text into the SDK subprocess and we don't want that pinned
    in process RAM. The first ``_MAX_LINES`` / ``_MAX_TOTAL_CHARS``
    are kept; the rest is silently dropped (the API-error JSON line
    we actually care about is always near the start).
    """

    _MAX_LINES: int = 200
    _MAX_TOTAL_CHARS: int = 16_000

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._chars: int = 0

    def __call__(self, line: str) -> None:
        if len(self._lines) >= self._MAX_LINES:
            return
        budget = self._MAX_TOTAL_CHARS - self._chars
        if budget <= 0:
            return
        if len(line) > budget:
            line = line[:budget]
        self._lines.append(line)
        self._chars += len(line)

    def text(self) -> str:
        return "\n".join(self._lines).strip()

    def reset(self) -> None:
        self._lines.clear()
        self._chars = 0


def _enrich_with_stderr(err_text: str, captured: str) -> str:
    """Splice captured stderr into an SDK error string when useful.

    The SDK's ``ProcessError`` carries the literal placeholder
    ``"Check stderr output for details"``. When we have real captured
    output, swap the placeholder for it; otherwise append as a
    suffix so callers still get the original error class label.
    """
    if not captured:
        return err_text
    placeholder = "Check stderr output for details"
    if placeholder in err_text:
        return err_text.replace(placeholder, captured)
    return f"{err_text} | stderr: {captured}"


async def _stream_single_user_message(
    content: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield exactly one SDK-shaped ``user`` envelope for multimodal input.

    The SDK's string path writes a single ``{"type": "user", ...}``
    line to stdin and then closes input. For content-block prompts
    (images, inline file text) we take the ``AsyncIterable`` path so
    ``content`` can be a list instead of a bare string. The envelope
    shape mirrors ``claude_agent_sdk._internal.client`` exactly —
    including ``session_id: ""`` because actual session resumption is
    driven by ``options.resume``, not by anything in this envelope.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


async def run_query(
    prompt: str | list[dict[str, Any]],
    *,
    mcp_ctx: McpContext,
    model_chain: list[str] | None = None,
    session_id: str | None = None,
    system_prompt_append: str = "",
    cwd: str | Path | None = None,
    stream_text: bool = True,
    on_stream_event: StreamEventCallback | None = None,
) -> QueryResult:
    """Run a single agent turn via the Claude Agent SDK.

    Parameters
    ----------
    prompt:
        The user message to send. ``str`` for pure-text turns (the
        hot path), or a list of Anthropic-style content blocks
        (``[{"type": "text", ...}, {"type": "image", ...}]``) when
        the inbound carries images / inline files / voice
        transcriptions. See :func:`pip_agent.agent_host._format_prompt`
        for the block shape.
    mcp_ctx:
        Pre-configured MCP context with all host-side services.
    model_chain:
        Ordered list of concrete model names to try. The first candidate
        is used unless it fails with a model-invalid error (see
        :func:`pip_agent.models.is_model_invalid_error`), in which case
        we restart the SDK subprocess with the next candidate. ``None``
        or empty list defers entirely to the SDK / CC defaults — only
        appropriate for callers that have no tier mapping (none today).
    session_id:
        SDK session ID to resume. ``None`` starts a new session.
    system_prompt_append:
        Text appended to the ``claude_code`` preset. Carries Pip persona,
        memory enrichment, and user profile context.
    cwd:
        Working directory for the agent.
    stream_text:
        If True (default), stream ``TextBlock`` content to stdout as it
        arrives — the normal interactive-CLI experience. Callers that need
        to post-process the final text (e.g. ``AgentHost`` silencing the
        ``HEARTBEAT_OK`` sentinel) must pass ``False`` — once characters are
        on the wire there is nothing the host can do to unprint them.
    on_stream_event:
        Optional async callback fed semantic streaming events
        (``text_delta`` / ``thinking_delta`` / ``tool_use`` /
        ``tool_result`` / ``finalize``). When supplied, the SDK is asked for partial
        content-block messages so deltas land character-by-character;
        the callback drives :class:`pip_agent.channels.stream_render.\
WecomStreamRenderer` (or any other progressive-reply consumer). When
        ``None`` the runner behaves exactly as before — no extra SDK
        traffic, no extra overhead.

    Notes
    -----
    Tool-use traces (``[tool: NAME {...}]``) and the ``Session:`` / ``Done:``
    summary logs are *always* emitted. They are part of the interactive CLI
    contract, not debug-only output. If you want them quiet, lower the
    logger level globally (``VERBOSE=false`` ⇒ root at WARNING, which drops
    the two ``log.info`` calls below; the ``print`` for tool-use has no
    logger gate because a user staring at a silent 30-second tool-chain
    cannot tell the difference between "agent is thinking" and "agent
    crashed").
    """
    from pip_agent import _profile  # PROFILE
    from pip_agent.models import is_model_invalid_error

    # ``[""]`` means "no tier-resolved candidate, let the SDK pick its
    # own default". Keeping at least one element through the loop means
    # the no-chain branch and the chain branch share the same body.
    candidates: list[str] = list(model_chain) if model_chain else [""]

    last_error: str | None = None
    for attempt_idx, candidate_model in enumerate(candidates):
        stderr_buf = _StderrBuffer()
        async with _profile.span("runner.sdk_setup"):
            mcp_server = build_mcp_server(mcp_ctx)
            effective_cwd = str(cwd) if cwd else str(mcp_ctx.workdir)

            hooks = build_hooks(memory_store=mcp_ctx.memory_store)

            options = ClaudeAgentOptions(
                model=candidate_model or None,
                cwd=effective_cwd,
                resume=session_id,
                system_prompt=(
                    {
                        "type": "preset",
                        "preset": "claude_code",
                        "append": system_prompt_append,
                    }
                    if system_prompt_append
                    else None
                ),
                permission_mode="bypassPermissions",
                # Load all three Claude Code settings tiers so plugins
                # installed via ``/plugin install --scope {user|project|local}``
                # are picked up by the next ``query()`` regardless of where
                # the user (or agent) chose to land them. ``local`` files
                # are silently absent on a fresh checkout — that's fine,
                # the SDK skips missing sources.
                setting_sources=["user", "project", "local"],
                env=_build_env(),
                disallowed_tools=list(_BUILTIN_DISALLOWED_TOOLS),
                mcp_servers={"pip": mcp_server},
                hooks=hooks,
                # Capture subprocess stderr so a non-zero exit (gateway
                # rejected the model, claude.exe crashed mid-request,
                # etc.) carries a real reason instead of the SDK's
                # ``"Check stderr output for details"`` placeholder.
                stderr=stderr_buf,
                # Partial messages give us per-character ``text_delta`` /
                # ``thinking_delta`` events the WeCom renderer needs for
                # the typewriter effect. Off by default so callers that
                # don't care don't pay the per-token framing cost.
                include_partial_messages=on_stream_event is not None,
                # Adaptive extended thinking — without this the SDK
                # leaves ``thinking`` unset and the gateway never asks
                # the model to emit thinking blocks, so the WeCom
                # cloud-icon block stays empty no matter how many
                # ``content_block_delta`` we subscribe to. ``adaptive``
                # lets the model decide turn-by-turn whether to think,
                # which matches pipi's behaviour and avoids the
                # always-on token cost of ``enabled``.
                thinking={"type": "adaptive"},
            )

        result = await _run_one_attempt(
            options=options,
            prompt=prompt,
            session_id=session_id,
            stream_text=stream_text,
            stderr_buf=stderr_buf,
            on_stream_event=on_stream_event,
        )

        # Treat a model-invalid failure as a signal to fall back to the
        # next tier candidate; everything else (auth, network, quota)
        # surfaces unchanged because re-trying with a different model
        # would not help.
        if (
            result.error
            and len(candidates) > 1
            and attempt_idx < len(candidates) - 1
            and _looks_model_invalid(result.error, is_model_invalid_error)
        ):
            log.warning(
                "runner: candidate %d/%d (%s) rejected as invalid model; "
                "retrying with next tier candidate (err=%s)",
                attempt_idx + 1, len(candidates), candidate_model,
                result.error[:160],
            )
            _profile.event(
                "runner.model_fallback",
                idx=attempt_idx + 1,
                total=len(candidates),
                model=candidate_model,
                err=result.error[:200],
            )
            last_error = result.error
            continue

        return result

    fallback = QueryResult()
    fallback.error = last_error or "all model candidates rejected"
    return fallback


def _looks_model_invalid(
    err_text: str,
    matcher,
) -> bool:
    """Apply :func:`is_model_invalid_error` to a stringified SDK error.

    The SDK exception wraps the gateway error message but the structured
    type info is usually flattened into the message string by the time it
    reaches us; matching on the text is the most reliable signal.
    """
    return matcher(RuntimeError(err_text))


async def _run_one_attempt(
    *,
    options: "ClaudeAgentOptions",
    prompt: str | list[dict[str, Any]],
    session_id: str | None,
    stream_text: bool,
    stderr_buf: _StderrBuffer | None = None,
    on_stream_event: StreamEventCallback | None = None,
) -> QueryResult:
    """Execute a single SDK ``query`` pass with the prepared options.

    Extracted from :func:`run_query` so the tier-fallback loop can rebuild
    options per candidate without duplicating the streaming / result
    handling. Behaviour below is the previously-inlined body.

    ``stderr_buf`` (when supplied and bound to ``options.stderr``) is
    spliced into the error string on failure so the SDK's placeholder
    text is replaced with the real subprocess output — critical for
    classifying the error and for end-user diagnostics.
    """
    from pip_agent import _profile

    result = QueryResult()
    # Track whether we have an unterminated streaming line so we can close
    # it before the end-of-turn ``log.info`` fires. Without this the log
    # record glues onto the last chunk's trailing character, producing
    # ``hello2026-04-21 ... Done: turns=1 ...`` in the console. Same
    # thread, same stdout — it's a missing newline, not a race.
    streaming_line_open = False

    # String prompts go straight through; block-list prompts need the
    # streaming ``AsyncIterable`` wrapper. Keep the string branch as
    # the hot path — no iterator spin-up, no extra heap allocation.
    sdk_prompt: Any
    if isinstance(prompt, str):
        sdk_prompt = prompt
    else:
        sdk_prompt = _stream_single_user_message(prompt)

    import time as _time  # PROFILE

    # When a stream-event consumer is attached we emit fine-grained
    # deltas from ``StreamEvent`` and silence the per-block path so the
    # consumer is the single source of truth for streamed content.
    use_stream_events = on_stream_event is not None

    try:
        async with _profile.span(  # PROFILE
            "runner.sdk_stream",
            prompt_kind="str" if isinstance(prompt, str) else "blocks",
            resume=bool(session_id),
        ):
            stream_start_ns = _time.perf_counter_ns()  # PROFILE
            first_text_seen = False  # PROFILE
            tool_count = 0  # PROFILE
            async for message in query(prompt=sdk_prompt, options=options):
                if isinstance(message, StreamEvent) and use_stream_events:
                    await _emit_stream_event_deltas(
                        message, on_stream_event,
                    )
                    continue

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            # PROFILE — first-token latency anchor.
                            if not first_text_seen:
                                _profile.event(
                                    "runner.first_text",
                                    since_stream_ms=round(
                                        (_time.perf_counter_ns() - stream_start_ns)
                                        / 1_000_000.0,
                                        3,
                                    ),
                                    text_len=len(block.text),
                                )
                                first_text_seen = True
                            # Console mirror is gated independently from the
                            # event consumer: the renderer cares about the
                            # SDK deltas, the dev watching the terminal still
                            # benefits from seeing whole blocks.
                            if stream_text and not use_stream_events:
                                print(block.text, end="", flush=True)
                                streaming_line_open = True
                            # Fallback path: when partial messages are off
                            # (no consumer attached this is a no-op anyway,
                            # since ``use_stream_events`` is False) we still
                            # forward the whole block so the consumer gets
                            # *something*. With ``include_partial_messages``
                            # on, ``StreamEvent`` already delivered every
                            # delta — skip to avoid double-emit.
                        elif isinstance(block, ThinkingBlock) and not use_stream_events:
                            # Same fallback rationale as TextBlock above.
                            if on_stream_event is not None:
                                await on_stream_event(
                                    "thinking_delta", text=block.thinking,
                                )
                        elif isinstance(block, ToolUseBlock):
                            # Tool-use traces are always surfaced: a user staring
                            # at a silent 30-second tool-chain cannot distinguish
                            # "agent is thinking" from "agent crashed". This is a
                            # UX contract, not debug output — do NOT gate on
                            # ``VERBOSE``.
                            # PROFILE
                            tool_count += 1
                            _profile.event(
                                "runner.tool_use",
                                name=block.name,
                                since_stream_ms=round(
                                    (_time.perf_counter_ns() - stream_start_ns)
                                    / 1_000_000.0,
                                    3,
                                ),
                            )
                            args_preview = str(block.input)[:80]
                            # Console mirror is suppressed when a stream-event
                            # consumer is attached (TUI / WeCom); the consumer
                            # already gets a ``tool_use`` callback below and
                            # is the single source of truth for surfacing the
                            # trace in that mode. Without this gate the TUI
                            # canvas gets corrupted by ``\n  [tool: ...]``
                            # writing directly to ``sys.stdout`` underneath
                            # the rendered widgets.
                            if not use_stream_events:
                                print(
                                    f"\n  [tool: {block.name} {args_preview}]",
                                    flush=True,
                                )
                                # Tool traces start with ``\n`` and end without
                                # one, so the line remains "open" from the
                                # console's POV. Only set the flag in the
                                # ungated branch — the consumer-driven path
                                # has no streamed line to terminate.
                                streaming_line_open = True
                            if on_stream_event is not None:
                                await on_stream_event(
                                    "tool_use",
                                    id=block.id,
                                    name=block.name,
                                    input=block.input,
                                )

                elif isinstance(message, UserMessage):
                    # Surface tool-result arrivals so consumers can track
                    # lifecycle (e.g. status-bar "tool in flight" indicator).
                    # Only ``ToolResultBlock`` is interesting here; the
                    # agent pane's transcript of the next AssistantMessage
                    # already covers what the model does with the result.
                    if on_stream_event is not None and isinstance(
                        message.content, list
                    ):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                await on_stream_event(
                                    "tool_result",
                                    tool_use_id=block.tool_use_id,
                                    is_error=bool(block.is_error),
                                )

                elif isinstance(message, SystemMessage):
                    if message.subtype == "init":
                        result.session_id = message.data.get("session_id")
                        log.info("Session: %s", result.session_id)
                        # Capture the SDK's dispatchable slash list once
                        # per process so ``/T`` can warn on typos and
                        # ``/help`` can list what is actually reachable
                        # in headless mode (see :mod:`pip_agent.sdk_caps`).
                        sdk_caps.record(message.data.get("slash_commands"))
                        # PROFILE
                        _profile.event(
                            "runner.session_init",
                            sid=result.session_id,
                            since_stream_ms=round(
                                (_time.perf_counter_ns() - stream_start_ns)
                                / 1_000_000.0,
                                3,
                            ),
                        )

                elif isinstance(message, ResultMessage):
                    result.text = message.result
                    result.session_id = message.session_id
                    result.cost_usd = message.total_cost_usd
                    result.num_turns = message.num_turns
                    if message.is_error:
                        result.error = message.result
                    # Close the streamed line *before* the log record fires,
                    # otherwise the "Done: turns=..." log glues onto whatever
                    # the last ``TextBlock`` printed.
                    if streaming_line_open:
                        print(flush=True)
                        streaming_line_open = False
                    # PROFILE
                    _profile.event(
                        "runner.result",
                        turns=message.num_turns,
                        cost_usd=message.total_cost_usd or 0,
                        stop=message.stop_reason,
                        err=message.is_error,
                        tool_calls=tool_count,
                        reply_len=len(message.result or ""),
                    )
                    log.info(
                        "Done: turns=%d cost=$%.4f stop=%s",
                        message.num_turns,
                        message.total_cost_usd or 0,
                        message.stop_reason,
                    )
                    if on_stream_event is not None:
                        elapsed_s = (
                            _time.perf_counter_ns() - stream_start_ns
                        ) / 1e9
                        await on_stream_event(
                            "finalize",
                            final_text=message.result,
                            num_turns=message.num_turns,
                            cost_usd=message.total_cost_usd,
                            usage=message.usage or {},
                            elapsed_s=elapsed_s,
                        )

    except ClaudeSDKError as exc:
        if streaming_line_open:
            print(flush=True)
        captured = stderr_buf.text() if stderr_buf is not None else ""
        result.error = _enrich_with_stderr(str(exc), captured)
        log.error("SDK error: %s", result.error)
        _profile.event(  # PROFILE
            "runner.sdk_error",
            err=result.error[:200],
            stderr_chars=len(captured),
        )
    except Exception as exc:
        # Catch-all for non-ClaudeSDKError exceptions escaping the SDK
        # iterator. Notably covers the bare ``Exception`` raised by
        # ``Query.receive_messages`` (``_internal/query.py``) when the
        # background reader posts a ``{"type": "error"}`` envelope after
        # subprocess exit-code != 0: the type info and full stack are
        # lost by the time they reach ``agent_host``'s outer catch, so
        # surface them here before re-raising.
        if streaming_line_open:
            print(flush=True)
        captured = stderr_buf.text() if stderr_buf is not None else ""
        log.exception(
            "runner: non-SDK exception escaping attempt "
            "(type=%s, stderr_chars=%d): %r",
            type(exc).__name__,
            len(captured),
            exc,
        )
        if captured:
            log.warning(
                "runner: claude.exe stderr at non-SDK exception (%d chars): %s",
                len(captured),
                captured[:2000],
            )
        _profile.event(  # PROFILE
            "runner.non_sdk_error",
            exc_type=type(exc).__name__,
            err=str(exc)[:200],
            stderr_chars=len(captured),
        )
        raise
    finally:
        # Dump captured stderr whenever the subprocess wrote anything,
        # regardless of how we're exiting. The ``except ClaudeSDKError``
        # branch above enriches the error message, but other exception
        # paths escape it — e.g. the SDK's background message-reader
        # task faulting after ``ResultMessage`` has already been
        # processed (observed as "Fatal error in message reader:
        # Command failed with exit code 1" logged by the SDK itself,
        # then a bare ``ProcessError`` re-raised from a teardown path
        # that sidesteps our ``async for``). The ``if captured:`` guard
        # keeps this silent on the happy path because ``claude.exe``
        # does not write to stderr on clean runs.
        if stderr_buf is not None:
            captured = stderr_buf.text()
            if captured:
                log.warning(
                    "runner: claude.exe stderr (%d chars): %s",
                    len(captured),
                    captured[:2000],
                )

    return result


async def _emit_stream_event_deltas(
    message: StreamEvent,
    on_stream_event: StreamEventCallback,
) -> None:
    """Translate one ``StreamEvent`` into renderer-shaped semantic events.

    The SDK forwards the raw Anthropic streaming envelope verbatim in
    ``message.event``. We only care about ``content_block_delta``
    frames here — block-start/stop carry no streamed text and are
    already represented by ``AssistantMessage`` whole-block events
    elsewhere in the loop. Keeping this function tiny and side-effect
    free (single ``await``) is intentional: a slow callback would
    starve the SDK's stream pump.
    """
    event = getattr(message, "event", None)
    if not isinstance(event, dict):
        return
    if event.get("type") != "content_block_delta":
        return
    delta = event.get("delta") or {}
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        text = delta.get("text") or ""
        if text:
            await on_stream_event("text_delta", text=text)
    elif delta_type == "thinking_delta":
        text = delta.get("thinking") or ""
        if text:
            await on_stream_event("thinking_delta", text=text)
