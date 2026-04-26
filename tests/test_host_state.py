"""Tests for ``pip_agent.host_state`` — workspace-level prefs persistence.

The module owns the ``<workspace>/.pip/host_state.json`` file. The
file is intentionally separate from the per-agent ``state.json``
that :class:`pip_agent.memory.MemoryStore` rewrites — these tests
keep the two surfaces from drifting back together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pip_agent.host_state import (
    HOST_STATE_FILENAME,
    TUI_THEME_ENV_VAR,
    HostState,
    load_host_state,
    resolve_active_theme_name,
)


@pytest.fixture
def workspace_pip_dir(tmp_path: Path) -> Path:
    pip_dir = tmp_path / ".pip"
    pip_dir.mkdir()
    return pip_dir


# ---------------------------------------------------------------------------
# HostState read / write
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_file_missing(workspace_pip_dir: Path) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    assert state.load() == {}


def test_load_warns_on_corrupt_file(
    workspace_pip_dir: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    (workspace_pip_dir / HOST_STATE_FILENAME).write_text(
        "this isn't json", encoding="utf-8",
    )
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    with caplog.at_level("WARNING", logger="pip_agent.host_state"):
        result = state.load()
    assert result == {}
    assert any("host_state" in rec.getMessage() for rec in caplog.records)


def test_load_warns_when_payload_is_not_object(
    workspace_pip_dir: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    (workspace_pip_dir / HOST_STATE_FILENAME).write_text(
        json.dumps([1, 2, 3]), encoding="utf-8",
    )
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    with caplog.at_level("WARNING", logger="pip_agent.host_state"):
        assert state.load() == {}


def test_set_theme_writes_under_tui_key(workspace_pip_dir: Path) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    state.set_theme("vault-amber")

    blob = json.loads(state.path.read_text(encoding="utf-8"))
    assert blob == {"tui": {"theme": "vault-amber"}}


def test_set_theme_preserves_unrelated_keys(
    workspace_pip_dir: Path,
) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    # Forward-compat: a future host wrote a key we don't own; we
    # must not drop it on a read-modify-write cycle.
    state.save({"tui": {"theme": "wasteland"}, "future_key": {"x": 1}})

    state.set_theme("vault-amber")

    blob = json.loads(state.path.read_text(encoding="utf-8"))
    assert blob["future_key"] == {"x": 1}
    assert blob["tui"]["theme"] == "vault-amber"


def test_get_theme_returns_none_when_absent(workspace_pip_dir: Path) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    assert state.get_theme() is None


def test_get_theme_handles_missing_tui_section(
    workspace_pip_dir: Path,
) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    state.save({"other": True})
    assert state.get_theme() is None


def test_get_theme_strips_whitespace(workspace_pip_dir: Path) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    state.save({"tui": {"theme": "  wasteland  "}})
    assert state.get_theme() == "wasteland"


def test_load_host_state_factory(workspace_pip_dir: Path) -> None:
    state = load_host_state(workspace_pip_dir)
    assert isinstance(state, HostState)
    assert state.path == workspace_pip_dir / HOST_STATE_FILENAME


# ---------------------------------------------------------------------------
# resolve_active_theme_name precedence chain
# ---------------------------------------------------------------------------


def test_resolve_uses_default_when_nothing_set(
    workspace_pip_dir: Path,
) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    assert (
        resolve_active_theme_name(state=state, env={}, default="wasteland")
        == "wasteland"
    )


def test_resolve_prefers_state_over_default(
    workspace_pip_dir: Path,
) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    state.set_theme("vault-amber")

    assert (
        resolve_active_theme_name(state=state, env={}, default="wasteland")
        == "vault-amber"
    )


def test_resolve_prefers_env_over_state(workspace_pip_dir: Path) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    state.set_theme("vault-amber")

    env = {TUI_THEME_ENV_VAR: "wasteland"}
    assert (
        resolve_active_theme_name(state=state, env=env, default="other")
        == "wasteland"
    )


def test_resolve_ignores_blank_env_override(workspace_pip_dir: Path) -> None:
    state = HostState(workspace_pip_dir=workspace_pip_dir)
    state.set_theme("vault-amber")

    env = {TUI_THEME_ENV_VAR: "   "}
    assert (
        resolve_active_theme_name(state=state, env=env, default="other")
        == "vault-amber"
    )


def test_resolve_works_without_state_object() -> None:
    env = {TUI_THEME_ENV_VAR: "vault-amber"}
    assert (
        resolve_active_theme_name(state=None, env=env, default="wasteland")
        == "vault-amber"
    )
