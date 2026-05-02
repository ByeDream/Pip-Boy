"""Codex SDK agent runner — wraps ``codex-python`` for Pip-Boy.

Lifecycle:
    1. ``Codex()`` → starts the app-server (bundled binary, ~11s first time)
    2. ``start_thread(ThreadStartOptions(...))`` → creates a thread
    3. ``thread.run(prompt)`` → streams events for one turn
    4. Same thread can ``run()`` again for multi-turn (~3s subsequent)
    5. ``client.close()`` → shuts down the app-server

The event stream is consumed by ``event_translator.translate_event``
which maps SDK JSON-RPC notifications into the 5 Pip-Boy semantic events.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pip_agent.backends.base import QueryResult, StreamEventCallback

log = logging.getLogger(__name__)


def _resolve_codex_api_key() -> str | None:
    """Resolve the API key for the Codex backend.

    Reads from ``CODEX_API_KEY`` → ``OPENAI_API_KEY`` env vars.
    Returns ``None`` if neither is set (SDK will use config.toml).
    """
    import os

    return (
        os.environ.get("CODEX_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or None
    )


async def run_query(
    prompt: str | list[dict[str, Any]],
    *,
    model: str | None = None,
    session_id: str | None = None,
    system_prompt_append: str = "",
    cwd: str | Path | None = None,
    sandbox: str = "workspace-write",
    on_stream_event: StreamEventCallback | None = None,
) -> QueryResult:
    """Run a single agent turn via the codex-python SDK.

    Creates a fresh ``Codex()`` client per call (one-shot mode).
    For persistent connections, use ``CodexStreamingSession`` instead.
    """
    from pip_agent import _profile

    try:
        from codex import Codex, CodexOptions, ThreadStartOptions
        from codex.protocol import types as proto
    except ImportError as exc:
        return QueryResult(
            error=f"codex-python not installed: {exc}. "
            "Install with: pip install codex-python",
        )

    from pip_agent.backends.codex_cli.event_translator import translate_event

    api_key = _resolve_codex_api_key()
    options_kwargs: dict[str, Any] = {}
    if api_key:
        options_kwargs["api_key"] = api_key

    client = Codex(CodexOptions(**options_kwargs) if options_kwargs else None)

    result = QueryResult()
    state: dict[str, Any] = {}

    try:
        async with _profile.span("codex.run_query"):
            start_ns = time.perf_counter_ns()

            thread_opts = ThreadStartOptions(
                sandbox=proto.SandboxMode(root=sandbox),
                approval_policy=proto.AskForApproval(root="never"),
                cwd=str(cwd) if cwd else None,
            )

            if session_id:
                thread = client.resume_thread(
                    session_id,
                    options=thread_opts,
                )
            else:
                thread = client.start_thread(thread_opts)

            result.session_id = thread.id

            prompt_text = prompt if isinstance(prompt, str) else _blocks_to_text(prompt)
            stream = thread.run(prompt_text)

            for event in stream:
                if on_stream_event is not None:
                    await translate_event(event, on_stream_event, state=state)

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
            result.num_turns = 1

            usage = state.get("token_usage", {})
            if usage:
                result.cost_usd = None

            _profile.event(
                "codex.result",
                thread_id=thread.id,
                reply_len=len(result.text or ""),
                elapsed_s=round(elapsed_s, 2),
            )
            log.info(
                "Codex done: thread=%s len=%d elapsed=%.1fs",
                thread.id[:12] if thread.id else "?",
                len(result.text or ""),
                elapsed_s,
            )

    except Exception as exc:
        log.exception("Codex run_query failed: %s", exc)
        result.error = f"{type(exc).__name__}: {exc}"
        from pip_agent import _profile as _p
        _p.event("codex.error", err=str(exc)[:200])
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    return result


def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten Anthropic-style content blocks into plain text.

    The Codex SDK accepts plain strings only; multimodal blocks
    (images, etc.) are reduced to their text parts.
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts) if parts else str(blocks)
