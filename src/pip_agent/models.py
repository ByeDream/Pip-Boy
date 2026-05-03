"""Tiered model registry — single source of truth for model name resolution.

Each backend has its own tier set in ``.env``:

* **Codex** (default): ``CODEX_MODEL_T0`` / ``CODEX_MODEL_T1`` / ``CODEX_MODEL_T2``
* **Claude**:          ``CLAUDE_MODEL_T0`` / ``CLAUDE_MODEL_T1`` / ``CLAUDE_MODEL_T2``

Every call site picks a tier (``t0`` / ``t1`` / ``t2``); never a concrete
model name.  ``resolve_chain`` selects the correct set at runtime based on
``settings.backend``.

Failures on a specific model degrade DOWN the chain (never up):

* ``t0`` -> ``[*_t0, *_t1, *_t2]``
* ``t1`` -> ``[*_t1, *_t2]``
* ``t2`` -> ``[*_t2]``

Empty env entries are skipped, so a partly-configured ``.env`` (e.g. only
``*_T2`` set) still works for the tiers that do have a name.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Literal, TypeVar

log = logging.getLogger(__name__)


Tier = Literal["t0", "t1", "t2"]

DEFAULT_TIER: Tier = "t0"

VALID_TIERS: frozenset[str] = frozenset({"t0", "t1", "t2"})


# Task -> tier map. Hard-coded on purpose: the design intent is that an
# async/cheap stage cannot be bent to use the strongest model. The
# persona-driven main turn is the only stage whose tier varies at runtime,
# and it does so via :attr:`AgentConfig.tier` rather than this table.
TASK_TIER: dict[str, Tier] = {
    "heartbeat": "t2",
    "cron": "t2",
    "reflect": "t1",
    "consolidate": "t1",
    "axioms": "t0",
}


_CLAUDE_CHAIN: dict[Tier, tuple[str, ...]] = {
    "t0": ("claude_model_t0", "claude_model_t1", "claude_model_t2"),
    "t1": ("claude_model_t1", "claude_model_t2"),
    "t2": ("claude_model_t2",),
}

_CODEX_CHAIN: dict[Tier, tuple[str, ...]] = {
    "t0": ("codex_model_t0", "codex_model_t1", "codex_model_t2"),
    "t1": ("codex_model_t1", "codex_model_t2"),
    "t2": ("codex_model_t2",),
}


def resolve_chain(tier: Tier) -> list[str]:
    """Return the ordered list of concrete model names to try for ``tier``.

    Selects the tier table matching ``settings.backend``:

    * ``codex_cli``  -> ``CODEX_MODEL_T*``
    * ``claude_code`` -> ``CLAUDE_MODEL_T*``

    Empty / whitespace entries are skipped. The result may be empty when
    the env is not configured for ``tier`` or any lower tier; callers
    should treat that as "no model available, skip this LLM call".
    """
    from pip_agent.config import settings

    table = _CODEX_CHAIN if settings.backend == "codex_cli" else _CLAUDE_CHAIN

    chain: list[str] = []
    for attr in table[tier]:
        value = (getattr(settings, attr, "") or "").strip()
        if value:
            chain.append(value)
    return chain


def primary_model(tier: Tier) -> str:
    """First model name from :func:`resolve_chain`; empty if none configured.

    Used where a single concrete name is needed up front (e.g. the
    ``{model_name}`` template substitution in persona system prompts).
    """
    chain = resolve_chain(tier)
    return chain[0] if chain else ""


# ``claude.exe`` wraps proxy errors as ``"API Error: <code> {json}"`` on
# stderr; the SDK then surfaces the whole line as the exception text.
# Capturing the status code lets us cheaply discard non-4xx and the
# well-known non-model categories (auth/rate/billing) without parsing.
_CLI_STATUS_RE = re.compile(r"\bAPI\s*Error:\s*(\d{3})\b", re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# HTTP statuses where switching the model name CANNOT help, even if the
# error happens to mention "model" in passing (e.g. a localised proxy
# message like "Rate limit exceeded for model X"). 408 stays out: it's
# bare-bones request-timeout territory, never a model-availability claim.
_NON_MODEL_STATUSES: frozenset[int] = frozenset({401, 402, 403, 429})

# Anthropic SDK typed-exception class-name fragments that pre-empt the
# whole check. ``NotFoundError`` is the upstream signal for an unknown
# model; the others are categorical wrong-call-site issues that no model
# substitution would fix.
_SDK_NEVER_MODEL: tuple[str, ...] = (
    "ratelimit",
    "authentication",
    "permission",
    # Codex SDK equivalents
    "codexautherror",
)
_SDK_DEFINITELY_MODEL: tuple[str, ...] = (
    "notfound",
    "modelinvaliderror",
)


def is_model_invalid_error(exc: BaseException) -> bool:
    """True when ``exc`` indicates the requested model name is unusable.

    Three layers, cheapest first; later layers only run when earlier
    ones can't decide:

    1. **Typed SDK exception.** ``RateLimitError`` /
       ``AuthenticationError`` / ``PermissionDeniedError`` mean
       "swapping models won't help". ``NotFoundError`` is unambiguous.
    2. **Structured envelope.** ``claude.exe`` surfaces proxy failures
       as ``"API Error: <status> {json}"``. We pull the status code
       and the upstream ``error.type`` / ``error.message`` to
       discriminate cleanly — most domestic gateways still return a
       JSON body even when their type/message strings are localised.
    3. **Keyword floor.** For un-parseable wrappers (custom exception
       text, no JSON envelope), require the message to literally
       reference "model" or "模型". This is intentionally permissive
       — proxies localise their bodies and we can't list every phrase.
       A false positive merely walks the rest of the tier chain once
       and surfaces the same upstream error; the cost is bounded.
    """
    name = type(exc).__name__.lower()
    if any(marker in name for marker in _SDK_NEVER_MODEL):
        return False
    if any(marker in name for marker in _SDK_DEFINITELY_MODEL):
        return True

    raw = str(exc)

    status_match = _CLI_STATUS_RE.search(raw)
    if status_match:
        status = int(status_match.group(1))
        if not (400 <= status < 500):
            return False
        if status in _NON_MODEL_STATUSES:
            return False

    json_match = _JSON_OBJECT_RE.search(raw)
    if json_match:
        try:
            payload = json.loads(json_match.group(0))
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                err_type = str(err.get("type") or "").lower()
                err_msg = str(err.get("message") or "")
                if err_type == "not_found_error":
                    return True
                if err_type == "invalid_request_error":
                    return _mentions_model(err_msg)
                # Any other typed upstream error is categorical and
                # not a model-name issue — trust the upstream label.
                if err_type:
                    return False

    return _mentions_model(raw)


def _mentions_model(text: str) -> bool:
    """Floor check used by :func:`is_model_invalid_error`.

    Bilingual on purpose: domestic proxies overwhelmingly localise
    error bodies but keep the keyword "模型" intact, just as upstream
    keeps "model".
    """
    return "model" in text.lower() or "模型" in text


T = TypeVar("T")


def with_model_fallback(
    tier: Tier,
    call: Callable[[str], T],
    *,
    label: str = "",
) -> T:
    """Run ``call(model_name)`` over the resolved tier chain.

    Each candidate model is passed to ``call`` in turn. If ``call`` raises
    and :func:`is_model_invalid_error` matches, we move to the next
    candidate; any other exception re-raises immediately.

    Raises ``RuntimeError`` when the chain is empty (caller should treat
    that as "no model configured" and skip). Raises the final candidate's
    exception when every candidate fails with a model-invalid error.
    """
    chain = resolve_chain(tier)
    if not chain:
        raise RuntimeError(
            f"No model configured for tier {tier}. "
            "Set CODEX_MODEL_T* or CLAUDE_MODEL_T* in .env.",
        )

    last_exc: BaseException | None = None
    for idx, model in enumerate(chain):
        try:
            return call(model)
        except BaseException as exc:  # noqa: BLE001
            if not is_model_invalid_error(exc):
                raise
            last_exc = exc
            tag = f" [{label}]" if label else ""
            log.warning(
                "model%s tier=%s candidate %d/%d (%s) rejected as invalid; "
                "falling back: %s",
                tag, tier, idx + 1, len(chain), model, exc,
            )
    assert last_exc is not None
    raise last_exc
