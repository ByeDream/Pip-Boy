from __future__ import annotations

from pip_agent.profiler import Profiler
from pip_agent.task_graph import PlanManager
from pip_agent.tool_dispatch import (
    TeammateToolSurface,
    ToolContext,
    dispatch_tool,
)


def test_dispatch_read_minimal_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pip_agent.tools.WORKDIR", tmp_path)
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    ctx = ToolContext()
    out = dispatch_tool(ctx, "read", {"file_path": "a.txt"})
    assert "hello" in out.content


def test_dispatch_unknown_tool() -> None:
    ctx = ToolContext()
    out = dispatch_tool(ctx, "not_a_real_tool", {})
    assert "Unknown tool" in out.content


def test_task_list_requires_plan_manager() -> None:
    ctx = ToolContext()
    out = dispatch_tool(ctx, "task_list", {})
    assert out.content == "Unknown tool: task_list"


def test_task_list_with_plan_manager(tmp_path) -> None:
    root = tmp_path / "tasks"
    pm = PlanManager(root)
    pm.create(None, [{"id": "s1", "title": "Story one"}])
    ctx = ToolContext(plan_manager=pm)
    out = dispatch_tool(ctx, "task_list", {})
    assert "Story one" in out.content
    assert out.used_task_tool is True


def test_send_requires_teammate_surface() -> None:
    ctx = ToolContext()
    out = dispatch_tool(
        ctx,
        "send",
        {"to": "lead", "content": "hi"},
    )
    assert out.content == "Unknown tool: send"


def test_send_with_surface() -> None:
    ctx = ToolContext(
        teammate=TeammateToolSurface(
            send=lambda inp: f"sent:{inp['content']}",
            read_inbox=lambda: "[]",
            request_idle=lambda: None,
        ),
    )
    out = dispatch_tool(ctx, "send", {"to": "x", "content": "hi"})
    assert out.content == "sent:hi"


def test_compact_sets_flag() -> None:
    ctx = ToolContext(profiler=Profiler())
    out = dispatch_tool(ctx, "compact", {})
    assert out.compact_requested is True
    assert "Acknowledged" in out.content


def test_task_board_overview_no_nag_flag(tmp_path) -> None:
    pm = PlanManager(tmp_path / "tasks")
    pm.create(None, [{"id": "s1", "title": "S1"}])
    ctx = ToolContext(plan_manager=pm)
    out = dispatch_tool(ctx, "task_board_overview", {})
    assert "S1" in out.content
    assert out.used_task_tool is False


def test_task_board_detail(tmp_path) -> None:
    pm = PlanManager(tmp_path / "tasks")
    pm.create(None, [{"id": "s1", "title": "S1"}])
    pm.create("s1", [{"id": "t1", "title": "T1"}])
    ctx = ToolContext(plan_manager=pm)
    out = dispatch_tool(ctx, "task_board_detail", {"story": "s1", "task_id": "t1"})
    assert "T1" in out.content
    assert "pending" in out.content
    assert out.used_task_tool is False


def test_task_board_detail_unknown_story(tmp_path) -> None:
    pm = PlanManager(tmp_path / "tasks")
    ctx = ToolContext(plan_manager=pm)
    out = dispatch_tool(
        ctx, "task_board_detail", {"story": "nope", "task_id": "t1"},
    )
    assert out.content.startswith("[error]")


def test_idle_sets_request_idle_on_result() -> None:
    idle_called: list[bool] = []

    def mark_idle() -> None:
        idle_called.append(True)

    ctx = ToolContext(
        teammate=TeammateToolSurface(
            send=lambda _inp: "",
            read_inbox=lambda: "",
            request_idle=mark_idle,
        ),
    )
    out = dispatch_tool(ctx, "idle", {})
    assert out.request_idle is True
    assert idle_called == [True]
