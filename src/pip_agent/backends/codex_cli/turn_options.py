"""Per-turn Codex SDK option helpers."""

from __future__ import annotations

from typing import Any

from pip_agent.runtime_mode import normalized_agent_mode


def ensure_experimental_api(client: Any) -> None:
    """Open Codex's app-server connection with experimental API enabled.

    ``collaboration_mode`` is an experimental turn field in the current
    codex-python SDK. The high-level SDK only exposes this capability
    implicitly, so Pip-Boy opts in when creating Codex clients.
    """
    ensure_client = getattr(client, "_ensure_client", None)
    if callable(ensure_client):
        ensure_client(require_experimental=True)


def build_turn_options(
    *,
    model: str | None,
    developer_instructions: str,
    effort: Any | None,
) -> Any | None:
    """Build ``codex.TurnOptions`` for the current runtime settings."""
    from codex import TurnOptions
    from codex.protocol import types as proto

    from pip_agent.config import settings

    kwargs: dict[str, Any] = {}
    if effort is not None:
        kwargs["effort"] = effort

    mode = normalized_agent_mode(settings.agent_mode)
    if mode == "plan":
        kwargs["collaboration_mode"] = proto.CollaborationMode(
            mode=proto.ModeKind(root="plan"),
            settings=proto.Settings(
                # Let Codex apply its built-in Plan Mode instruction pack.
                # Pip-Boy's persona/rules still ride on the thread-level
                # developer_instructions passed at start/resume.
                developer_instructions=None,
                model=model or "",
                reasoning_effort=effort,
            ),
        )

    return TurnOptions(**kwargs) if kwargs else None

