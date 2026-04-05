from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import anthropic

from pip_agent.background import BackgroundTaskManager
from pip_agent.compact import (
    COMPACT_SCHEMA,
    auto_compact,
    estimate_tokens,
    micro_compact,
)
from pip_agent.config import settings
from pip_agent.profiler import Profiler
from pip_agent.skills import SkillRegistry
from pip_agent.task_graph import PlanManager
from pip_agent.subagent import run_subagent
from pip_agent.tools import (
    ALL_TOOLS,
    TASK_TOOL_NAMES,
    WORKDIR,
    execute_tool,
    run_bash,
)

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
USER_SKILLS_DIR = WORKDIR / ".pip" / "skills"

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
    f"You are Pip, a personal assistant agent. "
    f"Your working directory is {WORKDIR}. "
    f"Use task tools to plan goals. "
    f"Load the 'task-planning' skill for guidance. "
    f"Prefer tools over prose."
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
    "task": "prompt",
    "load_skill": "name",
    "task_create": "tasks",
    "task_update": "tasks",
    "task_remove": "task_ids",
    "check_background": "task_id",
}


def _tool_summary(name: str, inputs: dict) -> str:
    key = _TOOL_KEY_PARAM.get(name)
    if key and key in inputs:
        value = str(inputs[key])
        if len(value) > 80:
            value = value[:77] + "..."
        return f"{name}: {value}"
    return name


def _handle_task_tool(name: str, inputs: dict, pm: PlanManager) -> str:
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
) -> None:
    messages.append({"role": "user", "content": user_input})
    rounds_since_todo = 0
    last_input_tokens = 0

    while True:
        micro_compact(messages)

        if bg_manager is not None:
            notifications = bg_manager.drain()
            if notifications:
                last_msg = messages[-1]
                if last_msg["role"] == "user" and isinstance(last_msg["content"], list):
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

        if transcripts_dir is not None and estimate_tokens(messages) > settings.compact_threshold:
            auto_compact(client, messages, system_prompt, transcripts_dir, profiler)

        profiler.start("api")
        response = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
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
            for block in assistant_content:
                if settings.verbose and hasattr(block, "text"):
                    print()
                    print(block.text)
                if block.type == "tool_use":
                    if settings.verbose:
                        print()
                        print(f"> {_tool_summary(block.name, block.input)}")
                    if block.name == "compact":
                        result = "Acknowledged. Context will be compacted."
                        compact_requested = True
                    elif block.name in TASK_TOOL_NAMES:
                        profiler.start(f"tool:{block.name}")
                        try:
                            result = _handle_task_tool(
                                block.name, block.input, plan_manager
                            )
                        except ValueError as e:
                            result = f"[error] {e}"
                        profiler.stop()
                        if settings.verbose:
                            print(result)
                        used_task_tool = True
                    elif block.name == "load_skill" and skill_registry is not None:
                        profiler.start("tool:load_skill")
                        result = skill_registry.load(block.input["name"])
                        profiler.stop()
                    elif block.name == "task":
                        profiler.start("tool:task")
                        result = run_subagent(
                            client,
                            block.input["prompt"],
                            profiler,
                            skill_registry=skill_registry,
                        )
                        profiler.stop()
                    elif (
                        block.name == "bash"
                        and block.input.get("background")
                        and bg_manager is not None
                    ):
                        task_id = uuid.uuid4().hex[:8]
                        bg_manager.spawn(
                            task_id, block.input["command"], run_bash, block.input
                        )
                        result = f"[background:{task_id}] started"
                    elif block.name == "check_background" and bg_manager is not None:
                        profiler.start("tool:check_background")
                        result = bg_manager.check(block.input.get("task_id"))
                        profiler.stop()
                    else:
                        profiler.start(f"tool:{block.name}")
                        result = execute_tool(block.name, block.input)
                        profiler.stop()
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
                if transcripts_dir is not None:
                    auto_compact(
                        client, messages, system_prompt, transcripts_dir, profiler
                    )
        else:
            break


def run() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    settings.check_required()

    client_kwargs: dict = {"api_key": settings.anthropic_api_key}
    if settings.anthropic_base_url:
        client_kwargs["base_url"] = settings.anthropic_base_url
        client_kwargs["default_headers"] = {
            "Authorization": f"Bearer {settings.anthropic_api_key}",
        }
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    client = anthropic.Anthropic(**client_kwargs)
    messages: list[dict] = []
    profiler = Profiler()
    bg_manager = BackgroundTaskManager()
    plan_manager = PlanManager(WORKDIR / settings.tasks_dir)
    skill_registry = SkillRegistry(BUILTIN_SKILLS_DIR, USER_SKILLS_DIR)

    transcripts_dir = WORKDIR / settings.transcripts_dir

    tools: list[dict] = list(ALL_TOOLS)
    tools.append(COMPACT_SCHEMA)
    system_prompt = SYSTEM_PROMPT
    if skill_registry.available:
        tools.append(skill_registry.tool_schema())
        system_prompt += "\n\n" + skill_registry.catalog_prompt()

    print("Pip Agent (type 'exit' to quit)")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input.lower() == "exit":
            break
        if not user_input:
            continue

        agent_loop(
            client,
            messages,
            user_input,
            profiler,
            plan_manager,
            tools=tools,
            system_prompt=system_prompt,
            skill_registry=skill_registry,
            transcripts_dir=transcripts_dir,
            bg_manager=bg_manager,
        )

        last = messages[-1]
        if last["role"] == "assistant":
            for block in last["content"]:
                if hasattr(block, "text"):
                    print()
                    print("================================================")
                    print(block.text)

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
