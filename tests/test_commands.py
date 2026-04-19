"""Tests for the unified slash-command dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from pip_agent.channels import InboundMessage
from pip_agent.commands import CommandContext, dispatch_command
from pip_agent.memory import MemoryStore
from pip_agent.routing import (
    AgentRegistry,
    BindingTable,
)


@pytest.fixture
def agents_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    pb = d / "pip-boy"
    pb.mkdir()
    (pb / "persona.md").write_text(
        "---\nname: Pip-Boy\nmodel: claude-sonnet-4-6\ndm_scope: per-guild\n---\nBody.\n",
        encoding="utf-8",
    )
    pm = d / "pm-bot"
    pm.mkdir()
    (pm / "persona.md").write_text(
        "---\nname: PM Bot\nmodel: gpt-4\ndm_scope: per-guild\n---\nPM stuff.\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def registry(agents_dir):
    return AgentRegistry(agents_dir)


@pytest.fixture
def bindings_path(tmp_path):
    return tmp_path / "bindings.json"


def _make_ctx(
    text: str,
    registry: AgentRegistry,
    bindings_path: Path,
    *,
    channel: str = "cli",
    peer_id: str = "u1",
    guild_id: str = "",
    is_group: bool = False,
    memory_store: MemoryStore | None = None,
) -> CommandContext:
    bt = BindingTable()
    bt.load(bindings_path)
    return CommandContext(
        inbound=InboundMessage(
            text=text,
            sender_id=peer_id,
            channel=channel,
            peer_id=peer_id,
            guild_id=guild_id,
            is_group=is_group,
        ),
        registry=registry,
        bindings=bt,
        bindings_path=bindings_path,
        workdir="/tmp/test",
        memory_store=memory_store,
    )


# ---------------------------------------------------------------------------
# dispatch_command basics
# ---------------------------------------------------------------------------

class TestDispatchCommand:
    def test_non_command(self, registry, bindings_path):
        ctx = _make_ctx("hello world", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is False

    def test_unknown_command(self, registry, bindings_path):
        ctx = _make_ctx("/unknown", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is False

    def test_at_mention_stripped(self, registry, bindings_path):
        ctx = _make_ctx("@Pip-Boy /status", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "pip-boy" in result.response.lower()

    def test_at_mention_bare_at_stripped(self, registry, bindings_path):
        """WeCom SDK sometimes strips the name, leaving just '@ /cmd'."""
        ctx = _make_ctx("@ /status", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "pip-boy" in result.response.lower()

    def test_double_at_mention_stripped(self, registry, bindings_path):
        """Double @-mention: '@ @Pip-Boy /cmd' should also be parsed."""
        ctx = _make_ctx("@ @Pip-Boy /status", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "pip-boy" in result.response.lower()

    def test_at_mention_non_command(self, registry, bindings_path):
        ctx = _make_ctx("@Pip-Boy hello", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is False


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

class TestHelp:
    def test_help(self, registry, bindings_path):
        ctx = _make_ctx("/help", registry, bindings_path, channel="cli")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "/bind" in result.response
        assert "/name" in result.response
        assert "/unbind" in result.response
        assert "/clean" in result.response
        assert "/reset" in result.response
        assert "/admin" in result.response
        assert "/status" in result.response
        assert "/exit" in result.response


# ---------------------------------------------------------------------------
# /bind
# ---------------------------------------------------------------------------

class TestBind:
    def test_no_args(self, registry, bindings_path):
        ctx = _make_ctx("/bind", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "usage" in result.response.lower()

    def test_bind_guild(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind pm-bot", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "PM Bot" in result.response

        bt = BindingTable()
        bt.load(bindings_path)
        aid, _ = bt.resolve(guild_id="g1")
        assert aid == "pm-bot"

    def test_bind_peer(self, registry, bindings_path):
        ctx = _make_ctx("/bind pip-boy", registry, bindings_path, peer_id="u2")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "Pip-Boy" in result.response

        bt = BindingTable()
        bt.load(bindings_path)
        aid, _ = bt.resolve(peer_id="u2")
        assert aid == "pip-boy"

    def test_auto_create_agent(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind new-bot", registry, bindings_path,
            guild_id="g3", is_group=True,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "Created new agent" in result.response
        assert "new-bot" in result.response

        assert registry.get_agent("new-bot") is not None
        md_path = registry.agents_dir / "new-bot" / "persona.md"
        assert md_path.exists()

        bt = BindingTable()
        bt.load(bindings_path)
        aid, _ = bt.resolve(guild_id="g3")
        assert aid == "new-bot"

    def test_with_overrides(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind pm-bot --model gpt-4o --scope main --max-tokens 2048",
            registry, bindings_path,
            guild_id="g2", is_group=True,
        )
        result = dispatch_command(ctx)
        assert result.handled is True

        bt = BindingTable()
        bt.load(bindings_path)
        aid, binding = bt.resolve(guild_id="g2")
        assert aid == "pm-bot"
        assert binding.overrides["model"] == "gpt-4o"
        assert binding.overrides["scope"] == "main"
        assert binding.overrides["max_tokens"] == "2048"

    def test_unknown_flag(self, registry, bindings_path):
        ctx = _make_ctx("/bind pm-bot --bogus", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "Unknown" in result.response

    def test_replaces_existing_binding(self, registry, bindings_path):
        ctx1 = _make_ctx(
            "/bind pip-boy", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        dispatch_command(ctx1)

        ctx2 = _make_ctx(
            "/bind pm-bot", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        ctx2.bindings.load(bindings_path)
        dispatch_command(ctx2)

        bt = BindingTable()
        bt.load(bindings_path)
        aid, _ = bt.resolve(guild_id="g1")
        assert aid == "pm-bot"


# ---------------------------------------------------------------------------
# /name
# ---------------------------------------------------------------------------

class TestName:
    def test_name_no_args(self, registry, bindings_path):
        ctx = _make_ctx("/name", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "usage" in result.response.lower()

    def test_name_default_agent(self, registry, bindings_path):
        ctx = _make_ctx("/name NewPip", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "NewPip" in result.response
        agent = registry.get_agent("pip-boy")
        assert agent.name == "NewPip"

    def test_name_bound_agent(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind pm-bot", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        dispatch_command(ctx)

        ctx2 = _make_ctx(
            "/name 产品助手", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        ctx2.bindings.load(bindings_path)
        result = dispatch_command(ctx2)
        assert result.handled is True
        assert "产品助手" in result.response
        assert registry.get_agent("pm-bot").name == "产品助手"

    def test_name_persists_to_file(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind pm-bot", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        dispatch_command(ctx)

        ctx2 = _make_ctx(
            "/name Alice", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        ctx2.bindings.load(bindings_path)
        dispatch_command(ctx2)

        md_path = registry.agents_dir / "pm-bot" / "persona.md"
        content = md_path.read_text(encoding="utf-8")
        assert "name: Alice" in content


# ---------------------------------------------------------------------------
# /unbind
# ---------------------------------------------------------------------------

class TestUnbind:
    def test_unbind_removes_binding(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind pm-bot", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        dispatch_command(ctx)

        ctx2 = _make_ctx(
            "/unbind", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        ctx2.bindings.load(bindings_path)
        result = dispatch_command(ctx2)
        assert result.handled is True
        assert "removed" in result.response.lower()

    def test_unbind_no_binding(self, registry, bindings_path):
        ctx = _make_ctx("/unbind", registry, bindings_path)
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "no binding" in result.response.lower()


# ---------------------------------------------------------------------------
# /clean
# ---------------------------------------------------------------------------

class TestClean:
    def test_clean_deletes_agent(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind new-bot", registry, bindings_path,
            guild_id="g1", is_group=True, channel="cli",
        )
        dispatch_command(ctx)
        assert registry.get_agent("new-bot") is not None

        ctx2 = _make_ctx(
            "/clean", registry, bindings_path,
            guild_id="g1", is_group=True, channel="cli",
        )
        ctx2.bindings.load(bindings_path)
        result = dispatch_command(ctx2)
        assert result.handled is True
        assert "deleted" in result.response.lower()
        assert registry.get_agent("new-bot") is None

        agent_dir = registry.agents_dir / "new-bot"
        assert not agent_dir.exists()

    def test_clean_default_not_deleted(self, registry, bindings_path):
        ctx = _make_ctx("/clean", registry, bindings_path, channel="cli")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "default" in result.response.lower()
        assert registry.get_agent("pip-boy") is not None

    def test_clean_no_binding(self, registry, bindings_path):
        ctx = _make_ctx("/clean", registry, bindings_path, peer_id="nobody", channel="cli")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "no binding" in result.response.lower()


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_default_status(self, registry, bindings_path):
        ctx = _make_ctx("/status", registry, bindings_path, channel="cli", peer_id="cli-user")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "pip-boy" in result.response.lower()

    def test_status_with_binding(self, registry, bindings_path):
        ctx = _make_ctx(
            "/bind pm-bot", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        dispatch_command(ctx)

        ctx2 = _make_ctx(
            "/status", registry, bindings_path,
            guild_id="g1", is_group=True,
        )
        ctx2.bindings.load(bindings_path)
        result = dispatch_command(ctx2)
        assert result.handled is True
        assert "pm-bot" in result.response.lower()


# ---------------------------------------------------------------------------
# /exit
# ---------------------------------------------------------------------------

class TestExit:
    def test_exit_cli(self, registry, bindings_path):
        ctx = _make_ctx("/exit", registry, bindings_path, channel="cli")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert result.exit_requested is True

    def test_exit_non_cli(self, registry, bindings_path, tmp_path):
        ms = MemoryStore(tmp_path / "agents", "pip-boy")
        users_dir = ms.agent_dir / "users"
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / "admin-user.md").write_text(
            "# Admin\n\n- **Name:** admin-user\n- **Admin:** yes\n"
            "- **Identifiers:**\n  - `wecom:u1`\n",
            encoding="utf-8",
        )
        ctx = _make_ctx(
            "/exit", registry, bindings_path,
            channel="wecom", memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert result.exit_requested is False
        assert "CLI" in result.response


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_clears_memory_files(self, registry, bindings_path):
        agents_dir = registry.agents_dir
        agent_dir = agents_dir / "pip-boy"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "observations").mkdir(exist_ok=True)
        (agent_dir / "observations" / "2025-01-01.jsonl").write_text(
            '{"ts":1,"text":"test"}\n', encoding="utf-8",
        )
        (agent_dir / "memories.json").write_text('[{"text":"mem"}]', encoding="utf-8")
        (agent_dir / "axioms.md").write_text("Be kind.\n", encoding="utf-8")
        (agent_dir / "state.json").write_text('{"last_reflect_at":1}', encoding="utf-8")
        persona = agent_dir / "persona.md"

        ctx = _make_ctx("/reset", registry, bindings_path, channel="cli")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "factory-reset" in result.response.lower()

        assert not (agent_dir / "observations" / "2025-01-01.jsonl").exists()
        assert not (agent_dir / "memories.json").exists()
        assert not (agent_dir / "axioms.md").exists()
        assert not (agent_dir / "state.json").exists()
        assert persona.exists()


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------

class TestACL:
    def test_cli_always_owner(self, registry, bindings_path):
        ctx = _make_ctx("/status", registry, bindings_path, channel="cli")
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "permission denied" not in result.response.lower()

    def test_non_admin_blocked(self, registry, bindings_path, tmp_path):
        ms = MemoryStore(tmp_path / "agents", "pip-boy")
        ctx = _make_ctx(
            "/reset", registry, bindings_path,
            channel="wecom", memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "permission denied" in result.response.lower()

    def test_admin_can_execute(self, registry, bindings_path, tmp_path):
        ms = MemoryStore(tmp_path / "agents", "pip-boy")
        users_dir = ms.agent_dir / "users"
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / "alice.md").write_text(
            "# Alice\n\n- **Name:** Alice\n- **Admin:** yes\n"
            "- **Identifiers:**\n  - `wecom:u1`\n",
            encoding="utf-8",
        )
        ctx = _make_ctx(
            "/status", registry, bindings_path,
            channel="wecom", memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "permission denied" not in result.response.lower()

    def test_admin_cannot_use_admin_command(self, registry, bindings_path, tmp_path):
        ms = MemoryStore(tmp_path / "agents", "pip-boy")
        users_dir = ms.agent_dir / "users"
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / "alice.md").write_text(
            "# Alice\n\n- **Name:** Alice\n- **Admin:** yes\n"
            "- **Identifiers:**\n  - `wecom:u1`\n",
            encoding="utf-8",
        )
        ctx = _make_ctx(
            "/admin list", registry, bindings_path,
            channel="wecom", memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "owner only" in result.response.lower()

    def test_owner_can_use_admin_command(self, registry, bindings_path, tmp_path):
        ms = MemoryStore(tmp_path / "agents", "pip-boy")
        ctx = _make_ctx(
            "/admin list", registry, bindings_path,
            channel="cli", memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert result.handled is True
        assert "no admin" in result.response.lower()

    def test_admin_grant_and_revoke(self, registry, bindings_path, tmp_path):
        ms = MemoryStore(tmp_path / "agents", "pip-boy")
        users_dir = ms.agent_dir / "users"
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / "bob.md").write_text(
            "# Bob\n\n- **Name:** Bob\n- **Identifiers:**\n  - `wecom:bob1`\n",
            encoding="utf-8",
        )

        ctx = _make_ctx(
            "/admin grant Bob", registry, bindings_path,
            channel="cli", memory_store=ms,
        )
        result = dispatch_command(ctx)
        assert "granted" in result.response.lower()

        assert ms.is_admin("wecom", "bob1") is True

        ctx2 = _make_ctx(
            "/admin revoke Bob", registry, bindings_path,
            channel="cli", memory_store=ms,
        )
        result2 = dispatch_command(ctx2)
        assert "revoked" in result2.response.lower()

        assert ms.is_admin("wecom", "bob1") is False

    def test_owner_identified_by_owner_md(self, tmp_path):
        pip_dir = tmp_path / ".pip"
        pip_dir.mkdir()
        (pip_dir / "owner.md").write_text(
            "# Owner\n\n- **Identifiers:**\n  - `wechat:owner123`\n",
            encoding="utf-8",
        )
        agents_dir = pip_dir / "agents"
        agents_dir.mkdir()
        ms = MemoryStore(agents_dir, "pip-boy")
        assert ms.is_owner("wechat", "owner123") is True
        assert ms.is_owner("wechat", "stranger") is False

    def test_llm_cannot_set_admin(self, tmp_path):
        """update_user_profile preserves admin flag but LLM can't set it."""
        agents_dir = tmp_path / "agents"
        ms = MemoryStore(agents_dir, "pip-boy")
        users_dir = ms.agent_dir / "users"
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / "eve.md").write_text(
            "# Eve\n\n_Profile managed by Pip._\n\n"
            "- **Name:** Eve\n- **What to call them:** \n"
            "- **Timezone:** \n- **Notes:** \n"
            "- **Admin:** yes\n"
            "- **Identifiers:**\n  - `wecom:eve1`\n",
            encoding="utf-8",
        )

        ms.update_user_profile(
            sender_id="eve1", channel="wecom", notes="likes cats",
        )

        content = (users_dir / "eve.md").read_text(encoding="utf-8")
        assert "- **Admin:** yes" in content
        assert "likes cats" in content
