"""Multi-channel host for Pip-Boy.

Routes inbound messages from CLI / WeChat / WeCom through the Claude Agent
SDK, manages per-session state, and dispatches replies back to the originating
channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pip_agent import host_commands
from pip_agent.agent_runner import QueryResult, run_query
# Tier 3 cold-start: only import the light channel primitives at module
# import time. Wechat / wecom pull ``aibot`` and ``aiohttp`` (~450 ms +
# ~180 ms on a warm machine); CLI-only launches never need them, and
# even wecom-only launches don't need wechat. The heavy channel
# symbols are imported lazily at their use sites below.
from pip_agent.channels import (
    Channel,
    ChannelManager,
    CLIChannel,
    InboundMessage,
    send_with_retry,
)
from pip_agent.config import WORKDIR, settings
from pip_agent.host_scheduler import HostScheduler
from pip_agent.mcp_tools import McpContext
from pip_agent.memory import MemoryStore
from pip_agent.memory.transcript_source import locate_session_jsonl
from pip_agent.routing import (
    AgentPaths,
    AgentRegistry,
    Binding,
    BindingTable,
    build_session_key,
    normalize_agent_id,
    resolve_effective_config,
)
from pip_agent.streaming_session import StaleSessionError, StreamingSession

log = logging.getLogger(__name__)

# Workspace-level paths (v2 layout). All per-agent paths are resolved
# through ``AgentRegistry.paths_for`` — nothing in the host except
# ``run_host`` boot should reach for ``WORKDIR`` directly.
WORKSPACE_PIP_DIR = WORKDIR / ".pip"
BINDINGS_PATH = WORKSPACE_PIP_DIR / "bindings.json"
SESSION_STORE_PATH = WORKSPACE_PIP_DIR / "sdk_sessions.json"

# Inbound file/image bytes get dropped under each agent's own
# ``.pip/incoming/`` so (a) the LLM can reach them with its native
# ``Read`` / ``Bash`` tools via a cwd-relative path, and (b) agents
# don't clobber each other's uploads. The "incoming" directory name
# itself lives on ``AgentPaths.incoming_dir``.
_MAX_INCOMING_BYTES = 50 * 1024 * 1024  # 50 MB — matches send_file cap


@dataclass(slots=True)
class FlushSummary:
    """Outcome of :meth:`AgentHost.flush_and_rotate`.

    Exists so the caller (today: the CLI ``/exit`` path) can print an
    *honest* status line instead of the old unconditional "reflecting…"
    message, which fired even when there was literally nothing in
    ``_sessions`` to reflect. See `tests/test_reply_dispatch.py::
    TestFlushAndRotate` for the contract.

    Fields:

    * ``rotated`` — sessions that were dropped from the in-memory map.
      This is the number that matters for correctness of the next
      launch ("did we actually clear state?").
    * ``reflected`` — sessions where ``reflect_and_persist`` was
      *invoked* (client present AND transcript file located). A value
      less than ``rotated`` means some sessions were rotated without
      reflect — either the JSONL was gone or credentials were missing.
    * ``observations`` — total observations written across all
      reflected sessions. Zero is a valid outcome (Q7 zero-delta
      guard short-circuits without an LLM call).
    """

    rotated: int = 0
    reflected: int = 0
    observations: int = 0


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def _load_sessions() -> dict[str, str]:
    if not SESSION_STORE_PATH.is_file():
        return {}
    try:
        return json.loads(SESSION_STORE_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sessions(sessions: dict[str, str]) -> None:
    # ``atomic_write`` (tmp + fsync + os.replace) prevents a crashed or
    # power-cut write from leaving a half-written JSON blob on disk.
    # ``_load_sessions`` swallows ``JSONDecodeError`` and returns ``{}``,
    # so a partial write silently wipes every agent's session binding —
    # exactly the kind of failure this path must not have.
    from pip_agent.fileutil import atomic_write

    atomic_write(
        SESSION_STORE_PATH,
        json.dumps(sessions, indent=2, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Prompt shaping
# ---------------------------------------------------------------------------


_LEADING_AT_RE = re.compile(r"^@\S*\s*")

# Sentinel sender ids set by :class:`HostScheduler` when it injects a timed
# message. Kept in sync with ``host_scheduler._Sender``.
_CRON_SENDER = "__cron__"
_HEARTBEAT_SENDER = "__heartbeat__"


def _is_ephemeral_sender(sender_id: str) -> bool:
    """True if inbound must NOT participate in SDK session persistence.

    Scheduler-injected senders (``__cron__`` / ``__heartbeat__``) are
    stateless by design — they're background keepalives / scheduled
    probes, not conversation. Giving them a ``session_id`` causes the
    SDK to (a) load the full user transcript on every tick and ship
    it through the proxy on each cold start, and (b) *append* their
    scaffolding back into that transcript, which then feeds (a) next
    time. With 30 s cron + Opus via a slow proxy this is the single
    biggest driver of "pip-boy got slow over the day".

    If you're extending the scheduler with a new synthetic sender,
    add it here. User / channel messages are never ephemeral.
    """
    return sender_id in (_CRON_SENDER, _HEARTBEAT_SENDER)


@dataclass
class _NullTracked:
    """Placeholder for the ``scheduler.track`` yielded value when no
    scheduler is wired in (test harnesses, a future lean-mode host).

    Keeps :meth:`AgentHost.process_inbound` uniform: it always has a
    ``tracked.failure()`` call site, regardless of whether the scheduler is
    actually present. Without this sentinel, every call site would need a
    separate ``if scheduler is not None`` branch and we'd be right back
    to implicit-contract territory.
    """

    def failure(self, message: str = "") -> None:
        return None


def _batch_group_key(m: InboundMessage) -> tuple[str, str, str, str, str, bool]:
    """Identifier for "same logical conversation" used by Tier 2 batching.

    Two messages only coalesce when every field here matches. We
    intentionally include ``agent_id`` and ``guild_id`` so a group
    DM and a 1-on-1 DM from the same sender stay separate, and
    different bot accounts never cross-talk.
    """
    return (
        m.channel,
        m.sender_id,
        m.peer_id,
        m.guild_id,
        m.agent_id,
        m.is_group,
    )


def _batch_eligible(m: InboundMessage) -> bool:
    """Tier 2 filter: may ``m`` participate in text coalescing?

    See the ``batch_text_inbounds`` docstring in ``config.py`` for the
    policy rationale. The quick form:

    * no attachments (preserve multimodal ordering)
    * no scheduler marker (heartbeat / cron stay individual)
    * non-empty text that is NOT a host slash command
    """
    if m.attachments:
        return False
    if m.source_job_id:
        return False
    text = m.text.strip()
    if not text:
        return False
    if text.startswith("/"):
        return False
    return True


def _coalesce_text_inbounds(
    batch: list[InboundMessage],
    joiner: str,
) -> tuple[list[InboundMessage], int]:
    """Fuse contiguous same-conversation text-only inbounds into one.

    Walks ``batch`` once (O(n)) preserving FIFO order. A message is
    merged into the previous one when both pass :func:`_batch_eligible`
    and share the same :func:`_batch_group_key`. The merged message
    keeps all fields of the *first* (earliest) inbound except
    ``text``, which becomes ``joiner.join([first.text, …, last.text])``.

    Returns ``(new_batch, fused_count)`` where ``fused_count`` is the
    number of messages absorbed (``0`` means no coalescing happened,
    so the caller can short-circuit any profile emit).

    Why concatenate instead of replay-as-history? The LLM already
    sees prior turns via the streaming session. Splitting a single
    train of thought across 3 turns would *waste* 2 LLM round trips
    without adding information — concatenation gives the same
    information in one trip.
    """
    if len(batch) < 2:
        return batch, 0

    out: list[InboundMessage] = []
    fused = 0
    for m in batch:
        if not out:
            out.append(m)
            continue
        prev = out[-1]
        if (
            _batch_eligible(prev)
            and _batch_eligible(m)
            and _batch_group_key(prev) == _batch_group_key(m)
        ):
            # Build a new InboundMessage rather than mutating ``prev``
            # in place — ``prev`` might still be held by someone (e.g.
            # the caller's sort list). Cheap dataclass copy.
            merged = InboundMessage(
                text=(prev.text + joiner + m.text) if prev.text else m.text,
                sender_id=prev.sender_id,
                channel=prev.channel,
                peer_id=prev.peer_id,
                guild_id=prev.guild_id,
                account_id=prev.account_id,
                is_group=prev.is_group,
                agent_id=prev.agent_id,
                # Keep the EARLIEST raw — the reply routes off ``peer_id``
                # anyway and the raw blob is only used for debugging /
                # attachment backfill.
                raw=prev.raw,
                attachments=[],
                source_job_id="",
            )
            out[-1] = merged
            fused += 1
        else:
            out.append(m)
    return out, fused


def _inbound_sort_key(inbound: InboundMessage) -> tuple[int, int]:
    """Sort key that bubbles user / channel messages ahead of scheduler ones.

    Order within each tier is stable (all zeros), so FIFO is preserved.
    Tiers:

    * 0 — human- or channel-originated (CLI users, WeChat, WeCom).
    * 1 — cron jobs.
    * 2 — heartbeats (lowest priority — they are background keepalives
      whose only job is to exist *when nothing else is happening*).
    """
    if inbound.sender_id == _HEARTBEAT_SENDER:
        return (2, 0)
    if inbound.sender_id == _CRON_SENDER:
        return (1, 0)
    return (0, 0)

# "Nothing to report" sentinel documented in ``scaffold/heartbeat.md``. When the
# heartbeat reply matches this (case-insensitive, tolerant of common wrappers
# the model might add), we treat it as a quiet confirmation and skip delivery.
# Any other heartbeat reply — proactive greeting, reminder, found-an-issue
# message — flows through the normal dispatch path.
_HEARTBEAT_OK_RE = re.compile(
    r"^[\s`'\".]*heartbeat[_\s-]*ok[\s`'\".!]*$",
    re.IGNORECASE,
)


def _agent_id_from_session_key(sk: str) -> str:
    """Return the ``agent_id`` component of a session key, or ``""``.

    Session keys are built by :func:`routing.build_session_key` with an
    ``"agent:<agent_id>:..."`` prefix. This helper is a minimal reverse —
    if the format ever diverges from ``agent:*`` it returns empty rather
    than raise, so callers can log and skip instead of bringing down the
    shutdown path.
    """
    if not sk.startswith("agent:"):
        return ""
    parts = sk.split(":", 2)
    if len(parts) < 2:
        return ""
    return parts[1]


def _format_text_prompt(
    inbound: InboundMessage,
    memory_store: MemoryStore | None,
) -> str:
    """Text-only prompt rendering. See :func:`_format_prompt`.

    Dispatch order:

    1. **Scheduler-injected sentinels** — ``__cron__`` / ``__heartbeat__`` are
       wrapped in ``<cron_task>`` / ``<heartbeat>`` regardless of channel, so
       the agent can distinguish them from user messages even when the
       originating channel is ``cli``.
    2. **CLI messages** pass through bare (matches how developers type).
    3. **Remote-channel messages** get ``<user_query>`` XML with sender
       identity so the agent sees who is talking.
    """
    clean_text = _LEADING_AT_RE.sub("", inbound.text, count=1)

    if inbound.sender_id == _CRON_SENDER:
        return f"<cron_task>\n{clean_text}\n</cron_task>"
    if inbound.sender_id == _HEARTBEAT_SENDER:
        return f"<heartbeat>\n{clean_text}\n</heartbeat>"

    if inbound.channel == "cli":
        return clean_text

    sender_status = "unverified"
    if memory_store and inbound.sender_id:
        profile = memory_store.find_profile_by_sender(
            inbound.channel, inbound.sender_id,
        )
        if profile:
            name = memory_store.extract_profile_name(profile)
            sender_status = f"verified:{name}" if name else "verified"

    if inbound.is_group:
        return (
            f'<user_query from="{inbound.channel}:{inbound.sender_id}"'
            f' status="{sender_status}" group="true">'
            f"\n{clean_text}\n</user_query>"
        )
    if inbound.sender_id:
        return (
            f'<user_query from="{inbound.channel}:{inbound.sender_id}"'
            f' status="{sender_status}">'
            f"\n{clean_text}\n</user_query>"
        )
    return f"<user_query>\n{clean_text}\n</user_query>"


_SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    """Strip path separators and other filename-hostile chars.

    Only the basename survives — any caller-supplied path component is
    dropped. Result is clamped to 120 chars so pathologically long
    names don't blow past Windows MAX_PATH when combined with the
    workdir + timestamp prefix.
    """
    import os
    base = os.path.basename(name or "").strip()
    base = _SAFE_FILENAME_RE.sub("_", base)
    if not base:
        base = "file"
    return base[:120]


def _materialize_attachments(
    inbound: InboundMessage,
    *,
    workdir: Path,
    incoming_dir: Path,
) -> None:
    """Persist binary attachment bytes under ``incoming_dir`` in-place.

    Sets ``Attachment.saved_path`` to a **workdir-relative** POSIX
    path so the prompt renderer can hand the LLM a location its
    native ``Read`` / ``Bash`` tools (which inherit ``cwd=workdir``)
    can follow directly — ``unzip -l`` / ``file`` / ``grep`` etc.

    Conventionally ``incoming_dir`` is
    ``{agent_cwd}/.pip/incoming`` — one inbox per agent so two
    agents on the same channel can't trample each other's uploads,
    and ``/subagent reset`` for one agent doesn't affect another's
    pending files. Placing it under the per-agent dir also
    matches how memory, cron, and user profiles already partition
    state. Decoupled from ``workdir`` as a parameter so tests and
    future re-targeting (e.g. TTL sweep) don't need module patches.

    Mutates attachments on ``inbound``; returns nothing. Called from
    :meth:`Host.process_inbound` before prompt formatting, so
    :func:`_format_prompt` stays pure.

    Why disk, not inline
    --------------------
    Vision blocks cover images. Everything else — zips, docs, PDFs,
    audio the ASR layer didn't transcribe — is opaque to the model
    as bytes. The pre-SDK Pip-Boy and pipi both solved this by
    dropping bytes on disk in the agent's cwd and letting the LLM
    decide how to unpack them. This restores that: no custom
    per-format tools, just a path. Skips attachments with no bytes
    (``data is None``) and files that decoded cleanly to UTF-8
    (``text`` is already populated — inline rendering is cheaper).
    """
    import time

    if not inbound.attachments:
        return

    ts_prefix = time.strftime("%Y%m%d-%H%M%S")

    for i, att in enumerate(inbound.attachments):
        if att.saved_path or not att.data:
            continue
        # Text-file attachments already inline cheaply via ``att.text``;
        # no need to occupy disk with a second copy the model won't read.
        if att.type == "file" and att.text:
            continue
        if len(att.data) > _MAX_INCOMING_BYTES:
            log.warning(
                "attachment skipped (%d bytes exceeds cap): %s",
                len(att.data), att.filename or att.type,
            )
            continue

        if att.type == "image":
            ext = ""
            if att.mime_type == "image/jpeg":
                ext = ".jpg"
            elif att.mime_type == "image/png":
                ext = ".png"
            elif att.mime_type == "image/gif":
                ext = ".gif"
            elif att.mime_type == "image/webp":
                ext = ".webp"
            safe = _sanitize_filename(att.filename) if att.filename else f"image-{i}{ext}"
            if ext and not safe.lower().endswith(ext):
                safe = f"{safe}{ext}"
        else:
            safe = _sanitize_filename(att.filename or f"{att.type}-{i}")

        try:
            incoming_dir.mkdir(parents=True, exist_ok=True)
            dest = incoming_dir / f"{ts_prefix}-{safe}"
            dest.write_bytes(att.data)
            # Always use POSIX-style separators in what we hand the
            # LLM — the model will echo this path into shell commands
            # (Bash is bundled with CC even on Windows), and mixed
            # backslashes break word-splitting there.
            rel = dest.relative_to(workdir).as_posix()
            att.saved_path = rel
        except (OSError, ValueError) as exc:
            # ``ValueError`` from ``relative_to`` covers the pathological
            # case of ``incoming_dir`` sitting outside ``workdir`` —
            # shouldn't happen with our defaults but don't crash the
            # turn over a paths misconfiguration.
            log.warning(
                "could not materialize attachment %s: %s",
                att.filename or att.type, exc,
            )


def _format_prompt(
    inbound: InboundMessage,
    memory_store: MemoryStore | None,
) -> str | list[dict[str, Any]]:
    """Build the user-visible prompt from an InboundMessage.

    Returns a plain string for pure-text messages (the common case) and
    an Anthropic-style content-block list when the inbound carries
    attachments. Callers (specifically :func:`run_query`) must accept
    either shape.

    Why we keep both shapes instead of always returning blocks
    ----------------------------------------------------------
    1. The SDK's string path is a single stdin line; the block path is
       a streaming-mode ``AsyncIterable`` envelope. String mode is the
       simpler code path on both sides — when there's nothing to gain
       from blocks, we don't pay the complexity.
    2. Heartbeat / cron inbounds never carry attachments but *do* carry
       sentinel tags (``<heartbeat>``, ``<cron_task>``) that the LLM
       uses to route. A block-list with one text block would work, but
       a bare string is what existing transcripts and tests assume,
       and there's no benefit to churning them.

    Non-image attachments
    ---------------------
    Image bytes become base64 image blocks. ``file`` attachments with
    extracted text become ``<attached-file>`` wrappers so the LLM sees
    the content inline. Binary ``file`` attachments and ``voice``
    attachments become descriptive text markers (the channel has
    already done ASR for voice).
    """
    import base64

    text = _format_text_prompt(inbound, memory_store)

    if not inbound.attachments:
        return text

    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})

    for att in inbound.attachments:
        if att.type == "image" and att.data:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": att.mime_type or "image/jpeg",
                    "data": base64.b64encode(att.data).decode("ascii"),
                },
            })
        elif att.type == "image":
            # Image arrived but the channel couldn't fetch bytes —
            # preserve the placeholder rather than drop it silently.
            blocks.append({"type": "text", "text": att.text or "[Image]"})
        elif att.type == "file" and att.text:
            blocks.append({
                "type": "text",
                "text": (
                    f'<attached-file name="{att.filename or "unknown"}">'
                    f"\n{att.text}\n</attached-file>"
                ),
            })
        elif att.type == "file" and att.saved_path:
            # Binary file materialized to disk — hand the model a
            # workdir-relative path so its native tools (Read, Bash
            # ``unzip``/``file``, Glob) can take it from here. Size
            # is advisory; the extension in the filename is what
            # usually triggers the right unpacking strategy.
            size_hint = f"{len(att.data)} bytes" if att.data else "unknown size"
            blocks.append({
                "type": "text",
                "text": (
                    f"[File: {att.filename or 'unknown'}] "
                    f"saved to {att.saved_path} ({size_hint}). "
                    "Use your Read/Bash tools to inspect it "
                    "(e.g. `unzip -l` for archives)."
                ),
            })
        elif att.type == "file":
            blocks.append({
                "type": "text",
                "text": (
                    f"[File: {att.filename or 'unknown'}] "
                    "(binary, not inlined)"
                ),
            })
        elif att.type == "voice":
            blocks.append({
                "type": "text",
                "text": (
                    f"[Voice transcription]: {att.text}"
                    if att.text else "[Voice message]"
                ),
            })

    # Defensive: if every attachment was unrenderable (empty image, no
    # bytes, no text), fall back to plain text so the LLM still sees
    # the original message.
    return blocks if blocks else text


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


@dataclass
class _PerAgent:
    """Per-agent lazily-created service objects."""

    memory_store: MemoryStore
    paths: AgentPaths


@dataclass(slots=True)
class _PreparedTurn:
    """Value bundle returned by :meth:`AgentHost._prepare_turn`.

    Carries the state that :meth:`AgentHost._execute_turn` needs from
    the pre-processing phase without re-plumbing 7 positional arguments
    through every helper. Intentionally kept private — callers outside
    ``agent_host`` have no business assembling one.
    """

    eff: Any  # resolve_effective_config return (AgentConfig-shaped)
    svc: _PerAgent
    sk: str
    ch: Channel | None
    reply_peer: str
    prompt: Any  # str | list (multimodal content)
    system_prompt: str
    paths: AgentPaths


class AgentHost:
    """Multi-channel host driving the SDK agent for every inbound message."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        binding_table: BindingTable,
        channel_mgr: ChannelManager,
        scheduler: HostScheduler | None = None,
    ) -> None:
        self._registry = registry
        self._binding_table = binding_table
        self._channel_mgr = channel_mgr
        self._scheduler = scheduler

        self._sessions = _load_sessions()
        self._agents: dict[str, _PerAgent] = {}

        # Concurrency-control layers (post-Tier-1). Three distinct caps
        # guard three distinct failure modes:
        #
        # * ``_session_locks`` — one ``asyncio.Lock`` per session key,
        #   created on first use. Guarantees that two messages targeting
        #   the *same* session run sequentially. The canonical case this
        #   fixes is a group chat: members A and B reply to the bot at the
        #   same instant, both resolve to the same
        #   ``agent:pip:wecom:peer:<gid>`` session key, and without the
        #   lock their turns can interleave — both resume the SAME
        #   ``session_id``, then race to write it back. One of the two
        #   sessions gets lost. This lock is taken by every turn
        #   (streaming and one-shot alike).
        #
        # * ``_streaming_lock`` (below) — serialises NEW streaming-client
        #   spawns across all sessions. Because ``StreamingSession.connect()``
        #   is ~400–1000 ms (CC subprocess + MCP handshake), two concurrent
        #   first-turns on different peers would otherwise race the
        #   spawn; serialising them also means a "startup storm" (10
        #   peers saying hi at once) becomes 10 sequential connects
        #   instead of 10 concurrent ones — predictable RAM/CPU profile
        #   instead of spiking. Reuse path (subsequent turns on a cached
        #   session) does NOT take this lock.
        #
        # * ``_one_shot_semaphore`` — bounds concurrency on the fallback
        #   ``run_query`` code path (cron, heartbeat, anything with
        #   ``enable_streaming_session=false``). Each of those DOES spawn
        #   a fresh CC subprocess per turn, so an unbounded burst would
        #   torch RAM. Default 3 is plenty: cron/heartbeat fire at most
        #   once per 30 min each, so contention here is rare.
        #
        # Historical note (migration from one-shot-only world): a single
        # ``_semaphore(3)`` used to wrap *every* turn, which correctly
        # bounded subprocess spawns when every turn spawned one. After
        # Tier 1 made streaming turns reuse a long-lived subprocess, that
        # outer semaphore degenerated into "global cap on simultaneous
        # active streaming turns" — a bottleneck unrelated to the RAM
        # concern it was supposed to guard. With the split below, streaming
        # turns are capped by ``_streaming_lock`` (spawn rate) and
        # ``stream_max_live`` (live-session count), and one-shot turns
        # keep their own dedicated semaphore.
        #
        # Locks are never cleaned up: a few hundred bytes per distinct
        # session key, and the key space is bounded by the number of
        # peers we ever meet — negligible vs. the JSONL / memory-store
        # data we already persist per session.
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._one_shot_max_concurrent = 3
        self._one_shot_semaphore = asyncio.Semaphore(
            self._one_shot_max_concurrent,
        )

        # Tier 1: streaming-session cache. One ``StreamingSession`` per
        # session key keeps the CC subprocess alive across turns so we
        # only pay the ~400 ms spawn + handshake once per session. Guarded
        # by a dict-level lock (NOT a per-key one) because the expensive
        # operation — ``StreamingSession.connect()`` — needs to run once
        # per (session_key, process) and we don't want two concurrent
        # first-turns on the same key to both spawn a client. Per-session
        # locks (``_session_locks`` above) serialise actual turn dispatch
        # and are acquired by the caller (``_execute_turn``) AFTER this
        # create step returns, so holding ``_streaming_lock`` during
        # connect is fine.
        #
        # Eviction: idle sweep runs once per tick of the background
        # ``_streaming_idle_sweep`` task (started in :meth:`start_idle_sweep`),
        # closing any session whose ``last_used_ns`` is older than
        # ``settings.stream_idle_ttl_sec``. Max-live cap drops the oldest
        # idle session when a new create would exceed the limit.
        self._streaming_sessions: dict[str, StreamingSession] = {}
        self._streaming_lock = asyncio.Lock()
        self._streaming_sweep_task: asyncio.Task[None] | None = None

        # Tier 2 lock-time coalescing.
        #
        # Drain-time ``_coalesce_text_inbounds`` only catches messages
        # that arrive in the same drain tick. For the common real-world
        # pattern "user fires 3 messages ~1s apart while the first turn
        # is still running", each message is drained alone and each
        # spawns its own ``_execute_turn`` coroutine, all queueing on
        # the same session lock. Result: N turns, N LLM calls, no
        # fusion — exactly the workload Tier 2 was meant to compress.
        #
        # Fix: a second fusion point keyed on the session lock. As each
        # ``_execute_turn`` enters, if another turn is already claimed
        # for this session_key AND the inbound is batch-eligible, the
        # inbound is parked in ``_pending_per_session[sk]`` instead of
        # running. When the active turn is about to push user text to
        # the SDK (inside ``_run_turn_streaming``, after lock acquired),
        # it pops pending[sk] and fuses them into a single merged
        # prompt. Late arrivals that land AFTER the pop are flushed as
        # a follow-up turn in ``_release_or_flush_session``.
        #
        # Invariants protected by ``_pending_lock``:
        # 1. sk in ``_session_active`` ⇔ some coroutine is claimed to
        #    run a text-batchable turn for sk (either currently
        #    executing or being spawned as a leftover flush).
        # 2. Every message appended to ``_pending_per_session[sk]`` is
        #    batch-eligible at append time — we never mix attachments
        #    or slash commands into the pending pool.
        # 3. On release, pending[sk] is drained into a merged leftover
        #    turn (keeping active set) OR active is cleared (no
        #    leftovers). No path leaves active=True with pending=[].
        self._pending_lock = asyncio.Lock()
        self._session_active: set[str] = set()
        self._pending_per_session: dict[str, list[InboundMessage]] = {}
        # Leftover-flush tasks are fire-and-forget from the claimant's
        # POV but must be drained on shutdown so reflect / memory
        # writes aren't truncated mid-flight. See
        # :meth:`drain_lock_flush_tasks` called from ``run_host``.
        self._lock_flush_tasks: set[asyncio.Task[None]] = set()

    def _get_session_lock(self, sk: str) -> asyncio.Lock:
        """Return the per-session lock, creating it lazily."""
        # asyncio runs on a single thread — no extra guard needed for
        # the dict mutation.
        lock = self._session_locks.get(sk)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[sk] = lock
        return lock

    def _get_agent_services(self, agent_id: str) -> _PerAgent:
        if agent_id not in self._agents:
            paths = self._resolve_paths(agent_id)
            ms = MemoryStore(
                agent_dir=paths.pip_dir,
                workspace_pip_dir=paths.workspace_pip_dir,
                agent_id=paths.agent_id,
            )
            self._agents[agent_id] = _PerAgent(memory_store=ms, paths=paths)
        return self._agents[agent_id]

    def invalidate_agent_cache(self, agent_id: str) -> None:
        """Drop a per-agent service + its session rows.

        Called by lifecycle commands (``delete``, ``archive``, ``reset``)
        after they mutate the agent's on-disk state. Without this, the
        cached ``MemoryStore`` keeps writing to the wiped/relocated
        ``.pip/`` on the next save_state/reflect, resurrecting files
        like ``state.json`` the user explicitly deleted. Removing the
        session rows (both in-memory and in ``sdk_sessions.json``) also
        ensures ``flush_and_rotate`` on ``/exit`` won't try to reflect
        an agent that no longer exists.
        """
        self._agents.pop(agent_id, None)
        prefix = f"agent:{agent_id}:"
        dropped = [sk for sk in self._sessions if sk.startswith(prefix)]
        if dropped:
            for sk in dropped:
                self._sessions.pop(sk, None)
            _save_sessions(self._sessions)

    def _resolve_paths(self, agent_id: str) -> AgentPaths:
        """Resolve an agent's filesystem paths, auto-provisioning the root.

        The registry is authoritative, but we still want a sensible
        fallback when some code path hands us an unknown agent id (e.g.
        a binding that references an agent whose directory was moved
        outside of ``/subagent archive``). Returning root-level paths in
        that edge case keeps the turn alive instead of 500-ing.
        """
        paths = self._registry.paths_for(agent_id)
        if paths is not None:
            return paths
        default = self._registry.paths_for(self._registry.default_agent().id)
        if default is not None:
            return default
        # Degenerate last-resort: no workspace configured (unit tests).
        return AgentPaths(
            agent_id=agent_id,
            cwd=WORKDIR,
            pip_dir=WORKSPACE_PIP_DIR,
            workspace_pip_dir=WORKSPACE_PIP_DIR,
            kind="root",
        )

    def _reap_stale_session(
        self, sk: str, *, prefer_cwd: Path | None = None,
    ) -> str | None:
        """Return the session id for ``sk`` iff its JSONL is still on disk.

        A user can delete the JSONL out from under us — either by hand
        (happens) or by CC's own ``/clear`` (also happens). The in-memory
        ``_sessions`` map has no way to know the id went dead. Passing a dead
        id into ``run_query(resume=...)`` makes the CC subprocess print
        ``No conversation found with session ID: <uuid>``, exit 1, and the
        SDK surfaces that as a ``ClaudeSDKError`` that kills the whole turn.
        From the user's seat: "我发一句你好就爆 fatal error".

        Pre-flight the glob once per turn. If the file is gone, drop the id
        from the map and persist, so this turn (and subsequent ones) start
        fresh. ``locate_session_jsonl`` is a single directory glob — cheap
        even with a large ``projects/`` root.
        """
        sid = self._sessions.get(sk)
        if not sid:
            return None
        cwd = prefer_cwd or WORKDIR
        if locate_session_jsonl(sid, prefer_cwd=cwd) is not None:
            return sid
        log.warning(
            "Session %s for %s is missing on disk — starting fresh",
            sid, sk,
        )
        self._sessions.pop(sk, None)
        _save_sessions(self._sessions)
        return None

    # ------------------------------------------------------------------
    # Tier 1: streaming-session cache
    # ------------------------------------------------------------------

    async def _run_turn_streaming(
        self,
        *,
        session_key: str,
        prepared: _PreparedTurn,
        inbound: InboundMessage,
        mcp_ctx: McpContext,
        current_session_id: str | None,
    ) -> QueryResult:
        """Dispatch one non-ephemeral turn through the cached client.

        Implements the one-retry stale-session recovery documented in
        the Tier 4.2 note of the optimisation plan:

        * First attempt: get-or-create a ``StreamingSession`` for this
          key (creating one means we pay the spawn cost here, on this
          turn, rather than amortised over future turns).
        * If the CC server reports the session id is gone
          (``StaleSessionError``), drop the cached client + wipe the
          persisted id, then try ONCE more with a fresh connection and
          ``resume=None``. Second failure surfaces as a normal error
          path (caller handles).
        """
        from pip_agent import _profile

        try:
            session = await self._get_or_create_streaming_session(
                session_key=session_key,
                prepared=prepared,
                mcp_ctx=mcp_ctx,
                resume_session_id=current_session_id,
            )
        except Exception:
            log.exception(
                "stream %s: failed to open streaming client — "
                "falling back to one-shot run_query for this turn",
                session_key,
            )
            _profile.event(
                "stream.create_failed",
                session_key=session_key,
            )
            return await run_query(
                prompt=prepared.prompt,
                mcp_ctx=mcp_ctx,
                model=prepared.eff.effective_model,
                session_id=current_session_id,
                system_prompt_append=prepared.system_prompt,
                cwd=prepared.paths.cwd,
                stream_text=True,
            )

        # Tier 2 lock-time coalescing — final fusion point.
        #
        # The ``_execute_turn`` gate parked any same-session text
        # inbounds that arrived while this turn was queued for
        # dispatch in ``_pending_per_session[session_key]``. With the
        # session lock now held, drain that bucket and fuse everything
        # into one merged prompt. This is the only path that actually
        # COMPRESSES N rapid-fire messages into 1 LLM call — the
        # drain-time pass in ``run_host`` only catches messages that
        # hit the same ``queue_drain`` tick, which loses any user
        # typing faster than the drain cadence.
        effective_prompt = prepared.prompt
        if (
            settings.batch_text_inbounds
            and _batch_eligible(inbound)
        ):
            async with self._pending_lock:
                late = self._pending_per_session.pop(session_key, None)
            if late:
                fused_batch, fused = _coalesce_text_inbounds(
                    [inbound, *late],
                    settings.batch_text_joiner,
                )
                # By construction every entry in ``late`` + the current
                # ``inbound`` is batch-eligible and shares ``session_key``,
                # so ``_coalesce_text_inbounds`` collapses to exactly one
                # merged inbound. Fall through unchanged if that
                # assumption ever breaks (prompt-rebuild would be wrong).
                if len(fused_batch) == 1:
                    merged_inbound = fused_batch[0]
                    effective_prompt = _format_prompt(
                        merged_inbound, prepared.svc.memory_store,
                    )
                    _profile.event(
                        "host.batch_coalesced",
                        source="lock_time",
                        session_key=session_key,
                        before=len(late) + 1,
                        after=1,
                        fused=fused,
                        channel=inbound.channel,
                    )
                    log.info(
                        "Tier2 lock-time batch: fused %d late-arrivals "
                        "into current turn for %s", len(late), session_key,
                    )
                else:
                    log.warning(
                        "Tier2 lock-time fusion for %s produced %d merged "
                        "inbounds (expected 1) — using current prompt as-is",
                        session_key, len(fused_batch),
                    )

        try:
            result = await session.run_turn(
                effective_prompt,
                sender_id=inbound.sender_id,
                peer_id=mcp_ctx.peer_id,
                stream_text=True,
            )
        except StaleSessionError as exc:
            log.warning(
                "stream %s: stale CC session — retrying fresh (%s)",
                session_key, exc,
            )
            _profile.event(
                "stream.stale_detected",
                session_key=session_key,
                err=str(exc)[:160],
            )
            # Drop the dead session from the cache and from persistence.
            await self._evict_streaming_session(
                session_key, reason="stale_session",
            )
            self._sessions.pop(session_key, None)
            _save_sessions(self._sessions)
            # Rebuild with no resume id — fresh conversation on this key.
            session = await self._get_or_create_streaming_session(
                session_key=session_key,
                prepared=prepared,
                mcp_ctx=mcp_ctx,
                resume_session_id=None,
            )
            _profile.event(
                "stream.stale_recovered",
                session_key=session_key,
            )
            result = await session.run_turn(
                effective_prompt,
                sender_id=inbound.sender_id,
                peer_id=mcp_ctx.peer_id,
                stream_text=True,
            )
        return result

    async def _get_or_create_streaming_session(
        self,
        *,
        session_key: str,
        prepared: _PreparedTurn,
        mcp_ctx: McpContext,
        resume_session_id: str | None,
    ) -> StreamingSession:
        """Return a live ``StreamingSession`` for ``session_key``.

        Holds ``self._streaming_lock`` around the create path so two
        concurrent first-turns on the same key can't both spawn a
        client. Per-turn dispatch happens OUTSIDE this lock (each
        session owns its own ``_turn_lock``).
        """
        from pip_agent import _profile

        async with self._streaming_lock:
            existing = self._streaming_sessions.get(session_key)
            if existing is not None and not existing._closed:
                _profile.event(
                    "stream.reused",
                    session_key=session_key,
                    turns_so_far=existing.turn_count,
                    age_ms=round(
                        (time.perf_counter_ns() - existing.created_ns) / 1e6, 1,
                    ),
                )
                return existing

            # Max-live eviction: if we're at the cap, drop the stalest
            # idle session to make room. Exception: if ALL live sessions
            # are actively in the middle of a turn (very unusual —
            # implies >stream_max_live concurrent peers), we still
            # create the new one to avoid stalling the turn; the old
            # ones will expire via idle TTL on their own.
            if len(self._streaming_sessions) >= settings.stream_max_live:
                await self._evict_oldest_idle(reason="max_live_cap")

            # Resolve the right resume id — mirror _reap_stale_session
            # semantics on just this id to avoid attempting resume
            # against a JSONL that's been pruned on disk.
            effective_resume = resume_session_id
            if effective_resume and locate_session_jsonl(
                effective_resume, prefer_cwd=prepared.paths.cwd,
            ) is None:
                log.info(
                    "stream %s: resume id %s has no JSONL — starting fresh",
                    session_key, effective_resume,
                )
                _profile.event(
                    "stream.resume_jsonl_missing",
                    session_key=session_key,
                    sid=effective_resume,
                )
                effective_resume = None
                # Keep persistence in sync — the old id is toast.
                self._sessions.pop(session_key, None)
                _save_sessions(self._sessions)

            session = StreamingSession(
                session_key=session_key,
                mcp_ctx=mcp_ctx,
                model=prepared.eff.effective_model,
                cwd=prepared.paths.cwd,
                system_prompt_append=prepared.system_prompt,
                resume_session_id=effective_resume,
            )
            await session.connect()
            self._streaming_sessions[session_key] = session
            return session

    async def _evict_streaming_session(
        self, session_key: str, *, reason: str,
    ) -> None:
        """Remove + close the session for ``session_key`` if present."""
        session = self._streaming_sessions.pop(session_key, None)
        if session is not None:
            await session.close(reason=reason)

    async def _evict_oldest_idle(self, *, reason: str) -> None:
        """Close the most-stale cached session. Caller holds ``_streaming_lock``.

        "Most stale" = largest ``now - last_used_ns`` among sessions
        whose per-session ``_turn_lock`` is not held. If every session
        is currently in a turn, we no-op — the idle sweep will catch
        them once they're back to idle.
        """
        now = time.perf_counter_ns()
        candidate: tuple[str, StreamingSession] | None = None
        candidate_age_ns = -1
        for sk, sess in self._streaming_sessions.items():
            # ``asyncio.Lock.locked()`` is safe to call from inside the
            # same loop — no blocking.
            if sess._turn_lock.locked():
                continue
            age = now - sess.last_used_ns
            if age > candidate_age_ns:
                candidate_age_ns = age
                candidate = (sk, sess)
        if candidate is None:
            log.info(
                "stream eviction (%s) skipped — all %d sessions busy",
                reason, len(self._streaming_sessions),
            )
            return
        sk, _ = candidate
        log.info(
            "stream eviction (%s): closing %s (idle %.1fs)",
            reason, sk, candidate_age_ns / 1e9,
        )
        await self._evict_streaming_session(sk, reason=reason)

    async def _idle_sweep_loop(self) -> None:
        """Background task: periodically close idle streaming sessions."""
        from pip_agent import _profile

        ttl_ns = settings.stream_idle_ttl_sec * 1_000_000_000
        # Sweep cadence is deliberately coarse: the stream cache is an
        # optimisation, not a correctness primitive, so sweeping once
        # every 1/3 of the TTL is plenty. Bounded below by 5 s so very
        # short TTLs (test scenarios) don't spin.
        sweep_interval = max(5, settings.stream_idle_ttl_sec // 3)
        log.info(
            "stream idle-sweep loop started (ttl=%ds, interval=%ds)",
            settings.stream_idle_ttl_sec, sweep_interval,
        )
        while True:
            try:
                await asyncio.sleep(sweep_interval)
                now = time.perf_counter_ns()
                stale_keys: list[str] = []
                # Snapshot to avoid dict-mutation-during-iteration.
                for sk, sess in list(self._streaming_sessions.items()):
                    if sess._turn_lock.locked():
                        continue
                    if now - sess.last_used_ns >= ttl_ns:
                        stale_keys.append(sk)
                if stale_keys:
                    _profile.event(
                        "stream.idle_sweep",
                        count=len(stale_keys),
                        live=len(self._streaming_sessions),
                    )
                    async with self._streaming_lock:
                        for sk in stale_keys:
                            sess = self._streaming_sessions.get(sk)
                            if sess is None:
                                continue
                            # Re-check inside the lock: something could
                            # have bumped the session in the meantime.
                            if sess._turn_lock.locked():
                                continue
                            if now - sess.last_used_ns < ttl_ns:
                                continue
                            await self._evict_streaming_session(
                                sk, reason="idle_ttl",
                            )
            except asyncio.CancelledError:
                log.info("stream idle-sweep loop cancelled")
                return
            except Exception:
                log.exception(
                    "stream idle-sweep tick failed — continuing loop",
                )

    def start_idle_sweep(self) -> None:
        """Kick off the background idle-sweep task. Called from ``run_host``.

        Safe to call multiple times; subsequent calls are a no-op once
        the task is running. Separate method (rather than kicked off in
        ``__init__``) because ``AgentHost`` may be constructed on a
        thread that doesn't have a running event loop, and
        ``asyncio.create_task`` would raise there.
        """
        if self._streaming_sweep_task is not None:
            return
        loop = asyncio.get_running_loop()
        self._streaming_sweep_task = loop.create_task(
            self._idle_sweep_loop(),
        )

    async def close_all_streaming_sessions(self, *, reason: str) -> None:
        """Disconnect every cached streaming client. Used at shutdown."""
        async with self._streaming_lock:
            keys = list(self._streaming_sessions.keys())
            for sk in keys:
                await self._evict_streaming_session(sk, reason=reason)
        if self._streaming_sweep_task is not None:
            self._streaming_sweep_task.cancel()
            try:
                await self._streaming_sweep_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._streaming_sweep_task = None

    async def flush_and_rotate(self) -> FlushSummary:
        """On-exit memory handoff: reflect every live session, then rotate.

        Called from the CLI's ``/exit`` handler (and any future clean-shutdown
        path) as the last thing before the event loop stops. For each
        ``(session_key, session_id)`` in :attr:`_sessions`:

        1. Resolve the session's JSONL via ``locate_session_jsonl``. If the
           file is already gone, skip — nothing left to reflect.
        2. Invoke ``reflect_and_persist`` (same helper PreCompact and the
           reflect MCP tool use), which extracts up to 5 observations,
           appends them to ``observations.jsonl``, and advances the cursor
           in ``state.json``.
        3. Regardless of reflect outcome, drop the session id from the
           in-memory map so the NEXT launch starts a fresh SDK session.
           This is the "session rotation" half of Q5 in §11.3 — it's what
           lets us cap JSONL growth without losing memory continuity:
           observations survive, conversation bytes don't.

        Design points worth pinning:

        * **Best-effort per session, never fatal.** A failure on one
          session key must not block rotation on the others; a user's
          shutdown takes priority over any single reflect pass.
        * **Reflect runs in a thread executor.** ``reflect_from_jsonl``
          is synchronous (anthropic SDK is blocking), and /exit is the
          one path where we genuinely want to wait for it — but blocking
          the event loop means channel close callbacks and the scheduler
          stop signal can't interleave. ``asyncio.to_thread`` gives us
          "wait for reflect" without "freeze the loop".
        * **Rotation happens even if reflect_and_persist raised.** The
          user pressed /exit; our job is to hand control back. Worst
          case a failed reflect leaves the cursor where it was, so the
          next trigger (PreCompact or the next /exit) retries the same
          delta — that's the Q8 contract.
        """
        summary = FlushSummary()

        if not self._sessions:
            return summary

        try:
            from pip_agent.anthropic_client import build_anthropic_client
            from pip_agent.memory.reflect import reflect_and_persist
        except Exception:  # pragma: no cover — memory pkg is bundled
            log.exception("flush_and_rotate: memory package import failed")
            summary.rotated = len(self._sessions)
            self._sessions.clear()
            _save_sessions(self._sessions)
            return summary

        client = build_anthropic_client()
        snapshot = dict(self._sessions)
        summary.rotated = len(snapshot)

        if client is None:
            log.info(
                "flush_and_rotate: rotating %d session(s) but skipping "
                "reflect — no ANTHROPIC_API_KEY/AUTH_TOKEN configured",
                len(snapshot),
            )
        else:
            log.info(
                "flush_and_rotate: reflecting %d session(s) on exit",
                len(snapshot),
            )

            # ``_reflect_one`` bumps ``summary`` only on the happy path —
            # a missing transcript / missing agent_id / reflect crash all
            # fall through rotating the session without counting it as
            # "reflected". That's intentional: ``summary.reflected`` is
            # "did an LLM call actually happen for this session", which
            # is what the CLI status line needs to not lie to the user.
            def _reflect_one(sk: str, sid: str) -> None:
                agent_id = _agent_id_from_session_key(sk)
                if not agent_id:
                    log.warning(
                        "flush_and_rotate: cannot derive agent_id from %r; "
                        "skipping reflect", sk,
                    )
                    return
                # An agent that was ``/subagent delete``d or ``archive``d
                # mid-run will no longer resolve in the registry. Skip
                # it — materialising ``_get_agent_services`` would
                # resurrect a ``.pip/`` we just wiped via
                # ``atomic_write`` in save_state. ``invalidate_agent_cache``
                # already removes its sessions, but a race (reflect
                # scheduled before the invalidation landed) can still
                # reach here.
                if self._registry.get_agent(agent_id) is None:
                    log.info(
                        "flush_and_rotate: agent %r no longer registered; "
                        "skipping reflect for session=%s",
                        agent_id, sid[:8],
                    )
                    return
                svc = self._get_agent_services(agent_id)
                path = locate_session_jsonl(sid, prefer_cwd=svc.paths.cwd)
                if path is None:
                    log.info(
                        "flush_and_rotate: transcript for %s missing; "
                        "skipping reflect", sid[:8],
                    )
                    return
                try:
                    start_offset, new_offset, obs_count = reflect_and_persist(
                        memory_store=svc.memory_store,
                        session_id=sid,
                        transcript_path=path,
                        client=client,
                    )
                    log.info(
                        "flush_and_rotate: session=%s obs=%d offset=%d→%d",
                        sid[:8], obs_count, start_offset, new_offset,
                    )
                    summary.reflected += 1
                    summary.observations += obs_count
                except Exception as exc:  # noqa: BLE001
                    # Per-session failure isolated: log and keep going.
                    log.warning(
                        "flush_and_rotate: reflect failed for session=%s: %s",
                        sid[:8], exc,
                    )

            for sk, sid in snapshot.items():
                await asyncio.to_thread(_reflect_one, sk, sid)

        # Rotation: clear the whole map regardless of reflect outcome.
        # The JSONL files themselves are not touched — CC owns those —
        # but the next turn will mint a fresh session_id.
        self._sessions.clear()
        _save_sessions(self._sessions)
        return summary

    def _build_mcp_ctx(
        self,
        svc: _PerAgent,
        model: str,
        sender_id: str,
        channel: Channel | None = None,
        peer_id: str = "",
        session_id: str = "",
    ) -> McpContext:
        return McpContext(
            memory_store=svc.memory_store,
            workdir=svc.paths.cwd,
            model=model,
            session_id=session_id,
            sender_id=sender_id,
            channel=channel,
            peer_id=peer_id,
            scheduler=self._scheduler,
        )

    async def process_inbound(self, inbound: InboundMessage) -> None:
        """Route one inbound message through the SDK agent and reply.

        Split into three phases for readability:

        1. **pre** — :meth:`_prepare_turn` resolves the agent, runs
           host-layer slash commands (which may short-circuit the whole
           turn), enriches the system prompt, materialises attachments
           and renders the SDK prompt.
        2. **dispatch** — :meth:`_execute_turn` acquires the per-session
           lock and the global semaphore, runs the SDK query, and
           persists the resulting session id back to ``self._sessions``.
        3. **post** — :meth:`_dispatch_reply` routes the reply text /
           error back through the originating channel, with the
           heartbeat-sentinel silencing contract applied.
        """
        # PROFILE — open a turn context for all downstream spans.
        from pip_agent import _profile

        _profile.new_turn(
            channel=inbound.channel,
            sender=inbound.sender_id,
            peer=inbound.peer_id,
            text_len=len(inbound.text) if inbound.text else 0,
            atts=len(inbound.attachments or []),
            is_group=inbound.is_group,
        )
        try:
            async with _profile.span(
                "host.process_inbound",
                channel=inbound.channel,
            ):
                prepared = self._prepare_turn(inbound)
                if prepared is None:
                    return  # slash command handled inline.

                await self._execute_turn(inbound, prepared)
        finally:
            _profile.end_turn()

    def _prepare_turn(
        self, inbound: InboundMessage,
    ) -> _PreparedTurn | None:
        """Resolve routing, enrich the prompt, and short-circuit slash commands.

        Returns ``None`` when the inbound was fully handled by the
        host-layer command dispatcher (no SDK turn required). Otherwise
        returns a :class:`_PreparedTurn` bundle with everything
        :meth:`_execute_turn` needs to run the SDK call and route the
        reply.
        """
        # PROFILE
        from pip_agent import _profile

        with _profile.span_sync("host.prepare_turn"):
            with _profile.span_sync("host.route_session"):
                if inbound.agent_id:
                    agent_id = inbound.agent_id
                    binding = None
                else:
                    agent_id, binding = self._binding_table.resolve(
                        channel=inbound.channel,
                        account_id=inbound.account_id,
                        guild_id=inbound.guild_id,
                        peer_id=inbound.peer_id,
                    )
                if not agent_id:
                    agent_id = self._registry.default_agent().id

                agent_cfg = (
                    self._registry.get_agent(agent_id) or self._registry.default_agent()
                )
                eff = resolve_effective_config(agent_cfg, binding)

                svc = self._get_agent_services(eff.id)

            # Short-circuit host-layer slash commands BEFORE we do the more
            # expensive prompt enrichment + SDK subprocess spawn. Dispatch
            # runs cheaply off in-memory registry / bindings / memory-store
            # state; its response (if any) is routed back through the same
            # channel that delivered the inbound. Unknown slashes fall
            # through to the agent so the LLM can still interpret them.
            with _profile.span_sync("host.dispatch_command"):
                cmd_result = host_commands.dispatch_command(
                    host_commands.CommandContext(
                        inbound=inbound,
                        registry=self._registry,
                        bindings=self._binding_table,
                        bindings_path=BINDINGS_PATH,
                        memory_store=svc.memory_store,
                        scheduler=self._scheduler,
                        invalidate_agent=self.invalidate_agent_cache,
                    ),
                )
            if cmd_result.handled:
                _profile.event(
                    "host.command_handled",
                    cmd=inbound.text[:60] if inbound.text else "",
                )
                self._deliver_command_response(inbound, cmd_result.response)
                return None

            sk = build_session_key(
                agent_id=eff.id,
                channel=inbound.channel,
                peer_id=inbound.peer_id,
                guild_id=inbound.guild_id,
                is_group=inbound.is_group,
                dm_scope=eff.effective_dm_scope,
            )

            agent_cwd = svc.paths.cwd
            base_prompt = eff.system_prompt(workdir=str(agent_cwd))
            user_text = inbound.text if isinstance(inbound.text, str) else ""
            with _profile.span_sync(
                "memory.enrich_prompt",
                agent_id=eff.id,
                channel=inbound.channel,
            ):
                system_prompt = svc.memory_store.enrich_prompt(
                    base_prompt, user_text,
                    channel=inbound.channel,
                    agent_id=eff.id,
                    workdir=str(agent_cwd),
                    sender_id=inbound.sender_id,
                )

            # Drop binary attachment bytes into the per-agent incoming box
            # *before* prompt rendering so :func:`_format_prompt` can hand
            # the model a real path. Per-agent isolation means a zip sent
            # to agent A can't be clobbered by one sent to B with the same
            # filename on the same second. Has to run after slash dispatch
            # (no point persisting a file the user intended for a host
            # command) but before prompt formatting.
            with _profile.span_sync(
                "host.materialize_attachments",
                n=len(inbound.attachments or []),
            ):
                _materialize_attachments(
                    inbound,
                    workdir=agent_cwd,
                    incoming_dir=svc.paths.incoming_dir,
                )

            with _profile.span_sync("host.format_prompt"):
                prompt = _format_prompt(inbound, svc.memory_store)

            ch = self._channel_mgr.get(inbound.channel)
            reply_peer = inbound.peer_id
            if inbound.is_group and inbound.guild_id:
                reply_peer = inbound.guild_id

            if inbound.channel == "wechat":
                # Import lazily: the wechat channel pulls in its own
                # aiohttp + pywinauto stack that CLI/wecom-only runs
                # don't need. By the time we actually dispatch a
                # wechat inbound we've already paid the import during
                # channel construction in :func:`run_host`, so this is
                # free.
                from pip_agent.channels.wechat import WeChatChannel

                if isinstance(ch, WeChatChannel):
                    _profile.event("host.send_typing", channel="wechat")
                    ch.send_typing(inbound.peer_id)

            return _PreparedTurn(
                eff=eff, svc=svc, sk=sk, ch=ch,
                reply_peer=reply_peer, prompt=prompt,
                system_prompt=system_prompt,
                paths=svc.paths,
            )

    async def _execute_turn(
        self, inbound: InboundMessage, prepared: _PreparedTurn,
    ) -> None:
        """Acquire per-session + global locks, run the SDK query, route reply.

        Kept in one block (rather than split into separate "call" and
        "post" helpers) because the session-id persistence has to
        happen *inside* the lock that guarded the read — see the inline
        note on H4 — and splitting would force either re-entry or a
        fragile lock-hand-off between methods.
        """
        sk = prepared.sk

        is_heartbeat = inbound.sender_id == _HEARTBEAT_SENDER
        # Scheduler-injected senders skip SDK session persistence —
        # see :func:`_is_ephemeral_sender` for the full rationale and
        # the measurements that motivated this. TL;DR: heartbeat / cron
        # poisoning the user transcript turns a 10 s cold start into a
        # 3 min one over the course of a day. ``stream_text=not is_heartbeat``
        # remains a separate concern (HEARTBEAT_OK silencing).
        is_ephemeral = _is_ephemeral_sender(inbound.sender_id)

        # PROFILE — imported early so the lock-time coalescing gate
        # below can emit observability events.
        from pip_agent import _profile

        # Tier 2 lock-time coalescing gate.
        #
        # If a batch-eligible (text-only, human-originated, no slash)
        # inbound arrives while another turn is already claimed for
        # this session, park it in ``_pending_per_session`` instead of
        # running a separate turn. The active turn will drain pending
        # inside ``_run_turn_streaming`` (lock held, right before the
        # SDK push) and fuse them into one LLM call. Late arrivals
        # that miss that drain are flushed as a follow-up turn by
        # ``_release_or_flush_session``.
        #
        # Non-eligible inbounds (attachments, scheduler payloads,
        # heartbeats, slash commands) skip the gate entirely — they
        # can't be merged anyway, and we don't want them blocking the
        # active-set (which is what drives the redirect). They still
        # serialise via the per-session asyncio.Lock below, unchanged.
        claimed_active = False
        if settings.batch_text_inbounds and _batch_eligible(inbound):
            async with self._pending_lock:
                if sk in self._session_active:
                    self._pending_per_session.setdefault(sk, []).append(
                        inbound,
                    )
                    pending_depth = len(self._pending_per_session[sk])
                    _profile.event(
                        "host.redirected_to_lock_batch",
                        session_key=sk,
                        channel=inbound.channel,
                        text_len=len(inbound.text or ""),
                        pending_depth=pending_depth,
                    )
                    log.debug(
                        "Tier2 lock-time redirect: sk=%s depth=%d "
                        "(parked for fusion with in-flight turn)",
                        sk, pending_depth,
                    )
                    return
                self._session_active.add(sk)
                claimed_active = True

        try:
            await self._execute_turn_body(
                inbound=inbound,
                prepared=prepared,
                is_heartbeat=is_heartbeat,
                is_ephemeral=is_ephemeral,
            )
        finally:
            if claimed_active:
                await self._release_or_flush_session(sk)

    async def _execute_turn_body(
        self,
        *,
        inbound: InboundMessage,
        prepared: _PreparedTurn,
        is_heartbeat: bool,
        is_ephemeral: bool,
    ) -> None:
        """Inner body of :meth:`_execute_turn` (split for gating clarity).

        Extracted so the lock-time coalescing gate + its try/finally can
        wrap the whole dispatch without deeply indenting the existing
        lock/semaphore/turn-run code. All behaviour below is pre-existing
        and unchanged.
        """
        eff = prepared.eff
        svc = prepared.svc
        sk = prepared.sk
        ch = prepared.ch
        reply_peer = prepared.reply_peer

        # ``track`` owns the scheduler-side bookkeeping: coalesce-key
        # release on exit (so the next tick can fire again) and cron
        # ``consecutive_errors`` accounting. It is a no-op for inbounds
        # without a ``source_job_id`` (user / channel messages), so we
        # wrap every inbound unconditionally. See
        # :meth:`HostScheduler.track` for the contract.
        tracker = (
            self._scheduler.track(inbound)
            if self._scheduler is not None
            else nullcontext(_NullTracked())
        )
        # PROFILE
        from pip_agent import _profile

        with tracker as tracked:
            # Only the per-session lock is required at this outer scope.
            # The historical ``_semaphore`` wrap was moved down to the
            # one-shot ``run_query`` branch below — streaming turns
            # operate on an already-spawned long-lived subprocess and
            # don't need global throttling (their spawn path is guarded
            # by ``_streaming_lock`` + ``stream_max_live`` instead).
            _profile.event("host.lock_wait_start", session_key=sk)
            async with self._get_session_lock(sk):
                _profile.event("host.lock_wait_end", session_key=sk)  # PROFILE
                # Resolve the resume session id INSIDE the per-session
                # lock. Two concurrent inbounds on the same ``sk`` would
                # otherwise both read the same stale id before either
                # had a chance to persist ``result.session_id`` back —
                # the second turn would then resume a session the first
                # turn has already rotated away from, and whichever
                # finishes last clobbers the other's persisted state.
                # Per-session serialisation is already enforced; bringing
                # the read inside closes the gap for free.
                async with _profile.span("host.session_preflight"):  # PROFILE
                    current_session = self._reap_stale_session(
                        sk, prefer_cwd=prepared.paths.cwd,
                    )
                # Two distinct concepts, intentionally decoupled:
                #
                # * ``session_for_turn`` controls SDK *resume* — whether this
                #   turn's context is built from an existing JSONL. ``None``
                #   for ephemeral senders so cron / heartbeat don't load
                #   and don't append.
                # * ``ctx_session_id`` is the session id made visible to
                #   Pip-Boy's own MCP tools (``reflect`` in particular).
                #   It must point at the *user's* session JSONL even for
                #   ephemeral turns, because the whole point of an "at 2 am
                #   run reflect" cron is for that cron to process the user
                #   conversation that the cron itself never participated in.
                #   Zeroing this out would silently break cron-driven memory
                #   maintenance.
                session_for_turn: str | None = (
                    None if is_ephemeral else current_session
                )
                ctx_session_id = current_session or ""

                mcp_ctx = self._build_mcp_ctx(
                    svc, eff.effective_model, inbound.sender_id,
                    channel=ch, peer_id=reply_peer,
                    session_id=ctx_session_id,
                )
                _profile.event(  # PROFILE
                    "host.mcp_ctx_built",
                    model=eff.effective_model,
                    resume=bool(session_for_turn),
                )

                # Tier 1 decision point:
                #
                # Ephemeral senders (cron / heartbeat) MUST use the
                # one-shot path — they opt out of session persistence by
                # design (see :func:`_is_ephemeral_sender`), and binding
                # them to a cached client would pollute the user's
                # transcript on the next user turn. Similarly,
                # ``stream_text=False`` (heartbeats need the full reply
                # buffered for HEARTBEAT_OK silencing) is cleanly served
                # by the one-shot path.
                #
                # Non-ephemeral senders go through the streaming cache
                # when enabled. Stale-session recovery: one retry with a
                # fresh client. After that, surface the error normally.
                use_streaming = (
                    settings.enable_streaming_session
                    and not is_ephemeral
                    and not is_heartbeat
                )
                try:
                    if use_streaming:
                        result = await self._run_turn_streaming(
                            session_key=sk,
                            prepared=prepared,
                            inbound=inbound,
                            mcp_ctx=mcp_ctx,
                            current_session_id=current_session,
                        )
                    else:
                        # One-shot path (cron / heartbeat / streaming
                        # disabled). Unlike the streaming path, each
                        # call here spawns a fresh CC subprocess, so
                        # we throttle with ``_one_shot_semaphore`` to
                        # cap worst-case RAM during a cron/heartbeat
                        # burst. Narrowing the wrap to just this
                        # branch avoids the old semantic where the
                        # semaphore bottlenecked streaming turns as
                        # well (see ``__init__`` comment block).
                        async with self._one_shot_semaphore, _profile.span(
                            "host.run_query",
                            model=eff.effective_model,
                            resume=bool(session_for_turn),
                            prompt_kind=(
                                "str" if isinstance(prepared.prompt, str) else "blocks"
                            ),
                        ):
                            result = await run_query(
                                prompt=prepared.prompt,
                                mcp_ctx=mcp_ctx,
                                model=eff.effective_model,
                                session_id=session_for_turn,
                                system_prompt_append=prepared.system_prompt,
                                cwd=prepared.paths.cwd,
                                # Heartbeats must NOT stream: we need the full
                                # reply before deciding whether to print (so
                                # the HEARTBEAT_OK sentinel can be silenced).
                                # Everything else streams unconditionally —
                                # streaming is an interactive contract, not a
                                # debug toggle.
                                stream_text=not is_heartbeat,
                            )
                except Exception as exc:
                    log.error("SDK query failed for %s: %s", sk, exc)
                    tracked.failure(f"SDK query failed: {exc}")
                    if ch:
                        send_with_retry(
                            ch, reply_peer, f"[error] {exc}",
                            inbound_id=str(
                                inbound.raw.get("_pip_inbound_id") or ""
                            ),
                        )
                    return

                # Persist the new session id BEFORE releasing the lock so
                # the next inbound on the same ``sk`` sees it. Doing this
                # after the ``async with`` closes reopens the race H4 was
                # supposed to fix: two concurrent turns would both read a
                # stale ``current_session`` and the later one would
                # clobber the earlier one's id on save.
                #
                # Skip for ephemeral senders — their ``result.session_id``
                # is a throwaway the SDK minted for this one turn, and
                # binding it to ``sk`` would overwrite the user's real
                # session on the next save.
                if not is_ephemeral and result.session_id:
                    self._sessions[sk] = result.session_id
                    _save_sessions(self._sessions)

            if result.error:
                # Soft failure — ``run_query`` returned normally but the
                # SDK reported a tool / API error. Count it toward the
                # cron auto-disable streak just like a raised exception.
                tracked.failure(result.error)

            with _profile.span_sync(  # PROFILE
                "host.dispatch_reply",
                channel=inbound.channel,
                reply_len=len(result.text or ""),
                has_error=bool(result.error),
            ):
                self._dispatch_reply(
                    inbound=inbound,
                    result=result,
                    ch=ch,
                    reply_peer=reply_peer,
                    session_key=sk,
                )

    async def _release_or_flush_session(self, sk: str) -> None:
        """Clear the Tier 2 active claim; flush leftover inbounds if any.

        Called from ``_execute_turn``'s ``finally`` block whenever the
        current turn claimed ``_session_active[sk]`` on entry. Three
        outcomes:

        1. ``_pending_per_session[sk]`` is empty → drop the active flag
           and return. Normal path when the current turn already ate
           all late-arrivals at lock-time.
        2. Pending has entries → fuse them into a merged inbound and
           dispatch a follow-up ``process_inbound`` task. Keep the
           active flag held so the new coroutine's own gate doesn't
           re-claim (it will re-acquire during its own run). Actually
           we *release* the claim here and let the new task re-claim,
           which is simpler — any racing inbound that also wants the
           lock will park itself correctly.
        3. Fusion produced no single merged result (can't happen by
           construction — everything in pending passed ``_batch_eligible``
           when appended — but we guard it and clear the flag).

        Leftover-flush tasks are tracked in ``_lock_flush_tasks`` so
        :meth:`drain_lock_flush_tasks` can await them on shutdown;
        otherwise reflect / memory-store writes could be truncated
        mid-flight on ``/exit``.
        """
        from pip_agent import _profile

        async with self._pending_lock:
            lefts = self._pending_per_session.pop(sk, [])
            self._session_active.discard(sk)
        if not lefts:
            return

        # Everything in ``lefts`` was batch-eligible at append time
        # and shares the same session key, so ``_coalesce_text_inbounds``
        # collapses to a single element. Guard the assumption with a
        # len-check rather than ``assert`` so a future refactor that
        # relaxes the invariant degrades to "dispatch per leftover"
        # instead of crashing.
        fused_batch, fused = _coalesce_text_inbounds(
            lefts, settings.batch_text_joiner,
        )
        if len(fused_batch) != 1:
            log.warning(
                "Tier2 leftover flush for %s: expected 1 merged inbound, "
                "got %d — dispatching each independently",
                sk, len(fused_batch),
            )
            for m in fused_batch:
                task = asyncio.create_task(self.process_inbound(m))
                self._lock_flush_tasks.add(task)
                task.add_done_callback(self._lock_flush_tasks.discard)
            return

        merged = fused_batch[0]
        _profile.event(
            "host.batch_coalesced",
            source="leftover_flush",
            session_key=sk,
            before=len(lefts),
            after=1,
            fused=fused,
            channel=merged.channel,
        )
        log.info(
            "Tier2 leftover flush: fused %d late-arrivals for %s "
            "into 1 follow-up turn", len(lefts), sk,
        )
        task = asyncio.create_task(self.process_inbound(merged))
        self._lock_flush_tasks.add(task)
        task.add_done_callback(self._lock_flush_tasks.discard)

    async def drain_lock_flush_tasks(self, *, timeout: float = 10.0) -> None:
        """Await in-flight Tier 2 leftover-flush tasks. Shutdown helper.

        Called from ``run_host`` just before / alongside the main
        ``pending_tasks`` drain so ``/exit`` doesn't truncate a
        leftover-merged turn mid-LLM. Bounded timeout mirrors the
        existing streaming-session shutdown contract.
        """
        if not self._lock_flush_tasks:
            return
        # Snapshot — the set may mutate (done_callback removes) while
        # we await.
        pending = list(self._lock_flush_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Tier2 lock-flush drain timed out after %.1fs "
                "(%d tasks still running)",
                timeout, sum(1 for t in pending if not t.done()),
            )

    def _deliver_command_response(
        self, inbound: InboundMessage, response: str | None,
    ) -> None:
        """Route a slash-command response back to the originating channel.

        Separate from :meth:`_dispatch_reply` because command responses
        are synthetic — they never went through the SDK, so they carry
        no ``session_id``, no streaming state, and no HEARTBEAT_OK
        sentinel to silence. Keeping the paths separate avoids the
        temptation to grow a ``QueryResult``-shaped wrapper just for
        them.
        """
        if not response:
            return
        ch = self._channel_mgr.get(inbound.channel)
        reply_peer = inbound.peer_id
        if inbound.is_group and inbound.guild_id:
            reply_peer = inbound.guild_id

        if inbound.channel == "cli":
            # Mirror the indentation that streaming replies use so the
            # CLI transcript reads uniformly, then force a newline so
            # the next ``>>>`` prompt starts on its own line.
            print(f"  {response}")
        elif ch:
            send_with_retry(
                ch, reply_peer, response,
                inbound_id=str(inbound.raw.get("_pip_inbound_id") or ""),
            )

    @staticmethod
    def _dispatch_reply(
        *,
        inbound: InboundMessage,
        result: QueryResult,
        ch: Channel | None,
        reply_peer: str,
        session_key: str,
    ) -> None:
        """Route the agent's reply back to the originating surface.

        Heartbeat replies are *generally* delivered like any other reply — a
        proactive greeting, a reminder, or a "found something" alert is the
        whole point of the heartbeat. The single exception is the
        ``HEARTBEAT_OK`` sentinel defined in ``scaffold/heartbeat.md`` which
        means "nothing to report"; we swallow that to avoid CLI noise.

        Silencing only works because :func:`AgentHost.process_inbound` disables
        text streaming for heartbeat inbounds — once characters have been
        streamed to stdout there is nothing dispatch can do to unprint them.
        Heartbeat text therefore always prints from *here*, never from
        ``agent_runner``.

        Cron replies always flow through the normal dispatch — the cron job's
        configured ``channel``/``peer_id`` is the intended delivery target.
        """
        is_heartbeat = inbound.sender_id == _HEARTBEAT_SENDER

        if (
            is_heartbeat
            and result.text
            and _HEARTBEAT_OK_RE.match(result.text)
        ):
            log.info(
                "Heartbeat sentinel for %s (suppressed): %r",
                session_key, result.text[:80],
            )
            return

        inbound_id = str(inbound.raw.get("_pip_inbound_id") or "")

        if result.error:
            log.warning("Agent error for %s: %s", session_key, result.error)
            if inbound.channel == "cli":
                print(f"\n  [error] {result.error}")
            elif ch:
                send_with_retry(
                    ch, reply_peer, f"[error] {result.error}",
                    inbound_id=inbound_id,
                )
            return

        if result.text:
            if inbound.channel == "cli":
                # Heartbeats never stream (see docstring), so dispatch is the
                # sole source of their output — print full text. User / cron
                # inbounds were streamed live by ``agent_runner`` regardless
                # of VERBOSE (streaming is an interactive UX contract, not a
                # debug toggle), so dispatch only needs to terminate the
                # line before the next ``>>>`` prompt.
                if is_heartbeat:
                    print(f"\n{result.text}")
                else:
                    print()
            elif ch:
                send_with_retry(
                    ch, reply_peer, result.text, inbound_id=inbound_id,
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_host(mode: str = "auto", bind_agent: str | None = None) -> None:
    """Blocking multi-channel entry point.

    Starts channel threads, then enters an asyncio event loop that processes
    inbound messages through the SDK agent.

    UTF-8 console setup (for Windows CJK input) is not done here — it must
    happen *before* :mod:`logging` is configured, so
    :func:`pip_agent.console_io.force_utf8_console` is called from
    ``__main__.main()`` instead. See that module's docstring for why.
    """

    from pip_agent import _profile
    from pip_agent.scaffold import ensure_workspace

    _profile.cold_start("run_host_entered", mode=mode)

    ensure_workspace(WORKDIR)
    settings.check_required()

    registry = AgentRegistry(WORKDIR)
    binding_table = BindingTable()
    binding_table.load(BINDINGS_PATH)
    _profile.cold_start(
        "registry_ready",
        agents=len(registry.list_agents()),
    )

    channel_mgr = ChannelManager()
    cli_channel = CLIChannel()
    channel_mgr.register(cli_channel)
    # Fine-grained cold-start markers inside the historical
    # ``channels_ready`` block. v2 profiling showed a ~500 ms opaque
    # gap here between ``registry_ready`` and ``channels_ready``; these
    # sub-markers decompose it into (wechat import / wechat init /
    # wechat login-check / wecom import / wecom init) so optimisation
    # effort can target the actual tall bar instead of guessing.
    _profile.cold_start("cli_channel_registered")

    stop_event = threading.Event()
    msg_queue: list[InboundMessage] = []
    q_lock = threading.Lock()
    bg_threads: list[threading.Thread] = []

    state_dir = WORKDIR / ".pip"

    wechat_channel = None
    if mode != "cli":
        try:
            # Deferred import: keeps CLI cold-start off the wechat
            # (pywinauto / aiohttp) dependency graph.
            from pip_agent.channels.wechat import (
                WeChatChannel,
                wechat_poll_loop,
            )
            _profile.cold_start("wechat_import_done")

            wechat_channel = WeChatChannel(state_dir)
            _profile.cold_start(
                "wechat_instance_ready",
                logged_in=bool(wechat_channel.is_logged_in),
            )
            if mode == "scan":
                wechat_channel._clear_creds()
                if not wechat_channel.login():
                    print("  [wechat] Login failed, falling back to CLI-only.")
                    wechat_channel = None
            elif not wechat_channel.is_logged_in:
                if not wechat_channel.login():
                    print("  [wechat] Login failed, falling back to CLI-only.")
                    wechat_channel = None
            # Emit login-check marker AFTER any login attempt; the
            # ``logged_in`` flag makes the "was the user already in?"
            # vs "did we just log in?" distinction visible post-hoc.
            _profile.cold_start(
                "wechat_login_checked",
                logged_in=bool(
                    wechat_channel and wechat_channel.is_logged_in,
                ),
            )
            if wechat_channel and wechat_channel.is_logged_in:
                channel_mgr.register(wechat_channel)
                t = threading.Thread(
                    target=wechat_poll_loop, daemon=True,
                    args=(wechat_channel, msg_queue, q_lock, stop_event),
                )
                t.start()
                bg_threads.append(t)
                _profile.cold_start("wechat_poll_spawned")
                if bind_agent:
                    aid = normalize_agent_id(bind_agent)
                    if registry.get_agent(aid):
                        binding_table.remove("channel", "wechat")
                        binding_table.add(Binding(
                            agent_id=aid, tier=4,
                            match_key="channel", match_value="wechat",
                        ))
                        binding_table.save(BINDINGS_PATH)
                        print(f"  [wechat] Bound to agent: {aid}")
        except Exception as exc:
            print(f"  [wechat] Init failed: {exc}")

    if settings.wecom_bot_id and settings.wecom_bot_secret:
        try:
            # Deferred import: aibot (WeCom SDK) + aiohttp add ~600 ms
            # to cold start. Only pay it if wecom creds are actually
            # configured.
            from pip_agent.channels.wecom import WecomChannel, wecom_ws_loop
            _profile.cold_start("wecom_import_done")

            wecom_channel = WecomChannel(
                settings.wecom_bot_id,
                settings.wecom_bot_secret,
                msg_queue,
                q_lock,
            )
            channel_mgr.register(wecom_channel)
            _profile.cold_start("wecom_instance_ready")
            t = threading.Thread(
                target=wecom_ws_loop, daemon=True,
                args=(wecom_channel, stop_event),
            )
            t.start()
            bg_threads.append(t)
            _profile.cold_start("wecom_ws_spawned")
        except Exception as exc:
            print(f"  [wecom] Init failed: {exc}")

    _profile.cold_start(
        "channels_ready",
        channels=channel_mgr.list_channels(),
    )

    scheduler = HostScheduler(
        registry=registry,
        msg_queue=msg_queue,
        q_lock=q_lock,
        stop_event=stop_event,
    )
    scheduler.start()
    _profile.cold_start("scheduler_ready")

    host = AgentHost(
        registry=registry,
        binding_table=binding_table,
        channel_mgr=channel_mgr,
        scheduler=scheduler,
    )
    _profile.cold_start("host_ready")

    from pip_agent import __version__

    agents_list = ", ".join(a.id for a in registry.list_agents())
    print(
        "============================================\n"
        "  ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL\n"
        "  PIP-BOY 3000 MARK IV  [SDK HOST]\n"
        f"  Personal Assistant Module v{__version__}\n"
        "============================================\n"
        "  Welcome, Vault Dweller. Type '/exit' to\n"
        "  power down.\n"
        f"  Channels: {', '.join(channel_mgr.list_channels())}\n"
        f"  Agents: {agents_list}\n"
        "============================================"
    )

    # The scheduler and all remote channels push into ``msg_queue``, so the
    # main loop always drains that queue (even in CLI-only mode).
    async def _run() -> None:
        loop = asyncio.get_running_loop()
        # Final cold-start anchor: event loop is up and we're one step
        # away from ``stdin.readline()`` / WS / long-poll inbound. Any
        # wall time before this point counts as cold-start cost.
        _profile.cold_start("loop_ready")

        # Tier 1: start the streaming-session idle sweep now that we
        # have a running event loop. Safe to call even when the
        # streaming cache is disabled — the loop itself only evicts
        # sessions that were actually added.
        host.start_idle_sweep()

        def _stdin_reader() -> None:
            while not stop_event.is_set():
                try:
                    line = sys.stdin.readline()
                except (EOFError, OSError):
                    break
                if not line:
                    break
                text = line.strip()
                if text:
                    # PROFILE
                    _profile.event(
                        "cli.inbound_received",
                        channel="cli",
                        text_len=len(text),
                    )
                    with q_lock:
                        msg_queue.append(InboundMessage(
                            text=text,
                            sender_id="cli-user",
                            channel="cli",
                            peer_id="cli-user",
                        ))

        stdin_t = threading.Thread(target=_stdin_reader, daemon=True)
        stdin_t.start()
        print("  (type and press Enter; /exit to quit)")

        # ``process_inbound`` tasks are fired and tracked, not awaited
        # each tick. Awaiting would re-introduce the head-of-line bug
        # where a slow WeChat turn (e.g. ``send_file`` uploading a
        # multi-MB image) blocks a fresh WeCom ``hi`` from even being
        # dispatched. Concurrency is already safe: :meth:`AgentHost
        # .process_inbound` serialises same-session turns via a
        # per-key lock and caps total SDK subprocesses with a global
        # semaphore. At /exit-time we drain pending_tasks so reflect
        # doesn't lose mid-flight observations.
        pending_tasks: list[asyncio.Task[None]] = []

        while not stop_event.is_set():
            with q_lock:
                batch = msg_queue[:]
                msg_queue.clear()

            # PROFILE
            if batch:
                _profile.event("host.queue_drain", batch=len(batch))

            # User-originated messages go first. With the scheduler's new
            # coalescing there is at most one in-flight cron/heartbeat per
            # key at a time, but if the user types while a batch has a cron
            # payload in it we still want the human message to run ahead of
            # the keepalive.
            if batch:
                batch.sort(key=_inbound_sort_key)

            # Tier 2: fuse contiguous text-only messages from the same
            # conversation into one LLM turn. ``batch`` is already
            # sorted by priority-then-FIFO, so same-conversation text
            # bubbles from a single user land adjacent. Toggle off via
            # ``batch_text_inbounds=false`` in ``.env``.
            if batch and settings.batch_text_inbounds:
                before_n = len(batch)
                batch, fused = _coalesce_text_inbounds(
                    batch, settings.batch_text_joiner,
                )
                if fused:
                    _profile.event(
                        "host.batch_coalesced",
                        source="drain_time",
                        before=before_n,
                        after=len(batch),
                        fused=fused,
                    )
                    log.info(
                        "Tier2 batch: fused %d text inbound(s) "
                        "(%d -> %d)", fused, before_n, len(batch),
                    )

            for inbound in batch:
                # Only real interactive CLI input can terminate the host; a
                # cron payload that happens to say "/exit" must not kill us.
                # Bare ``exit`` (no slash) is deliberately NOT a shutdown
                # trigger — it collides with the user legitimately asking
                # the LLM about the ``exit`` shell builtin / Python call
                # and makes the host-layer slash surface feel inconsistent
                # with every other command (which all require the slash).
                if (
                    inbound.channel == "cli"
                    and inbound.sender_id == "cli-user"
                    and inbound.text.strip().lower() == "/exit"
                ):
                    # Shutdown handoff: reflect every live session and
                    # rotate so next launch starts clean. See
                    # ``AgentHost.flush_and_rotate`` for the contract.
                    # The status line is chosen *after* we know what
                    # actually happened — old code printed "reflecting…"
                    # unconditionally, which mis-sold the no-op case as
                    # real work. See ``FlushSummary``.
                    try:
                        summary = await host.flush_and_rotate()
                    except Exception:  # noqa: BLE001
                        # /exit must never be blocked by reflect. The
                        # cursor-does-not-advance contract means a crash
                        # here is picked up on the next PreCompact anyway.
                        log.exception("flush_and_rotate failed during /exit")
                        summary = FlushSummary()
                    # Message priority is *observations* first — the only
                    # signal the user actually cares about is whether
                    # memory grew. "reflected N session(s) wrote 0" is
                    # technically true but reads like progress when there
                    # was none. Separating the "we looked but saw nothing
                    # new" case avoids over-promising.
                    if summary.observations:
                        print(
                            f"  Powering down — reflected "
                            f"{summary.reflected} session(s), "
                            f"wrote {summary.observations} observation(s)."
                        )
                    elif summary.reflected:
                        print(
                            f"  Powering down — reviewed "
                            f"{summary.reflected} session(s); "
                            f"no new observations."
                        )
                    elif summary.rotated:
                        print(
                            f"  Powering down — rotated "
                            f"{summary.rotated} session(s) "
                            f"(reflect skipped)."
                        )
                    else:
                        print("  Powering down.")
                    stop_event.set()
                    break
                log.info(
                    "Picked up %s from %s/%s: %r",
                    inbound.sender_id,
                    inbound.channel,
                    inbound.peer_id,
                    inbound.text[:80],
                )
                pending_tasks.append(
                    loop.create_task(host.process_inbound(inbound)),
                )

            # Reap finished tasks so the list doesn't grow unbounded
            # over a day of traffic. Exceptions were already logged by
            # ``process_inbound`` itself; we just check ``.done()``.
            if pending_tasks:
                pending_tasks = [t for t in pending_tasks if not t.done()]

            if stop_event.is_set():
                break
            await asyncio.sleep(0.3)

        # Shutdown: drain any in-flight turns. Reflect during
        # ``flush_and_rotate`` happens synchronously *before* we reach
        # here, so this wait is purely about letting the user see the
        # assistant's last streamed token and the channel's final
        # ``send`` complete — not about memory consistency.
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        # Tier 2 lock-time coalescing may have spawned leftover-flush
        # tasks that aren't in ``pending_tasks`` (they were fired from
        # inside ``_release_or_flush_session``). Drain them with a
        # bounded timeout so reflect / memory writes complete cleanly.
        try:
            await host.drain_lock_flush_tasks(timeout=10.0)
        except Exception:  # noqa: BLE001
            log.exception("drain_lock_flush_tasks during shutdown failed")

        # Tier 1: close every cached streaming client so the
        # ``claude.exe`` subprocesses exit cleanly before we unwind the
        # event loop. Done HERE (inside the loop) rather than in the
        # outer ``finally`` because ``disconnect()`` is an ``async``
        # call and the outer block spins a fresh ``asyncio.run``. See
        # the note there about reflect being reachable from a new loop
        # — the same doesn't apply to the streaming cache, which is
        # tied to *this* loop's subprocess transports.
        try:
            await host.close_all_streaming_sessions(reason="shutdown")
        except Exception:  # noqa: BLE001
            log.exception("close_all_streaming_sessions during shutdown failed")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        # Mirror the ``/exit`` reflect path on any exit — Ctrl+C / a
        # crashing channel thread / SystemExit all land here instead
        # of the graceful ``/exit`` branch. Without this, a few hours
        # of interactive traffic can evaporate because reflect only
        # ever runs on heartbeat / PreCompact / clean /exit. The call
        # is synchronous in its own event loop; exceptions are
        # swallowed so a broken reflect never blocks shutdown.
        try:
            asyncio.run(host.flush_and_rotate())
        except Exception:  # noqa: BLE001
            log.exception("shutdown flush_and_rotate failed")
        scheduler.stop()
        channel_mgr.close_all()
        for t in bg_threads:
            t.join(timeout=5.0)
