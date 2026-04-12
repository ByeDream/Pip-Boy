from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import anthropic

from pip_agent.background import BackgroundTaskManager
from pip_agent.channels import (
    Channel,
    ChannelManager,
    CLIChannel,
    InboundMessage,
    WeChatChannel,
    WecomChannel,
    build_session_key,
    wechat_poll_loop,
    wecom_ws_loop,
)
from pip_agent.compact import (
    auto_compact,
    estimate_tokens,
    micro_compact,
)
from pip_agent.config import settings
from pip_agent.profiler import Profiler
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
USER_TEAM_DIR = WORKDIR / ".pip" / "team"

try:
    import readline  # noqa: F401 — enables input() history and line editing

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

SYSTEM_PROMPT = (
    f"You are Pip-Boy, a personal assistant agent. "
    f"Your working directory is {WORKDIR}. "
    f"Read AGENTS.md in your working directory before starting work."
)

NAG_THRESHOLD = 3

_TOOL_KEY_PARAM: dict[str, str] = {
    "bash": "command",
    "read": "file_path",
    "write": "file_path",
    "edit": "file_path",
    "glob": "pattern",
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
    "notify_user": "text",
}


def _tool_summary(name: str, inputs: dict) -> str:
    key = _TOOL_KEY_PARAM.get(name)
    if key and key in inputs:
        value = str(inputs[key])
        if len(value) > 80:
            value = value[:77] + "..."
        return f"{name}: {value}"
    return name


def agent_loop(
    client: anthropic.Anthropic,
    messages: list[dict],
    user_input: str,
    profiler: Profiler,
    plan_manager: PlanManager,
    *,
    tools: list[dict],
    system_prompt: str,
    skill_registry: SkillRegistry | None = None,
    transcripts_dir: Path | None = None,
    bg_manager: BackgroundTaskManager | None = None,
    team_manager: TeamManager | None = None,
    worktree_manager: WorktreeManager | None = None,
    channel: Channel | None = None,
    peer_id: str = "",
) -> str | None:
    """Run one agent turn.  Returns the final assistant text (if any)."""
    messages.append({"role": "user", "content": user_input})
    rounds_since_todo = 0
    last_input_tokens = 0
    final_text: str | None = None

    while True:
        micro_compact(messages)

        if bg_manager is not None:
            notifications = bg_manager.drain()
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
                        profiler.record(f"bg:bash", n.elapsed_ms)

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

        if transcripts_dir is not None and estimate_tokens(messages) > settings.compact_threshold:
            auto_compact(client, messages, system_prompt, transcripts_dir, profiler)

        profiler.start("api")
        try:
            response = client.messages.create(
                model=settings.model,
                max_tokens=settings.max_tokens,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
        except KeyboardInterrupt:
            profiler.stop()
            print("\n  [interrupted] API call cancelled.")
            break
        except anthropic.APIError as exc:
            profiler.stop()
            print(f"\n  [api_error] {exc}")
            break
        usage = response.usage
        last_input_tokens = usage.input_tokens
        profiler.stop(
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
                profiler=profiler,
                plan_manager=plan_manager,
                skill_registry=skill_registry,
                bg_manager=bg_manager,
                team_manager=team_manager,
                worktree_manager=worktree_manager,
                channel=channel,
                peer_id=peer_id,
            )
            for block in assistant_content:
                if settings.verbose and hasattr(block, "text"):
                    print()
                    print(block.text)
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
                    {"type": "text", "text": "<reminder>Update your tasks.</reminder>"}
                )
            messages.append({"role": "user", "content": tool_results})

            if compact_requested or last_input_tokens > settings.compact_threshold:
                if settings.verbose:
                    reason = "tool:compact" if compact_requested else f"input_tokens={last_input_tokens}"
                    print(f"  [context] auto_compact triggered ({reason})")
                if transcripts_dir is not None:
                    auto_compact(
                        client, messages, system_prompt, transcripts_dir, profiler
                    )
        else:
            final_text = "".join(
                b.text for b in assistant_content if hasattr(b, "text")
            )
            break

    return final_text


def _process_inbound(
    inbound: InboundMessage,
    conversations: dict[str, list[dict]],
    channel_mgr: ChannelManager,
    client: anthropic.Anthropic,
    profiler: Profiler,
    plan_manager: PlanManager,
    *,
    tools: list[dict],
    system_prompt: str,
    skill_registry: SkillRegistry | None = None,
    transcripts_dir: Path | None = None,
    bg_manager: BackgroundTaskManager | None = None,
    team_manager: TeamManager | None = None,
    worktree_manager: WorktreeManager | None = None,
) -> None:
    """Run agent_loop for one InboundMessage and route the reply."""
    sk = build_session_key(inbound.channel, inbound.peer_id)
    if sk not in conversations:
        conversations[sk] = []
    messages = conversations[sk]

    if inbound.channel == "wechat":
        wc = channel_mgr.get("wechat")
        if isinstance(wc, WeChatChannel):
            wc.send_typing(inbound.peer_id)

    ch = channel_mgr.get(inbound.channel)

    try:
        reply_text = agent_loop(
            client,
            messages,
            inbound.text,
            profiler,
            plan_manager,
            tools=tools,
            system_prompt=system_prompt,
            skill_registry=skill_registry,
            transcripts_dir=transcripts_dir,
            bg_manager=bg_manager,
            team_manager=team_manager,
            worktree_manager=worktree_manager,
            channel=ch,
            peer_id=inbound.peer_id,
        )
    except KeyboardInterrupt:
        print("\n  [interrupted] Returning to prompt.")
        return
    except anthropic.APIError as exc:
        print(f"\n  [api_error] {exc}")
        return

    if reply_text:
        if ch:
            if not ch.send(inbound.peer_id, reply_text):
                print(f"  [warning] Failed to send reply via {inbound.channel}, "
                      f"printing to terminal instead:")
                print(reply_text)

    profiler.flush()


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
    plan_manager = PlanManager(WORKDIR / ".pip" / "tasks")
    skill_registry = SkillRegistry(BUILTIN_SKILLS_DIR, USER_SKILLS_DIR)

    worktree_manager = WorktreeManager(WORKDIR)
    transcripts_dir = WORKDIR / ".pip" / "transcripts"
    team_manager = TeamManager(
        BUILTIN_TEAM_DIR,
        USER_TEAM_DIR,
        client,
        profiler,
        skill_registry=skill_registry,
        plan_manager=plan_manager,
        worktree_manager=worktree_manager,
    )

    tools: list[dict] = tools_for_role("lead")
    team_manager.patch_model_enum(tools)
    system_prompt = SYSTEM_PROMPT
    if skill_registry.available:
        tools.append(skill_registry.tool_schema())
        system_prompt += "\n\n" + skill_registry.catalog_prompt()

    # -- Channel setup --
    channel_mgr = ChannelManager()
    cli_channel = CLIChannel()
    channel_mgr.register(cli_channel)

    stop_event = threading.Event()
    poll_pause = threading.Event()
    msg_queue: list[InboundMessage] = []
    q_lock = threading.Lock()
    bg_threads: list[threading.Thread] = []
    has_remote_channels = False

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

    print(
        "============================================\n"
        "  ROBCO INDUSTRIES (TM) TERMLINK PROTOCOL\n"
        "  PIP-BOY 3000 MARK IV\n"
        "  Personal Assistant Module v0.1.0\n"
        "============================================\n"
        "  Welcome, Vault Dweller. Type 'exit' to\n"
        "  power down.\n"
        f"  Channels: {', '.join(channel_mgr.list_channels())}\n"
        "============================================"
    )

    conversations: dict[str, list[dict]] = {}
    cli_session_key = build_session_key("cli", "cli-user")
    conversations[cli_session_key] = []

    common_kwargs = dict(
        tools=tools,
        system_prompt=system_prompt,
        skill_registry=skill_registry,
        transcripts_dir=transcripts_dir,
        bg_manager=bg_manager,
        team_manager=team_manager,
        worktree_manager=worktree_manager,
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
                if inbound.channel == "cli":
                    if inbound.text.lower() == "exit":
                        stop_event.set()
                        break
                    if inbound.text == "/team":
                        print(team_manager.status())
                        continue
                    if inbound.text == "/inbox":
                        inbox = team_manager.peek_inbox()
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
                            inbound, conversations, channel_mgr, client, profiler,
                            plan_manager, **common_kwargs,
                        )
                    finally:
                        poll_pause.clear()
                else:
                    sk = build_session_key(inbound.channel, inbound.peer_id)
                    remote_buffers.setdefault(sk, []).append(inbound)

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

                        combined = InboundMessage(
                            text="\n".join(m.text for m in msgs),
                            sender_id=first.sender_id,
                            channel=first.channel,
                            peer_id=first.peer_id,
                        )
                        _process_inbound(
                            combined, conversations, channel_mgr, client, profiler,
                            plan_manager, **common_kwargs,
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
        messages = conversations[cli_session_key]

        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                team_manager.deactivate_all()
                break

            if user_input.lower() == "exit":
                team_manager.deactivate_all()
                break
            if not user_input:
                continue
            if user_input == "/team":
                print(team_manager.status())
                continue
            if user_input == "/inbox":
                inbox = team_manager.peek_inbox()
                print(json.dumps(inbox, indent=2) if inbox else "(no messages)")
                continue

            try:
                reply_text = agent_loop(
                    client,
                    messages,
                    user_input,
                    profiler,
                    plan_manager,
                    channel=cli_channel,
                    peer_id="cli-user",
                    **common_kwargs,
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
    team_manager.deactivate_all()
    channel_mgr.close_all()
    for t in bg_threads:
        t.join(timeout=5.0)
