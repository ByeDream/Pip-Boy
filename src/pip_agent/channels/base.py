"""Channel ABC plus dataclasses shared by every concrete transport.

Split out from the old monolithic ``channels.py`` so that only this
module carries the types everyone imports. Concrete channels live in
sibling modules (``cli.py``, ``wechat.py``, ``wecom.py``) and the
package ``__init__`` re-exports them so existing callers keep working.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes. Returns '' for non-images."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    """Media attachment carried alongside an InboundMessage."""

    type: str                       # "image", "file", "voice"
    data: bytes | None = None
    filename: str = ""
    mime_type: str = ""
    text: str = ""                  # ASR transcription for voice, or text file content
    saved_path: str = ""            # workdir-relative path once materialized to disk


@dataclass
class InboundMessage:
    """Platform-agnostic inbound message.  The agent loop only sees this."""

    text: str
    sender_id: str
    channel: str = ""           # "cli", "wechat", "wecom"
    peer_id: str = ""           # conversation scope key (user id for DMs)
    guild_id: str = ""          # group/guild id (e.g. WeCom chatid)
    account_id: str = ""        # bot account id (for T3 routing)
    is_group: bool = False
    agent_id: str = ""          # routing override: skip binding_table.resolve
    raw: dict = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    # Host-scheduler coalescing key. Empty for user/channel messages. For
    # scheduler-injected payloads it identifies the "logical job" so
    # :class:`HostScheduler` can refuse to stack a second copy on the queue
    # while the first is still pending or in flight. ``AgentHost`` must call
    # ``scheduler.ack(source_job_id)`` after processing so subsequent ticks
    # can re-enqueue. See ``host_scheduler._pending_key_*`` for the format.
    source_job_id: str = ""


# ---------------------------------------------------------------------------
# Channel ABC
# ---------------------------------------------------------------------------

class Channel(ABC):
    name: str = "unknown"

    @property
    def send_lock(self) -> threading.Lock:
        """Per-instance lock that serializes outbound writes.

        Multiple lanes may call ``send`` concurrently once agent turns run
        in parallel. Chunked messages must not interleave on the wire, and
        underlying async / HTTP clients are not universally thread-safe,
        so every network-facing send should hold this lock.
        """
        lk = getattr(self, "_send_lock", None)
        if lk is None:
            lk = threading.Lock()
            # object.__setattr__ so dataclass-like subclasses still work.
            object.__setattr__(self, "_send_lock", lk)
        return lk

    @abstractmethod
    def send(self, to: str, text: str, *, account_id: str = "", **kw: Any) -> bool:
        """Deliver ``text`` to ``to`` through this channel.

        ``account_id`` identifies *which bot identity* should originate
        the reply, for channels that hold multiple sessions under a
        single ``Channel`` instance (currently WeChat — one ``iLink_bot_id``
        per scanned account). Single-identity channels (CLI, WeCom)
        ignore the parameter. Callers that route replies for a specific
        inbound should pass ``inbound.account_id`` so the same bot that
        received the message is the one that replies.
        """
        ...

    def send_image(
        self, to: str, image_data: bytes, caption: str = "",
        *, account_id: str = "", **kw: Any,
    ) -> bool:
        """Send an image. Subclasses override; default is no-op."""
        return False

    def send_file(
        self, to: str, file_data: bytes, filename: str = "",
        caption: str = "", *, account_id: str = "", **kw: Any,
    ) -> bool:
        """Send a file. Subclasses override; default is no-op."""
        return False

    def release_inbound(self, inbound_id: str) -> None:
        """Release any per-frame state cached under ``inbound_id``.

        Called by :func:`send_with_retry` once every chunk of a reply has
        been dispatched. Channels that cache inbound frames (currently only
        :class:`pip_agent.channels.wecom.WecomChannel`) override this to
        drop the entry so the pending-frames table doesn't grow without
        bound. Default: no-op.
        """
        return None

    # ------------------------------------------------------------------
    # Optional progressive-reply API
    # ------------------------------------------------------------------
    #
    # Channels that can render incremental updates within a single reply
    # bubble (currently only WeCom via ``reply_stream``) override the
    # three methods below. Default implementations return ``None`` /
    # ``False`` so the caller can detect non-support and fall back to the
    # one-shot :func:`send_with_retry` path. The contract:
    #
    # * ``start_stream`` — open a reply stream toward ``to`` for the
    #   inbound identified by ``inbound_id``; return an opaque handle
    #   the caller threads back into ``update_stream``/``finish_stream``,
    #   or ``None`` when streaming isn't available.
    # * ``update_stream`` — replace the in-flight reply body with
    #   ``text`` (full snapshot, not delta). Safe to call repeatedly.
    # * ``finish_stream`` — final replace + close. Channels release any
    #   per-inbound state at this point.
    #
    # All three are blocking; callers from an asyncio loop should wrap
    # invocations in ``asyncio.to_thread`` to keep the loop responsive.
    def start_stream(
        self, to: str, *, inbound_id: str = "", account_id: str = "",
    ) -> str | None:
        return None

    def update_stream(
        self, to: str, handle: str, text: str,
        *, inbound_id: str = "", account_id: str = "",
    ) -> bool:
        return False

    def finish_stream(
        self, to: str, handle: str, text: str,
        *, inbound_id: str = "", account_id: str = "",
    ) -> bool:
        return False

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# send_with_retry — retry + backoff wrapper for Channel.send
# ---------------------------------------------------------------------------

BACKOFF_SCHEDULE = [2.0, 5.0, 15.0]  # seconds per retry attempt


def send_with_retry(
    ch: Channel,
    to: str,
    text: str,
    *,
    inbound_id: str = "",
    account_id: str = "",
) -> bool:
    """Chunk *text* per channel limits, then send each chunk with retries.

    Returns True only if every chunk was delivered.  CLI channels skip
    chunking and retry (stdout never fails). Holds ``ch.send_lock`` so that
    concurrent callers (one per lane) never interleave chunks or trigger
    races inside the underlying HTTP / WebSocket client.

    ``inbound_id`` (optional) identifies the original inbound frame the
    reply belongs to, for channels that need the frame to thread a
    reply back to the right message (e.g. WeCom ``reply_stream``). See
    :meth:`pip_agent.channels.wecom.WecomChannel.send` for why keying
    pending frames by ``peer_id`` is unsafe under concurrency. Unused by
    CLI / WeChat.

    ``account_id`` (optional) picks which bot identity should originate
    the reply when the channel holds multiple. WeChat needs this because
    one ``WeChatChannel`` instance wraps N scanned accounts and the
    reply must go through the same bot that received the inbound.
    Single-identity channels ignore the parameter.
    """
    # PROFILE
    from pip_agent import _profile

    if ch.name == "cli":
        with _profile.span_sync(
            "channel.send_with_retry", channel=ch.name, chunks=1, text_len=len(text),
        ), ch.send_lock:
            return ch.send(to, text, account_id=account_id)

    from pip_agent.fileutil import chunk_message

    chunks = chunk_message(text, ch.name)
    all_ok = True
    retries = 0
    with _profile.span_sync(
        "channel.send_with_retry",
        channel=ch.name,
        chunks=len(chunks),
        text_len=len(text),
    ), ch.send_lock:
        for chunk in chunks:
            ok = ch.send(to, chunk, inbound_id=inbound_id, account_id=account_id)
            if ok:
                continue
            for delay in BACKOFF_SCHEDULE:
                retries += 1
                jitter = delay * 0.2 * (random.random() - 0.5)
                time.sleep(delay + jitter)
                ok = ch.send(to, chunk, inbound_id=inbound_id, account_id=account_id)
                if ok:
                    break
            if not ok:
                all_ok = False
                log.warning(
                    "send_with_retry: gave up after %d retries to %s on %s",
                    len(BACKOFF_SCHEDULE), to, ch.name,
                )
        if retries:  # PROFILE
            _profile.event(
                "channel.send_retries", channel=ch.name, retries=retries,
            )
    if inbound_id:
        # Drop the cached inbound frame now that every chunk has been
        # attempted. Channels without per-frame state override this to
        # a no-op.
        try:
            ch.release_inbound(inbound_id)
        except Exception:
            log.exception("release_inbound failed on %s", ch.name)
    return all_ok


# ---------------------------------------------------------------------------
# ChannelManager
# ---------------------------------------------------------------------------

class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel
        # Route through the host_io shim so TUI mode lights up the
        # status bar instead of writing escape-corrupted text into the
        # canvas. Line mode falls back to the historical print line.
        from pip_agent.host_io import emit_channel_ready

        emit_channel_ready(channel.name)

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)

    def list_channels(self) -> list[str]:
        return list(self.channels.keys())

    def close_all(self) -> None:
        for ch in self.channels.values():
            try:
                ch.close()
            except Exception as exc:
                log.warning("error closing channel %s: %s", ch.name, exc)
