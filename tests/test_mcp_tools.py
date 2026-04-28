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
    _plugin_tools,
    _web_tools,
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
    the shared ``addressbook/`` lives there alongside any per-test
    scratch state.
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
    * Unverified caller (no ``ctx.user_id``) → creates a new contact with
      a fresh 8-hex ``user_id``; current ``channel:sender_id`` becomes
      the first identifier.
    * Verified caller (``ctx.user_id`` set) → updates ONLY their own
      record. Supplying a different ``user_id`` argument is refused
      with an explanatory error (must not silently succeed, must not
      crash — the model needs the feedback to switch to memory_write).
    * Unverified caller cannot target an existing ``user_id`` either —
      that'd let an anonymous sender hijack someone else's profile.
    * ``channel:peer`` sender_id prefix is stripped before persistence
      so the LLM re-emitting the fully qualified id it saw doesn't
      double-prefix the stored identifier.
    * The addressbook is workspace-shared: writes from any agent land in
      ``<workspace>/.pip/addressbook/<user_id>.md``.
    """

    def _call(self, ctx, args):
        handler = _tool(_memory_tools(ctx), "remember_user").handler
        return _run(handler(args))

    @staticmethod
    def _sub_store(tmp_path: Path) -> tuple[MemoryStore, Path]:
        workspace = tmp_path / "workspace"
        workspace_pip = workspace / ".pip"
        workspace_pip.mkdir(parents=True)
        sub_dir = workspace / "sub" / ".pip"
        sub_dir.mkdir(parents=True)
        ms = MemoryStore(
            agent_dir=sub_dir,
            workspace_pip_dir=workspace_pip,
            agent_id="sub",
        )
        return ms, workspace_pip

    def test_no_store_is_error(self):
        assert self._call(McpContext(memory_store=None), {}).get(
            "is_error") is True

    def test_unverified_caller_creates_contact_and_strips_prefix(
        self, tmp_path,
    ):
        ms = _make_store(tmp_path / "agents")

        class _FakeCh:
            name = "wecom"

        ctx = McpContext(
            memory_store=ms, channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="wecom:alice",  # LLM echoes the qualified id
        )
        result = self._call(
            ctx, {"name": "Alice", "timezone": "Asia/Shanghai"},
        )
        assert result.get("is_error") is not True
        body = _text_of(result)
        assert "user_id=" in body

        ab = ms.addressbook_dir
        files = list(ab.glob("*.md"))
        assert len(files) == 1
        uid = files[0].stem
        assert len(uid) == 8 and all(c in "0123456789abcdef" for c in uid)
        text = files[0].read_text(encoding="utf-8")
        # Prefix was stripped — stored identifier is ``wecom:alice``,
        # not ``wecom:wecom:alice`` which would break future lookups.
        assert "`wecom:alice`" in text
        assert "wecom:wecom:alice" not in text
        assert "Alice" in text
        assert "Asia/Shanghai" in text

    def test_default_channel_is_cli(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        ctx = McpContext(memory_store=ms, sender_id="local")
        self._call(ctx, {"name": "User"})
        files = list(ms.addressbook_dir.glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "`cli:local`" in text

    def test_verified_caller_updates_own_record(self, tmp_path):
        ms = _make_store(tmp_path / "agents")

        class _FakeCh:
            name = "cli"

        # Seed an initial unverified create.
        seed_ctx = McpContext(
            memory_store=ms, channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="cli-user",
        )
        self._call(seed_ctx, {"name": "Alice"})
        uid = next(ms.addressbook_dir.glob("*.md")).stem

        # Now simulate a verified follow-up turn — ctx carries user_id.
        verified = McpContext(
            memory_store=ms, channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="cli-user", user_id=uid,
        )
        result = self._call(verified, {"notes": "prefers terse replies"})
        assert result.get("is_error") is not True
        text = (ms.addressbook_dir / f"{uid}.md").read_text(encoding="utf-8")
        assert "prefers terse replies" in text
        # Still the same single file — no rogue create.
        assert len(list(ms.addressbook_dir.glob("*.md"))) == 1

    def test_verified_caller_cannot_target_other_user_id(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        # Two separate contacts, Alice and Bob.
        uid_a, _ = ms.create_contact(
            sender_id="alice", channel="cli", name="Alice",
        )
        uid_b, _ = ms.create_contact(
            sender_id="bob", channel="cli", name="Bob",
        )

        class _FakeCh:
            name = "cli"

        ctx = McpContext(
            memory_store=ms, channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="alice", user_id=uid_a,
        )
        result = self._call(
            ctx, {"user_id": uid_b, "notes": "sneaky"},
        )
        assert result.get("is_error") is True
        # Bob's record stays untouched.
        bob_text = (ms.addressbook_dir / f"{uid_b}.md").read_text(encoding="utf-8")
        assert "sneaky" not in bob_text

    def test_unverified_cannot_target_existing_user_id(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        uid, _ = ms.create_contact(
            sender_id="alice", channel="cli", name="Alice",
        )

        class _FakeCh:
            name = "wecom"

        # Stranger from a fresh channel tries to claim Alice's record.
        ctx = McpContext(
            memory_store=ms, channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="imposter",  # user_id left empty → unverified
        )
        result = self._call(
            ctx, {"user_id": uid, "notes": "injected"},
        )
        assert result.get("is_error") is True
        text = (ms.addressbook_dir / f"{uid}.md").read_text(encoding="utf-8")
        assert "injected" not in text
        assert "wecom:imposter" not in text

    def test_write_lands_in_workspace_addressbook(self, tmp_path):
        """End-to-end: a ``remember_user`` call from a sub-agent's
        ``MemoryStore`` must persist in the workspace root's
        ``addressbook/``, not under the sub-agent's own ``.pip/``."""
        ms, workspace_pip = self._sub_store(tmp_path)

        class _FakeCh:
            name = "wecom"

        ctx = McpContext(
            memory_store=ms,
            channel=_FakeCh(),  # type: ignore[arg-type]
            sender_id="wecom:alice",
        )
        result = self._call(ctx, {"name": "Alice", "call_me": "Ali"})
        assert result.get("is_error") is not True

        root_ab = workspace_pip / "addressbook"
        assert root_ab.is_dir()
        files = list(root_ab.glob("*.md"))
        assert files, "expected the contact to land in the root addressbook"
        body = files[0].read_text(encoding="utf-8")
        assert "Alice" in body
        # The sub-agent dir stays addressbook-free.
        sub_dir = ms.agent_dir
        assert not (sub_dir / "addressbook").exists()
        assert not (sub_dir / "users").exists()


class TestLookupUser:
    """``lookup_user`` is the lazy-load counterpart to ``remember_user``:
    the addressbook is no longer auto-injected, so the model reads
    profiles on demand by user_id.

    * ``None`` store or missing ``user_id`` → is_error.
    * Unknown id → is_error with the id in the message so the model
      can distinguish "typo" from "storage wired wrong".
    * Known id → returns the raw markdown body (name, identifiers,
      notes intact).
    """

    def _call(self, ctx, args):
        handler = _tool(_memory_tools(ctx), "lookup_user").handler
        return _run(handler(args))

    def test_missing_user_id_is_error(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        result = self._call(McpContext(memory_store=ms), {})
        assert result.get("is_error") is True
        assert "required" in _text_of(result).lower()

    def test_unknown_id_is_error(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        result = self._call(
            McpContext(memory_store=ms), {"user_id": "deadbeef"},
        )
        assert result.get("is_error") is True
        assert "deadbeef" in _text_of(result)

    def test_known_id_returns_profile_body(self, tmp_path):
        ms = _make_store(tmp_path / "agents")
        uid, _ = ms.create_contact(
            sender_id="alice", channel="cli",
            name="Alice", call_me="Ali", notes="likes terse replies",
        )
        result = self._call(
            McpContext(memory_store=ms), {"user_id": uid},
        )
        assert result.get("is_error") is not True
        body = _text_of(result)
        assert "Alice" in body
        assert "Ali" in body
        assert "likes terse replies" in body
        assert "cli:alice" in body


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
        plugin_names = {t.name for t in _plugin_tools(ctx)}
        web_names = {t.name for t in _web_tools(ctx)}
        # Channel tools depend on having a channel on ctx for the
        # send_file handler to do anything useful, but they're still
        # registered here — that's precisely the regression we care
        # about (channel tools silently missing from the registry).
        expected = (
            mem_names | cron_names | plugin_names | web_names | {"send_file"}
        )
        assert "memory_search" in expected
        assert "cron_list" in expected
        assert "send_file" in expected
        assert "plugin_list" in expected
        assert "plugin_install" in expected
        assert "web_fetch" in expected
        assert "web_search" in expected


# ---------------------------------------------------------------------------
# Plugin tools — wrap the bundled Claude Code CLI; tests mock
# ``plugins._run`` so they never spawn the real binary.
# ---------------------------------------------------------------------------


class TestPluginTools:
    """Contract:

    * Every tool's ``input_schema`` is well-formed for the SDK
      (object root, scope enum where applicable).
    * Read tools (``plugin_list``, ``plugin_marketplace_list``,
      ``plugin_search``) emit JSON or a friendly empty-state message.
    * Write tools (``plugin_install``, ``plugin_marketplace_add``)
      validate the ``scope`` argument and propagate ``ctx.workdir``
      to the subprocess for project / local scopes.
    * Destructive operations (``uninstall``, ``disable``,
      ``marketplace_remove``) are deliberately NOT exposed — the agent
      only gets read + additive surface; humans drive removal via
      ``/plugin``.
    """

    @staticmethod
    def _patch_run(monkeypatch, results):
        from pip_agent import plugins as plug

        calls = []
        queue = list(results)

        async def _fake(*argv, cwd=None, timeout=None):
            calls.append({"argv": list(argv), "cwd": cwd})
            if not queue:
                return ("", "", 0)
            return queue.pop(0)

        monkeypatch.setattr(plug, "_run", _fake)
        return calls

    def test_only_additive_ops_are_exposed(self):
        names = {t.name for t in _plugin_tools(McpContext())}
        assert names == {
            "plugin_list",
            "plugin_search",
            "plugin_install",
            "plugin_marketplace_add",
            "plugin_marketplace_list",
        }
        # Belt-and-braces: catch the regression where a future
        # contributor "helpfully" adds uninstall / disable to the
        # MCP surface. Those stay on /plugin host commands.
        assert "plugin_uninstall" not in names
        assert "plugin_disable" not in names
        assert "plugin_marketplace_remove" not in names

    def test_install_schema_advertises_scope_enum(self):
        tool = _tool(_plugin_tools(McpContext()), "plugin_install")
        scope = tool.input_schema["properties"]["scope"]
        assert scope["type"] == "string"
        assert set(scope["enum"]) == {"user", "project", "local"}
        assert "spec" in tool.input_schema["required"]

    def test_marketplace_add_schema_requires_source(self):
        tool = _tool(_plugin_tools(McpContext()), "plugin_marketplace_add")
        assert "source" in tool.input_schema["required"]
        scope = tool.input_schema["properties"]["scope"]
        assert set(scope["enum"]) == {"user", "project", "local"}

    def test_plugin_list_returns_json_text(self, monkeypatch):
        self._patch_run(monkeypatch, [
            (json.dumps([{"name": "x", "scope": "user"}]), "", 0),
        ])
        tool = _tool(_plugin_tools(McpContext()), "plugin_list")
        result = _run(tool.handler({"available": False}))
        assert result.get("is_error") is not True
        parsed = json.loads(_text_of(result))
        assert parsed[0]["name"] == "x"

    def test_plugin_list_passes_available_flag(self, monkeypatch):
        calls = self._patch_run(monkeypatch, [("[]", "", 0)])
        tool = _tool(_plugin_tools(McpContext()), "plugin_list")
        _run(tool.handler({"available": True}))
        assert "--available" in calls[0]["argv"]

    def test_plugin_search_empty_query_is_error(self, monkeypatch):
        self._patch_run(monkeypatch, [])  # nothing should be called
        tool = _tool(_plugin_tools(McpContext()), "plugin_search")
        result = _run(tool.handler({"query": "  "}))
        assert result.get("is_error") is True

    def test_plugin_search_filters_locally(self, monkeypatch):
        self._patch_run(monkeypatch, [
            (json.dumps([
                {"name": "pdf-tools", "description": "Read PDFs"},
                {"name": "browser", "description": "Web fetch"},
            ]), "", 0),
        ])
        tool = _tool(_plugin_tools(McpContext()), "plugin_search")
        result = _run(tool.handler({"query": "pdf"}))
        assert result.get("is_error") is not True
        body = _text_of(result)
        assert "pdf-tools" in body
        assert "browser" not in body

    def test_plugin_search_no_match_returns_friendly_text(self, monkeypatch):
        self._patch_run(monkeypatch, [
            (json.dumps([{"name": "browser", "description": "Web fetch"}]), "", 0),
        ])
        tool = _tool(_plugin_tools(McpContext()), "plugin_search")
        result = _run(tool.handler({"query": "nope"}))
        assert result.get("is_error") is not True
        assert "no plugins matched" in _text_of(result).lower()

    def test_plugin_install_default_scope_user(self, monkeypatch, tmp_path):
        calls = self._patch_run(monkeypatch, [("ok\n", "", 0)])
        tool = _tool(
            _plugin_tools(McpContext(workdir=tmp_path)), "plugin_install",
        )
        result = _run(tool.handler({"spec": "web-search"}))
        assert result.get("is_error") is not True
        assert calls[0]["argv"] == [
            "plugin", "install", "web-search", "-s", "user",
        ]

    def test_plugin_install_project_scope_uses_workdir(
        self, monkeypatch, tmp_path,
    ):
        calls = self._patch_run(monkeypatch, [("ok\n", "", 0)])
        tool = _tool(
            _plugin_tools(McpContext(workdir=tmp_path)), "plugin_install",
        )
        _run(tool.handler({"spec": "foo", "scope": "project"}))
        assert calls[0]["argv"][-2:] == ["-s", "project"]
        # cwd MUST be the agent's workdir for project / local scopes
        # — otherwise CC writes to ``.claude/`` in the host's cwd and
        # per-agent isolation breaks.
        assert calls[0]["cwd"] == tmp_path

    def test_plugin_install_invalid_scope_is_error(self, monkeypatch):
        self._patch_run(monkeypatch, [])  # nothing should be called
        tool = _tool(_plugin_tools(McpContext()), "plugin_install")
        result = _run(tool.handler({"spec": "x", "scope": "global"}))
        assert result.get("is_error") is True
        assert "invalid scope" in _text_of(result).lower()

    def test_marketplace_list_empty_state_is_friendly(self, monkeypatch):
        self._patch_run(monkeypatch, [("\n", "", 0)])
        tool = _tool(_plugin_tools(McpContext()), "plugin_marketplace_list")
        result = _run(tool.handler({}))
        assert result.get("is_error") is not True
        assert "no marketplaces configured" in _text_of(result).lower()

    def test_marketplace_add_propagates_source_and_scope(
        self, monkeypatch, tmp_path,
    ):
        calls = self._patch_run(monkeypatch, [("added\n", "", 0)])
        tool = _tool(
            _plugin_tools(McpContext(workdir=tmp_path)),
            "plugin_marketplace_add",
        )
        result = _run(tool.handler(
            {"source": "anthropics/claude-code", "scope": "local"},
        ))
        assert result.get("is_error") is not True
        argv = calls[0]["argv"]
        assert argv == [
            "plugin", "marketplace", "add",
            "anthropics/claude-code", "--scope", "local",
        ]
        assert calls[0]["cwd"] == tmp_path

    def test_subprocess_error_becomes_is_error(self, monkeypatch):
        self._patch_run(monkeypatch, [
            ("", "marketplace not registered", 1),
        ])
        tool = _tool(_plugin_tools(McpContext()), "plugin_install")
        result = _run(tool.handler({"spec": "x"}))
        assert result.get("is_error") is True
        assert "marketplace not registered" in _text_of(result)


# ---------------------------------------------------------------------------
# Web tools — thin MCP wrapper around ``pip_agent.web.fetch_url``.
# ``test_web.py`` covers the fetcher itself; here we only check the
# argument validation, the success / failure formatting, and the
# tool's input schema (which is the SDK's contract with the model).
# ---------------------------------------------------------------------------


class TestWebTools:
    def _patch_fetch(self, monkeypatch, result: dict) -> dict:
        captured: dict = {}

        async def fake_fetch(url: str, *, max_chars: int = 50_000, **_kw):
            captured["url"] = url
            captured["max_chars"] = max_chars
            return result

        monkeypatch.setattr("pip_agent.web.fetch_url", fake_fetch)
        return captured

    def test_missing_url_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({}))
        assert result.get("is_error") is True
        assert "url" in _text_of(result).lower()

    def test_blank_url_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({"url": "   "}))
        assert result.get("is_error") is True

    def test_non_integer_max_chars_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({
            "url": "https://example.com/", "max_chars": "lots",
        }))
        assert result.get("is_error") is True
        assert "integer" in _text_of(result)

    def test_non_positive_max_chars_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({
            "url": "https://example.com/", "max_chars": 0,
        }))
        assert result.get("is_error") is True

    def test_success_response_includes_header_and_body(self, monkeypatch):
        captured = self._patch_fetch(monkeypatch, {
            "ok": True,
            "url": "https://example.com/page",
            "status": 200,
            "content_type": "text/html",
            "content": "## Hello\n\nWorld",
            "truncated": False,
        })
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({"url": "https://example.com/page"}))

        assert result.get("is_error") is not True
        body = _text_of(result)
        # Header surfaces the resolved URL / status / content-type so
        # the model can reason about redirects and JSON-vs-HTML.
        assert "URL: https://example.com/page" in body
        assert "Status: 200" in body
        assert "Content-Type: text/html" in body
        assert "## Hello" in body
        assert "World" in body
        # Default ``max_chars`` propagates through to the fetcher.
        assert captured["max_chars"] == 50_000

    def test_truncation_marker_is_surfaced(self, monkeypatch):
        self._patch_fetch(monkeypatch, {
            "ok": True,
            "url": "https://example.com/long",
            "status": 200,
            "content_type": "text/plain",
            "content": "x" * 50,
            "truncated": True,
        })
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({
            "url": "https://example.com/long", "max_chars": 50,
        }))
        body = _text_of(result)
        assert "truncated to 50 chars" in body

    def test_failure_dict_becomes_is_error_with_status_hint(
        self, monkeypatch,
    ):
        self._patch_fetch(monkeypatch, {
            "ok": False,
            "error": "HTTP 503",
            "url": "https://example.com/down",
            "status": 503,
        })
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        result = _run(tool.handler({"url": "https://example.com/down"}))

        assert result.get("is_error") is True
        body = _text_of(result)
        assert "web_fetch failed" in body
        assert "status=503" in body
        assert "HTTP 503" in body

    def test_max_chars_is_forwarded(self, monkeypatch):
        captured = self._patch_fetch(monkeypatch, {
            "ok": True,
            "url": "u", "status": 200, "content_type": "text/plain",
            "content": "ok", "truncated": False,
        })
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        _run(tool.handler({
            "url": "https://example.com/", "max_chars": 1000,
        }))
        assert captured["max_chars"] == 1000

    def test_input_schema_is_well_formed(self):
        tool = _tool(_web_tools(McpContext()), "web_fetch")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "url" in schema["properties"]
        assert schema["properties"]["url"]["type"] == "string"
        assert schema["required"] == ["url"]
        assert schema["properties"]["max_chars"]["type"] == "integer"


class TestWebSearchTool:
    """MCP wrapper around ``pip_agent.web.search_web``.

    The provider-level fallback logic (Tavily → DDG) is covered in
    ``test_web.py``; here we pin argument validation, the rendered
    output shape the model receives, and the input schema.
    """

    def _patch_search(self, monkeypatch, result: dict) -> dict:
        captured: dict = {}

        async def fake(query: str, *, max_results: int = 5, **_kw):
            captured["query"] = query
            captured["max_results"] = max_results
            return result

        monkeypatch.setattr("pip_agent.web.search_web", fake)
        return captured

    def test_missing_query_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_search")
        result = _run(tool.handler({}))
        assert result.get("is_error") is True
        assert "query" in _text_of(result).lower()

    def test_blank_query_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_search")
        result = _run(tool.handler({"query": "  "}))
        assert result.get("is_error") is True

    def test_non_integer_max_results_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_search")
        result = _run(tool.handler({
            "query": "q", "max_results": "lots",
        }))
        assert result.get("is_error") is True

    def test_non_positive_max_results_is_error(self):
        tool = _tool(_web_tools(McpContext()), "web_search")
        result = _run(tool.handler({
            "query": "q", "max_results": 0,
        }))
        assert result.get("is_error") is True

    def test_success_render_contains_header_and_numbered_hits(
        self, monkeypatch,
    ):
        self._patch_search(monkeypatch, {
            "ok": True,
            "provider": "tavily",
            "query": "NVDA stock",
            "results": [
                {
                    "title": "NVIDIA Corp",
                    "url": "https://example.com/nvda",
                    "snippet": "NVDA is up.",
                },
                {
                    "title": "NVDA News",
                    "url": "https://news.example/nvda",
                    "snippet": "Latest headlines.",
                },
            ],
        })
        tool = _tool(_web_tools(McpContext()), "web_search")
        result = _run(tool.handler({"query": "NVDA stock"}))

        assert result.get("is_error") is not True
        body = _text_of(result)
        assert "Provider: tavily" in body
        assert "Query: NVDA stock" in body
        assert "1. NVIDIA Corp" in body
        assert "https://example.com/nvda" in body
        assert "NVDA is up." in body
        assert "2. NVDA News" in body

    def test_empty_results_renders_placeholder(self, monkeypatch):
        self._patch_search(monkeypatch, {
            "ok": True,
            "provider": "duckduckgo",
            "query": "q",
            "results": [],
        })
        tool = _tool(_web_tools(McpContext()), "web_search")
        body = _text_of(_run(tool.handler({"query": "q"})))
        assert "(no results)" in body
        assert "Provider: duckduckgo" in body

    def test_max_results_is_forwarded(self, monkeypatch):
        captured = self._patch_search(monkeypatch, {
            "ok": True, "provider": "tavily", "query": "q", "results": [],
        })
        tool = _tool(_web_tools(McpContext()), "web_search")
        _run(tool.handler({"query": "q", "max_results": 10}))
        assert captured["max_results"] == 10

    def test_default_max_results_is_5(self, monkeypatch):
        captured = self._patch_search(monkeypatch, {
            "ok": True, "provider": "tavily", "query": "q", "results": [],
        })
        tool = _tool(_web_tools(McpContext()), "web_search")
        _run(tool.handler({"query": "q"}))
        assert captured["max_results"] == 5

    def test_failure_becomes_is_error_with_reason(self, monkeypatch):
        self._patch_search(monkeypatch, {
            "ok": False,
            "error": "tavily: HTTP 429; duckduckgo: rate limited",
            "provider": "duckduckgo",
        })
        tool = _tool(_web_tools(McpContext()), "web_search")
        result = _run(tool.handler({"query": "q"}))
        assert result.get("is_error") is True
        body = _text_of(result)
        assert "web_search failed" in body
        assert "tavily" in body and "duckduckgo" in body

    def test_input_schema_is_well_formed(self):
        tool = _tool(_web_tools(McpContext()), "web_search")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["required"] == ["query"]
        assert schema["properties"]["max_results"]["type"] == "integer"
