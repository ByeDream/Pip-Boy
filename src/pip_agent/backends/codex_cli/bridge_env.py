"""Shared helpers for the Codex backend: credential resolution and
environment propagation.

Both the one-shot runner and the persistent streaming session need
these — centralised here to avoid duplication.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_RELAY_PROVIDER_ID = "pip-relay"


def _config_toml_env_keys() -> list[str]:
    """Read ``env_key`` values from ``~/.codex/config.toml`` model_providers.

    Returns the env var names that the Codex app-server will look for
    when resolving API keys through its global config.  This lets
    ``build_bridge_env`` inject the Pip-Boy-resolved key under whatever
    alias the operator's global config expects, keeping Pip-Boy fully
    decoupled from the specific provider setup.
    """
    try:
        config_path = Path.home() / ".codex" / "config.toml"
        if not config_path.is_file():
            return []
        import tomllib
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        keys: list[str] = []
        for _name, provider in (data.get("model_providers") or {}).items():
            ek = (provider.get("env_key") or "").strip()
            if ek:
                keys.append(ek)
        return keys
    except Exception:  # noqa: BLE001
        return []


def resolve_codex_credentials() -> tuple[str | None, str | None]:
    """Return ``(api_key, base_url)`` for the Codex backend.

    Resolution order (first non-empty wins):

    * ``Settings.codex_api_key`` / ``Settings.codex_base_url``
      (from ``.env`` or env var ``CODEX_API_KEY`` / ``CODEX_BASE_URL``)
    * ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` env vars
    * ``(None, None)`` — SDK falls back to ``~/.codex/config.toml``

    This is the single place where Codex credentials are resolved;
    both ``runner.py`` and ``streaming.py`` call it.
    """
    from pip_agent.config import settings

    api_key = (
        settings.codex_api_key
        or os.environ.get("CODEX_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or None
    )
    base_url = (
        settings.codex_base_url
        or os.environ.get("CODEX_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or None
    )
    return api_key, base_url


def build_codex_config_override(
    base_url: str | None,
    api_key: str | None,
) -> Any:
    """Build a Codex config override for Pip-Boy's relay.

    The Codex app-server routes LLM requests through ``model_providers``;
    ``OPENAI_BASE_URL`` alone is not sufficient.  We inject this config
    via SDK process options so Pip-Boy can use its own relay/key without
    modifying the user's global ``~/.codex/config.toml``.
    """
    if not base_url:
        return None
    try:
        from codex._config_types import CodexConfig

        env_key = "CODEX_API_KEY" if api_key else "OPENAI_API_KEY"
        return CodexConfig(**{
            "model_provider": _RELAY_PROVIDER_ID,
            "model_providers": {
                _RELAY_PROVIDER_ID: {
                    "name": _RELAY_PROVIDER_ID,
                    "base_url": base_url,
                    "env_key": env_key,
                },
            },
        })
    except Exception:  # noqa: BLE001
        return None


def build_bridge_env(
    *,
    mcp_ctx: Any = None,
    session_id: str = "",
    sender_id: str = "",
    peer_id: str = "",
    user_id: str = "",
    account_id: str = "",
) -> dict[str, str]:
    """Build the environment dict for ``CodexOptions(env=...)``.

    Starts from ``os.environ`` (so the app-server keeps PATH, TEMP,
    etc.) and **force-overwrites** credential env vars with Pip-Boy's
    resolved values.  This ensures the spawned app-server subprocess
    always uses Pip-Boy's own API key and base URL, even when the
    global Codex app (``~/.codex/config.toml``) or the parent process
    environment points to a different relay / key.  When Pip-Boy has
    no ``base_url`` configured, any inherited ``OPENAI_BASE_URL`` is
    removed so the app-server falls back to its own default.

    **Credential aliasing**: the Codex app-server resolves API keys
    through ``~/.codex/config.toml``'s ``model_providers.*.env_key``
    field.  We inject the resolved key under every alias the global
    config declares so the app-server finds it regardless of which
    ``env_key`` the operator's config uses.
    """
    env: dict[str, str] = dict(os.environ)

    api_key, base_url = resolve_codex_credentials()
    if api_key:
        env["OPENAI_API_KEY"] = api_key
        env["CODEX_API_KEY"] = api_key
        for extra in _config_toml_env_keys():
            env[extra] = api_key
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
    elif "OPENAI_BASE_URL" in env:
        del env["OPENAI_BASE_URL"]

    if session_id:
        env["PIP_SESSION_ID"] = session_id

    if mcp_ctx is not None:
        for attr, key in (
            ("sender_id", "PIP_SENDER_ID"),
            ("peer_id", "PIP_PEER_ID"),
            ("user_id", "PIP_USER_ID"),
            ("account_id", "PIP_ACCOUNT_ID"),
        ):
            val = getattr(mcp_ctx, attr, "") or ""
            if val:
                env[key] = val
    else:
        if sender_id:
            env["PIP_SENDER_ID"] = sender_id
        if peer_id:
            env["PIP_PEER_ID"] = peer_id
        if user_id:
            env["PIP_USER_ID"] = user_id
        if account_id:
            env["PIP_ACCOUNT_ID"] = account_id

    return env
