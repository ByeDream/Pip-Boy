"""Tests for the multi-agent routing layer."""

from __future__ import annotations

from pip_agent.routing import (
    DEFAULT_AGENT_ID,
    DEFAULT_DM_SCOPE,
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


class TestNormalizeAgentId:
    def test_simple(self):
        # Ids are lowercased — Windows is case-insensitive on disk, so
        # ``Helper`` and ``helper`` would collide.
        assert normalize_agent_id("Pip-Boy") == "pip-boy"

    def test_empty(self):
        assert normalize_agent_id("") == DEFAULT_AGENT_ID

    def test_whitespace(self):
        assert normalize_agent_id("  ") == DEFAULT_AGENT_ID

    def test_special_chars(self):
        # Whitespace + punctuation become dashes, and case is folded.
        assert normalize_agent_id("My Bot!") == "my-bot"

    def test_already_valid(self):
        assert normalize_agent_id("pm-bot") == "pm-bot"

    def test_uppercase_already_valid_charset(self):
        # Even if the characters would be valid after lowercasing, mixed
        # case input is folded rather than rejected.
        assert normalize_agent_id("ProjectStella") == "projectstella"

    def test_long_id(self):
        result = normalize_agent_id("a" * 100)
        assert len(result) <= 64


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig(id="test")
        assert cfg.effective_model == DEFAULT_MODEL
        assert cfg.effective_dm_scope == DEFAULT_DM_SCOPE

    def test_overridden(self):
        cfg = AgentConfig(id="custom", model="gpt-4o", dm_scope="main")
        assert cfg.effective_model == "gpt-4o"
        assert cfg.effective_dm_scope == "main"

    def test_system_prompt(self):
        cfg = AgentConfig(id="bot", name="TestBot", system_body="Working at {workdir}.")
        prompt = cfg.system_prompt(workdir="/tmp/test")
        assert "/tmp/test" in prompt

    def test_system_prompt_empty_body(self):
        cfg = AgentConfig(id="my-agent")
        prompt = cfg.system_prompt()
        assert prompt == ""

    def test_system_prompt_substitutes_agent_name(self):
        # ``{agent_name}`` is the source of truth for how the model
        # introduces itself — it's sourced from YAML ``name:`` with a
        # fallback to id so frontmatter edits take effect without a
        # body rewrite.
        cfg = AgentConfig(
            id="helper", name="Stella",
            system_body="You are {agent_name}, an assistant running {model_name}.",
            model="claude-opus-4-6",
        )
        prompt = cfg.system_prompt()
        assert "You are Stella" in prompt
        assert "claude-opus-4-6" in prompt

    def test_system_prompt_agent_name_fallback_to_id(self):
        cfg = AgentConfig(id="helper", system_body="You are {agent_name}.")
        assert cfg.system_prompt() == "You are helper."

    def test_display_name_prefers_name_over_id(self):
        assert AgentConfig(id="pip-boy", name="Pip-Boy").display_name == "Pip-Boy"
        assert AgentConfig(id="helper").display_name == "helper"


class TestAgentConfigFromFile:
    def test_load(self, tmp_path):
        md = tmp_path / "test-bot.md"
        md.write_text(
            "---\n"
            "id: test-bot\n"
            "name: TestBot\n"
            "model: gpt-4\n"
            "dm_scope: main\n"
            "---\n"
            "Be concise.\n",
            encoding="utf-8",
        )
        cfg = agent_config_from_file(md)
        assert cfg.id == "test-bot"
        assert cfg.name == "TestBot"
        assert cfg.model == "gpt-4"
        assert cfg.dm_scope == "main"
        assert cfg.system_body == "Be concise."

    def test_default_id_fallback_when_frontmatter_omits_it(self, tmp_path):
        """Callers that know the id out-of-band can pass ``default_id``."""
        md = tmp_path / "plain.md"
        md.write_text(
            "---\nname: Plain\n---\nJust a plain body.\n",
            encoding="utf-8",
        )
        cfg = agent_config_from_file(md, default_id="plain")
        assert cfg.id == "plain"

    def test_raises_without_id_or_default(self, tmp_path):
        """No ``id:`` and no ``default_id`` is a hard error — we don't
        silently guess an id from the filename."""
        import pytest

        md = tmp_path / "plain.md"
        md.write_text("Just a body, no frontmatter.", encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'id:'"):
            agent_config_from_file(md)

    def test_ignores_unknown_frontmatter_keys(self, tmp_path):
        md = tmp_path / "legacy.md"
        md.write_text(
            "---\n"
            "id: legacy\n"
            "name: Legacy\n"
            "model: gpt-4\n"
            "max_tokens: 2048\n"
            "compact_threshold: 10000\n"
            "compact_micro_age: 2\n"
            "---\n"
            "Body.\n",
            encoding="utf-8",
        )
        cfg = agent_config_from_file(md)
        assert cfg.name == "Legacy"
        assert cfg.model == "gpt-4"
        assert not hasattr(cfg, "max_tokens")


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


class TestBindingTable:
    def test_resolve_peer(self):
        bt = BindingTable()
        bt.add(Binding(agent_id="peer-bot", tier=1, match_key="peer_id", match_value="u1"))
        bt.add(Binding(agent_id="default", tier=5, match_key="default", match_value="*"))
        aid, _ = bt.resolve(peer_id="u1")
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
        # Session keys use the lowercased, filesystem-safe id — the
        # same one that names the agent's directory on disk.
        sk = build_session_key("Pip-Boy", "cli", "cli-user")
        assert sk.startswith("agent:pip-boy:")


class TestAgentRegistry:
    def test_default_when_empty(self):
        reg = AgentRegistry()
        assert reg.default_agent().id == DEFAULT_AGENT_ID
        assert len(reg.list_agents()) == 1

    def _scaffold(
        self, workspace, dirname, *, agent_id=None, name="", extra_fm=""
    ):
        """Scaffold a registered sub-agent on disk.

        Writes ``<workspace>/<dirname>/.pip/persona.md`` **and** a
        registry entry — the registry is now the source of truth, so
        both are required for the agent to be discovered on load.
        """
        import json

        aid = agent_id or dirname
        sub = workspace / dirname / ".pip"
        sub.mkdir(parents=True)
        fm = f"id: {aid}\nname: {name or aid}\n{extra_fm}".rstrip()
        (sub / "persona.md").write_text(
            f"---\n{fm}\n---\nBody.\n", encoding="utf-8",
        )
        reg_dir = workspace / ".pip"
        reg_dir.mkdir(parents=True, exist_ok=True)
        reg_path = reg_dir / "agents_registry.json"
        if reg_path.is_file():
            data = json.loads(reg_path.read_text(encoding="utf-8"))
        else:
            data = {"version": 1, "agents": {}}
        data["agents"][aid] = {"kind": "sub", "cwd": dirname}
        reg_path.write_text(
            json.dumps(data), encoding="utf-8",
        )
        return sub

    def test_load_from_dir(self, tmp_path):
        workspace = tmp_path / "workspace"
        sub = self._scaffold(
            workspace, "test-bot", name="TestBot", extra_fm="model: gpt-4\n",
        )
        reg = AgentRegistry(workspace)
        assert reg.get_agent("test-bot") is not None
        assert reg.get_agent("test-bot").name == "TestBot"
        paths = reg.paths_for("test-bot")
        assert paths is not None
        assert paths.cwd == workspace / "test-bot"
        assert paths.pip_dir == sub

    def test_unregistered_directory_is_ignored(self, tmp_path):
        """A ``.pip/persona.md`` on disk without a registry entry is
        invisible — the registry is authoritative."""
        workspace = tmp_path / "workspace"
        sub = workspace / "stray-bot" / ".pip"
        sub.mkdir(parents=True)
        (sub / "persona.md").write_text(
            "---\nid: stray-bot\nname: Stray\n---\nBody.\n",
            encoding="utf-8",
        )
        reg = AgentRegistry(workspace)
        assert reg.get_agent("stray-bot") is None

    def test_missing_dir(self, tmp_path):
        reg = AgentRegistry(tmp_path / "nonexistent")
        assert reg.default_agent().id == DEFAULT_AGENT_ID

    def test_get_agent_normalizes(self, tmp_path):
        workspace = tmp_path / "workspace"
        self._scaffold(workspace, "my-bot", name="MyBot")
        reg = AgentRegistry(workspace)
        # get_agent routes whitespace-noisy input through
        # normalize_agent_id before lookup.
        assert reg.get_agent("my-bot") is not None
        assert reg.get_agent(" my-bot ") is not None


class TestDirnameDecoupling:
    """agent_id and dirname are distinct dimensions (see /subagent create --id)."""

    def _write_persona(self, workspace, dirname, *, agent_id, name):
        """Helper: scaffold ``<workspace>/<dirname>/.pip/persona.md``."""
        pip = workspace / dirname / ".pip"
        pip.mkdir(parents=True)
        (pip / "persona.md").write_text(
            f"---\nid: {agent_id}\nname: {name}\n---\nBody.\n",
            encoding="utf-8",
        )
        return pip

    def _write_registry(self, workspace, agents: dict):
        """Helper: scaffold ``<workspace>/.pip/agents_registry.json``."""
        import json

        root_pip = workspace / ".pip"
        root_pip.mkdir(parents=True, exist_ok=True)
        (root_pip / "agents_registry.json").write_text(
            json.dumps({"version": 1, "agents": agents}),
            encoding="utf-8",
        )

    def test_register_agent_with_explicit_dirname(self, tmp_path):
        reg = AgentRegistry(tmp_path)
        cfg = AgentConfig(id="alice", name="Alice")
        reg.register_agent(cfg, dirname="foo")
        paths = reg.paths_for("alice")
        assert paths is not None
        assert paths.cwd == tmp_path / "foo"
        assert paths.pip_dir == tmp_path / "foo" / ".pip"
        assert reg.dirname_for("alice") == "foo"

    def test_register_agent_defaults_dirname_to_id(self, tmp_path):
        reg = AgentRegistry(tmp_path)
        cfg = AgentConfig(id="bob", name="Bob")
        reg.register_agent(cfg)
        paths = reg.paths_for("bob")
        assert paths is not None
        assert paths.cwd == tmp_path / "bob"
        assert reg.dirname_for("bob") == "bob"

    def test_get_by_dirname_finds_decoupled_agent(self, tmp_path):
        reg = AgentRegistry(tmp_path)
        cfg = AgentConfig(id="alice", name="Alice")
        reg.register_agent(cfg, dirname="foo")

        found = reg.get_by_dirname("foo")
        assert found is not None
        assert found.id == "alice"
        # normalization: mixed-case dirname input resolves the same.
        assert reg.get_by_dirname("FOO") is not None
        assert reg.get_by_dirname("FOO").id == "alice"
        # get_agent still works by id as well.
        assert reg.get_agent("alice") is not None

    def test_get_by_dirname_unknown(self, tmp_path):
        reg = AgentRegistry(tmp_path)
        reg.register_agent(AgentConfig(id="alice"), dirname="foo")
        assert reg.get_by_dirname("ghost") is None

    def test_load_workspace_honors_registry_cwd(self, tmp_path):
        workspace = tmp_path / "ws"
        # Directory on disk is ``foo/`` but the agent's id is ``alice``.
        self._write_persona(workspace, "foo", agent_id="alice", name="Alice")
        self._write_registry(workspace, {"alice": {"kind": "sub", "cwd": "foo"}})

        reg = AgentRegistry(workspace)
        assert reg.get_agent("alice") is not None
        assert reg.get_agent("foo") is None  # not an id, only a dirname
        assert reg.get_by_dirname("foo") is not None
        assert reg.get_by_dirname("foo").id == "alice"
        paths = reg.paths_for("alice")
        assert paths.cwd == workspace / "foo"

    def test_dirname_survives_save_and_reload(self, tmp_path):
        """Round-trip: create decoupled agent, save registry, reload — mapping preserved."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        # First session: create + persist.
        self._write_persona(workspace, "foo", agent_id="alice", name="Alice")
        reg1 = AgentRegistry(workspace)
        reg1.register_agent(AgentConfig(id="alice", name="Alice"), dirname="foo")
        reg1.save_registry()

        # Second session: reload from disk.
        reg2 = AgentRegistry(workspace)
        assert reg2.get_agent("alice") is not None
        assert reg2.get_by_dirname("foo") is not None
        assert reg2.get_by_dirname("foo").id == "alice"

    def test_root_agent_dirname_is_dot(self, tmp_path):
        workspace = tmp_path / "ws"
        (workspace / ".pip").mkdir(parents=True)
        (workspace / ".pip" / "persona.md").write_text(
            "---\nname: Pip-Boy\n---\nRoot.\n", encoding="utf-8",
        )
        reg = AgentRegistry(workspace)
        assert reg.dirname_for(DEFAULT_AGENT_ID) == "."


class TestResolveEffectiveConfig:
    def test_no_overrides(self):
        agent = AgentConfig(id="bot", model="gpt-4", dm_scope="per-guild")
        result = resolve_effective_config(agent, None)
        assert result is agent

    def test_with_overrides(self):
        agent = AgentConfig(id="bot", model="gpt-4", dm_scope="per-guild")
        binding = Binding(
            agent_id="bot", tier=2, match_key="guild_id", match_value="g1",
            overrides={"model": "gpt-4o", "scope": "main"},
        )
        result = resolve_effective_config(agent, binding)
        assert result.model == "gpt-4o"
        assert result.dm_scope == "main"
        assert result.id == "bot"

    def test_empty_overrides(self):
        agent = AgentConfig(id="bot")
        binding = Binding(agent_id="bot", tier=5, match_key="default", match_value="*")
        result = resolve_effective_config(agent, binding)
        assert result is agent
