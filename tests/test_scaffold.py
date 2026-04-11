from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from pip_agent.scaffold import (
    _SCAFFOLD_DIR,
    _SENTINEL,
    _SENTINEL_END,
    ensure_workspace,
)


def test_fresh_init(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    assert (tmp_path / ".pip").is_dir()
    assert (tmp_path / ".pip" / "team").is_dir()
    assert (tmp_path / ".pip" / "skills").is_dir()

    agents = tmp_path / "AGENTS.md"
    assert agents.exists()
    text = agents.read_text(encoding="utf-8")
    assert _SENTINEL in text
    assert _SENTINEL_END in text

    models = tmp_path / ".pip" / "models.json"
    assert models.exists()
    data = json.loads(models.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert any(m["id"] == "claude-sonnet-4-6" for m in data)

    assert (tmp_path / ".env").exists()

    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".pip/" in lines


def test_idempotent(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    snapshots: dict[str, str] = {}
    for f in tmp_path.rglob("*"):
        if f.is_file():
            snapshots[str(f.relative_to(tmp_path))] = f.read_text(encoding="utf-8")

    ensure_workspace(tmp_path)

    for rel, content in snapshots.items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == content, (
            f"File changed on second run: {rel}"
        )


def test_agents_md_append(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    custom = "# My Project\n\nSome custom content.\n"
    (tmp_path / "AGENTS.md").write_text(custom, encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# My Project")
    assert _SENTINEL in text
    assert _SENTINEL_END in text
    assert "Some custom content." in text


def test_agents_md_no_duplicate(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    template = (_SCAFFOLD_DIR / "agents.md").read_text(encoding="utf-8")
    existing = f"# Existing\n\n{_SENTINEL}\n{template}\n{_SENTINEL_END}\n"
    (tmp_path / "AGENTS.md").write_text(existing, encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text == existing


def test_gitignore_merge(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("node_modules/\n.pip/\n", encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.count(".pip/") == 1
    assert "node_modules/" in lines
    assert ".env" in lines


def test_gitignore_create(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert not (tmp_path / ".gitignore").exists()

    ensure_workspace(tmp_path)

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".pip/" in text


def test_env_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n", encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-secret" in text


def test_no_git_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pip_agent.scaffold"):
        ensure_workspace(tmp_path)

    assert any("Not a git repository" in r.message for r in caplog.records)
    assert (tmp_path / ".pip").is_dir()
