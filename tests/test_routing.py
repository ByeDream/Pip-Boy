"""Tests for the multi-agent routing layer."""

from __future__ import annotations

from pip_agent.routing import (
    DEFAULT_AGENT_ID,
    DEFAULT_COMPACT_MICRO_AGE,
    DEFAULT_COMPACT_THRESHOLD,
    DEFAULT_DM_SCOPE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AgentConfig,
    AgentRegistry,
    Binding,
    BindingTable,
    agent_config_from_file,
    build_session_key,
    normalize_agent_id,
    resolve_effective_config,
)

# ---------------------------------------------------------------------------
# normalize_agent_id
# ---------------------------------------------------------------------------

class TestNormalizeAgentId:
    def test_simple(self):
        assert normalize_agent_id("Pip-Boy") == "pip-boy"

    def test_empty(self):
        assert normalize_agent_id("") == DEFAULT_AGENT_ID

    def test_whitespace(self):
        assert normalize_agent_id("  ") == DEFAULT_AGENT_ID

    def test_special_chars(self):
        assert normalize_agent_id("My Bot!") == "my-bot"

    def test_already_valid(self):
        assert normalize_agent_id("pm-bot") == "pm-bot"

    def test_long_id(self):
        result = normalize_agent_id("a" * 100)
        assert len(result) <= 64


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------

class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig(id="test")
        assert cfg.effective_model == DEFAULT_MODEL
        assert cfg.effective_max_tokens == DEFAULT_MAX_TOKENS
        assert cfg.effective_dm_scope == DEFAULT_DM_SCOPE
        assert cfg.effective_compact_threshold == DEFAULT_COMPACT_THRESHOLD
        assert cfg.effective_compact_micro_age == DEFAULT_COMPACT_MICRO_AGE

    def test_overridden(self):
        cfg = AgentConfig(
            id="custom",
            model="gpt-4o",
            max_tokens=4096,
            dm_scope="main",
            compact_threshold=20000,
            compact_micro_age=5,
        )
        assert cfg.effective_model == "gpt-4o"
        assert cfg.effective_max_tokens == 4096
        assert cfg.effective_dm_scope == "main"
        assert cfg.effective_compact_threshold == 20000
        assert cfg.effective_compact_micro_age == 5

    def test_system_prompt(self):
        cfg = AgentConfig(id="bot", name="TestBot", system_body="Working at {workdir}.")
        prompt = cfg.system_prompt(workdir="/tmp/test")
        assert "/tmp/test" in prompt

    def test_system_prompt_empty_body(self):
        cfg = AgentConfig(id="my-agent")
        prompt = cfg.system_prompt()
        assert prompt == ""


# ---------------------------------------------------------------------------
# agent_config_from_file
# ---------------------------------------------------------------------------

class TestAgentConfigFromFile:
    def test_load(self, tmp_path):
        md = tmp_path / "test-bot.md"
        md.write_text(
            "---\n"
            "name: TestBot\n"
            "model: gpt-4\n"
            "max_tokens: 2048\n"
            "dm_scope: main\n"
            "compact_threshold: 10000\n"
            "compact_micro_age: 2\n"
            "---\n"
            "Be concise.\n",
            encoding="utf-8",
        )
        cfg = agent_config_from_file(md)
        assert cfg.id == "test-bot"
        assert cfg.name == "TestBot"
        assert cfg.model == "gpt-4"
        assert cfg.max_tokens == 2048
        assert cfg.dm_scope == "main"
        assert cfg.compact_threshold == 10000
        assert cfg.compact_micro_age == 2
        assert cfg.system_body == "Be concise."

    def test_no_frontmatter(self, tmp_path):
        md = tmp_path / "plain.md"
        md.write_text("Just a plain body.", encoding="utf-8")
        cfg = agent_config_from_file(md)
        assert cfg.id == "plain"
        assert cfg.system_body == "Just a plain body."


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------

class TestBinding:
    def test_display(self):
        b = Binding(agent_id="bot", tier=2, match_key="guild_id", match_value="g1")
        assert "guild" in b.display()
        assert "bot" in b.display()

    def test_round_trip(self):
        b = Binding(
            agent_id="bot", tier=1, match_key="peer_id",
            match_value="wecom:u1", priority=5,
            overrides={"model": "gpt-4"},
        )
        d = b.to_dict()
        b2 = Binding.from_dict(d)
        assert b2.agent_id == "bot"
        assert b2.tier == 1
        assert b2.match_value == "wecom:u1"
        assert b2.priority == 5
        assert b2.overrides == {"model": "gpt-4"}


# ---------------------------------------------------------------------------
# BindingTable
# ---------------------------------------------------------------------------

class TestBindingTable:
    def test_resolve_peer(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="peer-bot", tier=1, match_key="peer_id", match_value="u1"))
        bt.add(Binding(agent_id="default", tier=5, match_key="default", match_value="*"))

        aid, binding = bt.resolve(peer_id="u1")
        assert aid == "peer-bot"

    def test_resolve_guild(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="guild-bot", tier=2, match_key="guild_id", match_value="g1"))
        bt.add(Binding(agent_id="default", tier=5, match_key="default", match_value="*"))

        aid, _ = bt.resolve(guild_id="g1")
        assert aid == "guild-bot"

    def test_resolve_channel(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="ch-bot", tier=4, match_key="channel", match_value="wecom"))

        aid, _ = bt.resolve(channel="wecom")
        assert aid == "ch-bot"

    def test_resolve_default(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="fallback", tier=5, match_key="default", match_value="*"))

        aid, _ = bt.resolve(channel="cli", peer_id="x")
        assert aid == "fallback"

    def test_resolve_empty(self):
        bt = BindingTable()
        aid, binding = bt.resolve(channel="cli", peer_id="x")
        assert aid is None
        assert binding is None

    def test_tier_priority(self):
        """T1 (peer) should beat T2 (guild) which should beat T5 (default)."""
        bt = BindingTable()
        bt.add(Binding(agent_id="default", tier=5, match_key="default", match_value="*"))
        bt.add(Binding(agent_id="guild-bot", tier=2, match_key="guild_id", match_value="g1"))
        bt.add(Binding(agent_id="peer-bot", tier=1, match_key="peer_id", match_value="u1"))

        aid, _ = bt.resolve(guild_id="g1", peer_id="u1")
        assert aid == "peer-bot"

    def test_resolve_guild_with_channel_prefix(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="bot", tier=2, match_key="guild_id", match_value="wecom:g1"))
        aid, _ = bt.resolve(channel="wecom", guild_id="g1")
        assert aid == "bot"

    def test_resolve_account(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="acc-bot", tier=3, match_key="account_id", match_value="bot123"))
        aid, _ = bt.resolve(account_id="bot123")
        assert aid == "acc-bot"

    def test_remove(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="bot", tier=2, match_key="guild_id", match_value="g1"))
        assert bt.remove("guild_id", "g1") is True
        assert bt.remove("guild_id", "g1") is False
        aid, _ = bt.resolve(guild_id="g1")
        assert aid is None

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "bindings.json"
        bt = BindingTable()
        bt.add(Binding(agent_id="bot", tier=2, match_key="guild_id", match_value="g1"))
        bt.add(Binding(agent_id="default", tier=5, match_key="default", match_value="*"))
        bt.save(path)

        bt2 = BindingTable()
        bt2.load(path)
        assert len(bt2.list_all()) == 2
        aid, _ = bt2.resolve(guild_id="g1")
        assert aid == "bot"

    def test_load_missing_file(self, tmp_path):
        bt = BindingTable()
        bt.load(tmp_path / "nonexistent.json")
        assert bt.list_all() == []


# ---------------------------------------------------------------------------
# build_session_key
# ---------------------------------------------------------------------------

class TestBuildSessionKey:
    def test_per_guild_group(self):
        sk = build_session_key("bot", "wecom", "u1", guild_id="g1", is_group=True)
        assert sk == "agent:bot:wecom:guild:g1"

    def test_per_guild_dm(self):
        sk = build_session_key("bot", "wecom", "u1")
        assert sk == "agent:bot:wecom:peer:u1"

    def test_per_guild_peer_group(self):
        sk = build_session_key(
            "bot", "wecom", "u1",
            guild_id="g1", is_group=True, dm_scope="per-guild-peer",
        )
        assert sk == "agent:bot:wecom:guild:g1:peer:u1"

    def test_main_scope(self):
        sk = build_session_key("bot", "wecom", "u1", guild_id="g1", is_group=True, dm_scope="main")
        assert sk == "agent:bot:wecom:main"

    def test_cli_fallback(self):
        sk = build_session_key("pip-boy", "cli", "cli-user")
        assert sk == "agent:pip-boy:cli:peer:cli-user"

    def test_normalizes_agent_id(self):
        sk = build_session_key("Pip-Boy", "cli", "cli-user")
        assert sk.startswith("agent:pip-boy:")


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------

class TestAgentRegistry:
    def test_default_when_empty(self):
        reg = AgentRegistry()
        assert reg.default_agent().id == DEFAULT_AGENT_ID
        assert len(reg.list_agents()) == 1

    def test_load_from_dir(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        bot_dir = agents_dir / "test-bot"
        bot_dir.mkdir()
        (bot_dir / "persona.md").write_text(
            "---\nname: TestBot\nmodel: gpt-4\n---\nBody.\n",
            encoding="utf-8",
        )
        reg = AgentRegistry(agents_dir)
        assert reg.get_agent("test-bot") is not None
        assert reg.get_agent("test-bot").name == "TestBot"

    def test_missing_dir(self, tmp_path):
        reg = AgentRegistry(tmp_path / "nonexistent")
        assert reg.default_agent().id == DEFAULT_AGENT_ID

    def test_get_agent_normalizes(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        bot_dir = agents_dir / "my-bot"
        bot_dir.mkdir()
        (bot_dir / "persona.md").write_text(
            "---\nname: MyBot\n---\nHello.\n",
            encoding="utf-8",
        )
        reg = AgentRegistry(agents_dir)
        assert reg.get_agent("My-Bot") is not None


# ---------------------------------------------------------------------------
# resolve_effective_config
# ---------------------------------------------------------------------------

class TestResolveEffectiveConfig:
    def test_no_overrides(self):
        agent = AgentConfig(id="bot", model="gpt-4", dm_scope="per-guild")
        result = resolve_effective_config(agent, None)
        assert result is agent

    def test_with_overrides(self):
        agent = AgentConfig(id="bot", model="gpt-4", dm_scope="per-guild")
        binding = Binding(
            agent_id="bot", tier=2, match_key="guild_id", match_value="g1",
            overrides={"model": "gpt-4o", "scope": "main", "max_tokens": "2048"},
        )
        result = resolve_effective_config(agent, binding)
        assert result.model == "gpt-4o"
        assert result.dm_scope == "main"
        assert result.max_tokens == 2048
        assert result.id == "bot"

    def test_empty_overrides(self):
        agent = AgentConfig(id="bot")
        binding = Binding(agent_id="bot", tier=5, match_key="default", match_value="*")
        result = resolve_effective_config(agent, binding)
        assert result is agent
