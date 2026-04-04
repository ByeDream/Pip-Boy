from __future__ import annotations

import anthropic

from pip_agent.config import settings
from pip_agent.profiler import Profiler
from pip_agent.tools import (
    ALL_TOOLS,
    WORKDIR,
    execute_tool,
)

SUBAGENT_TOOLS = [t for t in ALL_TOOLS if t["name"] not in ("task", "todo_write")]

MAX_TOOL_OUTPUT = 50_000

SUBAGENT_SYSTEM_PROMPT = (
    f"You are a focused sub-agent. Your working directory is {WORKDIR}. "
    "Complete the assigned task using the tools available, then provide a "
    "concise summary of your findings or results. Do not ask follow-up "
    "questions — deliver the answer directly."
)

_TOOL_KEY_PARAM: dict[str, str] = {
    "bash": "command",
    "read": "file_path",
    "write": "file_path",
    "edit": "file_path",
    "glob": "pattern",
    "web_search": "query",
    "web_fetch": "url",
}


def _tool_summary(name: str, inputs: dict) -> str:
    key = _TOOL_KEY_PARAM.get(name)
    if key and key in inputs:
        value = str(inputs[key])
        if len(value) > 80:
            value = value[:77] + "..."
        return f"{name}: {value}"
    return name


def run_subagent(
    client: anthropic.Anthropic,
    prompt: str,
    profiler: Profiler,
) -> str:
    """Run an isolated sub-agent and return its final text response."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    max_rounds = settings.subagent_max_rounds

    for _ in range(max_rounds):
        profiler.start("api")
        response = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=SUBAGENT_SYSTEM_PROMPT,
            tools=SUBAGENT_TOOLS,
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

        if response.stop_reason != "tool_use":
            break

        tool_results: list[dict] = []
        for block in assistant_content:
            if settings.verbose and hasattr(block, "text"):
                print(f"  [sub] {block.text}")
            if block.type == "tool_use":
                if settings.verbose:
                    print(f"  [sub] > {_tool_summary(block.name, block.input)}")
                profiler.start(f"tool:{block.name}")
                result = execute_tool(block.name, block.input)
                profiler.stop()
                if len(result) > MAX_TOOL_OUTPUT:
                    result = result[:MAX_TOOL_OUTPUT] + "\n\n[truncated]"
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    parts: list[str] = []
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            for block in msg["content"]:
                if hasattr(block, "text"):
                    parts.append(block.text)
            break

    return "\n".join(parts) if parts else "(sub-agent returned no text)"
