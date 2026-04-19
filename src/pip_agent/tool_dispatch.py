from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

from pip_agent.tools import (  # noqa: E402
    run_bash,
    run_download,
    run_edit,
    run_glob,
    run_grep,
    run_read,
    run_web_fetch,
    run_web_search,
    run_write,
)

if TYPE_CHECKING:
    import anthropic

    from pip_agent.background import BackgroundTaskManager
    from pip_agent.channels import Channel
    from pip_agent.memory import MemoryStore
    from pip_agent.profiler import Profiler
    from pip_agent.skills import SkillRegistry
    from pip_agent.task_graph import PlanManager
    from pip_agent.team import TeamManager
    from pip_agent.worktree import WorktreeManager


@dataclass
class DispatchResult:
    content: str | list[dict]
    used_task_tool: bool = False
    compact_requested: bool = False
    request_idle: bool = False


@dataclass
class TeammateToolSurface:
    """Bound callables for teammate-only tools (avoids importing team from here)."""

    send: Callable[[dict], str]
    read_inbox: Callable[[], str]
    request_idle: Callable[[], None]


@dataclass
class ToolContext:
    profiler: Profiler | None = None
    plan_manager: PlanManager | None = None
    skill_registry: SkillRegistry | None = None
    bg_manager: BackgroundTaskManager | None = None
    team_manager: TeamManager | None = None
    worktree_manager: WorktreeManager | None = None
    memory_store: MemoryStore | None = None
    teammate: TeammateToolSurface | None = None
    caller: str = "lead"
    workdir: Path | None = None
    channel: Channel | None = None
    peer_id: str = ""
    sender_id: str = ""
    client: anthropic.Anthropic | None = None
    transcripts_dir: Path | None = None
    messages: list[dict] | None = None
    scheduler: Any | None = None
    model: str = ""


def _handle_plan_tool(name: str, inputs: dict, pm: PlanManager) -> str:
    story = inputs.get("story")
    if name == "task_create":
        return pm.create(story, inputs.get("tasks", []))
    if name == "task_update":
        return pm.update(story, inputs.get("tasks", []))
    if name == "task_list":
        return pm.render(story)
    if name == "task_remove":
        return pm.remove(story, inputs.get("task_ids", []))
    return f"Unknown task tool: {name}"


def _wrap_simple(
    run: Callable, inp: dict, *, workdir: Path | None = None,
) -> str:
    try:
        return run(inp, workdir=workdir) if workdir else run(inp)
    except ValueError as e:
        return f"[blocked] {e}"
    except OSError as e:
        log.warning("Tool raised critical OS error: %s", e)
        return f"[critical_error] {e}"
    except Exception as e:
        return f"[error] {e}"


def _handle_compact(_ctx: ToolContext, _inp: dict) -> DispatchResult:
    return DispatchResult(
        content="Acknowledged. Context will be compacted.",
        compact_requested=True,
    )


def _make_task_handler(tool_name: str) -> Callable[[ToolContext, dict], DispatchResult]:
    def _handler(ctx: ToolContext, inp: dict) -> DispatchResult:
        if ctx.plan_manager is None:
            return DispatchResult(content=f"Unknown tool: {tool_name}")
        try:
            text = _handle_plan_tool(tool_name, inp, ctx.plan_manager)
        except ValueError as e:
            return DispatchResult(
                content=f"[error] {e}",
                used_task_tool=True,
            )
        return DispatchResult(content=text, used_task_tool=True)

    return _handler


def _handle_load_skill(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.skill_registry is None:
        return DispatchResult(content="Unknown tool: load_skill")
    name = inp.get("name", "")
    if not name:
        return DispatchResult(content="[error] 'name' is required for load_skill")
    return DispatchResult(content=ctx.skill_registry.load(name))



def _handle_bash(ctx: ToolContext, inp: dict) -> DispatchResult:
    if inp.get("background"):
        if ctx.bg_manager is not None:
            from functools import partial
            task_id = uuid.uuid4().hex[:8]
            fn = partial(run_bash, workdir=ctx.workdir) if ctx.workdir else run_bash
            ctx.bg_manager.spawn(task_id, inp["command"], fn, inp)
            return DispatchResult(content=f"[background:{task_id}] started")
        log.warning("background=True requested but bg_manager is None; running foreground")
    return DispatchResult(content=_wrap_simple(run_bash, inp, workdir=ctx.workdir))


def _handle_check_background(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.bg_manager is None:
        return DispatchResult(content="Unknown tool: check_background")
    return DispatchResult(content=ctx.bg_manager.check(inp.get("task_id")))


def _handle_team_spawn(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_spawn")
    missing = [k for k in ("name", "prompt", "model", "max_turns") if k not in inp]
    if missing:
        return DispatchResult(content=f"[error] Missing required fields: {', '.join(missing)}")
    text = ctx.team_manager.spawn(
        inp["name"],
        inp["prompt"],
        model=inp["model"],
        max_turns=inp["max_turns"],
    )
    return DispatchResult(content=text)


def _handle_team_send(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_send")
    missing = [k for k in ("to", "content") if k not in inp]
    if missing:
        return DispatchResult(content=f"[error] Missing required fields: {', '.join(missing)}")
    extra: dict = {}
    for key in ("req_id", "approve"):
        if key in inp:
            extra[key] = inp[key]
    text = ctx.team_manager.send(
        inp["to"],
        inp["content"],
        inp.get("msg_type", "message"),
        **extra,
    )
    return DispatchResult(content=text)


def _handle_team_read_inbox(ctx: ToolContext, _inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_read_inbox")
    inbox = ctx.team_manager.read_inbox()
    text = json.dumps(inbox, indent=2) if inbox else "(no messages)"
    return DispatchResult(content=text)


def _handle_team_status(ctx: ToolContext, _inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_status")
    return DispatchResult(content=ctx.team_manager.status())


def _handle_team_list_models(ctx: ToolContext, _inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_list_models")
    return DispatchResult(content=ctx.team_manager.list_models())


def _handle_team_create(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_create")
    missing = [k for k in ("name", "description", "system_prompt") if k not in inp]
    if missing:
        return DispatchResult(content=f"[error] Missing required fields: {', '.join(missing)}")
    text = ctx.team_manager.create_teammate(
        inp["name"], inp["description"], inp["system_prompt"],
    )
    return DispatchResult(content=text)


def _handle_team_edit(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_edit")
    name = inp.get("name", "")
    if not name:
        return DispatchResult(content="[error] 'name' is required for team_edit")
    updates: dict[str, str] = {}
    if "description" in inp:
        updates["description"] = inp["description"]
    if "system_prompt" in inp:
        updates["system_prompt"] = inp["system_prompt"]
    text = ctx.team_manager.edit_teammate(name, **updates)
    return DispatchResult(content=text)


def _handle_team_delete(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.team_manager is None:
        return DispatchResult(content="Unknown tool: team_delete")
    name = inp.get("name", "")
    if not name:
        return DispatchResult(content="[error] 'name' is required for team_delete")
    return DispatchResult(content=ctx.team_manager.delete_teammate(name))


_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


def _handle_download(ctx: ToolContext, inp: dict) -> DispatchResult:
    import base64

    dl_dir = None
    if ctx.memory_store:
        dl_dir = ctx.memory_store.agent_dir / "downloads"
    text = _wrap_simple(run_download, inp, downloads_dir=dl_dir)
    if not text.startswith("Saved ") or " -> " not in text:
        return DispatchResult(content=text)

    path_str = text.split(" -> ", 1)[1]
    path = Path(path_str)
    if path.suffix.lower() not in _IMAGE_EXTENSIONS or not path.is_file():
        return DispatchResult(content=text)

    data = path.read_bytes()
    ext = path.suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
    }
    mime = mime_map.get(ext, "image/jpeg")
    return DispatchResult(content=[
        {"type": "text", "text": text},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(data).decode(),
            },
        },
    ])


def _handle_send_file(ctx: ToolContext, inp: dict) -> DispatchResult:
    """Send a local file to the current conversation via the active channel."""
    ch = ctx.channel
    if not ch or ch.name == "cli":
        return DispatchResult(
            content="send_file is only available on messaging channels (not CLI).",
        )

    raw_path = inp.get("path", "")
    if not raw_path:
        return DispatchResult(content="[error] 'path' is required.")

    path = Path(raw_path)
    if not path.is_absolute() and ctx.workdir:
        path = ctx.workdir / path

    if not path.is_file():
        return DispatchResult(content=f"[error] File not found: {path}")

    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
    size = path.stat().st_size
    if size > _MAX_FILE_SIZE:
        return DispatchResult(
            content=f"[error] File too large ({size} bytes, max {_MAX_FILE_SIZE}).",
        )

    file_data = path.read_bytes()
    caption = inp.get("caption", "")
    peer = ctx.peer_id
    if not peer:
        return DispatchResult(content="[error] No peer_id — cannot determine recipient.")

    is_image = path.suffix.lower() in _IMAGE_EXTENSIONS
    with ch.send_lock:
        if is_image:
            ok = ch.send_image(peer, file_data, caption=caption)
        else:
            ok = ch.send_file(peer, file_data, filename=path.name, caption=caption)

    if ok:
        kind = "Image" if is_image else "File"
        return DispatchResult(content=f"{kind} sent: {path.name} ({size} bytes)")
    return DispatchResult(content=f"[error] Channel failed to send {path.name}.")


def _handle_send(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.teammate is None:
        return DispatchResult(content="Unknown tool: send")
    return DispatchResult(content=ctx.teammate.send(inp))


def _handle_read_inbox(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.teammate is None:
        return DispatchResult(content="Unknown tool: read_inbox")
    return DispatchResult(content=ctx.teammate.read_inbox())


def _handle_idle(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.teammate is None:
        return DispatchResult(content="Unknown tool: idle")
    ctx.teammate.request_idle()
    return DispatchResult(
        content="Entering idle mode.",
        request_idle=True,
    )


def _handle_task_update(ctx: ToolContext, inp: dict) -> DispatchResult:
    """Lead-only task_update with worktree integrate/cleanup hooks."""
    if ctx.plan_manager is None:
        return DispatchResult(content="Unknown tool: task_update")

    story = inp.get("story")
    tasks = inp.get("tasks", [])

    for task_entry in tasks:
        status = task_entry.get("status")
        task_id = task_entry.get("id", "")
        wt = ctx.worktree_manager

        if status == "merged" and wt is not None and story is not None:
            ng = ctx.plan_manager._task_graph(story)
            all_tasks = ng.load_all()
            task_obj = all_tasks.get(task_id)
            owner = task_obj.owner if task_obj else ""

            if owner and owner != "lead" and wt.exists(owner):
                result = wt.integrate(owner)
                if not result.ok:
                    try:
                        ctx.plan_manager.update(
                            story, [{"id": task_id, "status": "failed"}],
                        )
                    except ValueError:
                        pass
                    msg = f"[integrate failed] {result.message}"
                    if result.conflict_files:
                        msg += f"\nConflict files: {', '.join(result.conflict_files)}"
                    return DispatchResult(content=msg, used_task_tool=True)

        if status == "completed" and wt is not None and story is not None:
            ng = ctx.plan_manager._task_graph(story)
            all_tasks = ng.load_all()
            task_obj = all_tasks.get(task_id)
            owner = task_obj.owner if task_obj else ""

            if owner and owner != "lead" and wt.exists(owner):
                wt.remove(owner)

    try:
        text = _handle_plan_tool("task_update", inp, ctx.plan_manager)
    except ValueError as e:
        return DispatchResult(content=f"[error] {e}", used_task_tool=True)
    return DispatchResult(content=text, used_task_tool=True)


def _handle_task_submit(ctx: ToolContext, inp: dict) -> DispatchResult:
    """Subagent submits work for review: sync branch then set in_review."""
    if ctx.plan_manager is None:
        return DispatchResult(content="Unknown tool: task_submit")
    missing = [k for k in ("story", "task_id") if k not in inp]
    if missing:
        return DispatchResult(content=f"[error] Missing required fields: {', '.join(missing)}")

    if ctx.worktree_manager is not None and ctx.worktree_manager.exists(ctx.caller):
        sync = ctx.worktree_manager.sync(ctx.caller)
        if not sync.ok:
            try:
                ctx.plan_manager.update(
                    inp["story"],
                    [{"id": inp["task_id"], "status": "failed"}],
                )
            except ValueError:
                pass
            msg = f"[sync failed] {sync.message}"
            if sync.conflict_files:
                msg += f"\nConflict files: {', '.join(sync.conflict_files)}"
                msg += "\nResolve conflicts, commit, then call task_submit again."
            return DispatchResult(content=msg, used_task_tool=True)

    try:
        result = ctx.plan_manager.update(
            inp["story"],
            [{"id": inp["task_id"], "status": "in_review"}],
        )
    except ValueError as e:
        return DispatchResult(content=f"[error] {e}")
    return DispatchResult(content=result, used_task_tool=True)


def _handle_claim_task(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.plan_manager is None:
        return DispatchResult(content="Unknown tool: claim_task")
    missing = [k for k in ("story", "task_id") if k not in inp]
    if missing:
        return DispatchResult(content=f"[error] Missing required fields: {', '.join(missing)}")
    try:
        result = ctx.plan_manager.update(
            inp["story"],
            [{"id": inp["task_id"], "status": "in_progress", "owner": ctx.caller}],
        )
    except ValueError as e:
        return DispatchResult(content=f"[error] {e}")

    if ctx.caller != "lead" and ctx.worktree_manager is not None:
        try:
            wt_path = ctx.worktree_manager.create(ctx.caller)
            result += f"\nWorktree created at: {wt_path}"
        except Exception as e:
            result += f"\n[warning] Worktree creation failed: {e}"

    return DispatchResult(content=result, used_task_tool=True)


def _handle_task_board_overview(ctx: ToolContext, _inp: dict) -> DispatchResult:
    if ctx.plan_manager is None:
        return DispatchResult(content="Unknown tool: task_board_overview")
    return DispatchResult(content=ctx.plan_manager.render(None))


def _handle_remember_user(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.memory_store is None:
        return DispatchResult(content="Unknown tool: remember_user")
    ch_name = ctx.channel.name if ctx.channel else "cli"
    sid = inp.get("sender_id") or ctx.sender_id
    if sid and ch_name and sid.startswith(f"{ch_name}:"):
        sid = sid[len(ch_name) + 1:]
    result = ctx.memory_store.update_user_profile(
        sender_id=sid,
        channel=ch_name,
        name=inp.get("name", ""),
        call_me=inp.get("call_me", ""),
        timezone=inp.get("timezone", ""),
        notes=inp.get("notes", ""),
    )
    return DispatchResult(content=result)


def _handle_reflect(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.memory_store is None or ctx.client is None:
        return DispatchResult(content="[error] Reflection not available.")
    from pip_agent.compact import save_transcript
    from pip_agent.memory.reflect import reflect

    # Save current conversation transcript first so it's included in reflection
    if ctx.transcripts_dir is not None and ctx.messages:
        save_transcript(ctx.messages, ctx.transcripts_dir)

    state = ctx.memory_store.load_state()
    since = state.get("last_reflect_transcript_ts", 0)

    transcripts_dir = ctx.transcripts_dir
    if transcripts_dir is None:
        return DispatchResult(content="[error] No transcripts directory configured.")

    observations = reflect(
        ctx.client,
        transcripts_dir,
        ctx.memory_store.agent_id,
        since,
        model=ctx.model,
    )

    if observations:
        ctx.memory_store.write_observations(observations)

    # Update state with latest transcript timestamp
    latest = 0
    if transcripts_dir.is_dir():
        for fp in transcripts_dir.glob("*.json"):
            try:
                ts = int(fp.stem)
            except ValueError:
                continue
            if ts > latest:
                latest = ts
    if latest > 0:
        state["last_reflect_transcript_ts"] = latest
    state["last_reflect_at"] = time.time()
    ctx.memory_store.save_state(state)

    if observations:
        return DispatchResult(
            content=f"Reflection complete: extracted {len(observations)} observations."
        )
    return DispatchResult(content="Reflection complete: no new observations found.")


def _handle_memory_search(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.memory_store is None:
        return DispatchResult(content="Unknown tool: memory_search")
    query = inp.get("query", "").strip()
    if not query:
        return DispatchResult(content="[error] 'query' is required")
    try:
        top_k = int(inp.get("top_k", 5))
    except (ValueError, TypeError):
        top_k = 5
    results = ctx.memory_store.search(query, top_k=top_k)
    if not results:
        return DispatchResult(content="(no matching memories)")
    lines = [f"- {r.get('text', '')} (score: {r.get('score', 0)})" for r in results]
    return DispatchResult(content="\n".join(lines))


def _handle_task_board_detail(ctx: ToolContext, inp: dict) -> DispatchResult:
    if ctx.plan_manager is None:
        return DispatchResult(content="Unknown tool: task_board_detail")
    missing = [k for k in ("story", "task_id") if k not in inp]
    if missing:
        return DispatchResult(content=f"[error] Missing required fields: {', '.join(missing)}")
    text = ctx.plan_manager.format_task(inp["story"], inp["task_id"])
    return DispatchResult(content=text)


def _handle_cron_add(ctx: ToolContext, inp: dict) -> DispatchResult:
    if not ctx.scheduler:
        return DispatchResult(content="Scheduler not available.")
    cs = ctx.scheduler.get_cron_service()
    if cs is None:
        return DispatchResult(content="Cron service not available.")
    result = cs.add_job(
        name=inp.get("name", ""),
        schedule_kind=inp.get("schedule_kind", ""),
        schedule_config=inp.get("schedule_config", {}),
        message=inp.get("message", ""),
        channel=ctx.channel.name if ctx.channel else "cli",
        peer_id=ctx.peer_id or "cli-user",
        sender_id=ctx.sender_id,
        agent_id=ctx.memory_store.agent_id if ctx.memory_store else "",
    )
    return DispatchResult(content=result)


def _handle_cron_remove(ctx: ToolContext, inp: dict) -> DispatchResult:
    if not ctx.scheduler:
        return DispatchResult(content="Scheduler not available.")
    cs = ctx.scheduler.get_cron_service()
    if cs is None:
        return DispatchResult(content="Cron service not available.")
    return DispatchResult(content=cs.remove_job(inp.get("job_id", "")))


def _handle_cron_update(ctx: ToolContext, inp: dict) -> DispatchResult:
    if not ctx.scheduler:
        return DispatchResult(content="Scheduler not available.")
    cs = ctx.scheduler.get_cron_service()
    if cs is None:
        return DispatchResult(content="Cron service not available.")
    job_id = inp.get("job_id", "")
    if not job_id:
        return DispatchResult(content="[error] job_id is required.")
    return DispatchResult(content=cs.update_job(job_id, **inp))


def _handle_cron_list(ctx: ToolContext, _inp: dict) -> DispatchResult:
    if not ctx.scheduler:
        return DispatchResult(content="Scheduler not available.")
    cs = ctx.scheduler.get_cron_service()
    if cs is None:
        return DispatchResult(content="No scheduled tasks.")
    import json
    jobs = cs.list_jobs()
    if not jobs:
        return DispatchResult(content="No scheduled tasks.")
    return DispatchResult(content=json.dumps(jobs, indent=2, ensure_ascii=False))


_TOOL_REGISTRY: dict[str, Callable[[ToolContext, dict], DispatchResult]] = {
    "compact": _handle_compact,
    "task_create": _make_task_handler("task_create"),
    "task_update": _handle_task_update,
    "task_list": _make_task_handler("task_list"),
    "task_remove": _make_task_handler("task_remove"),
    "load_skill": _handle_load_skill,
    "bash": _handle_bash,
    "check_background": _handle_check_background,
    "team_spawn": _handle_team_spawn,
    "team_send": _handle_team_send,
    "team_status": _handle_team_status,
    "team_read_inbox": _handle_team_read_inbox,
    "team_list_models": _handle_team_list_models,
    "team_create": _handle_team_create,
    "team_edit": _handle_team_edit,
    "team_delete": _handle_team_delete,
    "send_file": _handle_send_file,
    "send": _handle_send,
    "read_inbox": _handle_read_inbox,
    "idle": _handle_idle,
    "task_submit": _handle_task_submit,
    "claim_task": _handle_claim_task,
    "task_board_overview": _handle_task_board_overview,
    "task_board_detail": _handle_task_board_detail,
    "remember_user": _handle_remember_user,
    "reflect": _handle_reflect,
    "memory_search": _handle_memory_search,
    "cron_add": _handle_cron_add,
    "cron_remove": _handle_cron_remove,
    "cron_update": _handle_cron_update,
    "cron_list": _handle_cron_list,
    "read": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_read, inp, workdir=ctx.workdir),
    ),
    "write": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_write, inp, workdir=ctx.workdir),
    ),
    "edit": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_edit, inp, workdir=ctx.workdir),
    ),
    "glob": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_glob, inp, workdir=ctx.workdir),
    ),
    "grep": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_grep, inp, workdir=ctx.workdir),
    ),
    "web_search": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_web_search, inp),
    ),
    "web_fetch": lambda ctx, inp: DispatchResult(
        content=_wrap_simple(run_web_fetch, inp),
    ),
    "download": lambda ctx, inp: _handle_download(ctx, inp),
}


def dispatch_tool(ctx: ToolContext, name: str, tool_input: dict) -> DispatchResult:
    handler = _TOOL_REGISTRY.get(name)
    if handler is None:
        return DispatchResult(content=f"Unknown tool: {name}")

    profile = name != "compact" and ctx.profiler is not None
    if profile:
        ctx.profiler.start(f"tool:{name}")
    try:
        return handler(ctx, tool_input)
    except Exception as exc:
        log.exception("tool '%s' raised: %s", name, exc)
        return DispatchResult(content=f"[error] tool '{name}' failed: {exc}")
    finally:
        if profile:
            ctx.profiler.stop()


