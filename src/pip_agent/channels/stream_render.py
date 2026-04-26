"""Progressive-reply renderer for channels with native streaming bubbles.

Currently only WeCom uses this — see
:meth:`pip_agent.channels.wecom.WecomChannel.start_stream` for the
client-side rendering conventions (``<think>...</think>`` cloud
block, automatic typing-dots while the bubble is empty).

The renderer is fed semantic events from the agent runner / streaming
session as the SDK emits them and turns them into incremental
``update_stream`` snapshots. The shape is borrowed wholesale from
``pipi``: cloud-icon thinking on top, body in the middle, a compact
``tools · turns · time · cost`` / ``tokens in / out`` footer on the
last frame. Differences from pipi:

* The footer is computed on our side (pipi never emitted one — we
  searched). We pull tool count + duration locally and read
  ``num_turns`` / ``cost_usd`` / ``usage`` from ``ResultMessage``.
* Updates are throttled to one per ``_FLUSH_INTERVAL_NS`` to avoid
  hammering the WeCom WS; a final flush happens unconditionally on
  finalize.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pip_agent.channels.base import Channel

log = logging.getLogger(__name__)

# Mirrors pipi's ``STREAM_FLUSH_INTERVAL_MS = 300``. Lower than this and
# we risk the WeCom gateway rate-limiting us; higher and the typewriter
# effect starts to feel choppy.
_FLUSH_INTERVAL_NS = 300_000_000

# Hard ceiling on a single update's body. The WeCom client truncates
# extremely long stream snapshots silently; cap a touch under the
# observed limit so the trailing footer is always preserved.
_MAX_UPDATE_CHARS = 12_000


class WecomStreamRenderer:
    """Buffer thinking + text deltas and push periodic snapshots to WeCom.

    One renderer per inbound. Created by :class:`AgentHost` after
    ``ch.start_stream`` returns a handle; the host hands the
    ``handle_event`` coroutine to the runner as ``on_stream_event``.
    """

    def __init__(
        self,
        *,
        channel: "Channel",
        to: str,
        handle: str,
        inbound_id: str,
        account_id: str = "",
    ) -> None:
        self._channel = channel
        self._to = to
        self._handle = handle
        self._inbound_id = inbound_id
        self._account_id = account_id

        self._thinking_parts: list[str] = []
        self._body_parts: list[str] = []
        self._tool_count: int = 0
        self._start_ns: int = time.perf_counter_ns()
        self._last_flush_ns: int = 0

        self._lock = asyncio.Lock()
        self._finalized: bool = False
        self._delivered: bool = False

    @property
    def handle(self) -> str:
        return self._handle

    @property
    def delivered(self) -> bool:
        """``True`` once :meth:`finalize` succeeded — host uses this to
        know whether ``_dispatch_reply`` still has work to do."""
        return self._delivered

    @property
    def tool_count(self) -> int:
        return self._tool_count

    # ------------------------------------------------------------------
    # Event ingest
    # ------------------------------------------------------------------

    async def handle_event(self, event_type: str, **kwargs: Any) -> None:
        """Single sink for runner-emitted streaming events.

        Recognised types:

        * ``thinking_delta`` — kwargs: ``text``
        * ``text_delta`` — kwargs: ``text``
        * ``tool_use`` — kwargs: ``name`` (purely for the footer count)
        * ``finalize`` — kwargs: ``final_text``, ``num_turns``,
          ``cost_usd``, ``usage`` (dict), optional ``elapsed_s``.
          Triggers the closing flush.

        Unknown types are ignored — the runner may add more later
        without breaking older renderers.
        """
        if self._finalized:
            return
        try:
            if event_type == "thinking_delta":
                text = kwargs.get("text", "")
                if text:
                    self._thinking_parts.append(text)
                    await self._maybe_flush()
            elif event_type == "text_delta":
                text = kwargs.get("text", "")
                if text:
                    self._body_parts.append(text)
                    await self._maybe_flush()
            elif event_type == "tool_use":
                self._tool_count += 1
            elif event_type == "finalize":
                raw_elapsed = kwargs.get("elapsed_s")
                try:
                    elapsed_override = (
                        float(raw_elapsed) if raw_elapsed is not None else None
                    )
                except (TypeError, ValueError):
                    elapsed_override = None
                await self.finalize(
                    final_text=kwargs.get("final_text"),
                    num_turns=int(kwargs.get("num_turns") or 0),
                    cost_usd=kwargs.get("cost_usd"),
                    usage=kwargs.get("usage") or {},
                    elapsed_s=elapsed_override,
                )
        except Exception:
            log.exception(
                "WecomStreamRenderer: %s event handling failed", event_type,
            )

    # ------------------------------------------------------------------
    # Flush + finalize
    # ------------------------------------------------------------------

    def _compose(self, *, footer: str = "") -> str:
        thinking = "".join(self._thinking_parts).strip()
        body = "".join(self._body_parts)
        parts: list[str] = []
        if thinking:
            parts.append(f"<think>{thinking}</think>")
        if body:
            parts.append(body)
        if not parts:
            # Empty placeholder so the bubble keeps the typing dots
            # animation while we wait for the first real delta.
            parts.append("...")
        text = "\n\n".join(parts)
        if footer:
            text = f"{text}\n\n{footer}"
        if len(text) > _MAX_UPDATE_CHARS:
            # Trim from the head of the body — keep thinking + footer
            # intact so the user can still read the tail of the answer.
            overflow = len(text) - _MAX_UPDATE_CHARS
            text = text[overflow:]
        return text

    async def _maybe_flush(self) -> None:
        now = time.perf_counter_ns()
        if now - self._last_flush_ns < _FLUSH_INTERVAL_NS:
            return
        await self._do_flush(final=False)

    async def _do_flush(self, *, final: bool, footer: str = "") -> bool:
        # Serialize updates so a flood of deltas doesn't have N flushes
        # racing on the WS — they'd land out of order and the bubble
        # would flicker between earlier and later snapshots.
        async with self._lock:
            if self._finalized and not final:
                return False
            text = self._compose(footer=footer)
            if final:
                ok = await asyncio.to_thread(
                    self._channel.finish_stream,
                    self._to,
                    self._handle,
                    text,
                    inbound_id=self._inbound_id,
                    account_id=self._account_id,
                )
            else:
                ok = await asyncio.to_thread(
                    self._channel.update_stream,
                    self._to,
                    self._handle,
                    text,
                    inbound_id=self._inbound_id,
                    account_id=self._account_id,
                )
            self._last_flush_ns = time.perf_counter_ns()
            return ok

    async def finalize(
        self,
        *,
        final_text: str | None,
        num_turns: int,
        cost_usd: float | None,
        usage: dict[str, Any],
        elapsed_s: float | None = None,
    ) -> None:
        """Send the closing snapshot with body + stats footer.

        ``final_text`` (when present) replaces the live-streamed body —
        the SDK's ``ResultMessage.result`` is authoritative; the live
        deltas can drop the first chunk if our subscription raced the
        first ``content_block_delta``. Falling back to the buffered
        body when the SDK omits ``result`` keeps the renderer useful
        in error / interrupt paths.
        """
        if self._finalized:
            return
        self._finalized = True
        if final_text:
            # Replace, not append — the live buffer was a best-effort
            # mirror; trust the SDK's complete reply text.
            self._body_parts = [final_text]
        footer = self._format_footer(
            num_turns=num_turns,
            cost_usd=cost_usd,
            usage=usage,
            elapsed_s=elapsed_s,
        )
        ok = await self._do_flush(final=True, footer=footer)
        self._delivered = bool(ok)

    async def fail(self, *, error: str) -> None:
        """Close the stream gracefully when the agent errored.

        Mirrors :meth:`finalize` but with no ``ResultMessage`` available:
        emit whatever body we already streamed plus a one-line error
        notice. Caller (``AgentHost``) is responsible for any further
        error reporting on its own.
        """
        if self._finalized:
            return
        self._finalized = True
        self._body_parts.append(f"\n\n[error] {error}")
        ok = await self._do_flush(final=True)
        self._delivered = bool(ok)

    # ------------------------------------------------------------------
    # Footer formatting
    # ------------------------------------------------------------------

    def _format_footer(
        self,
        *,
        num_turns: int,
        cost_usd: float | None,
        usage: dict[str, Any],
        elapsed_s: float | None = None,
    ) -> str:
        if elapsed_s is None:
            elapsed_s = (time.perf_counter_ns() - self._start_ns) / 1e9
        cost_text = f"${cost_usd:.3f}" if cost_usd is not None else "$0.000"
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        # Cache reads count as input from the user's perspective;
        # pipi's footer hid them, we follow suit to match the look.
        line1 = (
            f"⚙ {self._tool_count} tools · {num_turns} turn"
            f"{'s' if num_turns != 1 else ''} · {elapsed_s:.1f}s · {cost_text}"
        )
        line2 = f"📊 {in_tok} in / {out_tok} out"
        return f"{line1}\n{line2}"
