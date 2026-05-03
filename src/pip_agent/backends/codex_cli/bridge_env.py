"""Build the environment dict passed to ``CodexOptions(env=...)```.

Both the one-shot runner and the persistent streaming session need to
propagate identity/workdir context to the Codex app-server (and its
child MCP bridge processes).  This module centralises that logic.
"""

from __future__ import annotations

import os
from typing import Any


def build_bridge_env(
    *,
    mcp_ctx: Any = None,
    session_id: str = "",
    sender_id: str = "",
    peer_id: str = "",
    user_id: str = "",
    account_id: str = "",
) -> dict[str, str]:
    """Return env vars for ``CodexOptions`` so MCP bridge gets context.

    The Codex app-server inherits these env vars and propagates them to
    child MCP server processes (the STDIO bridge).  This is the only
    reliable way to pass live host context to the bridge —
    ``os.environ`` mutations after the app-server starts do not
    propagate.
    """
    env: dict[str, str] = {}

    workdir = os.environ.get("PIP_WORKDIR", "")
    if workdir:
        env["PIP_WORKDIR"] = workdir
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
