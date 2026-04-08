from __future__ import annotations

import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pip_agent.profiler import Profiler
from pip_agent.team import (
    Bus,
    ProtocolTracker,
    TeamManager,
    Teammate,
    TeammateSpec,
    VALID_MSG_TYPES,
    _parse_frontmatter,
)
from pip_agent.tool_dispatch import DispatchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_MD = """\
---
name: alice
description: "Python backend developer."
model: test-model
max_turns: 5
tools: [bash, read, write]
---

You are alice, a Python backend developer.
"""

MINIMAL_MD = """\
---
name: bob
description: "Helper bot."
---

You are bob.
"""

NO_FRONTMATTER_MD = "Just a plain body with no YAML."


def _write_md(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(
    name: str, tool_input: dict, block_id: str = "tu_1",
) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def _make_response(
    content: list, stop_reason: str = "end_turn",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _make_mgr(tmp_path, *names):
    """Create a TeamManager with the given teammate .md files."""
    user_dir = tmp_path / "user"
    mds = {"alice": SAMPLE_MD, "bob": MINIMAL_MD}
    for name in names:
        _write_md(user_dir, name, mds.get(name, SAMPLE_MD))
    return TeamManager(
        tmp_path / "builtin", user_dir, MagicMock(), Profiler(),
    )


# ---------------------------------------------------------------------------
# TeammateSpec
# ---------------------------------------------------------------------------


class TestTeammateSpec:
    def test_parse_full(self, tmp_path):
        path = _write_md(tmp_path, "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        assert spec.name == "alice"
        assert spec.description == "Python backend developer."
        assert "Python backend developer" in spec.system_body

    def test_parse_minimal(self, tmp_path):
        path = _write_md(tmp_path, "bob", MINIMAL_MD)
        spec = TeammateSpec.from_file(path)
        assert spec.name == "bob"
        assert spec.description == "Helper bot."

    def test_no_frontmatter_uses_filename(self, tmp_path):
        path = _write_md(tmp_path, "charlie", NO_FRONTMATTER_MD)
        spec = TeammateSpec.from_file(path)
        assert spec.name == "charlie"
        assert spec.system_body == NO_FRONTMATTER_MD


class TestParseFrontmatter:
    def test_valid(self):
        meta, body = _parse_frontmatter("---\nname: x\n---\nBody text.")
        assert meta["name"] == "x"
        assert body == "Body text."

    def test_no_frontmatter(self):
        meta, body = _parse_frontmatter("Just text.")
        assert meta == {}
        assert body == "Just text."

    def test_invalid_yaml(self):
        meta, body = _parse_frontmatter("---\n: [bad yaml\n---\nBody.")
        assert meta == {}
        assert body == "Body."


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


class TestBusSendAndRead:
    def test_send_then_read(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        bus.send("lead", "alice", "Hello", "message")
        msgs = bus.read_inbox("alice")
        assert len(msgs) == 1
        assert msgs[0]["from"] == "lead"
        assert msgs[0]["content"] == "Hello"
        assert msgs[0]["type"] == "message"
        assert "ts" in msgs[0]

    def test_drain_clears_inbox(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        bus.send("lead", "alice", "msg1")
        first = bus.read_inbox("alice")
        second = bus.read_inbox("alice")
        assert len(first) == 1
        assert len(second) == 0

    def test_empty_inbox(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        assert bus.read_inbox("nobody") == []

    def test_invalid_msg_type_rejected(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        result = bus.send("a", "b", "c", "invalid_type")
        assert "[error]" in result
        assert bus.read_inbox("b") == []

    def test_all_valid_msg_types(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        for mt in VALID_MSG_TYPES:
            result = bus.send("lead", "test", f"body-{mt}", mt)
            assert "Sent" in result
        msgs = bus.read_inbox("test")
        assert len(msgs) == len(VALID_MSG_TYPES)

    def test_concurrent_writes(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        errors: list[Exception] = []

        def writer(i: int) -> None:
            try:
                for j in range(20):
                    bus.send(f"w{i}", "target", f"msg-{i}-{j}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        msgs = bus.read_inbox("target")
        assert len(msgs) == 100

    def test_multiple_messages_accumulate(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        bus.send("a", "inbox_owner", "msg1")
        bus.send("b", "inbox_owner", "msg2")
        bus.send("c", "inbox_owner", "msg3")
        msgs = bus.read_inbox("inbox_owner")
        assert len(msgs) == 3
        assert [m["from"] for m in msgs] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# TeamManager — discovery
# ---------------------------------------------------------------------------


class TestTeamManagerDiscovery:
    def test_dual_dir_discovery(self, tmp_path):
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        _write_md(
            builtin, "alpha",
            "---\nname: alpha\ndescription: Alpha bot.\n---\nAlpha.",
        )
        _write_md(
            user, "beta",
            "---\nname: beta\ndescription: Beta bot.\n---\nBeta body.",
        )
        mgr = TeamManager(builtin, user, MagicMock(), Profiler())
        result = mgr.status()
        assert "alpha" in result
        assert "beta" in result

    def test_user_wins_on_collision(self, tmp_path):
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        _write_md(
            builtin, "alice",
            "---\nname: alice\ndescription: Builtin alice.\n---\nOld.",
        )
        _write_md(
            user, "alice",
            "---\nname: alice\ndescription: User alice.\n---\nNew.",
        )
        mgr = TeamManager(builtin, user, MagicMock(), Profiler())
        assert "User alice" in mgr.status()
        assert "Builtin alice" not in mgr.status()

    def test_missing_dirs_ok(self, tmp_path):
        mgr = TeamManager(
            tmp_path / "no_builtin",
            tmp_path / "no_user",
            MagicMock(),
            Profiler(),
        )
        assert mgr.status() == "No teammates defined."

    def test_malformed_md_skipped(self, tmp_path):
        d = tmp_path / "team"
        d.mkdir()
        (d / "bad.md").write_text("---\n: [invalid\n---\nbody", encoding="utf-8")
        _write_md(d, "good", SAMPLE_MD)
        mgr = TeamManager(
            tmp_path / "empty", d, MagicMock(), Profiler(),
        )
        result = mgr.status()
        assert "good" in result or "alice" in result


# ---------------------------------------------------------------------------
# TeamManager — spawn
# ---------------------------------------------------------------------------


class TestTeamManagerSpawn:
    def test_spawn_success(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            result = mgr.spawn("alice", "Do some work", model="m", max_turns=5)
        assert "Spawned" in result
        assert "alice" in result

    def test_spawn_writes_prompt_to_inbox(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Build the feature", model="m", max_turns=5)
        msgs = mgr._bus.read_inbox("alice")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Build the feature"
        assert msgs[0]["from"] == "lead"

    def test_spawn_already_working_rejected(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task 1", model="m", max_turns=5)
            result = mgr.spawn("alice", "Task 2", model="m", max_turns=5)
        assert "[error]" in result
        assert "currently working" in result

    def test_spawn_unknown_rejected(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        result = mgr.spawn("nobody", "Task", model="m", max_turns=5)
        assert "[error]" in result

    def test_spawn_shows_working_in_status(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Work", model="m", max_turns=5)
        assert "[working]" in mgr.status()


class TestTeamManagerRescan:
    def test_spawn_picks_up_files_created_after_init(self, tmp_path):
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        mgr = TeamManager(
            tmp_path / "builtin", user_dir, MagicMock(), Profiler(),
        )
        assert mgr.status() == "No teammates defined."

        _write_md(user_dir, "alice", SAMPLE_MD)
        with patch.object(Teammate, "start"):
            result = mgr.spawn("alice", "Task", model="m", max_turns=5)
        assert "Spawned" in result

    def test_status_picks_up_files_created_after_init(self, tmp_path):
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        mgr = TeamManager(
            tmp_path / "builtin", user_dir, MagicMock(), Profiler(),
        )
        assert mgr.status() == "No teammates defined."

        _write_md(user_dir, "alice", SAMPLE_MD)
        result = mgr.status()
        assert "alice" in result
        assert "[offline]" in result


# ---------------------------------------------------------------------------
# TeamManager — send
# ---------------------------------------------------------------------------


class TestTeamManagerSend:
    def test_send_to_working_succeeds(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Initial task", model="m", max_turns=5)
            mgr._bus.read_inbox("alice")
        result = mgr.send("alice", "Follow-up")
        assert "Sent" in result

    def test_send_to_unspawned_queued(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        result = mgr.send("alice", "hello")
        assert "offline" in result
        assert "[error]" not in result

    def test_send_to_done_teammate_queued(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
        mgr._on_done("alice")
        result = mgr.send("alice", "follow-up")
        assert "offline" in result
        assert "[error]" not in result

    def test_broadcast_only_to_working(self, tmp_path):
        user_dir = tmp_path / "user"
        _write_md(user_dir, "alice", SAMPLE_MD)
        _write_md(
            user_dir, "bob",
            "---\nname: bob\ndescription: Bob.\n---\nBob body.",
        )
        mgr = TeamManager(tmp_path / "b", user_dir, MagicMock(), Profiler())
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task A", model="m", max_turns=5)
            mgr.spawn("bob", "Task B", model="m", max_turns=5)
        result = mgr.send("all", "hello everyone", "broadcast")
        assert "Broadcast" in result
        assert "2" in result


# ---------------------------------------------------------------------------
# TeamManager — status (three-state: working / idle / offline)
# ---------------------------------------------------------------------------


class TestTeamManagerStatus:
    def test_all_offline_by_default(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        result = mgr.status()
        assert "[offline]" in result
        assert "[working]" not in result

    def test_working_after_spawn(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
        result = mgr.status()
        assert "[working]" in result

    def test_offline_after_done(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
        mgr._on_done("alice")
        result = mgr.status()
        assert "[offline]" in result
        assert "[working]" not in result

    def test_respawn_after_done(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task 1", model="m", max_turns=5)
        mgr._on_done("alice")
        with patch.object(Teammate, "start"):
            result = mgr.spawn("alice", "Task 2", model="m", max_turns=5)
        assert "Spawned" in result
        assert "[working]" in mgr.status()


# ---------------------------------------------------------------------------
# TeamManager — other
# ---------------------------------------------------------------------------


class TestTeamManagerReadInbox:
    def test_read_inbox_empty(self, tmp_path):
        mgr = TeamManager(
            tmp_path / "b", tmp_path / "u", MagicMock(), Profiler(),
        )
        assert mgr.read_inbox() == []


class TestTeamManagerLifecycle:
    def test_deactivate_all(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
        mgr.deactivate_all()
        assert "[offline]" in mgr.status()


# ---------------------------------------------------------------------------
# Teammate LLM loop
# ---------------------------------------------------------------------------


class TestTeammateLLMLoop:
    def _make_teammate(self, tmp_path, spec=None, max_turns=5):
        if spec is None:
            path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
            spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        profiler = Profiler()
        return Teammate(
            spec, client, bus, profiler,
            model="test-model", max_turns=max_turns,
            active_names_fn=lambda: ["alice"],
        ), client, bus

    def test_send_tool_dispatches_to_bus(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path)
        client.messages.create.side_effect = [
            _make_response(
                [_tool_use_block("send", {"to": "lead", "content": "done"})],
                stop_reason="tool_use",
            ),
            _make_response([_text_block("ok")]),
        ]
        bus.send("lead", "alice", "Do work")
        t.start()
        time.sleep(0.5)

        lead_msgs = bus.read_inbox("lead")
        assert any(m["content"] == "done" for m in lead_msgs)

    def test_read_inbox_tool(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path)
        bus.send("lead", "alice", "Initial task")
        bus.send("lead", "alice", "Extra info")

        call_count = [0]

        def create_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                bus.send("lead", "alice", "mid-task update")
                return _make_response(
                    [_tool_use_block("read_inbox", {})],
                    stop_reason="tool_use",
                )
            return _make_response([_text_block("done")])

        client.messages.create.side_effect = create_side_effect
        inbox = bus.read_inbox("alice")
        t._work([], inbox)

    def test_max_turns_respected(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path, max_turns=2)

        client.messages.create.return_value = _make_response(
            [_tool_use_block("read", {"file_path": "x.txt"})],
            stop_reason="tool_use",
        )

        with patch(
            "pip_agent.team.dispatch_tool",
            return_value=DispatchResult(content="content"),
        ):
            t._work([], [{"from": "lead", "type": "message", "content": "go"}])

        assert client.messages.create.call_count == 2

    def test_tool_allowlist_enforced(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path)
        t._plan_manager = MagicMock()
        tools = t._build_tools()
        tool_names = {tool["name"] for tool in tools}
        assert "bash" in tool_names
        assert "read" in tool_names
        assert "write" in tool_names
        assert "send" in tool_names
        assert "read_inbox" in tool_names
        assert "idle" in tool_names
        assert "claim_task" in tool_names
        assert "task_board_overview" in tool_names
        assert "task_board_detail" in tool_names
        assert "task_update" in tool_names
        assert "task" not in tool_names
        assert "team_spawn" not in tool_names
        assert "task_create" not in tool_names
        assert "compact" not in tool_names


# ---------------------------------------------------------------------------
# Single-shot → work-idle lifecycle
# ---------------------------------------------------------------------------


class TestTeammateSingleShot:
    def _make_teammate(self, tmp_path, spec=None):
        if spec is None:
            path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
            spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        profiler = Profiler()
        return Teammate(
            spec, client, bus, profiler,
            model="test-model", max_turns=5,
            active_names_fn=lambda: ["alice"],
        ), client, bus

    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 0.2)
    def test_thread_ends_after_idle_timeout(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path)
        client.messages.create.return_value = _make_response([_text_block("done")])
        bus.send("lead", "alice", "Do work")
        t.start()
        time.sleep(1)
        assert t.status == "offline"
        assert client.messages.create.call_count == 1

    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 5)
    def test_idle_picks_up_second_message(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path)
        client.messages.create.return_value = _make_response([_text_block("done")])
        bus.send("lead", "alice", "Do work")
        t.start()
        time.sleep(0.3)
        assert t.status == "idle"

        bus.send("lead", "alice", "Second task")
        time.sleep(0.3)
        assert client.messages.create.call_count == 2
        t.stop()

    def test_shutdown_during_wait_sets_offline(self, tmp_path):
        t, client, bus = self._make_teammate(tmp_path)
        t.start()
        time.sleep(0.3)
        t.stop()
        time.sleep(2.5)
        assert t.status == "offline"

    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 0.2)
    def test_done_fn_called_on_finish(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        done_fn = MagicMock()
        t = Teammate(
            spec, client, bus, Profiler(),
            model="test-model", max_turns=5,
            done_fn=done_fn,
        )
        client.messages.create.return_value = _make_response([_text_block("ok")])
        bus.send("lead", "alice", "Task")
        t.start()
        time.sleep(1)
        done_fn.assert_called_once_with("alice")


# ---------------------------------------------------------------------------
# Teammate send (bus-only, no wake)
# ---------------------------------------------------------------------------


class TestTeammateSend:
    def test_send_writes_to_bus_only(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        t = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5,
            active_names_fn=lambda: ["alice", "bob"],
        )
        result = t._handle_send({"to": "bob", "content": "hello"})
        assert "Sent" in result
        msgs = bus.read_inbox("bob")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    def test_broadcast_writes_to_bus_only(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        t = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5,
            active_names_fn=lambda: ["alice", "bob"],
        )
        result = t._handle_send({"to": "all", "content": "hey", "msg_type": "broadcast"})
        assert "Broadcast" in result
        bob_msgs = bus.read_inbox("bob")
        lead_msgs = bus.read_inbox("lead")
        assert len(bob_msgs) == 1
        assert len(lead_msgs) == 1


# ---------------------------------------------------------------------------
# Bus — extra fields
# ---------------------------------------------------------------------------


class TestBusExtra:
    def test_extra_fields_included_in_message(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        bus.send("lead", "alice", "wrap up", "shutdown_request", req_id="abc")
        msgs = bus.read_inbox("alice")
        assert len(msgs) == 1
        assert msgs[0]["req_id"] == "abc"
        assert msgs[0]["type"] == "shutdown_request"

    def test_extra_approve_field(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        bus.send(
            "alice", "lead", "ok", "shutdown_response",
            req_id="abc", approve=True,
        )
        msgs = bus.read_inbox("lead")
        assert len(msgs) == 1
        assert msgs[0]["req_id"] == "abc"
        assert msgs[0]["approve"] is True

    def test_no_extra_fields_when_not_provided(self, tmp_path):
        bus = Bus(tmp_path / "inbox")
        bus.send("lead", "alice", "hello")
        msgs = bus.read_inbox("alice")
        assert "req_id" not in msgs[0]
        assert "approve" not in msgs[0]


# ---------------------------------------------------------------------------
# ProtocolTracker
# ---------------------------------------------------------------------------


class TestProtocolTracker:
    def test_open_shutdown_creates_pending(self):
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        entry = pt.get(req_id)
        assert entry is not None
        assert entry["target"] == "alice"
        assert entry["status"] == "pending"

    def test_open_plan_creates_pending(self):
        pt = ProtocolTracker()
        req_id = pt.open_plan("alice", "Refactor auth module")
        entry = pt.get(req_id)
        assert entry is not None
        assert entry["from"] == "alice"
        assert entry["plan"] == "Refactor auth module"
        assert entry["status"] == "pending"

    def test_resolve_approve(self):
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        result = pt.resolve(req_id, approve=True)
        assert result == "approved"
        assert pt.get(req_id)["status"] == "approved"

    def test_resolve_reject(self):
        pt = ProtocolTracker()
        req_id = pt.open_plan("alice", "plan text")
        result = pt.resolve(req_id, approve=False)
        assert result == "rejected"
        assert pt.get(req_id)["status"] == "rejected"

    def test_double_resolve_returns_error(self):
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        pt.resolve(req_id, approve=True)
        result = pt.resolve(req_id, approve=False)
        assert "[error]" in result
        assert "already" in result
        assert pt.get(req_id)["status"] == "approved"

    def test_resolve_unknown_returns_error(self):
        pt = ProtocolTracker()
        result = pt.resolve("nonexistent", approve=True)
        assert "[error]" in result
        assert "Unknown" in result

    def test_get_unknown_returns_none(self):
        pt = ProtocolTracker()
        assert pt.get("nonexistent") is None

    def test_get_returns_copy(self):
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        entry = pt.get(req_id)
        entry["status"] = "tampered"
        assert pt.get(req_id)["status"] == "pending"

    def test_unique_req_ids(self):
        pt = ProtocolTracker()
        ids = {pt.open_shutdown("alice") for _ in range(50)}
        assert len(ids) == 50

    def test_thread_safety(self):
        pt = ProtocolTracker()
        errors: list[Exception] = []
        ids: list[str] = []
        lock = threading.Lock()

        def opener(i: int) -> None:
            try:
                for _ in range(20):
                    req_id = pt.open_shutdown(f"target-{i}")
                    with lock:
                        ids.append(req_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=opener, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(ids) == 100
        assert len(set(ids)) == 100


# ---------------------------------------------------------------------------
# Shutdown protocol
# ---------------------------------------------------------------------------


class TestShutdownProtocol:
    def test_manager_send_creates_req_id(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
            mgr._bus.read_inbox("alice")
        mgr.send("alice", "Please shut down", "shutdown_request")
        msgs = mgr._bus.read_inbox("alice")
        assert len(msgs) == 1
        assert msgs[0]["type"] == "shutdown_request"
        assert "req_id" in msgs[0]
        req_id = msgs[0]["req_id"]
        entry = mgr._protocol.get(req_id)
        assert entry["target"] == "alice"
        assert entry["status"] == "pending"

    def test_teammate_approve_resolves_tracker(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        t = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5,
            protocol=pt,
            active_names_fn=lambda: ["alice"],
        )
        result = t._handle_send({
            "to": "lead",
            "content": "Shutting down.",
            "msg_type": "shutdown_response",
            "req_id": req_id,
            "approve": True,
        })
        assert "Sent" in result
        assert pt.get(req_id)["status"] == "approved"
        assert t._approved_shutdown is True

    def test_teammate_reject_resolves_tracker(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        t = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5,
            protocol=pt,
            active_names_fn=lambda: ["alice"],
        )
        t._handle_send({
            "to": "lead",
            "content": "Still working.",
            "msg_type": "shutdown_response",
            "req_id": req_id,
            "approve": False,
        })
        assert pt.get(req_id)["status"] == "rejected"
        assert t._approved_shutdown is False

    def test_approved_shutdown_exits_loop(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        t = Teammate(
            spec, client, bus, Profiler(),
            model="test-model", max_turns=5,
            protocol=pt,
            active_names_fn=lambda: ["alice"],
        )
        client.messages.create.side_effect = [
            _make_response(
                [_tool_use_block("send", {
                    "to": "lead",
                    "content": "ok",
                    "msg_type": "shutdown_response",
                    "req_id": req_id,
                    "approve": True,
                })],
                stop_reason="tool_use",
            ),
        ]
        bus.send(
            "lead", "alice", "Please shut down",
            "shutdown_request", req_id=req_id,
        )
        t.start()
        time.sleep(1)
        assert t.status == "offline"
        assert pt.get(req_id)["status"] == "approved"
        assert client.messages.create.call_count == 1

    def test_shutdown_response_on_bus(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        pt = ProtocolTracker()
        req_id = pt.open_shutdown("alice")
        t = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5,
            protocol=pt,
            active_names_fn=lambda: ["alice"],
        )
        t._handle_send({
            "to": "lead",
            "content": "bye",
            "msg_type": "shutdown_response",
            "req_id": req_id,
            "approve": True,
        })
        lead_msgs = bus.read_inbox("lead")
        assert len(lead_msgs) == 1
        assert lead_msgs[0]["type"] == "shutdown_response"
        assert lead_msgs[0]["req_id"] == req_id
        assert lead_msgs[0]["approve"] is True


# ---------------------------------------------------------------------------
# Plan approval protocol
# ---------------------------------------------------------------------------


class TestPlanApprovalProtocol:
    def test_teammate_plan_request_creates_req_id(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        pt = ProtocolTracker()
        t = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5,
            protocol=pt,
            active_names_fn=lambda: ["alice"],
        )
        t._handle_send({
            "to": "lead",
            "content": "Refactor auth module",
            "msg_type": "plan_request",
        })
        lead_msgs = bus.read_inbox("lead")
        assert len(lead_msgs) == 1
        assert lead_msgs[0]["type"] == "plan_request"
        req_id = lead_msgs[0]["req_id"]
        entry = pt.get(req_id)
        assert entry["from"] == "alice"
        assert entry["plan"] == "Refactor auth module"
        assert entry["status"] == "pending"

    def test_manager_approve_plan(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        req_id = mgr._protocol.open_plan("alice", "Refactor auth")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
            mgr._bus.read_inbox("alice")
        mgr.send(
            "alice", "Approved, go ahead.",
            "plan_response", req_id=req_id, approve=True,
        )
        assert mgr._protocol.get(req_id)["status"] == "approved"
        msgs = mgr._bus.read_inbox("alice")
        assert len(msgs) == 1
        assert msgs[0]["type"] == "plan_response"
        assert msgs[0]["req_id"] == req_id
        assert msgs[0]["approve"] is True

    def test_manager_reject_plan(self, tmp_path):
        mgr = _make_mgr(tmp_path, "alice")
        req_id = mgr._protocol.open_plan("alice", "Delete everything")
        with patch.object(Teammate, "start"):
            mgr.spawn("alice", "Task", model="m", max_turns=5)
            mgr._bus.read_inbox("alice")
        mgr.send(
            "alice", "Too risky.",
            "plan_response", req_id=req_id, approve=False,
        )
        assert mgr._protocol.get(req_id)["status"] == "rejected"


# ---------------------------------------------------------------------------
# Idle tool
# ---------------------------------------------------------------------------


class TestIdleTool:
    def test_idle_tool_exits_work_loop(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=5)

        client.messages.create.side_effect = [
            _make_response(
                [_tool_use_block("idle", {})],
                stop_reason="tool_use",
            ),
        ]
        messages: list[dict] = []
        t._work(messages, [{"from": "lead", "type": "message", "content": "go"}])
        assert t._idle_requested is True
        assert client.messages.create.call_count == 1

    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 5)
    def test_idle_tool_triggers_idle_cycle(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=5)

        client.messages.create.return_value = _make_response(
            [_tool_use_block("idle", {})],
            stop_reason="tool_use",
        )
        bus.send("lead", "alice", "Do stuff")
        t.start()
        time.sleep(0.3)
        assert t.status == "idle"
        t.stop()


# ---------------------------------------------------------------------------
# claim_task tool
# ---------------------------------------------------------------------------


class TestClaimTaskTool:
    def test_claim_task_tool_calls_plan_manager(self, tmp_path):
        from pip_agent.task_graph import PlanManager

        pm = PlanManager(tmp_path / "tasks")
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "Task 1"}])

        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(
            spec, client, bus, Profiler(),
            model="test-model", max_turns=5,
            plan_manager=pm,
        )

        client.messages.create.side_effect = [
            _make_response(
                [_tool_use_block(
                    "claim_task",
                    {"story": "s1", "task_id": "t1"},
                )],
                stop_reason="tool_use",
            ),
            _make_response([_text_block("working on it")]),
        ]
        t._work([], [{"from": "lead", "type": "message", "content": "go"}])

        task = pm._task_graph("s1").load_all()["t1"]
        assert task.status == "in_progress"
        assert task.owner == "alice"

    def test_claim_task_tool_present_only_with_plan_manager(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")

        t_without = Teammate(
            spec, MagicMock(), bus, Profiler(), model="test-model", max_turns=5,
        )
        names = {tool["name"] for tool in t_without._build_tools()}
        assert "claim_task" not in names
        assert "task_update" not in names

        t_with = Teammate(
            spec, MagicMock(), bus, Profiler(),
            model="test-model", max_turns=5, plan_manager=MagicMock(),
        )
        names = {tool["name"] for tool in t_with._build_tools()}
        assert "claim_task" in names
        assert "task_board_overview" in names
        assert "task_board_detail" in names
        assert "task_update" in names


# ---------------------------------------------------------------------------
# Idle task board hint (no auto-claim)
# ---------------------------------------------------------------------------


class TestAutonomousClaim:
    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 2)
    def test_idle_injects_hint_without_claiming(self, tmp_path):
        from pip_agent.task_graph import PlanManager

        pm = PlanManager(tmp_path / "tasks")
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "Auto task"}])

        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(
            spec, client, bus, Profiler(),
            model="test-model", max_turns=5,
            plan_manager=pm,
        )

        call_count = [0]

        def create_side_effect(**kwargs):
            call_count[0] += 1
            return _make_response([_text_block("done")])

        client.messages.create.side_effect = create_side_effect

        bus.send("lead", "alice", "Initial task")
        t.start()
        time.sleep(1)

        task = pm._task_graph("s1").load_all()["t1"]
        assert task.status == "pending"
        assert task.owner == ""
        assert call_count[0] >= 2

    def test_task_board_hint_suppressed_when_revision_seen(self, tmp_path):
        from pip_agent.task_graph import PlanManager

        pm = PlanManager(tmp_path / "tasks")
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        t = Teammate(
            spec, MagicMock(), Bus(tmp_path / "inbox"), Profiler(),
            model="test-model", max_turns=5,
            plan_manager=pm,
        )
        assert t._maybe_task_board_hint() is not None
        t._board_revision_seen = pm.board_revision
        assert t._maybe_task_board_hint() is None

    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 0.5)
    def test_shutdown_request_in_idle_exits(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=5)

        client.messages.create.return_value = _make_response([_text_block("ok")])
        bus.send("lead", "alice", "Do work")
        t.start()
        time.sleep(0.3)
        assert t.status == "idle"

        bus.send("lead", "alice", "bye", "shutdown_request", req_id="r1")
        time.sleep(0.3)
        assert t.status == "offline"


# ---------------------------------------------------------------------------
# Identity re-injection
# ---------------------------------------------------------------------------


class TestIdentityReinjection:
    def test_injects_when_messages_short(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        t = Teammate(
            spec, MagicMock(), Bus(tmp_path / "inbox"), Profiler(),
            model="test-model", max_turns=5,
        )

        messages: list[dict] = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        t._reinject_identity(messages)
        assert len(messages) == 4
        assert "<identity>" in messages[0]["content"]
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert spec.name in messages[1]["content"]

    def test_skips_when_messages_long(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        t = Teammate(
            spec, MagicMock(), Bus(tmp_path / "inbox"), Profiler(),
            model="test-model", max_turns=5,
        )

        messages: list[dict] = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        t._reinject_identity(messages)
        assert len(messages) == 4


# ---------------------------------------------------------------------------
# Three-state status display
# ---------------------------------------------------------------------------


class TestThreeStateStatus:
    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 2)
    def test_status_transitions(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=5)

        assert t.status == "working"

        client.messages.create.return_value = _make_response([_text_block("ok")])
        bus.send("lead", "alice", "Task")
        t.start()
        time.sleep(0.3)
        assert t.status == "idle"

        t.stop()
        time.sleep(0.5)
        assert t.status == "offline"


# ---------------------------------------------------------------------------
# max_turns override and notifications
# ---------------------------------------------------------------------------


class TestMaxTurns:
    def test_max_turns_stored(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, MagicMock(), bus, Profiler(), model="test-model", max_turns=99)
        assert t._max_turns == 99

    def test_max_turns_exhausted_notifies_lead(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=2)

        client.messages.create.return_value = _make_response(
            [_tool_use_block("read", {"file_path": "x.txt"})],
            stop_reason="tool_use",
        )

        with patch(
            "pip_agent.team.dispatch_tool",
            return_value=DispatchResult(content="content"),
        ):
            t._work([], [{"from": "lead", "type": "message", "content": "go"}])

        assert client.messages.create.call_count == 2
        lead_msgs = bus.read_inbox("lead")
        assert any("turns" in m["content"] for m in lead_msgs)


class TestFinishNotification:
    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 0.2)
    def test_offline_sends_reason_to_lead(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=5)

        client.messages.create.return_value = _make_response([_text_block("ok")])
        bus.send("lead", "alice", "Task")
        t.start()
        time.sleep(1)
        assert t.status == "offline"

        lead_msgs = bus.read_inbox("lead")
        offline_msgs = [m for m in lead_msgs if m.get("type") == "status"]
        assert len(offline_msgs) >= 1
        assert "idle timeout" in offline_msgs[-1]["content"]

    @patch("pip_agent.team.IDLE_POLL_INTERVAL", 0.05)
    @patch("pip_agent.team.IDLE_TIMEOUT", 0.2)
    def test_max_turns_reason_in_finish(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=1)

        client.messages.create.return_value = _make_response(
            [_tool_use_block("read", {"file_path": "x.txt"})],
            stop_reason="tool_use",
        )

        bus.send("lead", "alice", "Task")
        with patch(
            "pip_agent.team.dispatch_tool",
            return_value=DispatchResult(content="ok"),
        ):
            t.start()
            time.sleep(1)

        assert t.status == "offline"
        lead_msgs = bus.read_inbox("lead")
        status_msgs = [m for m in lead_msgs if m.get("type") == "status"]
        reasons = " ".join(m["content"] for m in status_msgs)
        assert "turns" in reasons
        assert "idle timeout" in reasons

    def test_crash_reason_in_finish(self, tmp_path):
        path = _write_md(tmp_path / "team", "alice", SAMPLE_MD)
        spec = TeammateSpec.from_file(path)
        client = MagicMock()
        bus = Bus(tmp_path / "inbox")
        t = Teammate(spec, client, bus, Profiler(), model="test-model", max_turns=5)

        def run_inner_boom():
            raise RuntimeError("boom")

        t._run_inner = run_inner_boom
        bus.send("lead", "alice", "Task")
        t.start()
        time.sleep(0.5)

        assert t.status == "offline"
        lead_msgs = bus.read_inbox("lead")
        status_msgs = [m for m in lead_msgs if m.get("type") == "status"]
        reasons = " ".join(m["content"] for m in status_msgs)
        assert "crashed" in reasons


class TestSpawnMaxTurns:
    def test_spawn_respects_max_turns(self, tmp_path):
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        _write_md(builtin_dir, "alice", SAMPLE_MD)
        client = MagicMock()
        mgr = TeamManager(builtin_dir, user_dir, client, Profiler())

        client.messages.create.return_value = _make_response([_text_block("ok")])
        result = mgr.spawn("alice", "task", model="m", max_turns=99)
        assert "max 99 turns" in result
        assert mgr._active["alice"]._max_turns == 99
        mgr.deactivate_all()


# ---------------------------------------------------------------------------
# Unified tool pool (tools_for_role)
# ---------------------------------------------------------------------------


class TestToolsForRole:
    def test_lead_sees_claim_task_and_task_board(self):
        from pip_agent.tools import tools_for_role
        names = {t["name"] for t in tools_for_role("lead")}
        assert "claim_task" in names
        assert "task_board_overview" in names
        assert "task_board_detail" in names

    def test_lead_does_not_see_teammate_only_tools(self):
        from pip_agent.tools import tools_for_role
        names = {t["name"] for t in tools_for_role("lead")}
        assert "send" not in names
        assert "read_inbox" not in names
        assert "idle" not in names

    def test_teammate_does_not_see_lead_only_tools(self):
        from pip_agent.tools import tools_for_role
        names = {t["name"] for t in tools_for_role("teammate")}
        assert "team_spawn" not in names
        assert "task_create" not in names
        assert "task" not in names
        assert "compact" not in names

    def test_teammate_sees_shared_tools(self):
        from pip_agent.tools import tools_for_role
        names = {t["name"] for t in tools_for_role("teammate")}
        assert "bash" in names
        assert "read" in names
        assert "write" in names
        assert "claim_task" in names
        assert "task_board_overview" in names
        assert "task_update" in names
        assert "send" in names
        assert "read_inbox" in names
        assert "idle" in names


# ---------------------------------------------------------------------------
# Lead claim_task via dispatch
# ---------------------------------------------------------------------------


class TestLeadClaimTask:
    def test_lead_claims_task_via_dispatch(self, tmp_path):
        from pip_agent.task_graph import PlanManager
        from pip_agent.tool_dispatch import ToolContext, dispatch_tool

        pm = PlanManager(tmp_path / "tasks")
        pm.create(None, [{"id": "s1", "title": "Story"}])
        pm.create("s1", [{"id": "t1", "title": "Task 1"}])

        ctx = ToolContext(plan_manager=pm, caller="lead")
        result = dispatch_tool(ctx, "claim_task", {"story": "s1", "task_id": "t1"})
        assert "[error]" not in result.content

        task = pm._task_graph("s1").load_all()["t1"]
        assert task.status == "in_progress"
        assert task.owner == "lead"

    def test_lead_claim_task_without_plan_manager(self):
        from pip_agent.tool_dispatch import ToolContext, dispatch_tool

        ctx = ToolContext(caller="lead")
        result = dispatch_tool(ctx, "claim_task", {"story": "s1", "task_id": "t1"})
        assert "Unknown tool" in result.content

    def test_teammate_claim_sets_caller_as_owner(self, tmp_path):
        from pip_agent.task_graph import PlanManager
        from pip_agent.tool_dispatch import ToolContext, dispatch_tool

        pm = PlanManager(tmp_path / "tasks")
        pm.create(None, [{"id": "s1", "title": "Story"}])
        pm.create("s1", [{"id": "t1", "title": "Task 1"}])

        ctx = ToolContext(plan_manager=pm, caller="alice")
        result = dispatch_tool(ctx, "claim_task", {"story": "s1", "task_id": "t1"})
        assert "[error]" not in result.content

        task = pm._task_graph("s1").load_all()["t1"]
        assert task.status == "in_progress"
        assert task.owner == "alice"
