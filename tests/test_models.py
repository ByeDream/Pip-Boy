"""Tests for the tiered model registry recognizer + fallback wrapper.

Locking these in matters because :func:`is_model_invalid_error` is the
gate that decides "burn through tier candidates" vs "surface to the
user". A regression in either direction is bad: false negatives leave
misconfigured ``MODEL_T*`` failures unrecoverable; false positives
swallow auth/rate-limit errors as model issues and silently spend the
whole tier chain on each turn.
"""

from __future__ import annotations

import pytest

from pip_agent.models import (
    DEFAULT_TIER,
    TASK_TIER,
    VALID_TIERS,
    is_model_invalid_error,
    primary_model,
    resolve_chain,
    with_model_fallback,
)


class TestTaskTierTable:
    def test_default_tier_is_strongest(self):
        assert DEFAULT_TIER == "t0"

    def test_valid_tiers(self):
        assert VALID_TIERS == frozenset({"t0", "t1", "t2"})

    def test_task_tier_pinned(self):
        # Hardcoded by design — a stage cannot be bent to use a stronger
        # model than what the design assigns. Locking the exact mapping
        # makes accidental drift visible.
        assert TASK_TIER == {
            "heartbeat": "t2",
            "cron": "t2",
            "reflect": "t1",
            "consolidate": "t1",
            "axioms": "t0",
        }


class TestResolveChain:
    def test_t0_descends_through_all_three(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "strong")
        monkeypatch.setattr(config.settings, "model_t1", "medium")
        monkeypatch.setattr(config.settings, "model_t2", "cheap")
        assert resolve_chain("t0") == ["strong", "medium", "cheap"]

    def test_t1_skips_t0(self, monkeypatch):
        # Higher tiers must NEVER appear in a lower tier's chain — the
        # whole point is "downgrade only".
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "strong")
        monkeypatch.setattr(config.settings, "model_t1", "medium")
        monkeypatch.setattr(config.settings, "model_t2", "cheap")
        assert resolve_chain("t1") == ["medium", "cheap"]

    def test_t2_pins_to_one(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "strong")
        monkeypatch.setattr(config.settings, "model_t1", "medium")
        monkeypatch.setattr(config.settings, "model_t2", "cheap")
        assert resolve_chain("t2") == ["cheap"]

    def test_empty_entries_are_skipped(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "")
        monkeypatch.setattr(config.settings, "model_t1", "  ")
        monkeypatch.setattr(config.settings, "model_t2", "cheap")
        assert resolve_chain("t0") == ["cheap"]

    def test_primary_model_is_chain_head(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "")
        monkeypatch.setattr(config.settings, "model_t1", "medium")
        monkeypatch.setattr(config.settings, "model_t2", "cheap")
        # Empty t0 -> falls through to t1's name as the head of t0's chain.
        assert primary_model("t0") == "medium"

    def test_primary_model_empty_when_unconfigured(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "")
        monkeypatch.setattr(config.settings, "model_t1", "")
        monkeypatch.setattr(config.settings, "model_t2", "")
        assert primary_model("t0") == ""


class TestIsModelInvalidErrorStructured:
    """Layer 2: parsed JSON envelope ``{"error": {"type": ..., "message": ...}}``.

    This is the path domestic Anthropic-compatible proxies actually
    produce — they localise the message but keep the JSON shape, so
    structured discrimination is more reliable than string matching
    over a curated dictionary.
    """

    def test_upstream_not_found_error_is_model(self):
        msg = (
            'API Error: 404 {"type":"error","error":'
            '{"type":"not_found_error","message":"model not found: foo"}}'
        )
        assert is_model_invalid_error(RuntimeError(msg))

    def test_proxy_invalid_request_with_chinese_model_message(self):
        # Real-world payload — gateway flattens a 404 to a 400, message
        # localised to Chinese. The structured layer recognises it
        # without needing a Chinese-phrase dictionary.
        msg = (
            'API Error: 400 {"type":"error","error":'
            '{"type":"invalid_request_error","message":"模型不存在"}}'
        )
        assert is_model_invalid_error(RuntimeError(msg))

    def test_proxy_invalid_request_with_english_model_message(self):
        msg = (
            'API Error: 400 {"type":"error","error":'
            '{"type":"invalid_request_error","message":"Unknown model claude-foo"}}'
        )
        assert is_model_invalid_error(RuntimeError(msg))

    def test_invalid_request_without_model_reference_is_not_model(self):
        # Generic malformed payload — switching models would not help.
        msg = (
            'API Error: 400 {"type":"error","error":'
            '{"type":"invalid_request_error",'
            '"message":"messages.0.role must be one of [user, assistant]"}}'
        )
        assert not is_model_invalid_error(RuntimeError(msg))

    def test_typed_rate_limit_envelope_is_not_model(self):
        # Even when the rate-limit message mentions a model name, the
        # structured layer trusts the upstream type label and returns
        # False — switching models won't help a rate-limit lockout.
        msg = (
            'API Error: 429 {"type":"error","error":'
            '{"type":"rate_limit_error","message":"Rate limit exceeded for model X"}}'
        )
        assert not is_model_invalid_error(RuntimeError(msg))

    def test_typed_authentication_envelope_is_not_model(self):
        msg = (
            'API Error: 401 {"type":"error","error":'
            '{"type":"authentication_error","message":"invalid api key"}}'
        )
        assert not is_model_invalid_error(RuntimeError(msg))


class TestIsModelInvalidErrorTypedException:
    """Layer 1: typed SDK exception classes resolve before any parsing."""

    def test_not_found_class_name_short_circuits_to_true(self):
        # Mimics ``anthropic.NotFoundError``: classname carries the
        # signal regardless of message contents.
        class NotFoundError(Exception):
            pass

        assert is_model_invalid_error(NotFoundError("anything"))

    def test_rate_limit_class_name_short_circuits_to_false(self):
        class RateLimitError(Exception):
            pass

        # Even with "model" mentioned, the typed-exception filter wins.
        assert not is_model_invalid_error(RateLimitError("model X rate limited"))

    def test_authentication_class_name_short_circuits_to_false(self):
        class AuthenticationError(Exception):
            pass

        assert not is_model_invalid_error(AuthenticationError("bad model key"))

    def test_permission_class_name_short_circuits_to_false(self):
        class PermissionDeniedError(Exception):
            pass

        assert not is_model_invalid_error(PermissionDeniedError("model forbidden"))


class TestIsModelInvalidErrorKeywordFloor:
    """Layer 3: keyword floor for un-parseable wrappers.

    Pragmatic trade-off: bare strings without an envelope or typed
    class lose all structure, so we fall back to "does the message
    mention 'model' or '模型'". False positives walk the chain once
    and surface the same error — bounded harm.
    """

    @pytest.mark.parametrize(
        "msg",
        [
            "model not found",
            "Model does not exist",
            "Invalid model: gpt-foo",
            "Unknown model 'claude-bogus'",
            "no such model on this gateway",
            # Bare Chinese without an envelope still trips the floor
            # via the 模型 keyword — important because user-facing
            # surfaces sometimes drop the JSON shell.
            "找不到模型 abc",
            "模型不支持",
        ],
    )
    def test_model_keyword_matches(self, msg):
        assert is_model_invalid_error(RuntimeError(msg))

    @pytest.mark.parametrize(
        "msg",
        [
            "rate limit exceeded",
            "Error: 429 too many requests",
            "ECONNRESET",
            "timeout while reading response",
            "401 Unauthorized: invalid api key",
            "billing: account has zero credits",
            "context window exceeded",
        ],
    )
    def test_unrelated_failures_skip_the_floor(self, msg):
        assert not is_model_invalid_error(RuntimeError(msg))


class TestIsModelInvalidErrorStatusCode:
    """Layer 2 secondary: HTTP status filter."""

    def test_429_with_model_word_in_outer_text_still_false(self):
        # Status code wins before keyword floor: "model" in the outer
        # bracket text isn't enough when we know it's a 429.
        msg = "API Error: 429 some random model wrapper text"
        assert not is_model_invalid_error(RuntimeError(msg))

    def test_401_with_model_word_still_false(self):
        msg = "API Error: 401 invalid auth for model X"
        assert not is_model_invalid_error(RuntimeError(msg))

    def test_402_billing_with_model_word_still_false(self):
        msg = "API Error: 402 quota exceeded on model X"
        assert not is_model_invalid_error(RuntimeError(msg))

    def test_5xx_outside_4xx_band_returns_false(self):
        # 5xx is server-side; switching the model name doesn't help.
        msg = "API Error: 500 internal server error talking to model X"
        assert not is_model_invalid_error(RuntimeError(msg))


class TestWithModelFallback:
    def test_first_candidate_succeeds(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t1", "primary")
        monkeypatch.setattr(config.settings, "model_t2", "fallback")
        seen: list[str] = []

        def call(model: str) -> str:
            seen.append(model)
            return f"ok:{model}"

        assert with_model_fallback("t1", call) == "ok:primary"
        assert seen == ["primary"]

    def test_falls_through_invalid_to_next(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t1", "primary")
        monkeypatch.setattr(config.settings, "model_t2", "fallback")
        seen: list[str] = []

        def call(model: str) -> str:
            seen.append(model)
            if model == "primary":
                raise RuntimeError("模型不存在")
            return f"ok:{model}"

        assert with_model_fallback("t1", call) == "ok:fallback"
        assert seen == ["primary", "fallback"]

    def test_does_not_swallow_unrelated_errors(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t1", "primary")
        monkeypatch.setattr(config.settings, "model_t2", "fallback")
        seen: list[str] = []

        def call(model: str) -> str:
            seen.append(model)
            raise RuntimeError("rate limit exceeded")

        with pytest.raises(RuntimeError, match="rate limit"):
            with_model_fallback("t1", call)
        # A non-model error must NOT trigger fallback — the second
        # candidate would just hit the same rate limit.
        assert seen == ["primary"]

    def test_empty_chain_raises(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t0", "")
        monkeypatch.setattr(config.settings, "model_t1", "")
        monkeypatch.setattr(config.settings, "model_t2", "")

        with pytest.raises(RuntimeError, match="No model configured"):
            with_model_fallback("t0", lambda m: m)

    def test_all_candidates_invalid_raises_last(self, monkeypatch):
        from pip_agent import config

        monkeypatch.setattr(config.settings, "model_t1", "a")
        monkeypatch.setattr(config.settings, "model_t2", "b")

        def call(model: str) -> str:
            raise RuntimeError(f"模型不存在: {model}")

        with pytest.raises(RuntimeError, match="模型不存在: b"):
            with_model_fallback("t1", call)
