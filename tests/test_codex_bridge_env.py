from __future__ import annotations


def test_build_bridge_env_overrides_global_credentials(monkeypatch):
    from pip_agent.backends.codex_cli import bridge_env

    monkeypatch.setenv("OPENAI_API_KEY", "global-key")
    monkeypatch.setenv("CODEX_API_KEY", "global-codex-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://global.example/v1")
    monkeypatch.setattr(
        bridge_env,
        "resolve_codex_credentials",
        lambda: ("pip-key", "https://pip.example/v1"),
    )
    monkeypatch.setattr(bridge_env, "_config_toml_env_keys", lambda: [])

    env = bridge_env.build_bridge_env(session_id="sid")

    assert env["OPENAI_API_KEY"] == "pip-key"
    assert env["CODEX_API_KEY"] == "pip-key"
    assert env["OPENAI_BASE_URL"] == "https://pip.example/v1"
    assert env["PIP_SESSION_ID"] == "sid"


def test_build_bridge_env_removes_inherited_base_url(monkeypatch):
    from pip_agent.backends.codex_cli import bridge_env

    monkeypatch.setenv("OPENAI_BASE_URL", "https://global.example/v1")
    monkeypatch.setattr(
        bridge_env,
        "resolve_codex_credentials",
        lambda: ("pip-key", None),
    )
    monkeypatch.setattr(bridge_env, "_config_toml_env_keys", lambda: [])

    env = bridge_env.build_bridge_env()

    assert "OPENAI_BASE_URL" not in env


def test_build_codex_config_override_uses_custom_provider():
    from codex._runtime import serialize_config_overrides

    from pip_agent.backends.codex_cli.bridge_env import build_codex_config_override

    config = build_codex_config_override(
        "https://pip.example/v1",
        "pip-key",
    )

    overrides = set(serialize_config_overrides(config))
    assert 'model_provider="pip-relay"' in overrides
    assert 'model_providers.pip-relay.name="pip-relay"' in overrides
    assert (
        'model_providers.pip-relay.base_url="https://pip.example/v1"'
        in overrides
    )
    assert 'model_providers.pip-relay.env_key="CODEX_API_KEY"' in overrides
