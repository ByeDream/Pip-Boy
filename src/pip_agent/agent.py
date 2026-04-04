import os
import sys

import anthropic

from pip_agent.config import settings
from pip_agent.profiler import Profiler
from pip_agent.todo import TodoManager
from pip_agent.subagent import run_subagent
from pip_agent.tools import ALL_TOOLS, WORKDIR, execute_tool

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
    f"Use the todo_write tool to plan multi-step tasks. "
    f"Mark in_progress before starting, completed when done. "
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
    todo_manager: TodoManager,
) -> None:
    messages.append({"role": "user", "content": user_input})
    rounds_since_todo = 0

    while True:
        profiler.start("api")
        response = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=SYSTEM_PROMPT,
            tools=ALL_TOOLS,
            messages=messages,
        )
        usage = response.usage
        profiler.stop(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            stop=response.stop_reason,
        )

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "tool_use":
            tool_results = []
            used_todo = False
            for block in assistant_content:
                if settings.verbose and hasattr(block, "text"):
                    print()
                    print(block.text)
                if block.type == "tool_use":
                    if settings.verbose:
                        print()
                        print(f"> {_tool_summary(block.name, block.input)}")
                    if block.name == "todo_write":
                        profiler.start("tool:todo_write")
                        try:
                            result = todo_manager.write(
                                block.input.get("todos", [])
                            )
                        except ValueError as e:
                            result = f"[error] {e}"
                        profiler.stop()
                        if settings.verbose:
                            print(result)
                        used_todo = True
                    elif block.name == "task":
                        profiler.start("tool:task")
                        result = run_subagent(
                            client,
                            block.input["prompt"],
                            profiler,
                        )
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
            rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
            if todo_manager.has_items() and rounds_since_todo >= NAG_THRESHOLD:
                tool_results.append(
                    {"type": "text", "text": "<reminder>Update your todos.</reminder>"}
                )
            messages.append({"role": "user", "content": tool_results})
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
    todo_manager = TodoManager()

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

        agent_loop(client, messages, user_input, profiler, todo_manager)

        last = messages[-1]
        if last["role"] == "assistant":
            for block in last["content"]:
                if hasattr(block, "text"):
                    print()
                    print("================================================")
                    print(block.text)

        profiler.flush()
