"""Workspace-level host preferences (read/write).

This module owns ``<workspace>/.pip/host_state.json``. It is the
counterpart to the per-agent ``<agent_dir>/state.json`` written by
:class:`pip_agent.memory.MemoryStore` — *that* file belongs to the
agent's memory pipeline (``last_reflect_at``, dream offsets, ...) and
is rewritten wholesale by ``save_state``. Co-mingling host-level
preferences (theme slug, future TUI prefs) with that file would race
the memory writer, so we keep them in a sibling file.

Schema (v1):

.. code-block:: json

    {
        "tui": {
            "theme": "wasteland"
        }
    }

The schema is intentionally hand-rolled: a single nested string field,
read at host boot and written by ``/theme set``. No migration logic
is required; missing keys read as their defaults, and unknown keys
are preserved on rewrite (forward-compat for Phase C / later versions
that want to add doctor opt-outs, log-level overrides, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pip_agent.fileutil import atomic_write

log = logging.getLogger(__name__)

__all__ = [
    "HOST_STATE_FILENAME",
    "TUI_THEME_ENV_VAR",
    "HostState",
    "load_host_state",
    "resolve_active_theme_name",
]


HOST_STATE_FILENAME: str = "host_state.json"
"""Filename relative to ``<workspace>/.pip/``.

Public so ``pip-boy doctor`` (Phase C) can render the resolved path
even when the file does not exist yet."""

TUI_THEME_ENV_VAR: str = "PIP_TUI_THEME"
"""Operator override for the active theme slug.

Resolution chain (highest precedence first): env var → host_state →
default. Env wins so an operator can flip themes for a single boot
without rewriting state."""


class HostState:
    """Thin reader/writer over the host-level state JSON file.

    Thread-safe: the underlying ``atomic_write`` is process-atomic and
    the in-process lock guards read-modify-write sequences inside the
    same process. Cross-process safety is not a goal in v1; only one
    pip-boy host runs against a given workspace at a time.
    """

    def __init__(self, *, workspace_pip_dir: Path) -> None:
        self._dir = workspace_pip_dir
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Resolved file path, regardless of whether the file exists."""
        return self._dir / HOST_STATE_FILENAME

    def load(self) -> dict[str, Any]:
        """Return the on-disk payload, or ``{}`` when missing/corrupt.

        A corrupt file is tolerated (logged at WARNING) rather than
        raising — losing host prefs is annoying but should never
        wedge boot. The next successful ``save`` rewrites the file.
        """
        path = self.path
        if not path.is_file():
            return {}
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "Failed to read host_state at %s: %s — using defaults.",
                path, exc,
            )
            return {}
        if not isinstance(blob, dict):
            log.warning(
                "host_state at %s is not a JSON object; ignoring.", path,
            )
            return {}
        return blob

    def save(self, payload: dict[str, Any]) -> None:
        """Atomically replace the file with ``payload``.

        ``payload`` is written verbatim (with stable indentation for
        humans). Callers are expected to read-modify-write to avoid
        clobbering unrelated keys; see :meth:`set_theme` for the
        canonical pattern.
        """
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write(
                self.path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )

    def get_theme(self) -> str | None:
        """Return the persisted theme slug, or ``None`` when unset."""
        blob = self.load()
        tui = blob.get("tui")
        if not isinstance(tui, dict):
            return None
        theme = tui.get("theme")
        if not isinstance(theme, str) or not theme.strip():
            return None
        return theme.strip()

    def set_theme(self, name: str) -> None:
        """Persist ``name`` as the active theme.

        Read-modify-write so we don't drop unrelated keys an older
        host wrote (or a newer one will write).
        """
        with self._lock:
            blob = self.load()
            tui = blob.get("tui")
            if not isinstance(tui, dict):
                tui = {}
                blob["tui"] = tui
            tui["theme"] = name
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write(
                self.path,
                json.dumps(blob, ensure_ascii=False, indent=2),
            )


def load_host_state(workspace_pip_dir: Path) -> HostState:
    """Construct a :class:`HostState` rooted at ``workspace_pip_dir``."""
    return HostState(workspace_pip_dir=workspace_pip_dir)


def resolve_active_theme_name(
    *,
    state: HostState | None,
    env: dict[str, str] | None = None,
    default: str = "wasteland",
) -> str:
    """Apply the precedence chain and return the active theme slug.

    Precedence (highest first):

    1. ``PIP_TUI_THEME`` environment variable, when non-empty.
    2. ``state.json -> tui.theme``.
    3. ``default``.

    The function is pure given its inputs (``env`` defaults to
    :data:`os.environ`) so unit tests can drive each branch
    deterministically.
    """
    if env is None:
        env = os.environ  # type: ignore[assignment]
    override = (env.get(TUI_THEME_ENV_VAR) or "").strip()
    if override:
        return override
    if state is not None:
        persisted = state.get_theme()
        if persisted:
            return persisted
    return default
