"""Tests for pip_agent.agent — agent_loop and RuntimeContext."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pip_agent.agent import RuntimeContext, agent_loop
from pip_agent.profiler import Profiler
from pip_agent.task_graph import PlanManager


def _fake_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _fake_tool_block(tool_id: str, name: str, inp: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inp)


def _make_response(content, stop_reason="end_turn", input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.fixture()
def plan_manager(tmp_path):
    return PlanManager(tmp_path / "tasks")


@pytest.fixture()
def runtime_ctx():
    client = MagicMock()
    profiler = Profiler()
    return RuntimeContext(
        client=client,
        profiler=profiler,
        tools=[{"name": "read", "input_schema": {}}],
    )


class TestRuntimeContext:
    def test_defaults(self, runtime_ctx):
        assert runtime_ctx.skill_registry is None
        assert runtime_ctx.bg_manager is None
        assert runtime_ctx.memory_store is None

    def test_fields_set(self):
        client = MagicMock()
        bg = MagicMock()
        ctx = RuntimeContext(
            client=client,
            profiler=Profiler(),
            tools=[],
            bg_manager=bg,
        )
        assert ctx.bg_manager is bg


class TestAgentLoopTextReply:
    def test_returns_text_on_end_turn(self, runtime_ctx, plan_manager):
        runtime_ctx.client.messages.create.return_value = _make_response(
            [_fake_text_block("Hello from agent")],
        )
        result = agent_loop(
            runtime_ctx,
            [],
            "Hi",
            plan_manager,
            system_prompt="You are helpful.",
        )
        assert result == "Hello from agent"
        runtime_ctx.client.messages.create.assert_called_once()

    def test_accumulates_multiblock_text(self, runtime_ctx, plan_manager):
        runtime_ctx.client.messages.create.return_value = _make_response(
            [_fake_text_block("Part A"), _fake_text_block(" Part B")],
        )
        result = agent_loop(
            runtime_ctx,
            [],
            "Hello",
            plan_manager,
            system_prompt="sys",
        )
        assert result == "Part A Part B"


class TestAgentLoopToolUse:
    def test_tool_use_then_text_reply(self, runtime_ctx, plan_manager, monkeypatch):
        tool_response = _make_response(
            [_fake_tool_block("t1", "read", {"file_path": "a.txt"})],
            stop_reason="tool_use",
        )
        text_response = _make_response(
            [_fake_text_block("Done reading")],
        )
        runtime_ctx.client.messages.create.side_effect = [tool_response, text_response]

        monkeypatch.setattr(
            "pip_agent.agent.dispatch_tool",
            lambda ctx, name, inp: SimpleNamespace(
                content="file contents", used_task_tool=False, compact_requested=False,
            ),
        )

        result = agent_loop(
            runtime_ctx,
            [],
            "Read the file",
            plan_manager,
            system_prompt="sys",
        )
        assert result == "Done reading"
        assert runtime_ctx.client.messages.create.call_count == 2


class TestAgentLoopInterrupt:
    def test_keyboard_interrupt_breaks_loop(self, runtime_ctx, plan_manager):
        runtime_ctx.client.messages.create.side_effect = KeyboardInterrupt

        result = agent_loop(
            runtime_ctx,
            [],
            "test",
            plan_manager,
            system_prompt="sys",
        )
        assert result is None
