"""Backend-agnostic LLM client for background tasks (reflect, consolidate, axioms).

The main agent turn runs through the configured backend (Codex CLI or Claude
Code).  Background memory-pipeline tasks need a *synchronous* LLM call that
works with whichever credentials are actually configured.

``build_background_client()`` returns a duck-typed client whose
``messages.create(model, system, messages, max_tokens)`` matches the subset
of the ``anthropic.Anthropic`` interface used by reflect / consolidate /
axioms.  When ``settings.backend == "codex_cli"`` and Codex credentials
exist, it returns an :class:`OpenAICompatClient` that talks to the
OpenAI-compatible ``/v1/chat/completions`` endpoint via ``httpx``.
Otherwise it falls through to the Anthropic SDK client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response adapter — duck-types ``anthropic.types.Message``
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class LLMResponse:
    """Minimal stand-in for ``anthropic.types.Message``."""

    content: list[TextBlock] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OpenAI-compatible client
# ---------------------------------------------------------------------------


class _Messages:
    """Namespace that mirrors ``anthropic.Anthropic().messages``."""

    _RETRIES = 3

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def create(
        self,
        *,
        model: str,
        max_tokens: int = 1024,
        system: str | list[Any] = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        import httpx

        oai_messages: list[dict[str, str]] = []
        if system:
            sys_text = system if isinstance(system, str) else str(system)
            oai_messages.append({"role": "system", "content": sys_text})
        if messages:
            oai_messages.extend(messages)

        url = f"{self._base_url}/v1/chat/completions"
        transport = httpx.HTTPTransport(retries=self._RETRIES)
        with httpx.Client(transport=transport, timeout=self._timeout) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": oai_messages,
                    "max_tokens": max_tokens,
                },
            )
        resp.raise_for_status()
        data = resp.json()

        text = ""
        choices = data.get("choices") or []
        if choices:
            text = choices[0].get("message", {}).get("content", "")
        return LLMResponse(content=[TextBlock(text=text)])


class OpenAICompatClient:
    """Drop-in replacement for ``anthropic.Anthropic`` using the OpenAI chat
    completions API via ``httpx``."""

    def __init__(self, *, base_url: str, api_key: str, timeout: float = 300) -> None:
        self.messages = _Messages(base_url, api_key, timeout)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_background_client() -> Any:
    """Return a client suitable for background LLM calls, or ``None``.

    Resolution order:

    1. When ``settings.backend == "codex_cli"`` AND Codex credentials
       are configured, return :class:`OpenAICompatClient`.
    2. When Anthropic credentials are configured, return an
       ``anthropic.Anthropic`` instance via :func:`build_anthropic_client`.
    3. ``None`` — callers skip the LLM call gracefully.
    """
    from pip_agent.config import settings

    if settings.backend == "codex_cli":
        try:
            from pip_agent.backends.codex_cli.bridge_env import (
                resolve_codex_credentials,
            )

            api_key, base_url = resolve_codex_credentials()
            if api_key and base_url:
                log.debug(
                    "background LLM: using OpenAI-compat client (%s)",
                    base_url,
                )
                return OpenAICompatClient(base_url=base_url, api_key=api_key)
        except Exception:  # noqa: BLE001
            log.debug("background LLM: Codex credential resolution failed", exc_info=True)

    from pip_agent.anthropic_client import build_anthropic_client

    return build_anthropic_client()
