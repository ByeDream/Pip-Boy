"""Unit tests for ``pip_agent.tui.tool_format``.

Pure dict → str tests; no Textual, no pump, no IO. Covers the
white-listed tools plus the fall-through for unknown tools.
"""

from __future__ import annotations

from pip_agent.tui.tool_format import format_tool_summary


def test_empty_input_returns_empty_string() -> None:
    assert format_tool_summary("Write", None) == ""
    assert format_tool_summary("Write", {}) == ""


def test_unknown_tool_returns_empty_string() -> None:
    # Better to render `[tool: X]` than to leak an unknown dict shape.
    assert format_tool_summary("SomeMcpTool", {"foo": "bar", "n": 42}) == ""


def test_write_shows_path_and_size() -> None:
    summary = format_tool_summary(
        "Write", {"file_path": "/tmp/x.md", "content": "hello world"}
    )
    assert "path=/tmp/x.md" in summary
    assert "size=11b" in summary


def test_read_shows_path_and_range() -> None:
    summary = format_tool_summary(
        "Read", {"file_path": "/a/b.py", "offset": 10, "limit": 50}
    )
    assert "path=/a/b.py" in summary
    assert "range=10+50" in summary


def test_read_without_range() -> None:
    summary = format_tool_summary("Read", {"file_path": "/a/b.py"})
    assert summary == "path=/a/b.py"


def test_edit_shows_path() -> None:
    summary = format_tool_summary(
        "Edit", {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}
    )
    assert summary == "path=/a/b.py"


def test_edit_replace_all_flag() -> None:
    summary = format_tool_summary(
        "Edit", {"file_path": "/a/b.py", "replace_all": True}
    )
    assert "path=/a/b.py" in summary
    assert "replace_all=true" in summary


def test_bash_shows_command_truncated() -> None:
    summary = format_tool_summary("Bash", {"command": "ls -la"})
    assert summary == "ls -la"


def test_bash_command_is_clipped() -> None:
    long = "echo " + ("x" * 200)
    summary = format_tool_summary("Bash", {"command": long})
    # _truncate with n=90 leaves 89 chars + ellipsis
    assert summary.endswith("…")
    assert len(summary) <= 90


def test_bash_newlines_are_flattened() -> None:
    summary = format_tool_summary(
        "Bash", {"command": "line1\nline2"}
    )
    # Literal backslash-n, not an actual newline, so the single-line
    # trace stays single-line.
    assert "\n" not in summary
    assert "\\n" in summary


def test_grep_shows_pattern_and_path() -> None:
    summary = format_tool_summary(
        "Grep", {"pattern": "FIXME", "path": "src/"}
    )
    assert "pattern=FIXME" in summary
    assert "path=src/" in summary


def test_glob_shows_pattern() -> None:
    summary = format_tool_summary("Glob", {"pattern": "**/*.py"})
    assert summary == "pattern=**/*.py"


def test_ask_user_question_shows_count_and_first_text() -> None:
    qs = [
        {"question": "Pick a color?", "options": [{"label": "red"}]},
        {"question": "Pick a size?",  "options": [{"label": "S"}]},
    ]
    summary = format_tool_summary("AskUserQuestion", {"questions": qs})
    assert "2 question(s)" in summary
    assert "Pick a color?" in summary


def test_ask_user_question_no_questions_returns_empty() -> None:
    summary = format_tool_summary("AskUserQuestion", {"questions": []})
    assert summary == ""


def test_enter_plan_mode_literal_marker() -> None:
    summary = format_tool_summary("EnterPlanMode", {})
    # EnterPlanMode takes no meaningful args; empty input → ""
    assert summary == ""


def test_enter_plan_mode_returns_label_with_dummy_key() -> None:
    # With any key present we go down the plan-mode branch.
    summary = format_tool_summary("EnterPlanMode", {"_": "_"})
    assert "plan mode" in summary


def test_exit_plan_mode_shows_plan_size() -> None:
    summary = format_tool_summary(
        "ExitPlanMode", {"plan": "# Plan\n1. step one\n"}
    )
    assert "plan=" in summary and "b" in summary


def test_todo_write_shows_count() -> None:
    summary = format_tool_summary(
        "TodoWrite",
        {"todos": [{"content": "a", "status": "pending", "activeForm": "A"}]},
    )
    assert summary == "1 items"


def test_webfetch_shows_url() -> None:
    summary = format_tool_summary(
        "WebFetch", {"url": "https://example.com/foo"}
    )
    assert "url=https://example.com/foo" in summary


def test_pip_webfetch_alias() -> None:
    summary = format_tool_summary(
        "mcp__pip__web_fetch", {"url": "https://example.com"}
    )
    assert "url=https://example.com" in summary


def test_websearch_shows_query() -> None:
    summary = format_tool_summary(
        "WebSearch", {"query": "Claude API caching"}
    )
    assert "q=Claude API caching" in summary


def test_agent_shows_subtype_and_description() -> None:
    summary = format_tool_summary(
        "Agent",
        {"subagent_type": "Explore", "description": "look for auth"},
    )
    assert "type=Explore" in summary
    assert "look for auth" in summary


def test_total_length_clamped() -> None:
    # Write with a monstrously long path gets clipped.
    long_path = "/" + ("a" * 300)
    summary = format_tool_summary(
        "Write", {"file_path": long_path, "content": "x"}
    )
    assert len(summary) <= 130  # _MAX_TOTAL_LEN + small slack for "path=" etc.


def test_sensitive_content_not_leaked() -> None:
    """Write's `content` must never appear verbatim — only its size."""
    summary = format_tool_summary(
        "Write",
        {"file_path": "/secret.env", "content": "AWS_KEY=AKIADEADBEEFBAADF00D"},
    )
    assert "AKIA" not in summary
    assert "size=" in summary
