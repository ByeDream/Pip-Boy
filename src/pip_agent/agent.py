from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic

from pip_agent.background import BackgroundTaskManager
from pip_agent.channels import (
    Attachment,
    Channel,
    ChannelManager,
    CLIChannel,
    InboundMessage,
    WeChatChannel,
    WecomChannel,
    wechat_poll_loop,
    wecom_ws_loop,
)
from pip_agent.commands import CommandContext, dispatch_command
from pip_agent.compact import (
    auto_compact,
    estimate_tokens,
    micro_compact,
    save_transcript,
)
from pip_agent.config import settings
from pip_agent.memory import MemoryStore
from pip_agent.memory.scheduler import MemoryScheduler
from pip_agent.profiler import Profiler
from pip_agent.routing import (
    AgentRegistry,
    BindingTable,
    build_session_key,
    resolve_effective_config,
)
from pip_agent.skills import SkillRegistry
from pip_agent.task_graph import PlanManager
from pip_agent.team import TeamManager
from pip_agent.tool_dispatch import ToolContext, dispatch_tool
from pip_agent.tools import (
    TASK_TOOL_NAMES,
    TEAM_TOOL_NAMES,
    WORKDIR,
    tools_for_role,
)
from pip_agent.worktree import WorktreeManager

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
USER_SKILLS_DIR = WORKDIR / ".pip" / "skills"

BUILTIN_TEAM_DIR = Path(__file__).resolve().parent / "team"

try:
    import readline  # noqa: F401 — enables input() history and line editing

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

AGENTS_DIR = WORKDIR / ".pip" / "agents"
BINDINGS_PATH = AGENTS_DIR / "bindings.json"

NAG_THRESHOLD = 3

_TOOL_KEY_PARAM: dict[str, str] = {
    "bash": "command",
    "read": "file_path",
    "write": "file_path",
    "edit": "file_path",
    "glob": "pattern",
    "grep": "pattern",
    "web_search": "query",
    "web_fetch": "url",
    "load_skill": "name",
    "task_create": "tasks",
    "task_update": "tasks",
    "task_remove": "task_ids",
    "task_submit": "task_id",
    "check_background": "task_id",
    "team_spawn": "name",
    "team_send": "to",
    "team_list_models": "",
    "claim_task": "task_id",
    "task_board_overview": "",
    "task_board_detail": "task_id",
    "remember_user": "name",
    "memory_write": "content",
    "memory_search": "query",
}


_LEADING_AT_RE = re.compile(r"^@\S*\s*")


@dataclass
class RuntimeContext:
    """Shared service objects threaded through agent_loop / _process_inbound."""

    client: anthropic.Anthropic
    profiler: Profiler
    tools: list[dict]
    skill_registry: SkillRegistry | None = None
    bg_manager: BackgroundTaskManager | None = None
    memory_store: MemoryStore | None = None


def _tool_summary(name: str, inputs: dict) -> str:
    key = _TOOL_KEY_PARAM.get(name)
    if key and key in inputs:
        value = str(inputs[key])
        if len(value) > 80:
            value = value[:77] + "..."
        return f"{name}: {value}"
    return name


def agent_loop(
    ctx: RuntimeContext,
    messages: list[dict],
    user_input: str | list,
    plan_manager: PlanManager,
    *,
    system_prompt: str,
    model: str = "",
    max_tokens: int = 0,
    compact_threshold: int = 0,
    compact_micro_age: int = 0,
    transcripts_dir: Path | None = None,
    team_manager: TeamManager | None = None,
    worktree_manager: WorktreeManager | None = None,
    channel: Channel | None = None,
    peer_id: str = "",
    sender_id: str = "",
) -> str | None:
    """Run one agent turn.  Returns the final assistant text (if any)."""
    from pip_agent.routing import DEFAULT_COMPACT_THRESHOLD, DEFAULT_MAX_TOKENS, DEFAULT_MODEL

    effective_model = model or DEFAULT_MODEL
    effective_max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    effective_compact_threshold = compact_threshold or DEFAULT_COMPACT_THRESHOLD

    if ctx.memory_store:
        _state = ctx.memory_store.load_state()
        _state["last_activity_at"] = time.time()
        ctx.memory_store.save_state(_state)

    messages.append({"role": "user", "content": user_input})
    rounds_since_todo = 0
    last_input_tokens = 0
    final_text: str | None = None

    while True:
        micro_compact(messages, max_age=compact_micro_age or None)

        if ctx.bg_manager is not None:
            notifications = ctx.bg_manager.drain()
            if notifications:
                last_msg = messages[-1]
                if last_msg["role"] == "user":
                    if isinstance(last_msg["content"], str):
                        last_msg["content"] = [
                            {"type": "text", "text": last_msg["content"]},
                        ]
                    for n in notifications:
                        last_msg["content"].append({
                            "type": "text",
                            "text": (
                                f'<background-result task_id="{n.task_id}"'
                                f' status="{n.status}"'
                                f' elapsed_ms="{n.elapsed_ms:.0f}">'
                                f"\n{n.result}\n</background-result>"
                            ),
                        })
                        ctx.profiler.record("bg:bash", n.elapsed_ms)

        if team_manager is not None:
            inbox = team_manager.read_inbox()
            if inbox:
                last_msg = messages[-1]
                if last_msg["role"] == "user":
                    if isinstance(last_msg["content"], str):
                        last_msg["content"] = [
                            {"type": "text", "text": last_msg["content"]},
                        ]
                    for msg in inbox:
                        attrs = (
                            f'from="{msg["from"]}"'
                            f' msg_type="{msg.get("type", "message")}"'
                        )
                        if "req_id" in msg:
                            attrs += f' req_id="{msg["req_id"]}"'
                        if "approve" in msg:
                            attrs += f' approve="{msg["approve"]}"'
                        last_msg["content"].append({
                            "type": "text",
                            "text": (
                                f"<team-message {attrs}>"
                                f'\n{msg["content"]}\n</team-message>'
                            ),
                        })

        if transcripts_dir is not None and estimate_tokens(messages) > effective_compact_threshold:
            current_input = messages[-1]["content"] if messages[-1]["role"] == "user" else None
            has_media = isinstance(current_input, list) and any(
                isinstance(b, dict) and b.get("type") not in ("text", "tool_result")
                for b in current_input
            )
            auto_compact(ctx.client, messages, system_prompt, transcripts_dir, ctx.profiler)
            if has_media and current_input is not None:
                summary_text = messages[0]["content"]
                blocks: list[dict] = [{"type": "text", "text": summary_text}]
                blocks.extend(current_input)
                messages[0]["content"] = blocks

        ctx.profiler.start("api")
        try:
            response = ctx.client.messages.create(
                model=effective_model,
                max_tokens=effective_max_tokens,
                system=system_prompt,
                tools=ctx.tools,
                messages=messages,
            )
        except KeyboardInterrupt:
            ctx.profiler.stop()
            print("\n  [interrupted] API call cancelled.")
            break
        except anthropic.APIError as exc:
            ctx.profiler.stop()
            print(f"\n  [api_error] {exc}")
            break
        usage = response.usage
        last_input_tokens = usage.input_tokens
        ctx.profiler.stop(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            stop=response.stop_reason,
        )

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "tool_use":
            tool_results = []
            used_task_tool = False
            compact_requested = False
            tool_ctx = ToolContext(
                profiler=ctx.profiler,
                plan_manager=plan_manager,
                skill_registry=ctx.skill_registry,
                bg_manager=ctx.bg_manager,
                team_manager=team_manager,
                worktree_manager=worktree_manager,
                memory_store=ctx.memory_store,
                channel=channel,
                peer_id=peer_id,
                sender_id=sender_id,
                workdir=WORKDIR,
                client=ctx.client,
                transcripts_dir=transcripts_dir,
                messages=messages,
            )
            for block in assistant_content:
                if hasattr(block, "text") and block.text.strip():
                    if settings.verbose:
                        print()
                        print(block.text)
                    if channel and channel.name != "cli":
                        channel.send(peer_id, block.text)
                if block.type == "tool_use":
                    if settings.verbose:
                        print()
                        print(f"> {_tool_summary(block.name, block.input)}")
                    outcome = dispatch_tool(tool_ctx, block.name, block.input)
                    result = outcome.content
                    used_task_tool |= outcome.used_task_tool
                    compact_requested |= outcome.compact_requested
                    if settings.verbose and block.name in (
                        TASK_TOOL_NAMES | TEAM_TOOL_NAMES
                    ):
                        print(result)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            rounds_since_todo = 0 if used_task_tool else rounds_since_todo + 1
            if plan_manager.has_tasks() and rounds_since_todo >= NAG_THRESHOLD:
                tool_results.append(
                    {
                        "type": "text",
                        "text": "<system_reminder>Update your tasks.</system_reminder>",
                    }
                )
            messages.append({"role": "user", "content": tool_results})

            if compact_requested or last_input_tokens > effective_compact_threshold:
                if settings.verbose:
                    reason = (
                        "tool:compact"
                        if compact_requested
                        else f"input_tokens={last_input_tokens}"
                    )
                    print(f"  [context] auto_compact triggered ({reason})")
                if transcripts_dir is not None:
                    auto_compact(
                        ctx.client, messages, system_prompt, transcripts_dir, ctx.profiler
                    )
        else:
            final_text = "".join(
                b.text for b in assistant_content if hasattr(b, "text")
            )
            break

    if transcripts_dir is not None and messages:
        save_transcript(messages, transcripts_dir)

    return final_text


def _process_inbound(
    inbound: InboundMessage,
    conversations: dict[str, list[dict]],
    channel_mgr: ChannelManager,
    ctx: RuntimeContext,
    plan_managers: dict[str, PlanManager],
    *,
    registry: AgentRegistry,
    binding_table: BindingTable,
    team_managers: dict[str, TeamManager] | None = None,
    worktree_managers: dict[str, WorktreeManager] | None = None,
) -> None:
    """Run agent_loop for one InboundMessage and route the reply."""
    agent_id, binding = binding_table.resolve(
        channel=inbound.channel,
        account_id=inbound.account_id,
        guild_id=inbound.guild_id,
        peer_id=inbound.peer_id,
    )
    if not agent_id:
        agent_id = registry.default_agent().id

    agent_cfg = registry.get_agent(agent_id)
    if not agent_cfg:
        agent_cfg = registry.default_agent()

    effective = resolve_effective_config(agent_cfg, binding)

    per_agent_transcripts = AGENTS_DIR / effective.id / "transcripts"
    per_agent_transcripts.mkdir(parents=True, exist_ok=True)

    if effective.id not in plan_managers:
        plan_managers[effective.id] = PlanManager(AGENTS_DIR / effective.id / "tasks")
    plan_manager = plan_managers[effective.id]

    worktree_manager: WorktreeManager | None = None
    if worktree_managers is not None:
        if effective.id not in worktree_managers:
            worktree_managers[effective.id] = WorktreeManager(WORKDIR, agent_id=effective.id)
        worktree_manager = worktree_managers[effective.id]

    team_manager: TeamManager | None = None
    if team_managers is not None:
        if effective.id not in team_managers:
            agent_team_dir = AGENTS_DIR / effective.id / "team"
            agent_team_dir.mkdir(parents=True, exist_ok=True)
            team_managers[effective.id] = TeamManager(
                BUILTIN_TEAM_DIR,
                agent_team_dir,
                ctx.client,
                ctx.profiler,
                skill_registry=ctx.skill_registry,
                plan_manager=plan_manager,
                worktree_manager=worktree_manager,
                pip_dir=WORKDIR / ".pip",
                workdir=WORKDIR,
            )
            team_managers[effective.id].patch_model_enum(ctx.tools)
        team_manager = team_managers[effective.id]

    if settings.verbose:
        print(
            f"  [route] agent={effective.id!r} model={effective.effective_model!r}"
            f" binding={binding!r}"
        )

    sk = build_session_key(
        agent_id=effective.id,
        channel=inbound.channel,
        peer_id=inbound.peer_id,
        guild_id=inbound.guild_id,
        is_group=inbound.is_group,
        dm_scope=effective.effective_dm_scope,
    )
    if sk not in conversations:
        conversations[sk] = []
    messages = conversations[sk]

    system_prompt = effective.system_prompt(workdir=str(WORKDIR))
    if ctx.skill_registry and ctx.skill_registry.available:
        system_prompt += "\n\n" + ctx.skill_registry.catalog_prompt()

    clean_text = (
        _LEADING_AT_RE.sub("", inbound.text, count=1)
        if isinstance(inbound.text, str) else inbound.text
    )

    user_text = clean_text if isinstance(clean_text, str) else ""
    if ctx.memory_store:
        system_prompt = ctx.memory_store.enrich_prompt(
            system_prompt, user_text,
            channel=inbound.channel,
            agent_id=effective.id,
            workdir=str(WORKDIR),
            sender_id=inbound.sender_id,
        )

    user_input: str | list = clean_text

    sender_status = "unverified"
    if ctx.memory_store and inbound.sender_id:
        _profile = ctx.memory_store.find_profile_by_sender(
            inbound.channel, inbound.sender_id,
        )
        if _profile:
            _name = ctx.memory_store.extract_profile_name(_profile)
            sender_status = f"verified:{_name}" if _name else "verified"

    if inbound.is_group:
        user_input = (
            f'<user_query from="{inbound.sender_id}" status="{sender_status}" group="true">'
            f"\n{clean_text}\n</user_query>"
        )
    elif inbound.sender_id:
        user_input = (
            f'<user_query from="{inbound.channel}:{inbound.sender_id}"'
            f' status="{sender_status}">'
            f"\n{clean_text}\n</user_query>"
        )
    else:
        user_input = f"<user_query>\n{clean_text}\n</user_query>"

    if inbound.attachments:
        content_blocks: list[dict] = []
        if user_input:
            content_blocks.append({"type": "text", "text": user_input})
        for att in inbound.attachments:
            if att.type == "image" and att.data:
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime_type or "image/jpeg",
                        "data": base64.b64encode(att.data).decode(),
                    },
                })
            elif att.type == "image":
                content_blocks.append({"type": "text", "text": att.text or "[Image]"})
            elif att.type == "file" and att.text:
                content_blocks.append({
                    "type": "text",
                    "text": f'<attached-file name="{att.filename}">\n{att.text}\n</attached-file>',
                })
            elif att.type == "file":
                content_blocks.append({
                    "type": "text",
                    "text": f"[File: {att.filename}] (binary, cannot display)",
                })
            elif att.type == "voice":
                content_blocks.append({
                    "type": "text",
                    "text": f"[Voice transcription]: {att.text}" if att.text else "[Voice message]",
                })
        if content_blocks:
            user_input = content_blocks

    if inbound.channel == "wechat":
        wc = channel_mgr.get("wechat")
        if isinstance(wc, WeChatChannel):
            wc.send_typing(inbound.peer_id)

    ch = channel_mgr.get(inbound.channel)
    reply_peer = inbound.peer_id
    if inbound.is_group and inbound.guild_id:
        reply_peer = inbound.guild_id

    try:
        reply_text = agent_loop(
            ctx,
            messages,
            user_input,
            plan_manager,
            system_prompt=system_prompt,
            model=effective.effective_model,
            max_tokens=effective.effective_max_tokens,
            compact_threshold=effective.effective_compact_threshold,
            compact_micro_age=effective.effective_compact_micro_age,
            transcripts_dir=per_agent_transcripts,
            team_manager=team_manager,
            worktree_manager=worktree_manager,
            channel=ch,
            peer_id=reply_peer,
            sender_id=inbound.sender_id,
        )
    except KeyboardInterrupt:
        print("\n  [interrupted] Returning to prompt.")
        return
    except anthropic.APIError as exc:
        print(f"\n  [api_error] {exc}")
        return

    if reply_text:
        if ch:
            if not ch.send(reply_peer, reply_text):
                print(f"  [warning] Failed to send reply via {inbound.channel}, "
                      f"printing to terminal instead:")
                print(reply_text)

    ctx.profiler.flush()


def _stdin_reader_thread(
    queue: list[InboundMessage],
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Read stdin lines and push as CLI InboundMessages.  Daemon thread."""
    while not stop.is_set():
        try:
            line = sys.stdin.readline()
        except (EOFError, OSError):
            break
        if not line:
            break
        text = line.strip()
        if text:
            msg = InboundMessage(
                text=text,
                sender_id="cli-user",
                channel="cli",
                peer_id="cli-user",
            )
            with lock:
                queue.append(msg)


def run(mode: str = "auto") -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    from pip_agent.scaffold import ensure_workspace

    ensure_workspace(WORKDIR)
    settings.check_required()

    # -- Routing setup --
    registry = AgentRegistry(AGENTS_DIR)
    binding_table = BindingTable()
    binding_table.load(BINDINGS_PATH)

    default_agent = registry.default_agent()

    client_kwargs: dict = {"api_key": settings.anthropic_api_key}
    if settings.anthropic_base_url:
        client_kwargs["base_url"] = settings.anthropic_base_url
        client_kwargs["default_headers"] = {
            "Authorization": f"Bearer {settings.anthropic_api_key}",
        }
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    client = anthropic.Anthropic(**client_kwargs)
    profiler = Profiler()
    bg_manager = BackgroundTaskManager()
    plan_manager = PlanManager(AGENTS_DIR / default_agent.id / "tasks")
    plan_managers: dict[str, PlanManager] = {default_agent.id: plan_manager}
    skill_registry = SkillRegistry(BUILTIN_SKILLS_DIR, USER_SKILLS_DIR)

    worktree_managers: dict[str, WorktreeManager] = {}
    worktree_managers[default_agent.id] = WorktreeManager(WORKDIR, agent_id=default_agent.id)

    transcripts_dir = AGENTS_DIR / default_agent.id / "transcripts"
    memory_store = MemoryStore(
        base_dir=AGENTS_DIR,
        agent_id=default_agent.id,
    )
    default_team_dir = AGENTS_DIR / default_agent.id / "team"
    default_team_dir.mkdir(parents=True, exist_ok=True)

    team_managers: dict[str, TeamManager] = {}
    team_managers[default_agent.id] = TeamManager(
        BUILTIN_TEAM_DIR,
        default_team_dir,
        client,
        profiler,
        skill_registry=skill_registry,
        plan_manager=plan_manager,
        worktree_manager=worktree_managers[default_agent.id],
        pip_dir=WORKDIR / ".pip",
        workdir=WORKDIR,
    )

    tools: list[dict] = tools_for_role("lead")
    team_managers[default_agent.id].patch_model_enum(tools)
    if skill_registry.available:
        tools.append(skill_registry.tool_schema())

    # -- Channel setup --
    channel_mgr = ChannelManager()
    cli_channel = CLIChannel()
    channel_mgr.register(cli_channel)

    stop_event = threading.Event()
    agent_active_event = threading.Event()
    poll_pause = threading.Event()
    msg_queue: list[InboundMessage] = []
    q_lock = threading.Lock()
    bg_threads: list[threading.Thread] = []
    has_remote_channels = False

    memory_scheduler = MemoryScheduler(
        memory_store, client, transcripts_dir, stop_event,
        model=settings.model,
        active_event=agent_active_event,
    )
    mem_thread = threading.Thread(target=memory_scheduler.run, daemon=True)
    mem_thread.start()
    bg_threads.append(mem_thread)

    state_dir = WORKDIR / ".pip"

    # WeChat iLink
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
                    args=(wechat_channel, msg_queue, q_lock, stop_event, poll_pause),
                )
                t.start()
                bg_threads.append(t)
                has_remote_channels = True
        except Exception as exc:
            print(f"  [wechat] Init failed: {exc}")

    # WeCom WebSocket
    wecom_channel: WecomChannel | None = None
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
            has_remote_channels = True
        except Exception as exc:
            print(f"  [wecom] Init failed: {exc}")

    agents_list = ", ".join(a.id for a in registry.list_agents())
    from pip_agent import __version__
    print(
        "============================================\n"
        "  ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL\n"
        "  PIP-BOY 3000 MARK IV\n"
        f"  Personal Assistant Module v{__version__}\n"
        "============================================\n"
        "  Welcome, Vault Dweller. Type '/exit' to\n"
        "  power down.\n"
        f"  Channels: {', '.join(channel_mgr.list_channels())}\n"
        f"  Agents: {agents_list}\n"
        "============================================"
    )

    conversations: dict[str, list[dict]] = {}
    cli_session_key = build_session_key(
        agent_id=default_agent.id,
        channel="cli",
        peer_id="cli-user",
        dm_scope=default_agent.effective_dm_scope,
    )
    conversations[cli_session_key] = []

    rt_ctx = RuntimeContext(
        client=client,
        profiler=profiler,
        tools=tools,
        skill_registry=skill_registry,
        bg_manager=bg_manager,
        memory_store=memory_store,
    )

    common_kwargs = dict(
        registry=registry,
        binding_table=binding_table,
        team_managers=team_managers,
        worktree_managers=worktree_managers,
    )

    if has_remote_channels:
        # Multi-channel mode: stdin reader thread + shared queue
        stdin_thread = threading.Thread(
            target=_stdin_reader_thread, daemon=True,
            args=(msg_queue, q_lock, stop_event),
        )
        stdin_thread.start()
        print("  (multi-channel mode: type and press Enter)")

        remote_buffers: dict[str, list[InboundMessage]] = {}

        while not stop_event.is_set():
            with q_lock:
                batch = msg_queue[:]
                msg_queue.clear()

            for inbound in batch:
                # -- Unified slash command dispatch (all channels) --
                if settings.verbose:
                    print(f"  [dispatch] channel={inbound.channel} text={inbound.text!r}")
                cmd_ctx = CommandContext(
                    inbound=inbound,
                    registry=registry,
                    bindings=binding_table,
                    bindings_path=BINDINGS_PATH,
                    workdir=str(WORKDIR),
                    memory_store=memory_store,
                )
                result = dispatch_command(cmd_ctx)
                if settings.verbose:
                    print(f"  [dispatch] handled={result.handled} response={result.response!r}")
                if result.handled:
                    if result.response:
                        ch = channel_mgr.get(inbound.channel)
                        if ch:
                            target = (
                                inbound.guild_id
                                if inbound.is_group and inbound.guild_id
                                else inbound.peer_id
                            )
                            ok = ch.send(target, result.response)
                            if settings.verbose:
                                print(f"  [dispatch] send to={target!r} ok={ok}")
                    if result.exit_requested:
                        stop_event.set()
                    continue

                # -- Legacy CLI-only commands --
                if inbound.channel == "cli":
                    if inbound.text.lower() == "exit":
                        stop_event.set()
                        break
                    if inbound.text == "/team":
                        print(team_managers[default_agent.id].status())
                        continue
                    if inbound.text == "/inbox":
                        inbox = team_managers[default_agent.id].peek_inbox()
                        print(json.dumps(inbox, indent=2) if inbox else "(no messages)")
                        continue
                    if inbound.text == "/channels":
                        for name in channel_mgr.list_channels():
                            print(f"  - {name}")
                        continue

                    if settings.verbose:
                        print(f"\n  [cli] {inbound.text[:80]}")

                    poll_pause.set()
                    try:
                        _process_inbound(
                            inbound, conversations, channel_mgr, rt_ctx,
                            plan_managers, **common_kwargs,
                        )
                    finally:
                        poll_pause.clear()
                else:
                    buf_key = f"{inbound.channel}:{inbound.guild_id or inbound.peer_id}"
                    remote_buffers.setdefault(buf_key, []).append(inbound)

            if stop_event.is_set():
                break

            ready = []
            for sk, msgs in remote_buffers.items():
                if not msgs:
                    continue
                first = msgs[0]
                ch = channel_mgr.get(first.channel)
                if not ch:
                    continue
                if isinstance(ch, WeChatChannel) and not ch.has_context_token(first.peer_id):
                    continue
                ready.append(sk)

            if ready:
                poll_pause.set()
                try:
                    for sk in ready:
                        msgs = remote_buffers[sk]
                        remote_buffers[sk] = []
                        first = msgs[0]

                        all_atts: list[Attachment] = []
                        for m in msgs:
                            all_atts.extend(m.attachments)
                        combined = InboundMessage(
                            text="\n".join(m.text for m in msgs if m.text),
                            sender_id=first.sender_id,
                            channel=first.channel,
                            peer_id=first.peer_id,
                            guild_id=first.guild_id,
                            account_id=first.account_id,
                            is_group=first.is_group,
                            attachments=all_atts,
                        )
                        _process_inbound(
                            combined, conversations, channel_mgr, rt_ctx,
                            plan_managers, **common_kwargs,
                        )
                finally:
                    poll_pause.clear()

            # drain background tasks for CLI session
            cli_messages = conversations.get(cli_session_key, [])
            while bg_manager.has_pending():
                if settings.verbose:
                    print("  (waiting for background tasks...)")
                time.sleep(1)
                notifications = bg_manager.drain()
                if notifications:
                    for n in notifications:
                        profiler.record("bg:bash", n.elapsed_ms)
                        if settings.verbose:
                            print(
                                f"  [bg:{n.task_id} {n.status}"
                                f" ({n.elapsed_ms:.0f}ms)] {n.result}"
                            )
                    parts = [
                        f'<background-result task_id="{n.task_id}"'
                        f' status="{n.status}"'
                        f' elapsed_ms="{n.elapsed_ms:.0f}">'
                        f"\n{n.result}\n</background-result>"
                        for n in notifications
                    ]
                    cli_messages.append({
                        "role": "user",
                        "content": (
                            "<background-results>\n"
                            + "".join(parts)
                            + "\n</background-results>"
                        ),
                    })

            if not batch and not any(remote_buffers.values()):
                time.sleep(0.3)
    else:
        # CLI-only mode: original blocking REPL (preserves readline, etc.)
        default_tm = team_managers[default_agent.id]
        default_wm = worktree_managers[default_agent.id]
        messages = conversations[cli_session_key]
        cli_system_prompt_base = default_agent.system_prompt(workdir=str(WORKDIR))
        if skill_registry.available:
            cli_system_prompt_base += "\n\n" + skill_registry.catalog_prompt()

        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                for tm in team_managers.values():
                    tm.deactivate_all()
                break

            if not user_input:
                continue

            # -- Unified slash command dispatch --
            if user_input.startswith("/"):
                cli_inbound = InboundMessage(
                    text=user_input,
                    sender_id="cli-user",
                    channel="cli",
                    peer_id="cli-user",
                )
                cmd_ctx = CommandContext(
                    inbound=cli_inbound,
                    registry=registry,
                    bindings=binding_table,
                    bindings_path=BINDINGS_PATH,
                    workdir=str(WORKDIR),
                    memory_store=memory_store,
                )
                result = dispatch_command(cmd_ctx)
                if result.handled:
                    if result.response:
                        print(result.response)
                    if result.exit_requested:
                        for tm in team_managers.values():
                            tm.deactivate_all()
                        break
                    continue

            # -- Legacy bare 'exit' compat --
            if user_input.lower() == "exit":
                for tm in team_managers.values():
                    tm.deactivate_all()
                break

            if user_input == "/team":
                print(default_tm.status())
                continue
            if user_input == "/inbox":
                inbox = default_tm.peek_inbox()
                print(json.dumps(inbox, indent=2) if inbox else "(no messages)")
                continue

            cli_system_prompt = memory_store.enrich_prompt(
                cli_system_prompt_base, user_input,
                channel="cli",
                agent_id=default_agent.id,
                workdir=str(WORKDIR),
                sender_id="cli-user",
            )

            try:
                reply_text = agent_loop(
                    rt_ctx,
                    messages,
                    user_input,
                    plan_manager,
                    system_prompt=cli_system_prompt,
                    model=default_agent.effective_model,
                    max_tokens=default_agent.effective_max_tokens,
                    compact_threshold=default_agent.effective_compact_threshold,
                    compact_micro_age=default_agent.effective_compact_micro_age,
                    channel=cli_channel,
                    peer_id="cli-user",
                    sender_id="cli-user",
                    transcripts_dir=transcripts_dir,
                    team_manager=default_tm,
                    worktree_manager=default_wm,
                )
            except KeyboardInterrupt:
                print("\n  [interrupted] Returning to prompt.")
                continue
            except anthropic.APIError as exc:
                print(f"\n  [api_error] {exc}")
                continue

            if reply_text:
                cli_channel.send("cli-user", reply_text)

            while bg_manager.has_pending():
                if settings.verbose:
                    print("  (waiting for background tasks...)")
                time.sleep(1)
                notifications = bg_manager.drain()
                if notifications:
                    for n in notifications:
                        profiler.record("bg:bash", n.elapsed_ms)
                        if settings.verbose:
                            print(
                                f"  [bg:{n.task_id} {n.status}"
                                f" ({n.elapsed_ms:.0f}ms)] {n.result}"
                            )
                    parts = [
                        f'<background-result task_id="{n.task_id}"'
                        f' status="{n.status}"'
                        f' elapsed_ms="{n.elapsed_ms:.0f}">'
                        f"\n{n.result}\n</background-result>"
                        for n in notifications
                    ]
                    messages.append({
                        "role": "user",
                        "content": (
                            "<background-results>\n"
                            + "".join(parts)
                            + "\n</background-results>"
                        ),
                    })

            profiler.flush()

    # -- Cleanup --
    stop_event.set()
    for tm in team_managers.values():
        tm.deactivate_all()
    channel_mgr.close_all()
    for t in bg_threads:
        t.join(timeout=5.0)
