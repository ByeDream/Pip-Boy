from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from pip_agent.scaffold import (
    _MANIFEST_NAME,
    _SCAFFOLD_DIR,
    ensure_workspace,
)


def test_fresh_init(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    assert (tmp_path / ".pip").is_dir()
    assert (tmp_path / ".pip" / "team").is_dir()
    assert (tmp_path / ".pip" / "skills").is_dir()
    assert (tmp_path / ".pip" / "memory" / "pip-boy").is_dir()

    assert not (tmp_path / "AGENTS.md").exists()

    models = tmp_path / ".pip" / "models.json"
    assert models.exists()
    data = json.loads(models.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert any(m["id"] == "claude-sonnet-4-6" for m in data)

    assert (tmp_path / ".env").exists()
    assert (tmp_path / ".pip" / "user.md").exists()

    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".pip/" in lines

    manifest_path = tmp_path / ".pip" / _MANIFEST_NAME
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "version" in manifest
    assert ".pip/models.json" in manifest["files"]


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


def test_existing_agents_md_untouched(tmp_path: Path) -> None:
    """If the user has their own AGENTS.md, scaffold should not touch it."""
    (tmp_path / ".git").mkdir()
    custom = "# My Project\n\nSome custom content.\n"
    (tmp_path / "AGENTS.md").write_text(custom, encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text == custom


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


def test_scaffold_migration_updates_unmodified(tmp_path: Path) -> None:
    """If upstream template changed and user hasn't modified the file, auto-update."""
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    manifest_path = tmp_path / ".pip" / _MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_hash = manifest["files"][".pip/models.json"]["scaffold_hash"]

    models_path = tmp_path / ".pip" / "models.json"
    original_content = models_path.read_text(encoding="utf-8")

    ensure_workspace(tmp_path)
    assert models_path.read_text(encoding="utf-8") == original_content


def test_scaffold_migration_skips_modified(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """If user modified a scaffold file, don't overwrite on migration."""
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    user_md = tmp_path / ".pip" / "user.md"
    user_md.write_text("# Custom user profile\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pip_agent.scaffold"):
        ensure_workspace(tmp_path)

    assert user_md.read_text(encoding="utf-8") == "# Custom user profile\n"


def test_no_git_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pip_agent.scaffold"):
        ensure_workspace(tmp_path)

    assert any("Not a git repository" in r.message for r in caplog.records)
    assert (tmp_path / ".pip").is_dir()
