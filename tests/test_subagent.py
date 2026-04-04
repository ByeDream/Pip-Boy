from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pip_agent.profiler import Profiler
from pip_agent.subagent import SUBAGENT_TOOLS, run_subagent


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(
    name: str, tool_input: dict, block_id: str = "tu_1"
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def _make_response(content: list, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class TestSubagentTools:
    def test_task_excluded(self):
        names = [t["name"] for t in SUBAGENT_TOOLS]
        assert "task" not in names

    def test_todo_write_excluded(self):
        names = [t["name"] for t in SUBAGENT_TOOLS]
        assert "todo_write" not in names

    def test_core_tools_included(self):
        names = [t["name"] for t in SUBAGENT_TOOLS]
        for tool in ("bash", "read", "write", "edit", "glob", "web_search", "web_fetch"):
            assert tool in names


class TestRunSubagent:
    def test_returns_final_text(self):
        client = MagicMock()
        client.messages.create.return_value = _make_response(
            [_text_block("pytest")]
        )
        profiler = Profiler()

        result = run_subagent(client, "What test framework?", profiler)

        assert result == "pytest"
        client.messages.create.assert_called_once()

    def test_returns_joined_text_blocks(self):
        client = MagicMock()
        client.messages.create.return_value = _make_response(
            [_text_block("Line 1"), _text_block("Line 2")]
        )
        profiler = Profiler()

        result = run_subagent(client, "Summarize", profiler)

        assert result == "Line 1\nLine 2"

    def test_executes_tools_then_returns_text(self):
        client = MagicMock()
        tool_response = _make_response(
            [_tool_use_block("glob", {"pattern": "**/*.py"})],
            stop_reason="tool_use",
        )
        final_response = _make_response([_text_block("found 3 files")])
        client.messages.create.side_effect = [tool_response, final_response]
        profiler = Profiler()

        with patch("pip_agent.subagent.execute_tool", return_value="a.py\nb.py\nc.py"):
            result = run_subagent(client, "List python files", profiler)

        assert result == "found 3 files"
        assert client.messages.create.call_count == 2

    @patch("pip_agent.subagent.settings")
    def test_iteration_cap_stops_loop(self, mock_settings):
        mock_settings.model = "test-model"
        mock_settings.max_tokens = 1024
        mock_settings.subagent_max_rounds = 2
        mock_settings.verbose = False

        client = MagicMock()
        tool_block = _tool_use_block("bash", {"command": "echo hi"})
        tool_response = _make_response([tool_block], stop_reason="tool_use")
        client.messages.create.return_value = tool_response
        profiler = Profiler()

        with patch("pip_agent.subagent.execute_tool", return_value="hi"):
            result = run_subagent(client, "Run forever", profiler)

        assert client.messages.create.call_count == 2

    def test_no_text_returns_fallback(self):
        client = MagicMock()
        client.messages.create.return_value = _make_response(
            [_tool_use_block("bash", {"command": "echo x"})],
            stop_reason="tool_use",
        )
        profiler = Profiler()

        with patch("pip_agent.subagent.settings") as mock_settings:
            mock_settings.model = "test-model"
            mock_settings.max_tokens = 1024
            mock_settings.subagent_max_rounds = 1
            mock_settings.verbose = False
            with patch("pip_agent.subagent.execute_tool", return_value="x"):
                result = run_subagent(client, "Do something", profiler)

        assert result == "(sub-agent returned no text)"
