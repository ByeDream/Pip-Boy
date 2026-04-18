"""Tests for pip_agent.resilience: failure classification, profile rotation, runner."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import pytest

from pip_agent.resilience import (
    AuthProfile,
    FailoverReason,
    ProfileManager,
    ResilienceExhausted,
    ResilienceRunner,
    SimulatedFailure,
    classify_failure,
    load_profiles,
)

# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    def test_rate_limit(self):
        exc = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        assert classify_failure(exc) == FailoverReason.rate_limit

    def test_auth(self):
        exc = anthropic.AuthenticationError(
            message="invalid key",
            response=MagicMock(status_code=401, headers={}),
            body=None,
        )
        assert classify_failure(exc) == FailoverReason.auth

    def test_timeout(self):
        exc = anthropic.APITimeoutError(request=MagicMock())
        assert classify_failure(exc) == FailoverReason.timeout

    def test_overflow_from_bad_request(self):
        exc = anthropic.BadRequestError(
            message="prompt is too long: context window token overflow",
            response=MagicMock(status_code=400, headers={}),
            body=None,
        )
        assert classify_failure(exc) == FailoverReason.overflow

    def test_permission_denied_generic(self):
        exc = anthropic.PermissionDeniedError(
            message="forbidden",
            response=MagicMock(status_code=403, headers={}),
            body=None,
        )
        assert classify_failure(exc) == FailoverReason.unknown

    def test_permission_denied_billing(self):
        exc = anthropic.PermissionDeniedError(
            message="Your billing account has been suspended",
            response=MagicMock(status_code=403, headers={}),
            body=None,
        )
        assert classify_failure(exc) == FailoverReason.billing

    def test_unknown_error(self):
        assert classify_failure(RuntimeError("something weird")) == FailoverReason.unknown

    def test_string_heuristic_rate(self):
        assert classify_failure(RuntimeError("429 Too Many Requests")) == FailoverReason.rate_limit

    def test_string_heuristic_overflow(self):
        exc = RuntimeError("token limit exceeded")
        assert classify_failure(exc) == FailoverReason.overflow


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------


class TestProfileManager:
    def test_select_available(self):
        p1 = AuthProfile(name="a", api_key="k1")
        p2 = AuthProfile(name="b", api_key="k2", cooldown_until=time.time() + 9999)
        pm = ProfileManager([p1, p2])
        assert pm.select_profile() == p1

    def test_all_on_cooldown(self):
        future = time.time() + 9999
        p1 = AuthProfile(name="a", api_key="k1", cooldown_until=future)
        pm = ProfileManager([p1])
        assert pm.select_profile() is None

    def test_mark_failure_and_recovery(self):
        p = AuthProfile(name="a", api_key="k1")
        pm = ProfileManager([p])
        pm.mark_failure(p, FailoverReason.rate_limit, 120.0)
        assert p.cooldown_until > time.time()
        assert p.failure_reason == "rate_limit"
        assert pm.select_profile() is None

        p.cooldown_until = 0
        pm.mark_success(p)
        assert p.failure_reason is None
        assert pm.select_profile() == p

    def test_client_caching(self):
        p = AuthProfile(name="env", api_key="sk-ant-test")
        pm = ProfileManager([p])
        c1 = pm.client_for(p)
        c2 = pm.client_for(p)
        assert c1 is c2


# ---------------------------------------------------------------------------
# SimulatedFailure
# ---------------------------------------------------------------------------


class TestSimulatedFailure:
    def test_arm_and_fire(self):
        sf = SimulatedFailure()
        assert not sf.is_armed
        sf.arm("rate_limit")
        assert sf.is_armed
        with pytest.raises(RuntimeError, match="rate limit"):
            sf.check_and_fire()
        assert not sf.is_armed

    def test_disarm(self):
        sf = SimulatedFailure()
        sf.arm("auth")
        sf.disarm()
        assert not sf.is_armed
        sf.check_and_fire()  # should not raise

    def test_invalid_reason(self):
        sf = SimulatedFailure()
        result = sf.arm("nonexistent")
        assert "Unknown reason" in result


# ---------------------------------------------------------------------------
# ResilienceRunner
# ---------------------------------------------------------------------------


class TestResilienceRunner:
    def _make_runner(self, profiles=None):
        if profiles is None:
            profiles = [AuthProfile(name="env", api_key="sk-test")]
        pm = ProfileManager(profiles)
        return ResilienceRunner(pm, verbose=False)

    def test_successful_call(self):
        runner = self._make_runner()
        mock_response = SimpleNamespace(
            content=[SimpleNamespace(text="hello")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            stop_reason="end_turn",
        )
        runner.profile_manager.profiles[0].api_key = "sk-test"
        client = runner.profile_manager.client_for(runner.profile_manager.profiles[0])
        client.messages = MagicMock()
        client.messages.create = MagicMock(return_value=mock_response)

        response, used_client = runner.call(
            messages=[{"role": "user", "content": "hi"}],
            system="test",
            tools=[],
            model="test-model",
            max_tokens=100,
        )
        assert response == mock_response
        assert runner.total_successes == 1

    def test_exhausted_raises(self):
        p = AuthProfile(name="env", api_key="sk-test")
        runner = self._make_runner([p])
        client = runner.profile_manager.client_for(p)
        client.messages = MagicMock()
        client.messages.create = MagicMock(
            side_effect=anthropic.AuthenticationError(
                message="bad key",
                response=MagicMock(status_code=401, headers={}),
                body=None,
            )
        )

        with pytest.raises(ResilienceExhausted, match="exhausted"):
            runner.call(
                messages=[{"role": "user", "content": "hi"}],
                system="test",
                tools=[],
                model="test-model",
                max_tokens=100,
            )

    def test_rotation_on_rate_limit(self):
        p1 = AuthProfile(name="a", api_key="k1")
        p2 = AuthProfile(name="b", api_key="k2")
        runner = self._make_runner([p1, p2])

        mock_ok = SimpleNamespace(
            content=[SimpleNamespace(text="ok")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            stop_reason="end_turn",
        )

        c1 = runner.profile_manager.client_for(p1)
        c1.messages = MagicMock()
        c1.messages.create = MagicMock(
            side_effect=anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )

        c2 = runner.profile_manager.client_for(p2)
        c2.messages = MagicMock()
        c2.messages.create = MagicMock(return_value=mock_ok)

        response, _ = runner.call(
            messages=[{"role": "user", "content": "hi"}],
            system="test",
            tools=[],
            model="test-model",
            max_tokens=100,
        )
        assert response == mock_ok
        assert runner.total_rotations == 1

    def test_fallback_model(self):
        p = AuthProfile(name="env", api_key="sk-test")
        runner = self._make_runner([p])

        mock_ok = SimpleNamespace(
            content=[SimpleNamespace(text="fallback ok")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            stop_reason="end_turn",
        )

        client = runner.profile_manager.client_for(p)
        client.messages = MagicMock()

        def _side_effect(**kwargs):
            if kwargs.get("model") == "primary":
                raise anthropic.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                )
            return mock_ok

        client.messages.create = MagicMock(side_effect=_side_effect)

        response, _ = runner.call(
            messages=[{"role": "user", "content": "hi"}],
            system="test",
            tools=[],
            model="primary",
            max_tokens=100,
            fallback_models=["fallback-model"],
        )
        assert response == mock_ok
        assert runner.total_fallbacks == 1

    def test_fallback_failure_marks_profile(self):
        p = AuthProfile(name="env", api_key="sk-test")
        runner = self._make_runner([p])

        client = runner.profile_manager.client_for(p)
        client.messages = MagicMock()
        client.messages.create.side_effect = anthropic.AuthenticationError(
            message="invalid key",
            response=MagicMock(status_code=401, headers={}),
            body=None,
        )

        with pytest.raises(ResilienceExhausted):
            runner.call(
                system="s",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                model="primary",
                max_tokens=100,
                fallback_models=["fb-model"],
            )
        assert p.failure_reason == "auth"
        assert p.cooldown_until > time.time()

    def test_stats(self):
        runner = self._make_runner()
        stats = runner.get_stats()
        assert "attempts" in stats
        assert "successes" in stats
        assert "failures" in stats
        assert "rotations" in stats


# ---------------------------------------------------------------------------
# load_profiles
# ---------------------------------------------------------------------------


class TestLoadProfiles:
    def test_env_key_only(self, tmp_path):
        profiles = load_profiles(
            tmp_path / "keys.json",
            env_api_key="sk-test",
            env_base_url="https://proxy.example.com",
        )
        assert len(profiles) == 1
        assert profiles[0].name == "env"
        assert profiles[0].base_url == "https://proxy.example.com"

    def test_keys_json_additive(self, tmp_path):
        import json
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({
            "profiles": [
                {"name": "backup", "api_key": "sk-backup", "base_url": ""},
            ]
        }))
        profiles = load_profiles(keys_file, env_api_key="sk-env")
        assert len(profiles) == 2
        assert profiles[0].name == "env"
        assert profiles[1].name == "backup"

    def test_dedup_same_key(self, tmp_path):
        import json
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({
            "profiles": [{"name": "dup", "api_key": "sk-env"}]
        }))
        profiles = load_profiles(keys_file, env_api_key="sk-env")
        assert len(profiles) == 1

    def test_empty_api_key_ignored(self, tmp_path):
        import json
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({
            "profiles": [{"name": "empty", "api_key": ""}]
        }))
        profiles = load_profiles(keys_file, env_api_key="sk-env")
        assert len(profiles) == 1

    def test_no_env_key_no_file(self, tmp_path):
        profiles = load_profiles(tmp_path / "missing.json")
        assert profiles == []

    def test_inherits_env_base_url(self, tmp_path):
        import json
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({
            "profiles": [{"name": "extra", "api_key": "sk-extra"}]
        }))
        profiles = load_profiles(
            keys_file, env_api_key="sk-env", env_base_url="https://proxy.example.com",
        )
        assert profiles[1].base_url == "https://proxy.example.com"
