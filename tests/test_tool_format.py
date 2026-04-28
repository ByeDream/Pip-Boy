"""Unit tests for ``pip_agent.tui.tool_format``.

Pure dict → str tests; no Textual, no pump, no IO. Covers the
white-listed tools plus the fall-through for unknown tools.
"""

from __future__ import annotations

from pip_agent.tui.tool_format import format_tool_detail, format_tool_summary


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


def test_bash_shows_command() -> None:
    summary = format_tool_summary("Bash", {"command": "ls -la"})
    assert summary == "ls -la"


def test_bash_long_command_is_not_clipped() -> None:
    long = "echo " + ("x" * 200)
    summary = format_tool_summary("Bash", {"command": long})
    assert summary == long
    assert "…" not in summary


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


def test_long_path_is_not_clipped() -> None:
    long_path = "/" + ("a" * 300)
    summary = format_tool_summary(
        "Write", {"file_path": long_path, "content": "x"}
    )
    assert long_path in summary
    assert "…" not in summary


def test_sensitive_content_not_leaked() -> None:
    """Write's `content` must never appear verbatim — only its size."""
    summary = format_tool_summary(
        "Write",
        {"file_path": "/secret.env", "content": "AWS_KEY=AKIADEADBEEFBAADF00D"},
    )
    assert "AKIA" not in summary
    assert "size=" in summary


# ---------------------------------------------------------------------------
# format_tool_detail — multi-line preview for interactive tools
# ---------------------------------------------------------------------------


def test_detail_none_for_unsupported_tools() -> None:
    assert format_tool_detail("Write", {"file_path": "/x"}) is None
    assert format_tool_detail("Read", {"file_path": "/x"}) is None
    assert format_tool_detail("Bash", {"command": "ls"}) is None
    assert format_tool_detail("EnterPlanMode", {"_": "_"}) is None


def test_detail_none_for_empty_input() -> None:
    assert format_tool_detail("AskUserQuestion", None) is None
    assert format_tool_detail("AskUserQuestion", {}) is None
    assert format_tool_detail("ExitPlanMode", {}) is None


def test_detail_askuserquestion_single_question() -> None:
    detail = format_tool_detail(
        "AskUserQuestion",
        {
            "questions": [
                {
                    "question": "Pick a color?",
                    "header": "Color",
                    "options": [
                        {"label": "red", "description": "warm"},
                        {"label": "blue"},
                    ],
                }
            ]
        },
    )
    assert detail is not None
    assert "Q1" in detail
    assert "Color" in detail
    assert "Pick a color?" in detail
    assert "- red" in detail
    assert "warm" in detail  # option description preserved
    assert "- blue" in detail


def test_detail_askuserquestion_marks_multiselect() -> None:
    detail = format_tool_detail(
        "AskUserQuestion",
        {
            "questions": [
                {
                    "question": "Which?",
                    "multiSelect": True,
                    "options": [{"label": "a"}, {"label": "b"}],
                }
            ]
        },
    )
    assert detail is not None
    assert "(multi)" in detail


def test_detail_askuserquestion_multiple_questions() -> None:
    detail = format_tool_detail(
        "AskUserQuestion",
        {
            "questions": [
                {"question": "First?",  "options": [{"label": "x"}]},
                {"question": "Second?", "options": [{"label": "y"}]},
            ]
        },
    )
    assert detail is not None
    assert "Q1" in detail
    assert "Q2" in detail
    assert "First?" in detail
    assert "Second?" in detail


def test_detail_askuserquestion_ignores_non_dict_questions() -> None:
    # Robustness: a malformed questions array shouldn't crash.
    detail = format_tool_detail(
        "AskUserQuestion", {"questions": [None, {"question": "Ok?"}]}
    )
    assert detail is not None
    assert "Ok?" in detail


def test_detail_exitplanmode_shows_plan_body() -> None:
    plan = (
        "# Plan\n"
        "1. Do thing A\n"
        "2. Do thing B\n"
    )
    detail = format_tool_detail("ExitPlanMode", {"plan": plan})
    assert detail is not None
    assert "# Plan" in detail
    assert "1. Do thing A" in detail
    assert "2. Do thing B" in detail


def test_detail_exitplanmode_clips_long_plans() -> None:
    huge = "\n".join(f"step {i}" for i in range(60))
    detail = format_tool_detail("ExitPlanMode", {"plan": huge})
    assert detail is not None
    assert "more line" in detail
    assert detail.count("\n") <= 30  # capped


def test_detail_exitplanmode_clips_long_lines() -> None:
    plan = "x" * 500
    detail = format_tool_detail("ExitPlanMode", {"plan": plan})
    assert detail is not None
    # One long line → truncated (ends with ellipsis), not overflowing.
    assert any(len(ln) <= 105 for ln in detail.splitlines())
    assert "…" in detail


def test_detail_exitplanmode_empty_plan_returns_none() -> None:
    assert format_tool_detail("ExitPlanMode", {"plan": ""}) is None
    assert format_tool_detail("ExitPlanMode", {"plan": None}) is None
