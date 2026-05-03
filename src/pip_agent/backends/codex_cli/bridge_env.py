"""Shared helpers for the Codex backend: credential resolution and
environment propagation.

Both the one-shot runner and the persistent streaming session need
these — centralised here to avoid duplication.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


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
    etc.) and layers Pip-Boy context on top.

    **Credential aliasing**: the Codex app-server resolves API keys
    through ``~/.codex/config.toml``'s ``model_providers.*.env_key``
    field. When Pip-Boy's resolved ``codex_api_key`` differs from the
    key name the global config expects (e.g. ``TUZ_API_KEY``), the
    app-server would fail with "Missing environment variable". We
    inject the resolved key under every common alias so the
    app-server finds it regardless of which env_key the operator's
    global config happens to use.
    """
    env: dict[str, str] = dict(os.environ)

    api_key, _ = resolve_codex_credentials()
    if api_key:
        env.setdefault("OPENAI_API_KEY", api_key)
        env.setdefault("CODEX_API_KEY", api_key)
        for extra in _config_toml_env_keys():
            env.setdefault(extra, api_key)

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
