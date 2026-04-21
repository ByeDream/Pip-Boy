"""Contract tests for ``pip_agent.anthropic_client``.

This module is the single source of truth for:

1. Resolving Anthropic credentials from settings + env.
2. Deciding bearer vs. x-api-key based on the proxy rule.
3. Building the ``anthropic.Anthropic`` client for direct-SDK calls.

The tests also cover ``agent_runner._build_env`` so both consumers of
``resolve_anthropic_credential`` stay in lockstep — if the proxy rule ever
drifts between the CC-subprocess path and the direct-SDK path, these tests
break first.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _clean_env(monkeypatch):
    """Scrub both settings and env so each test starts from zero."""
    from pip_agent import config

    monkeypatch.setattr(config.settings, "anthropic_api_key", "")
    monkeypatch.setattr(config.settings, "anthropic_auth_token", "")
    monkeypatch.setattr(config.settings, "anthropic_base_url", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    yield config


def _fake_sdk(monkeypatch):
    """Replace ``anthropic.Anthropic`` with a capture-only stub.

    ``build_anthropic_client`` lazy-imports the SDK inside the function body,
    but the import returns the same module object pytest is patching, so the
    stub takes effect for the very next call.
    """
    import anthropic

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    return captured


# ---------------------------------------------------------------------------
# resolve_anthropic_credential — pure logic, no SDK needed
# ---------------------------------------------------------------------------


class TestResolveCredential:
    """The proxy rule: BASE_URL set OR explicit AUTH_TOKEN → bearer."""

    def test_returns_none_when_nothing_set(self, _clean_env):
        from pip_agent.anthropic_client import resolve_anthropic_credential

        assert resolve_anthropic_credential() is None

    def test_api_key_only_is_x_api_key(self, _clean_env):
        from pip_agent.anthropic_client import resolve_anthropic_credential

        _clean_env.settings.anthropic_api_key = "sk-ant-direct"

        cred = resolve_anthropic_credential()
        assert cred is not None
        assert cred.token == "sk-ant-direct"
        assert cred.bearer is False
        assert cred.base_url == ""

    def test_api_key_plus_base_url_promotes_to_bearer(self, _clean_env):
        """The proxy case users actually hit: one ``ANTHROPIC_API_KEY`` env var
        that happens to be a gateway bearer token, plus ``ANTHROPIC_BASE_URL``.
        """
        from pip_agent.anthropic_client import resolve_anthropic_credential

        _clean_env.settings.anthropic_api_key = "proxy-bearer-token"
        _clean_env.settings.anthropic_base_url = "https://proxy.example.com/anthropic"

        cred = resolve_anthropic_credential()
        assert cred is not None
        assert cred.token == "proxy-bearer-token"
        assert cred.bearer is True
        assert cred.base_url == "https://proxy.example.com/anthropic"

    def test_explicit_auth_token_wins_and_is_bearer(self, _clean_env):
        from pip_agent.anthropic_client import resolve_anthropic_credential

        _clean_env.settings.anthropic_api_key = "ignored"
        _clean_env.settings.anthropic_auth_token = "winning-token"

        cred = resolve_anthropic_credential()
        assert cred is not None
        assert cred.token == "winning-token"
        assert cred.bearer is True

    def test_settings_wins_over_env(self, _clean_env, monkeypatch):
        from pip_agent.anthropic_client import resolve_anthropic_credential

        _clean_env.settings.anthropic_api_key = "from-settings"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")

        cred = resolve_anthropic_credential()
        assert cred is not None
        assert cred.token == "from-settings"

    def test_env_fallback_still_honors_proxy_rule(self, _clean_env, monkeypatch):
        from pip_agent.anthropic_client import resolve_anthropic_credential

        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env-proxy")

        cred = resolve_anthropic_credential()
        assert cred is not None
        assert cred.token == "env-key"
        assert cred.bearer is True
        assert cred.base_url == "https://env-proxy"


# ---------------------------------------------------------------------------
# default_direct_sdk_model — single knob for reflect / consolidate / axioms
# ---------------------------------------------------------------------------


class TestDefaultDirectSdkModel:
    """Direct-SDK calls (reflect, consolidate, axioms) must default to the
    same model as the default agent. Any stage that hardcodes its own
    shorthand breaks users whose proxy only whitelists the agent's model.
    """

    def test_matches_routing_default_model(self):
        from pip_agent import routing
        from pip_agent.anthropic_client import default_direct_sdk_model

        assert default_direct_sdk_model() == routing.DEFAULT_MODEL

    def test_reflect_uses_it_as_fallback(self, monkeypatch, tmp_path):
        """``reflect_from_jsonl`` without an explicit ``model=`` must fall
        back through ``default_direct_sdk_model()``, never a stage-local
        constant.

        ``reflect.py`` binds the helper at import time via ``from ... import``,
        so we patch the name in the reflect module's namespace — which is
        exactly what a stray refactor that re-introduces a local default
        would end up doing wrong too.
        """
        from pip_agent.memory import reflect

        monkeypatch.setattr(
            reflect, "default_direct_sdk_model", lambda: "sentinel-model",
        )

        captured = {}

        class FakeResp:
            content = [type("B", (), {"text": "[]"})()]

        class FakeLLM:
            class messages:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)
                    return FakeResp()

        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text(
            '{"type":"user","message":{"role":"user","content":"hi"}}\n',
            encoding="utf-8",
        )

        reflect.reflect_from_jsonl(jsonl, agent_id="pip-boy", client=FakeLLM())

        assert captured.get("model") == "sentinel-model"

    def test_consolidate_uses_it_as_fallback(self, monkeypatch):
        """Same invariant for ``consolidate`` — it must also resolve the
        default model via the shared helper.
        """
        from pip_agent.memory import consolidate

        # consolidate imports the helper inside the function body, so we
        # patch the source module instead of the caller's namespace.
        from pip_agent import anthropic_client

        monkeypatch.setattr(
            anthropic_client, "default_direct_sdk_model", lambda: "sentinel-model",
        )

        captured = {}

        class FakeResp:
            content = [type("B", (), {"text": "[]"})()]

        class FakeLLM:
            class messages:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)
                    return FakeResp()

        consolidate.consolidate(
            FakeLLM(),
            [{"ts": 1.0, "text": "x", "category": "lesson", "source": "auto"}],
            [],
            1,
        )

        assert captured.get("model") == "sentinel-model"


# ---------------------------------------------------------------------------
# build_anthropic_client — translates the credential into SDK kwargs
# ---------------------------------------------------------------------------


class TestBuildClient:
    def test_returns_none_without_credential(self, _clean_env):
        from pip_agent.anthropic_client import build_anthropic_client

        assert build_anthropic_client() is None

    def test_x_api_key_path(self, _clean_env, monkeypatch):
        from pip_agent.anthropic_client import build_anthropic_client

        _clean_env.settings.anthropic_api_key = "sk-ant-direct"
        captured = _fake_sdk(monkeypatch)

        assert build_anthropic_client() is not None
        assert captured.get("api_key") == "sk-ant-direct"
        assert "auth_token" not in captured
        assert "base_url" not in captured

    def test_bearer_path_for_proxy(self, _clean_env, monkeypatch):
        from pip_agent.anthropic_client import build_anthropic_client

        _clean_env.settings.anthropic_api_key = "proxy-bearer"
        _clean_env.settings.anthropic_base_url = "https://proxy"
        captured = _fake_sdk(monkeypatch)

        assert build_anthropic_client() is not None
        assert captured.get("auth_token") == "proxy-bearer"
        assert "api_key" not in captured
        assert captured["base_url"] == "https://proxy"

    def test_sdk_exception_returns_none(self, _clean_env, monkeypatch):
        """A misbehaving SDK constructor must not crash the caller."""
        import anthropic

        from pip_agent.anthropic_client import build_anthropic_client

        _clean_env.settings.anthropic_api_key = "x"

        def boom(**_kwargs):
            raise RuntimeError("sdk broken")

        monkeypatch.setattr(anthropic, "Anthropic", boom)
        assert build_anthropic_client() is None


# ---------------------------------------------------------------------------
# agent_runner._build_env — the *other* consumer of resolve_anthropic_credential
#
# Locks down that the CC subprocess env + the direct-SDK client always agree
# on which headers / env vars go out. If someone re-forks the proxy rule into
# _build_env, this class is the tripwire.
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_cron_kill_switch_is_always_set(self, _clean_env):
        """``CLAUDE_CODE_DISABLE_CRON`` must leak into every subprocess.

        CC's native ``CronCreate`` / ``CronList`` / ``CronDelete`` silently
        never fire in our architecture (the CC subprocess dies at
        ``end_turn``, so there is no thread left to tick the scheduler).
        Keeping them visible to the model would be an API that lies —
        ``host_scheduler`` is the only cron provider we promise.

        Tripwire: if a future refactor ever decides "let's make this env
        conditional", this test breaks first.
        """
        from pip_agent.agent_runner import _build_env

        assert _build_env().get("CLAUDE_CODE_DISABLE_CRON") == "1"

        _clean_env.settings.anthropic_api_key = "sk-ant-direct"
        assert _build_env().get("CLAUDE_CODE_DISABLE_CRON") == "1"

        _clean_env.settings.anthropic_base_url = "https://proxy.example.com"
        assert _build_env().get("CLAUDE_CODE_DISABLE_CRON") == "1"

    def test_no_credentials_means_only_the_cron_kill_switch(self, _clean_env):
        from pip_agent.agent_runner import _build_env

        assert _build_env() == {"CLAUDE_CODE_DISABLE_CRON": "1"}

    def test_api_key_only_emits_x_api_key_env(self, _clean_env):
        from pip_agent.agent_runner import _build_env

        _clean_env.settings.anthropic_api_key = "sk-ant-direct"

        env = _build_env()
        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-direct"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in env

    def test_proxy_mode_emits_auth_token_and_disables_betas(self, _clean_env):
        from pip_agent.agent_runner import _build_env

        _clean_env.settings.anthropic_api_key = "proxy-bearer"
        _clean_env.settings.anthropic_base_url = "https://proxy.example.com"

        env = _build_env()
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "proxy-bearer"
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("ANTHROPIC_BASE_URL") == "https://proxy.example.com"
        assert env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") == "1"

    def test_explicit_auth_token_promoted_without_base_url(self, _clean_env):
        """``ANTHROPIC_AUTH_TOKEN`` alone → bearer, even without a proxy URL."""
        from pip_agent.agent_runner import _build_env

        _clean_env.settings.anthropic_auth_token = "bearer-direct"

        env = _build_env()
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "bearer-direct"
        assert "ANTHROPIC_API_KEY" not in env
        # No base_url → no override and no beta disable.
        assert "ANTHROPIC_BASE_URL" not in env
        assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in env
