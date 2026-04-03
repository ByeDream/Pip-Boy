import os
import sys

import anthropic

from pip_agent.config import settings
from pip_agent.profiler import Profiler
from pip_agent.tools import ALL_TOOLS, execute_tool

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
    f"You work at {os.getcwd()}. "
    f"Use bash to solve tasks. Act, don't explain."
)


def agent_loop(
    client: anthropic.Anthropic,
    messages: list[dict],
    user_input: str,
    profiler: Profiler,
) -> None:
    messages.append({"role": "user", "content": user_input})

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
            for block in assistant_content:
                if block.type == "tool_use":
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
            messages.append({"role": "user", "content": tool_results})
        else:
            break


def run() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    client_kwargs: dict = {"api_key": settings.anthropic_api_key}
    if settings.anthropic_base_url:
        client_kwargs["base_url"] = settings.anthropic_base_url
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    client = anthropic.Anthropic(**client_kwargs)
    messages: list[dict] = []
    profiler = Profiler()

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

        agent_loop(client, messages, user_input, profiler)

        last = messages[-1]
        if last["role"] == "assistant":
            for block in last["content"]:
                if hasattr(block, "text"):
                    print(block.text)

        profiler.flush()
        print()
