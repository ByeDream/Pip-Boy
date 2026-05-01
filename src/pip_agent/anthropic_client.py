"""Single source of truth for Anthropic credential resolution + SDK clients.

Any Pip-Boy code path that talks to Anthropic — either by spawning the Claude
Code CLI subprocess (``agent_runner._build_env``) or by calling the Python
SDK directly (``memory/reflect.py``, future ``dream`` / ``consolidate`` /
any-other standalone LLM call) — MUST go through the helpers in this module
so that the proxy rule stays consistent across all entry points.

Pip-Boy exposes a single user-facing credential variable, ``ANTHROPIC_API_KEY``.
The auth header it goes out as is decided here, transparently:

* ``ANTHROPIC_BASE_URL`` set → proxy mode. The key is sent as
  ``Authorization: Bearer <token>`` (the de-facto standard most self-hosted
  LLM gateways — OneAPI, claude-relay, corporate middlewares — accept).
* Otherwise → direct to ``api.anthropic.com`` with ``x-api-key``.

Credential lookup order for each variable: ``settings.*`` (pydantic-settings,
loaded from ``.env``) → ``os.environ``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the heavy SDK at module load
    import anthropic

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnthropicCredential:
    """Resolved credential + destination for any Anthropic call."""

    token: str
    """The key/token string, irrespective of which env var name it came from."""

    bearer: bool
    """True → ``Authorization: Bearer``; False → ``x-api-key``."""

    base_url: str
    """Empty string means direct to ``api.anthropic.com``."""


def resolve_anthropic_credential() -> AnthropicCredential | None:
    """Resolve the Anthropic credential once, using the shared proxy rule.

    Returns ``None`` when no credential is configured — callers should treat
    this as "LLM access unavailable, skip the call gracefully" rather than
    a hard error.
    """
    # Lazy import to avoid circular deps during config bootstrap.
    from pip_agent.config import settings

    api_key = (
        settings.anthropic_api_key
        or os.getenv("ANTHROPIC_API_KEY")
        or ""
    )
    base_url = (
        settings.anthropic_base_url
        or os.getenv("ANTHROPIC_BASE_URL")
        or ""
    )

    if not api_key:
        return None
    # Proxy rule: base_url presence → bearer; otherwise x-api-key.
    return AnthropicCredential(token=api_key, bearer=bool(base_url), base_url=base_url)


def build_anthropic_client() -> "anthropic.Anthropic | None":
    """Build a direct-SDK Anthropic client, or ``None`` if unconfigured.

    Every call site that instantiates ``anthropic.Anthropic`` directly (reflect,
    dream/consolidate, axioms, any future standalone LLM utility) MUST call
    this function instead of constructing the client themselves — otherwise
    the proxy rule can drift and proxy deployments will silently break.
    """
    import anthropic  # heavy import; kept lazy so config-only consumers stay light

    cred = resolve_anthropic_credential()
    if cred is None:
        log.info(
            "anthropic: no ANTHROPIC_API_KEY configured; direct-SDK call skipped",
        )
        return None
    try:
        kwargs: dict[str, str] = {}
        if cred.bearer:
            kwargs["auth_token"] = cred.token
        else:
            kwargs["api_key"] = cred.token
        if cred.base_url:
            kwargs["base_url"] = cred.base_url
        return anthropic.Anthropic(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("anthropic: cannot build client: %s", exc)
        return None
