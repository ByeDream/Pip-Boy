from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from pip_agent.scaffold import (
    _MANIFEST_NAME,
    ensure_workspace,
)


def test_fresh_init(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    assert (tmp_path / ".pip").is_dir()
    assert (tmp_path / ".pip" / "skills").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy" / "observations").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy" / "users").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy" / "transcripts").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy" / "tasks").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy" / "team").is_dir()
    assert (tmp_path / ".pip" / "agents" / "pip-boy" / "team" / "inbox").is_dir()

    assert not (tmp_path / "AGENTS.md").exists()

    models = tmp_path / ".pip" / "models.json"
    assert models.exists()
    data = json.loads(models.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert any(m["id"] == "claude-sonnet-4-6" for m in data)

    assert (tmp_path / ".env").exists()
    assert (tmp_path / ".pip" / "owner.md").exists()

    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".pip/" in lines

    manifest_path = tmp_path / ".pip" / _MANIFEST_NAME
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "version" in manifest
    assert ".pip/models.json" in manifest["files"]
    assert ".pip/agents/pip-boy/persona.md" in manifest["files"]


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
    manifest["files"][".pip/models.json"]["scaffold_hash"]

    models_path = tmp_path / ".pip" / "models.json"
    original_content = models_path.read_text(encoding="utf-8")

    ensure_workspace(tmp_path)
    assert models_path.read_text(encoding="utf-8") == original_content


def test_scaffold_migration_skips_modified(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """If user modified a scaffold file, don't overwrite on migration."""
    (tmp_path / ".git").mkdir()
    ensure_workspace(tmp_path)

    owner_md = tmp_path / ".pip" / "owner.md"
    owner_md.write_text("# Custom owner profile\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pip_agent.scaffold"):
        ensure_workspace(tmp_path)

    assert owner_md.read_text(encoding="utf-8") == "# Custom owner profile\n"


def test_no_git_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pip_agent.scaffold"):
        ensure_workspace(tmp_path)

    assert any("Not a git repository" in r.message for r in caplog.records)
    assert (tmp_path / ".pip").is_dir()


def test_legacy_flat_persona_preserved_on_upgrade(tmp_path: Path) -> None:
    """When upgrading from old flat layout, the user's customised pip-boy.md
    content must end up in agents/pip-boy/persona.md — NOT overwritten by the
    new scaffold template."""
    (tmp_path / ".git").mkdir()
    pip = tmp_path / ".pip"
    agents = pip / "agents"
    agents.mkdir(parents=True)

    custom_persona = (
        "---\nname: Pip-Boy\nmodel: claude-opus-4-6\nmax_tokens: 4096\n---\n"
        "Custom system prompt from user.\n"
    )
    (agents / "pip-boy.md").write_text(custom_persona, encoding="utf-8")

    old_manifest = {
        "version": "0.1.2",
        "files": {
            ".pip/agents/pip-boy.md": {
                "scaffold_hash": "old_hash_not_matching_anything",
                "installed_version": "0.1.2",
            },
        },
    }
    pip.mkdir(exist_ok=True)
    (pip / _MANIFEST_NAME).write_text(
        json.dumps(old_manifest, indent=2), encoding="utf-8",
    )

    ensure_workspace(tmp_path)

    persona_path = agents / "pip-boy" / "persona.md"
    assert persona_path.exists()
    content = persona_path.read_text(encoding="utf-8")
    assert "Custom system prompt from user." in content
    assert "max_tokens: 4096" in content

    manifest = json.loads(
        (pip / _MANIFEST_NAME).read_text(encoding="utf-8"),
    )
    assert ".pip/agents/pip-boy/persona.md" in manifest["files"]
    assert ".pip/agents/pip-boy.md" not in manifest["files"]


def test_legacy_memory_migrated_to_agents(tmp_path: Path) -> None:
    """Legacy .pip/memory/<id>/ data should be copied into .pip/agents/<id>/."""
    (tmp_path / ".git").mkdir()
    pip = tmp_path / ".pip"

    obs_dir = pip / "memory" / "pip-boy" / "observations"
    obs_dir.mkdir(parents=True)
    (pip / "memory" / "pip-boy" / "state.json").write_text(
        '{"last_reflect_at": 100}', encoding="utf-8",
    )
    (obs_dir / "2026-01-01.jsonl").write_text(
        '{"text": "test observation"}\n', encoding="utf-8",
    )

    ensure_workspace(tmp_path)

    agent_dir = pip / "agents" / "pip-boy"
    assert (agent_dir / "state.json").exists()
    assert '{"last_reflect_at": 100}' in (agent_dir / "state.json").read_text(encoding="utf-8")
    assert (agent_dir / "observations" / "2026-01-01.jsonl").exists()


def test_legacy_users_tasks_team_transcripts_migrated(tmp_path: Path) -> None:
    """Legacy top-level .pip/users/, tasks/, team/, transcripts/ should migrate
    into the default agent's directory."""
    (tmp_path / ".git").mkdir()
    pip = tmp_path / ".pip"

    (pip / "users").mkdir(parents=True)
    (pip / "users" / "alice.md").write_text("# Alice\n", encoding="utf-8")

    (pip / "tasks").mkdir(parents=True)
    (pip / "tasks" / "plan.json").write_text("{}", encoding="utf-8")

    (pip / "team").mkdir(parents=True)
    (pip / "team" / "helper.md").write_text("---\nname: helper\n---\n", encoding="utf-8")

    (pip / "transcripts").mkdir(parents=True)
    (pip / "transcripts" / "1700000000.json").write_text("[]", encoding="utf-8")

    ensure_workspace(tmp_path)

    agent_dir = pip / "agents" / "pip-boy"
    assert (agent_dir / "users" / "alice.md").exists()
    assert (agent_dir / "tasks" / "plan.json").exists()
    assert (agent_dir / "team" / "helper.md").exists()
    assert (agent_dir / "transcripts" / "1700000000.json").exists()
