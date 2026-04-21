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
from contextlib import nullcontext
from dataclasses import dataclass

from pip_agent.agent_runner import QueryResult, run_query
from pip_agent.channels import (
    Channel,
    ChannelManager,
    CLIChannel,
    InboundMessage,
    WeChatChannel,
    WecomChannel,
    send_with_retry,
    wechat_poll_loop,
    wecom_ws_loop,
)
from pip_agent.config import WORKDIR, settings
from pip_agent.host_scheduler import HostScheduler
from pip_agent.mcp_tools import McpContext
from pip_agent.memory import MemoryStore
from pip_agent.routing import (
    AgentRegistry,
    Binding,
    BindingTable,
    build_session_key,
    normalize_agent_id,
    resolve_effective_config,
)

log = logging.getLogger(__name__)

AGENTS_DIR = WORKDIR / ".pip" / "agents"
BINDINGS_PATH = AGENTS_DIR / "bindings.json"

SESSION_STORE_PATH = WORKDIR / ".pip" / "sdk_sessions.json"


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
    SESSION_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_STORE_PATH.write_text(
        json.dumps(sessions, indent=2, ensure_ascii=False), "utf-8",
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


def _format_prompt(
    inbound: InboundMessage,
    memory_store: MemoryStore | None,
) -> str:
    """Build the user-visible prompt from an InboundMessage.

    Dispatch order matters:

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


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


@dataclass
class _PerAgent:
    """Per-agent lazily-created service objects."""

    memory_store: MemoryStore


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
        self._max_concurrent = 3
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

    def _get_agent_services(self, agent_id: str) -> _PerAgent:
        if agent_id not in self._agents:
            ms = MemoryStore(base_dir=AGENTS_DIR, agent_id=agent_id)
            self._agents[agent_id] = _PerAgent(memory_store=ms)
        return self._agents[agent_id]

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
            workdir=WORKDIR,
            model=model,
            session_id=session_id,
            sender_id=sender_id,
            channel=channel,
            peer_id=peer_id,
            scheduler=self._scheduler,
        )

    async def process_inbound(self, inbound: InboundMessage) -> None:
        """Route one inbound message through the SDK agent and reply."""
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

        agent_cfg = self._registry.get_agent(agent_id) or self._registry.default_agent()
        eff = resolve_effective_config(agent_cfg, binding)

        svc = self._get_agent_services(eff.id)

        sk = build_session_key(
            agent_id=eff.id,
            channel=inbound.channel,
            peer_id=inbound.peer_id,
            guild_id=inbound.guild_id,
            is_group=inbound.is_group,
            dm_scope=eff.effective_dm_scope,
        )

        base_prompt = eff.system_prompt(workdir=str(WORKDIR))
        user_text = inbound.text if isinstance(inbound.text, str) else ""
        system_prompt = svc.memory_store.enrich_prompt(
            base_prompt, user_text,
            channel=inbound.channel,
            agent_id=eff.id,
            workdir=str(WORKDIR),
            sender_id=inbound.sender_id,
        )

        prompt = _format_prompt(inbound, svc.memory_store)

        ch = self._channel_mgr.get(inbound.channel)
        reply_peer = inbound.peer_id
        if inbound.is_group and inbound.guild_id:
            reply_peer = inbound.guild_id

        if inbound.channel == "wechat" and isinstance(ch, WeChatChannel):
            ch.send_typing(inbound.peer_id)

        current_session = self._sessions.get(sk)
        is_heartbeat = inbound.sender_id == _HEARTBEAT_SENDER
        # Scheduler-injected senders skip SDK session persistence —
        # see :func:`_is_ephemeral_sender` for the full rationale and
        # the measurements that motivated this. TL;DR: heartbeat / cron
        # poisoning the user transcript turns a 10 s cold start into a
        # 3 min one over the course of a day. ``stream_text=not is_heartbeat``
        # remains a separate concern (HEARTBEAT_OK silencing).
        is_ephemeral = _is_ephemeral_sender(inbound.sender_id)
        session_for_turn: str | None = None if is_ephemeral else current_session
        # mcp_ctx carries the session_id to the ``reflect`` MCP tool so
        # it can locate the JSONL. For ephemeral turns reflect would skip
        # anyway (no session → no transcript), but we pass an empty
        # string to be explicit and keep the two layers in sync.
        ctx_session_id = "" if is_ephemeral else (current_session or "")

        mcp_ctx = self._build_mcp_ctx(
            svc, eff.effective_model, inbound.sender_id,
            channel=ch, peer_id=reply_peer,
            session_id=ctx_session_id,
        )

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
        with tracker as tracked:
            async with self._semaphore:
                try:
                    result: QueryResult = await run_query(
                        prompt=prompt,
                        mcp_ctx=mcp_ctx,
                        model=eff.effective_model,
                        session_id=session_for_turn,
                        system_prompt_append=system_prompt,
                        cwd=WORKDIR,
                        # Heartbeats must NOT stream: we need to inspect the
                        # full reply before deciding whether to print (so we
                        # can silence the HEARTBEAT_OK sentinel). Everything
                        # else streams unconditionally — streaming is an
                        # interactive contract, not a debug toggle.
                        stream_text=not is_heartbeat,
                    )
                except Exception as exc:
                    log.error("SDK query failed for %s: %s", sk, exc)
                    tracked.failure(f"SDK query failed: {exc}")
                    if ch:
                        send_with_retry(ch, reply_peer, f"[error] {exc}")
                    return

            if result.error:
                # Soft failure — ``run_query`` returned normally but the
                # SDK reported a tool / API error. Count it toward the
                # cron auto-disable streak just like a raised exception.
                tracked.failure(result.error)

            # Skip persistence for ephemeral senders — their ``result.session_id``
            # is a throwaway the SDK minted for this one turn, and binding it to
            # ``sk`` would overwrite the user's real session on the next save.
            if not is_ephemeral and result.session_id:
                self._sessions[sk] = result.session_id
                _save_sessions(self._sessions)

            self._dispatch_reply(
                inbound=inbound,
                result=result,
                ch=ch,
                reply_peer=reply_peer,
                session_key=sk,
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

        if result.error:
            log.warning("Agent error for %s: %s", session_key, result.error)
            if inbound.channel == "cli":
                print(f"\n  [error] {result.error}")
            elif ch:
                send_with_retry(ch, reply_peer, f"[error] {result.error}")
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
                send_with_retry(ch, reply_peer, result.text)


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

    from pip_agent.scaffold import ensure_workspace

    ensure_workspace(WORKDIR)
    settings.check_required()

    registry = AgentRegistry(AGENTS_DIR)
    binding_table = BindingTable()
    binding_table.load(BINDINGS_PATH)

    channel_mgr = ChannelManager()
    cli_channel = CLIChannel()
    channel_mgr.register(cli_channel)

    stop_event = threading.Event()
    msg_queue: list[InboundMessage] = []
    q_lock = threading.Lock()
    bg_threads: list[threading.Thread] = []

    state_dir = WORKDIR / ".pip"

    wechat_channel: WeChatChannel | None = None
    if mode != "cli":
        try:
            wechat_channel = WeChatChannel(state_dir)
            if mode == "scan":
                wechat_channel._clear_creds()
                if not wechat_channel.login():
                    print("  [wechat] Login failed, falling back to CLI-only.")
                    wechat_channel = None
            elif not wechat_channel.is_logged_in:
                if not wechat_channel.login():
                    print("  [wechat] Login failed, falling back to CLI-only.")
                    wechat_channel = None
            if wechat_channel and wechat_channel.is_logged_in:
                channel_mgr.register(wechat_channel)
                t = threading.Thread(
                    target=wechat_poll_loop, daemon=True,
                    args=(wechat_channel, msg_queue, q_lock, stop_event),
                )
                t.start()
                bg_threads.append(t)
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
            wecom_channel = WecomChannel(
                settings.wecom_bot_id,
                settings.wecom_bot_secret,
                msg_queue,
                q_lock,
            )
            channel_mgr.register(wecom_channel)
            t = threading.Thread(
                target=wecom_ws_loop, daemon=True,
                args=(wecom_channel, stop_event),
            )
            t.start()
            bg_threads.append(t)
        except Exception as exc:
            print(f"  [wecom] Init failed: {exc}")

    scheduler = HostScheduler(
        agents_dir=AGENTS_DIR,
        msg_queue=msg_queue,
        q_lock=q_lock,
        stop_event=stop_event,
    )
    scheduler.start()

    host = AgentHost(
        registry=registry,
        binding_table=binding_table,
        channel_mgr=channel_mgr,
        scheduler=scheduler,
    )

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

        while not stop_event.is_set():
            with q_lock:
                batch = msg_queue[:]
                msg_queue.clear()

            # User-originated messages go first. With the scheduler's new
            # coalescing there is at most one in-flight cron/heartbeat per
            # key at a time, but if the user types while a batch has a cron
            # payload in it we still want the human message to run ahead of
            # the keepalive.
            if batch:
                batch.sort(key=_inbound_sort_key)

            tasks = []
            for inbound in batch:
                # Only real interactive CLI input can terminate the host; a
                # cron payload that happens to say "/exit" must not kill us.
                if (
                    inbound.channel == "cli"
                    and inbound.sender_id == "cli-user"
                    and inbound.text.strip().lower() in ("/exit", "exit")
                ):
                    stop_event.set()
                    break
                log.info(
                    "Picked up %s from %s/%s: %r",
                    inbound.sender_id,
                    inbound.channel,
                    inbound.peer_id,
                    inbound.text[:80],
                )
                tasks.append(
                    loop.create_task(host.process_inbound(inbound)),
                )

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            if stop_event.is_set():
                break
            await asyncio.sleep(0.3)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        scheduler.stop()
        channel_mgr.close_all()
        for t in bg_threads:
            t.join(timeout=5.0)
