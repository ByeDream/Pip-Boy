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

__all__ = ["format_tool_detail", "format_tool_summary"]


# Cap for the multi-line detail block (AskUserQuestion questions,
# ExitPlanMode plan preview). Anything past this is replaced with a
# "… (N more lines)" summary row so a giant plan doesn't push the
# agent pane off-screen.
_DETAIL_MAX_LINES = 24
_DETAIL_MAX_LINE_LEN = 100


def _oneline(s: str) -> str:
    """Flatten newlines so a value renders on a single trace line."""
    return s.replace("\n", "\\n").replace("\r", "")


def _truncate(s: str, n: int) -> str:
    """Shrink ``s`` to ``n`` chars, adding ``…`` when clipped.

    Only used by the detail block — summary args render in full so the
    reader can see what a tool call is actually doing.
    """
    s = _oneline(s)
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
            frags.append(f"path={_oneline(path)}")
        if size:
            frags.append(f"size={size}b")

    elif name == "Read":
        path = _str(tool_input.get("file_path"))
        if path:
            frags.append(f"path={_oneline(path)}")
        if "offset" in tool_input or "limit" in tool_input:
            frags.append(
                f"range={_str(tool_input.get('offset', 0))}"
                f"+{_str(tool_input.get('limit', '*'))}"
            )

    elif name in {"Edit", "NotebookEdit"}:
        path = _str(tool_input.get("file_path") or tool_input.get("notebook_path"))
        if path:
            frags.append(f"path={_oneline(path)}")
        if tool_input.get("replace_all"):
            frags.append("replace_all=true")

    elif name in {"Bash", "PowerShell"}:
        cmd = _str(tool_input.get("command"))
        if cmd:
            frags.append(_oneline(cmd))

    elif name == "Grep":
        pattern = _str(tool_input.get("pattern"))
        path = _str(tool_input.get("path"))
        if pattern:
            frags.append(f"pattern={_oneline(pattern)}")
        if path:
            frags.append(f"path={_oneline(path)}")

    elif name == "Glob":
        pattern = _str(tool_input.get("pattern"))
        if pattern:
            frags.append(f"pattern={_oneline(pattern)}")

    elif name == "AskUserQuestion":
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            q_text = ""
            if isinstance(first, dict):
                q_text = _str(first.get("question"))
            frags.append(f"{len(questions)} question(s)")
            if q_text:
                frags.append(f"q1={_oneline(q_text)}")

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
            frags.append(f"url={_oneline(url)}")

    elif name == "WebSearch":
        query = _str(tool_input.get("query"))
        if query:
            frags.append(f"q={_oneline(query)}")

    elif name == "Agent":
        desc = _str(tool_input.get("description"))
        subtype = _str(tool_input.get("subagent_type"))
        if subtype:
            frags.append(f"type={subtype}")
        if desc:
            frags.append(_oneline(desc))

    # Unknown tool → no args shown. Better to render "[tool: X]" than
    # to leak an unbounded dict.

    if not frags:
        return ""

    return " ".join(frags)


def format_tool_detail(name: str, tool_input: dict[str, Any] | None) -> str | None:
    """Return a multi-line preview for tools whose content deserves its own
    block in the agent pane, or ``None`` when the one-line summary is
    enough.

    Covers two cases where the summary is too terse to be useful:

    * ``AskUserQuestion`` — the user needs to see every question and its
      options to know what's being asked; the summary only shows Q1's
      text truncated.
    * ``ExitPlanMode`` — the plan body is what the user cares about; the
      summary just says ``plan=Nb``.

    Output is capped at ``_DETAIL_MAX_LINES`` lines and each line at
    ``_DETAIL_MAX_LINE_LEN`` chars so a huge plan cannot flood the pane.
    Returned text has no trailing newline; the renderer is expected to
    indent each line to visually nest under the ``[tool: X]`` trace
    that precedes it.
    """
    if not tool_input:
        return None

    if name == "AskUserQuestion":
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return None
        lines: list[str] = []
        for idx, q in enumerate(questions, 1):
            if not isinstance(q, dict):
                continue
            q_text = _str(q.get("question"))
            header = _str(q.get("header"))
            multi = bool(q.get("multiSelect"))
            label = f"Q{idx}"
            if header:
                label = f"{label} [{_truncate(header, 20)}]"
            tag = " (multi)" if multi else ""
            lines.append(_truncate(f"  {label}: {q_text}{tag}",
                                   _DETAIL_MAX_LINE_LEN))
            options = q.get("options")
            if isinstance(options, list):
                for opt in options:
                    if not isinstance(opt, dict):
                        continue
                    opt_label = _str(opt.get("label"))
                    opt_desc = _str(opt.get("description"))
                    if opt_desc:
                        line = f"    - {opt_label} — {opt_desc}"
                    else:
                        line = f"    - {opt_label}"
                    lines.append(_truncate(line, _DETAIL_MAX_LINE_LEN))
        return _clip_block(lines)

    if name == "ExitPlanMode":
        plan = tool_input.get("plan")
        if not isinstance(plan, str) or not plan:
            return None
        lines = [_truncate(ln, _DETAIL_MAX_LINE_LEN)
                 for ln in plan.splitlines()]
        return _clip_block(["  " + ln for ln in lines])

    return None


def _clip_block(lines: list[str]) -> str | None:
    """Cap a multi-line block at ``_DETAIL_MAX_LINES`` rows.

    Returns ``None`` for an empty list, otherwise joins the (possibly
    truncated) lines with ``\\n`` and appends a ``… (N more lines)``
    tail when content was dropped.
    """
    if not lines:
        return None
    if len(lines) <= _DETAIL_MAX_LINES:
        return "\n".join(lines)
    kept = lines[: _DETAIL_MAX_LINES - 1]
    dropped = len(lines) - len(kept)
    kept.append(f"  … ({dropped} more line{'s' if dropped != 1 else ''})")
    return "\n".join(kept)
