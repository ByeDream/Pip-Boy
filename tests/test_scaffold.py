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
    ensure_workspace(tmp_path)

    # v2 layout: pip-boy's state lives at .pip/ (no more nested agents/<id>/).
    assert (tmp_path / ".pip").is_dir()
    assert (tmp_path / ".pip" / "persona.md").exists()
    assert (tmp_path / ".pip" / "observations").is_dir()
    # Addressbook replaces the per-agent ``users/`` directory: one
    # shared contact list under the workspace root, no ``users/``
    # anywhere. Same for ``owner.md`` — there is no owner concept.
    assert (tmp_path / ".pip" / "addressbook").is_dir()
    assert not (tmp_path / ".pip" / "users").exists()
    assert not (tmp_path / ".pip" / "owner.md").exists()
    assert (tmp_path / ".pip" / "incoming").is_dir()
    assert (tmp_path / ".pip" / "credentials").is_dir()
    # Phase B (TUI Themes): a placeholder ``.pip/themes/`` directory
    # plus an authoring README is seeded so users can drop a custom
    # theme without learning the package internals.
    assert (tmp_path / ".pip" / "themes").is_dir()
    assert (tmp_path / ".pip" / "themes" / "README.md").is_file()
    # Phase 4.5: transcripts now live under ~/.claude/projects/ (CC native),
    # so Pip no longer creates its own ``transcripts/`` directory.
    assert not (tmp_path / ".pip" / "transcripts").exists()

    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".pip" / "models.json").exists()
    assert not (tmp_path / ".pip" / "keys.json").exists()

    # No flat agents/<id>/ subtree under v2.
    assert not (tmp_path / ".pip" / "agents").exists()

    assert (tmp_path / ".env").exists()

    # The registry file is always seeded with the root agent.
    registry = tmp_path / ".pip" / "agents_registry.json"
    assert registry.is_file()
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert "pip-boy" in data.get("agents", {})
    assert data["agents"]["pip-boy"]["kind"] == "root"

    # Scaffold no longer touches .gitignore — that's the host workspace's
    # responsibility, not pip-boy's.
    assert not (tmp_path / ".gitignore").exists()

    manifest_path = tmp_path / ".pip" / _MANIFEST_NAME
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "version" in manifest
    assert ".pip/persona.md" in manifest["files"]


def test_idempotent(tmp_path: Path) -> None:
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
    custom = "# My Project\n\nSome custom content.\n"
    (tmp_path / "AGENTS.md").write_text(custom, encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text == custom


def test_existing_gitignore_untouched(tmp_path: Path) -> None:
    """Scaffold must NOT touch .gitignore — that's the workspace owner's call.

    Pip-Boy used to seed ``.pip/`` / ``.env`` entries here, but ignore
    rules belong to the host project, not the agent. We keep the
    invariant pinned so a regression doesn't re-introduce the side
    effect.
    """
    existing = "node_modules/\n"
    (tmp_path / ".gitignore").write_text(existing, encoding="utf-8")

    ensure_workspace(tmp_path)

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == existing


def test_env_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n", encoding="utf-8")

    ensure_workspace(tmp_path)

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-secret" in text


def test_scaffold_migration_skips_modified(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """If user modified a scaffold file, don't overwrite on migration."""
    ensure_workspace(tmp_path)

    persona = tmp_path / ".pip" / "persona.md"
    persona.write_text("# Custom persona\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pip_agent.scaffold"):
        ensure_workspace(tmp_path)

    assert persona.read_text(encoding="utf-8") == "# Custom persona\n"


# ---------------------------------------------------------------------------
# Theme seeding (Change 1): wheel seeds → workspace; deletion is respected.
# ---------------------------------------------------------------------------


def _seeded_theme_slugs() -> set[str]:
    from pip_agent.tui.themes import BUILTIN_THEMES_DIR

    return {
        child.name
        for child in BUILTIN_THEMES_DIR.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and child.name != "__pycache__"
    }


def test_seed_themes_copied_on_first_boot(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    themes_root = tmp_path / ".pip" / "themes"
    for slug in _seeded_theme_slugs():
        assert (themes_root / slug / "theme.toml").is_file(), (
            f"seed theme '{slug}' not copied to {themes_root}"
        )

    manifest = json.loads(
        (tmp_path / ".pip" / _MANIFEST_NAME).read_text(encoding="utf-8")
    )
    for slug in _seeded_theme_slugs():
        assert manifest["themes"][f".pip/themes/{slug}/"]["installed_once"] is True


def test_deleted_seed_theme_is_not_re_created(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    slug = next(iter(_seeded_theme_slugs()))
    theme_dir = tmp_path / ".pip" / "themes" / slug
    import shutil

    shutil.rmtree(theme_dir)

    # Second boot must respect the deletion.
    ensure_workspace(tmp_path)
    assert not theme_dir.exists(), (
        f"scaffold re-created '{slug}' after operator deleted it"
    )


def test_edited_seed_theme_is_not_overwritten(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    ensure_workspace(tmp_path)

    slug = next(iter(_seeded_theme_slugs()))
    theme_toml = tmp_path / ".pip" / "themes" / slug / "theme.toml"
    edited = theme_toml.read_text(encoding="utf-8") + "\n# local tweak\n"
    theme_toml.write_text(edited, encoding="utf-8")

    # Subsequent boots must leave the edit in place (scaffold can't tell
    # whether the user prefers their version — only the user can).
    ensure_workspace(tmp_path)
    assert theme_toml.read_text(encoding="utf-8") == edited
