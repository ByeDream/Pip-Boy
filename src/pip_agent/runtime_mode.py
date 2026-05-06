"""Shared runtime mode helpers for agent backends."""

from __future__ import annotations

VALID_AGENT_MODES = {"default", "plan"}


def normalized_agent_mode(raw: str | None) -> str:
    """Return the safe user-facing agent mode."""
    value = (raw or "").strip().lower()
    return value if value in VALID_AGENT_MODES else "default"


def claude_permission_mode(raw: str | None) -> str:
    """Map Pip-Boy's mode names to Claude Code permission modes."""
    mode = normalized_agent_mode(raw)
    return "plan" if mode == "plan" else "bypassPermissions"
