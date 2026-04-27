"""Format ``tool_use`` event args into a one-line trace for the TUI.

The agent-pane renders every ``tool_use`` event as a single ``[tool: X …]``
line. Without per-tool formatting the user can only see that *something*
happened; they can't tell which file ``Write`` targeted, which pattern
``Grep`` searched, or which question ``AskUserQuestion`` posed.

This module knows a small, hand-maintained whitelist of tools whose
arguments fit in one line. For everything else the trace stays at
``[tool: Name]`` with no args — better to render nothing than to spam
the pane with an unfiltered dict dump.

The formatter is pure (dict → str); it never touches the pump, the
App, or logging. That keeps unit tests trivial and makes it safe to
call from any thread.
"""

from __future__ import annotations

from typing import Any

__all__ = ["format_tool_summary"]


_MAX_VALUE_LEN = 60
_MAX_TOTAL_LEN = 120


def _truncate(s: str, n: int = _MAX_VALUE_LEN) -> str:
    """Shrink ``s`` to ``n`` chars, adding ``…`` when clipped."""
    s = s.replace("\n", "\\n").replace("\r", "")
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"


def _str(v: Any) -> str:
    """Coerce a JSON-ish tool-input value to a safe single-line string."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool | int | float):
        return str(v)
    if isinstance(v, list):
        return f"[{len(v)} items]"
    if isinstance(v, dict):
        return f"{{{len(v)} keys}}"
    return repr(v)


def format_tool_summary(name: str, tool_input: dict[str, Any] | None) -> str:
    """Return a one-line arg preview for a tool call, or ``""`` if none.

    The caller composes the full trace as ``[tool: <name> <summary>]``;
    an empty return collapses to ``[tool: <name>]`` with no args.
    """
    if not tool_input:
        return ""

    # Per-tool extractors — each returns a list of ``key=value`` fragments.
    # Order matters: earlier keys render first and get priority when we
    # truncate the whole line to _MAX_TOTAL_LEN.
    frags: list[str] = []

    if name == "Write":
        path = _str(tool_input.get("file_path"))
        content = tool_input.get("content")
        size = len(content) if isinstance(content, str) else 0
        if path:
            frags.append(f"path={_truncate(path)}")
        if size:
            frags.append(f"size={size}b")

    elif name == "Read":
        path = _str(tool_input.get("file_path"))
        if path:
            frags.append(f"path={_truncate(path)}")
        if "offset" in tool_input or "limit" in tool_input:
            frags.append(
                f"range={_str(tool_input.get('offset', 0))}"
                f"+{_str(tool_input.get('limit', '*'))}"
            )

    elif name in {"Edit", "NotebookEdit"}:
        path = _str(tool_input.get("file_path") or tool_input.get("notebook_path"))
        if path:
            frags.append(f"path={_truncate(path)}")
        if tool_input.get("replace_all"):
            frags.append("replace_all=true")

    elif name in {"Bash", "PowerShell"}:
        cmd = _str(tool_input.get("command"))
        if cmd:
            frags.append(_truncate(cmd, 90))

    elif name == "Grep":
        pattern = _str(tool_input.get("pattern"))
        path = _str(tool_input.get("path"))
        if pattern:
            frags.append(f"pattern={_truncate(pattern, 40)}")
        if path:
            frags.append(f"path={_truncate(path, 30)}")

    elif name == "Glob":
        pattern = _str(tool_input.get("pattern"))
        if pattern:
            frags.append(f"pattern={_truncate(pattern)}")

    elif name == "AskUserQuestion":
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            q_text = ""
            if isinstance(first, dict):
                q_text = _str(first.get("question"))
            frags.append(f"{len(questions)} question(s)")
            if q_text:
                frags.append(f"q1={_truncate(q_text, 50)}")

    elif name == "EnterPlanMode":
        frags.append("(entering plan mode)")

    elif name == "ExitPlanMode":
        # The plan text itself can be huge — show length only.
        plan = tool_input.get("plan")
        if isinstance(plan, str) and plan:
            frags.append(f"plan={len(plan)}b")
        else:
            frags.append("(exiting plan mode)")

    elif name == "TodoWrite":
        todos = tool_input.get("todos")
        if isinstance(todos, list):
            frags.append(f"{len(todos)} items")

    elif name in {"WebFetch", "mcp__pip__web_fetch"}:
        url = _str(tool_input.get("url"))
        if url:
            frags.append(f"url={_truncate(url, 70)}")

    elif name == "WebSearch":
        query = _str(tool_input.get("query"))
        if query:
            frags.append(f"q={_truncate(query, 70)}")

    elif name == "Agent":
        desc = _str(tool_input.get("description"))
        subtype = _str(tool_input.get("subagent_type"))
        if subtype:
            frags.append(f"type={subtype}")
        if desc:
            frags.append(_truncate(desc, 60))

    # Unknown tool → no args shown. Better to render "[tool: X]" than
    # to leak an unbounded dict.

    if not frags:
        return ""

    summary = " ".join(frags)
    if len(summary) > _MAX_TOTAL_LEN:
        summary = summary[: _MAX_TOTAL_LEN - 1] + "…"
    return summary
