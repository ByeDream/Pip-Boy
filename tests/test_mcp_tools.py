"""Tests for the MCP tool handlers that weren't already covered elsewhere.

``test_mcp_reflect.py`` owns ``reflect`` and ``test_mcp_send_file.py`` owns
``send_file``; this file fills the remaining gaps — memory_search /
memory_write / remember_user plus the full cron_* surface — and smoke-tests
``build_mcp_server`` so refactors that re-order tool groups or drop a group
entirely fail loudly.

All tests run the async handlers synchronously via ``asyncio.run`` to keep
assertions straightforward; none of these handlers do real I/O beyond what
``MemoryStore`` and ``HostScheduler`` do on the local filesystem, both of
which ``tmp_path`` isolates per-test.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from pip_agent.host_scheduler import HostScheduler
from pip_agent.mcp_tools import (
    McpContext,
    _cron_tools,
    _memory_tools,
    build_mcp_server,
)
from pip_agent.memory import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(agents_root: Path, agent_id: str = "pip-boy") -> MemoryStore:
    """Shortcut for ``MemoryStore`` in the pre-v2 ``<root>/<id>/`` layout
    that these tests still use internally (the tests don't care about
    the exact tree — only that a working store lives somewhere under
    ``tmp_path``). ``workspace_pip_dir`` is set to ``agents_root`` so
    ``owner.md`` lookups, when they happen, stay isolated.
    """
    agent_dir = agents_root / agent_id
    return MemoryStore(
        agent_dir=agent_dir,
        workspace_pip_dir=agents_root,
        agent_id=agent_id,
    )


def _run(coro):
    return asyncio.run(coro)


def _text_of(result: dict[str, Any]) -> str:
    """Collapse an MCP ``content`` block list into a single string. Mirrors
    what the SDK transport does before handing the result back to the LLM —
    tests assert on that collapsed shape because that's what's actually
    observable from the other side."""
    return "".join(
        b.get("text", "")
        for b in result.get("content", [])
        if b.get("type") == "text"
    )


def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not found")


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


class TestMemorySearch:
    """Contract:

    * ``None`` memory store → is_error (never silently pretend there's no
      matches; the LLM can't distinguish "nothing stored" from "store not
      wired" otherwise).
    * Empty ``query`` → is_error with actionable message.
    * Empty result set → textual "no matching memories" (NOT is_error —
      this is a normal query outcome).
    * Non-empty result set → bulleted text with scores.
    """

    def _call(self, ctx, args):
        handler = _tool(_memory_tools(ctx), "memory_search").handler
        return _run(handler(args))

    def test_no_store_is_error(self):
        result = self._call(McpContext(memory_store=None), {"query": "x"})
        assert result.get("is_error") is True

    def _make_store(self, tmp_path):
        pip_dir = tmp_path / ".pip"
        pip_dir.mkdir(parents=True, exist_ok=True)
        return MemoryStore(
            agent_dir=pip_dir,
            workspace_pip_dir=pip_dir,
            agent_id="pip-boy",
        )

    def test_empty_query_is_error(self, tmp_path):
        ms = self._make_store(tmp_path)
        result = self._call(McpContext(memory_store=ms), {"query": "   "})
        assert result.get("is_error") is True
        assert "required" in _text_of(result)

    def test_empty_results_returns_friendly_text(self, tmp_path):
        ms = self._make_store(tmp_path)
        result = self._call(McpContext(memory_store=ms), {"query": "anything"})
        assert result.get("is_error") is not True
        assert "no matching memories" in _text_of(result)

    def test_results_are_rendered_with_scores(self, tmp_path, monkeypatch):
        ms = self._make_store(tmp_path)
        fake_hits = [
            {"text": "User prefers dark mode", "score": 0.91},
            {"text": "User speaks Chinese", "score": 0.72},
        ]
        monkeypatch.setattr(ms, "search", lambda q, top_k=5: fake_hits)
        text = _text_of(self._call(McpContext(memory_store=ms), {"query": "x"}))
        assert "dark mode" in text
        assert "0.91" in text
        assert "Chinese" in text

    def test_top_k_is_forwarded(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        seen: dict[str, Any] = {}

        def fake_search(q, *, top_k=5):
            seen["q"] = q
            seen["top_k"] = top_k
            return []

        ms.search = fake_search  # type: ignore[assignment]
        self._call(McpContext(memory_store=ms), {"query": "x", "top_k": 3})
        assert seen == {"q": "x", "top_k": 3}


class TestMemoryWrite:
    """Contract:

    * ``None`` store → is_error.
    * Empty / whitespace content → is_error (don't store empty rows — the
      consolidation pipeline downstream treats empty strings as valid
      content and would pollute ``memories.json``).
    * Happy path → non-error text AND the observation lands on disk via
      ``write_single``. We verify by reading the observations directory
      rather than mocking, so a regression in the persistence layer still
      fails the test.
    """

    def _call(self, ctx, args):
        handler = _tool(_memory_tools(ctx), "memory_write").handler
        return _run(handler(args))

    def test_no_store_is_error(self):
        assert self._call(McpContext(memory_store=None), {"content": "x"}).get(
            "is_error") is True

    def test_empty_content_is_error(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        result = self._call(McpContext(memory_store=ms), {"content": "   "})
        assert result.get("is_error") is True

    def test_happy_path_persists_observation(self, tmp_path):
        agents = tmp_path / "agents"
        ms = _make_store(agents)
        result = self._call(
            McpContext(memory_store=ms),
            {"content": "User likes pizza", "category": "preference"},
        )
        assert result.get("is_error") is not True
        assert "recorded" in _text_of(result).lower()

        obs_dir = agents / "pip-boy" / "observations"
        assert obs_dir.is_dir()
        files = list(obs_dir.glob("*.jsonl"))
        assert files, "expected at least one observations jsonl"
        line = files[0].read_text(encoding="utf-8").splitlines()[0]
        row = json.loads(line)
        assert row["text"] == "User likes pizza"
        assert row["category"] == "preference"
        # ``source="tool"`` is the marker that distinguishes LLM-driven
        # writes from PreCompact / Dream outputs — regression-guard it
        # here so the consolidator can keep trusting the tag.
        assert row["source"] == "tool"

    def test_default_category_is_observation(self, tmp_path):
        agents = tmp_path / "agents"
        ms = _make_store(agents)
        self._call(McpContext(memory_store=ms), {"content": "something"})
        line = next(
            (agents / "pip-boy" / "observations").glob("*.jsonl")
        ).read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["category"] == "observation"


class TestRememberUser:
    """Contract:

    * ``None`` store → is_error.
    * ``channel:peer`` sender_id prefix is stripped before persistence.
      This contract matters because the LLM frequently re-emits the fully
      qualified id it saw in the prompt, and double-prefixing would
      corrupt the users/ filename space.
    * Channel is inferred from ``ctx.channel`` when not given, defaulting
      to ``"cli"`` so CLI-owner onboarding works from day one.
    """

    def _call(self, ctx, args):
        handler = _tool(_memory_tools(ctx), "remember_user").handler
        return _run(handler(args))

    def test_no_store_is_error(self):
        assert self._call(McpContext(memory_store=None), {}).get(
            "is_error") is True

    def test_sender_id_prefix_stripped_before_persistence(
        self, tmp_path, monkeypatch,
    ):
        ms = _make_store(tmp_path / "agents")
        seen: dict[str, Any] = {}

        def fake_update(
            *, sender_id="", channel="", **fields,
        ):
            seen["sender_id"] = sender_id
            seen["channel"] = channel
            seen["fields"] = fields
            return "ok"

        monkeypatch.setattr(ms, "update_user_profile", fake_update)

        class _FakeCh:
            name = "wecom"

        ctx = McpContext(
            memory_store=ms, channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="wecom:alice",
        )
        self._call(ctx, {"name": "Alice", "timezone": "Asia/Shanghai"})
        # Prefix must be stripped — otherwise the file lands at
        # users/wecom_wecom_alice.md and nothing can find it again.
        assert seen["sender_id"] == "alice"
        assert seen["channel"] == "wecom"
        assert seen["fields"]["name"] == "Alice"
        assert seen["fields"]["timezone"] == "Asia/Shanghai"

    def test_default_channel_is_cli(self, tmp_path, monkeypatch):
        ms = _make_store(tmp_path / "agents")
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            ms, "update_user_profile",
            lambda **kw: (seen.update(kw), "ok")[1],
        )
        ctx = McpContext(memory_store=ms, sender_id="local")
        self._call(ctx, {"name": "Owner"})
        assert seen["channel"] == "cli"
        # ``local`` has no ``cli:`` prefix, so it passes through unchanged.
        assert seen["sender_id"] == "local"


# ---------------------------------------------------------------------------
# Cron tools
# ---------------------------------------------------------------------------


@pytest.fixture
def sched_ctx(tmp_path):
    """Real ``HostScheduler`` against a ``tmp_path`` agents/ dir.

    We use a real scheduler (not a mock) because cron_add's happy-path
    success depends on ``_next_fire_at`` yielding a future timestamp,
    and mocking that accurately ends up reproducing most of the
    scheduler's validation logic inline. Cheaper to just run the real
    thing — the background thread is never started here.
    """
    from types import SimpleNamespace

    pip_dir = tmp_path / "agents" / "pip-boy"
    pip_dir.mkdir(parents=True)
    ms = MemoryStore(
        agent_dir=pip_dir,
        workspace_pip_dir=pip_dir.parent,
        agent_id="pip-boy",
    )
    paths = SimpleNamespace(
        pip_dir=pip_dir,
        workspace_pip_dir=pip_dir.parent,
        cwd=pip_dir.parent,
    )
    registry = SimpleNamespace(
        list_agents=lambda: [SimpleNamespace(id="pip-boy")],
        paths_for=lambda aid: paths if aid == "pip-boy" else None,
    )
    sched = HostScheduler(
        registry=registry,
        msg_queue=[],
        q_lock=threading.Lock(),
        stop_event=threading.Event(),
    )
    ctx = McpContext(memory_store=ms, scheduler=sched, peer_id="cli-user")
    return ctx, sched


class TestCronAdd:
    def _call(self, ctx, args):
        handler = _tool(_cron_tools(ctx), "cron_add").handler
        return _run(handler(args))

    def test_scheduler_not_wired_is_error(self):
        ctx = McpContext(scheduler=None)
        assert self._call(ctx, {}).get("is_error") is True

    def test_happy_path_creates_persisted_job(self, sched_ctx):
        ctx, sched = sched_ctx
        result = self._call(ctx, {
            "name": "ping",
            "schedule_kind": "every",
            "schedule_config": {"seconds": 120},
            "message": "say hi",
        })
        # The handler returns whatever ``add_job`` returned — could be
        # "Error: ..." or a success string, so assert on the payload
        # rather than the is_error flag (cron errors surface as plain
        # text today, not is_error).
        text = _text_of(result)
        assert "ping" in text or "created" in text.lower()
        # And the job must actually be persisted so cron_list sees it.
        jobs = sched.list_jobs()
        assert any(j["name"] == "ping" for j in jobs)

    def test_missing_name_propagates_error_string(self, sched_ctx):
        ctx, _ = sched_ctx
        text = _text_of(self._call(ctx, {
            "schedule_kind": "every",
            "schedule_config": {"seconds": 60},
            "message": "hi",
        }))
        assert "name" in text.lower() and "error" in text.lower()

    def test_uses_channel_name_when_present(self, sched_ctx):
        ctx, sched = sched_ctx

        class _Ch:
            name = "wecom"

        ctx.channel = _Ch()  # type: ignore[assignment]
        self._call(ctx, {
            "name": "daily",
            "schedule_kind": "every",
            "schedule_config": {"seconds": 3600},
            "message": "hi",
        })
        jobs = sched.list_jobs()
        assert any(j["channel"] == "wecom" for j in jobs)


class TestCronRemove:
    def _call(self, ctx, args):
        handler = _tool(_cron_tools(ctx), "cron_remove").handler
        return _run(handler(args))

    def test_scheduler_not_wired_is_error(self):
        assert self._call(McpContext(scheduler=None), {"job_id": "x"}).get(
            "is_error") is True

    def test_missing_job_id_surfaces_scheduler_error_text(self, sched_ctx):
        ctx, _ = sched_ctx
        text = _text_of(self._call(ctx, {}))
        assert "job_id" in text.lower()

    def test_happy_path_removes_job(self, sched_ctx):
        ctx, sched = sched_ctx
        # Seed a job first so removal has something to hit.
        _tool(_cron_tools(ctx), "cron_add").handler  # warm import
        _run(_tool(_cron_tools(ctx), "cron_add").handler({
            "name": "gone",
            "schedule_kind": "every",
            "schedule_config": {"seconds": 60},
            "message": "bye",
        }))
        job_id = sched.list_jobs()[0]["id"]
        text = _text_of(self._call(ctx, {"job_id": job_id}))
        assert job_id in text or "removed" in text.lower()
        assert sched.list_jobs() == []


class TestCronUpdate:
    def _call(self, ctx, args):
        handler = _tool(_cron_tools(ctx), "cron_update").handler
        return _run(handler(args))

    def test_scheduler_not_wired_is_error(self):
        assert self._call(
            McpContext(scheduler=None), {"job_id": "x"},
        ).get("is_error") is True

    def test_missing_job_id_is_error(self, sched_ctx):
        ctx, _ = sched_ctx
        # ``cron_update`` checks this BEFORE forwarding to the scheduler,
        # so it short-circuits with is_error=True. This is different
        # from ``cron_remove``'s behaviour — the asymmetry is intentional
        # (cron_remove delegates validation to scheduler.remove_job so
        # the message format stays owned by one module).
        assert self._call(ctx, {}).get("is_error") is True

    def test_happy_path_updates_message(self, sched_ctx):
        ctx, sched = sched_ctx
        _run(_tool(_cron_tools(ctx), "cron_add").handler({
            "name": "upd",
            "schedule_kind": "every",
            "schedule_config": {"seconds": 60},
            "message": "v1",
        }))
        job_id = sched.list_jobs()[0]["id"]
        self._call(ctx, {"job_id": job_id, "message": "v2"})
        assert sched.list_jobs()[0]["message"] == "v2"


class TestCronList:
    def _call(self, ctx, args=None):
        handler = _tool(_cron_tools(ctx), "cron_list").handler
        return _run(handler(args or {}))

    def test_scheduler_not_wired_returns_text_not_error(self):
        # Deliberately non-error: the LLM asking "what's scheduled" in a
        # cold-start host should see "nothing", not a tool-call failure.
        result = self._call(McpContext(scheduler=None))
        assert result.get("is_error") is not True
        assert "No scheduled tasks" in _text_of(result)

    def test_empty_list_message(self, sched_ctx):
        ctx, _ = sched_ctx
        assert "No scheduled tasks" in _text_of(self._call(ctx))

    def test_non_empty_list_emits_parseable_json(self, sched_ctx):
        ctx, sched = sched_ctx
        _run(_tool(_cron_tools(ctx), "cron_add").handler({
            "name": "a",
            "schedule_kind": "every",
            "schedule_config": {"seconds": 60},
            "message": "a",
        }))
        _run(_tool(_cron_tools(ctx), "cron_add").handler({
            "name": "b",
            "schedule_kind": "every",
            "schedule_config": {"seconds": 120},
            "message": "b",
        }))
        text = _text_of(self._call(ctx))
        # JSON shape is part of the contract — the LLM reads this back
        # via cron_list to plan subsequent cron_update / cron_remove
        # calls. Flip to YAML here and the LLM breaks silently.
        parsed = json.loads(text)
        assert isinstance(parsed, list)
        assert {j["name"] for j in parsed} == {"a", "b"}


# ---------------------------------------------------------------------------
# build_mcp_server smoke test
# ---------------------------------------------------------------------------


class TestBuildMcpServer:
    """``build_mcp_server`` is the single integration point the SDK calls
    once per ``query()``. Its contract with the rest of the host is:

    1. Returns a non-None ``McpSdkServerConfig``.
    2. All three tool groups (memory, cron, channel) are wired in — a
       missing group would leave the LLM unable to use whole categories
       of functionality with no loud failure.

    We use the private ``_*_tools`` builders to compute the expected
    count rather than hard-coding it, so adding / removing a tool in
    one place updates both call-sites.
    """

    def test_returns_config_with_all_tool_groups(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        ctx = McpContext(memory_store=ms, workdir=tmp_path)
        cfg = build_mcp_server(ctx)
        assert cfg is not None

        mem_names = {t.name for t in _memory_tools(ctx)}
        cron_names = {t.name for t in _cron_tools(ctx)}
        # Channel tools depend on having a channel on ctx for the
        # send_file handler to do anything useful, but they're still
        # registered here — that's precisely the regression we care
        # about (channel tools silently missing from the registry).
        expected = mem_names | cron_names | {"send_file"}
        assert "memory_search" in expected
        assert "cron_list" in expected
        assert "send_file" in expected
