"""SDK-native agent runner: wraps ``claude_agent_sdk.query()`` for Pip-Boy.

The SDK manages the full agent loop — tool dispatch, context compaction, and
session persistence — while Pip-Boy's unique capabilities are exposed via an
in-process MCP server (see ``mcp_tools.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from pip_agent.hooks import build_hooks
from pip_agent.mcp_tools import McpContext, build_mcp_server

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Return value from :func:`run_query`."""

    text: str | None = None
    session_id: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    num_turns: int = 0


_BUILTIN_TOOLS: list[str] = [
    "Bash", "Read", "Write", "Edit", "MultiEdit",
    "Glob", "Grep",
    "WebSearch", "WebFetch",
    "Task", "TodoWrite", "Skill",
    "NotebookEdit",
    "mcp__pip__*",
]


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


async def run_query(
    prompt: str,
    *,
    mcp_ctx: McpContext,
    model: str = "",
    session_id: str | None = None,
    system_prompt_append: str = "",
    cwd: str | Path | None = None,
    stream_text: bool = True,
) -> QueryResult:
    """Run a single agent turn via the Claude Agent SDK.

    Parameters
    ----------
    prompt:
        The user message to send. Plain string; Phase 7 will extend this to
        also accept ``list[dict]`` for image attachments.
    mcp_ctx:
        Pre-configured MCP context with all host-side services.
    model:
        Model identifier (e.g. ``claude-sonnet-4-6``). ``""`` lets CC pick.
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
    mcp_server = build_mcp_server(mcp_ctx)
    effective_cwd = str(cwd) if cwd else str(mcp_ctx.workdir)

    hooks = build_hooks(memory_store=mcp_ctx.memory_store)

    options = ClaudeAgentOptions(
        model=model or None,
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
        allowed_tools=_BUILTIN_TOOLS,
        permission_mode="bypassPermissions",
        setting_sources=["project", "user"],
        env=_build_env(),
        mcp_servers={"pip": mcp_server},
        hooks=hooks,
    )

    result = QueryResult()
    # Track whether we have an unterminated streaming line so we can close
    # it before the end-of-turn ``log.info`` fires. Without this the log
    # record glues onto the last chunk's trailing character, producing
    # ``hello2026-04-21 ... Done: turns=1 ...`` in the console. Same
    # thread, same stdout — it's a missing newline, not a race.
    streaming_line_open = False

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and stream_text:
                        print(block.text, end="", flush=True)
                        streaming_line_open = True
                    elif isinstance(block, ToolUseBlock):
                        # Tool-use traces are always surfaced: a user staring
                        # at a silent 30-second tool-chain cannot distinguish
                        # "agent is thinking" from "agent crashed". This is a
                        # UX contract, not debug output — do NOT gate on
                        # ``VERBOSE``.
                        args_preview = str(block.input)[:80]
                        print(
                            f"\n  [tool: {block.name} {args_preview}]",
                            flush=True,
                        )
                        # Tool traces start with ``\n`` and end without one,
                        # so the line remains "open" from the console's POV.
                        streaming_line_open = True

            elif isinstance(message, SystemMessage):
                if message.subtype == "init":
                    result.session_id = message.data.get("session_id")
                    log.info("Session: %s", result.session_id)

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
                log.info(
                    "Done: turns=%d cost=$%.4f stop=%s",
                    message.num_turns,
                    message.total_cost_usd or 0,
                    message.stop_reason,
                )

    except ClaudeSDKError as exc:
        if streaming_line_open:
            print(flush=True)
        result.error = str(exc)
        log.error("SDK error: %s", exc)

    return result
