from __future__ import annotations

from pathlib import Path

import pytest

from pip_agent.skills import SkillRegistry


@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    return d


@pytest.fixture()
def user_dir(tmp_path: Path) -> Path:
    d = tmp_path / "user"
    d.mkdir()
    return d


def _write_skill(
    directory: Path,
    name: str,
    description: str = "",
    body: str = "",
    tags: list[str] | None = None,
) -> Path:
    """Create a skill directory with SKILL.md containing YAML frontmatter."""
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"

    frontmatter_lines = [f"name: {name}"]
    if description:
        frontmatter_lines.append(f"description: {description}")
    if tags:
        tag_str = ", ".join(tags)
        frontmatter_lines.append(f"tags: [{tag_str}]")

    fm = "\n".join(frontmatter_lines)
    content = f"---\n{fm}\n---\n\n{body}\n" if body else f"---\n{fm}\n---\n"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


class TestScan:
    def test_scans_both_dirs(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers")
        _write_skill(user_dir, "deploy", "Deploy checklist")
        reg = SkillRegistry(builtin_dir, user_dir)
        assert sorted(reg.names()) == ["deploy", "git"]

    def test_user_overrides_builtin(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Built-in git", "built-in body")
        _write_skill(user_dir, "git", "User git", "user body")
        reg = SkillRegistry(builtin_dir, user_dir)
        assert reg.names() == ["git"]
        assert "user body" in reg.load("git")
        assert "built-in body" not in reg.load("git")

    def test_only_builtin(self, builtin_dir: Path, tmp_path: Path) -> None:
        _write_skill(builtin_dir, "test", "Testing")
        missing = tmp_path / "nonexistent"
        reg = SkillRegistry(builtin_dir, missing)
        assert reg.names() == ["test"]

    def test_only_user(self, tmp_path: Path, user_dir: Path) -> None:
        _write_skill(user_dir, "deploy", "Deploy")
        missing = tmp_path / "nonexistent"
        reg = SkillRegistry(missing, user_dir)
        assert reg.names() == ["deploy"]

    def test_empty_dirs(self, builtin_dir: Path, user_dir: Path) -> None:
        reg = SkillRegistry(builtin_dir, user_dir)
        assert not reg.available
        assert reg.names() == []

    def test_missing_dirs(self, tmp_path: Path) -> None:
        reg = SkillRegistry(tmp_path / "a", tmp_path / "b")
        assert not reg.available

    def test_ignores_flat_md_files(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers")
        (builtin_dir / "stray.md").write_text("not a skill", encoding="utf-8")
        reg = SkillRegistry(builtin_dir, user_dir)
        assert reg.names() == ["git"]

    def test_ignores_dirs_without_skill_md(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        (builtin_dir / "empty-skill").mkdir()
        _write_skill(builtin_dir, "git", "Git helpers")
        reg = SkillRegistry(builtin_dir, user_dir)
        assert reg.names() == ["git"]


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_description_from_frontmatter(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        _write_skill(builtin_dir, "git", "Git workflow helpers")
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "Git workflow helpers" in reg.catalog_prompt()

    def test_multiline_description(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        skill_dir = builtin_dir / "multi"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: multi\ndescription: >-\n  Line one.\n  Line two.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "Line one. Line two." in reg.catalog_prompt()

    def test_missing_frontmatter_falls_back_to_heading(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        skill_dir = builtin_dir / "nofm"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "# Heading fallback\n\nBody text.\n", encoding="utf-8"
        )
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "Heading fallback" in reg.catalog_prompt()

    def test_missing_frontmatter_no_heading_falls_back_to_dir_name(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        skill_dir = builtin_dir / "plaintext"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "No heading, no frontmatter.\n", encoding="utf-8"
        )
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "plaintext" in reg.catalog_prompt()

    def test_malformed_yaml_falls_back(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        skill_dir = builtin_dir / "bad"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n: invalid yaml [\n---\n\n# Bad YAML skill\n",
            encoding="utf-8",
        )
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "bad" in reg.names()
        assert "Bad YAML skill" in reg.catalog_prompt()

    def test_name_from_frontmatter_overrides_dir_name(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        skill_dir = builtin_dir / "dirname"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: custom-name\ndescription: Custom\n---\n\nBody.\n",
            encoding="utf-8",
        )
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "custom-name" in reg.names()
        assert "dirname" not in reg.names()


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TestTags:
    def test_tags_in_catalog(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers", tags=["git", "vcs"])
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "[git, vcs]" in reg.catalog_prompt()

    def test_no_tags_no_brackets(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers")
        reg = SkillRegistry(builtin_dir, user_dir)
        prompt = reg.catalog_prompt()
        assert "[" not in prompt

    def test_tags_stored_in_entry(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers", tags=["git", "vcs"])
        reg = SkillRegistry(builtin_dir, user_dir)
        assert reg._skills["git"].tags == ["git", "vcs"]


# ---------------------------------------------------------------------------
# Available
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_true_when_skills_exist(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers")
        reg = SkillRegistry(builtin_dir, user_dir)
        assert reg.available

    def test_false_when_empty(self, builtin_dir: Path, user_dir: Path) -> None:
        reg = SkillRegistry(builtin_dir, user_dir)
        assert not reg.available


# ---------------------------------------------------------------------------
# Catalog prompt
# ---------------------------------------------------------------------------


class TestCatalogPrompt:
    def test_format(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers", tags=["git"])
        _write_skill(builtin_dir, "test", "Testing best practices")
        reg = SkillRegistry(builtin_dir, user_dir)
        prompt = reg.catalog_prompt()
        assert "Skills available (use load_skill to activate):" in prompt
        assert "  - git: Git helpers [git]" in prompt
        assert "  - test: Testing best practices" in prompt

    def test_empty_returns_empty_string(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        reg = SkillRegistry(builtin_dir, user_dir)
        assert reg.catalog_prompt() == ""

    def test_sorted_alphabetically(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "zebra", "Zebra skill")
        _write_skill(builtin_dir, "alpha", "Alpha skill")
        reg = SkillRegistry(builtin_dir, user_dir)
        lines = reg.catalog_prompt().splitlines()
        skill_lines = [line for line in lines if line.startswith("  - ")]
        assert skill_lines[0].startswith("  - alpha:")
        assert skill_lines[1].startswith("  - zebra:")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


class TestLoad:
    def test_returns_wrapped_body_without_frontmatter(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        _write_skill(builtin_dir, "git", "Git helpers", "Step 1: commit")
        reg = SkillRegistry(builtin_dir, user_dir)
        result = reg.load("git")
        assert result.startswith('<skill name="git">')
        assert result.endswith("</skill>")
        assert "Step 1: commit" in result
        assert "---" not in result
        assert "name: git" not in result

    def test_unknown_skill_lists_available(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        _write_skill(builtin_dir, "git", "Git helpers")
        _write_skill(builtin_dir, "test", "Testing")
        reg = SkillRegistry(builtin_dir, user_dir)
        result = reg.load("nonexistent")
        assert "Unknown skill: nonexistent" in result
        assert "Available:" in result
        assert "git" in result
        assert "test" in result

    def test_unknown_skill_no_skills_available(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        reg = SkillRegistry(builtin_dir, user_dir)
        result = reg.load("nonexistent")
        assert "Unknown skill: nonexistent" in result
        assert "Available: " in result

    def test_loads_user_version_on_override(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        _write_skill(builtin_dir, "git", "Built-in", "built-in content")
        _write_skill(user_dir, "git", "Custom", "user content")
        reg = SkillRegistry(builtin_dir, user_dir)
        result = reg.load("git")
        assert "user content" in result
        assert "built-in content" not in result

    def test_load_picks_up_skill_created_after_init(
        self, builtin_dir: Path, user_dir: Path
    ) -> None:
        reg = SkillRegistry(builtin_dir, user_dir)
        assert "Unknown skill" in reg.load("late")

        _write_skill(user_dir, "late", "Late skill", "late body")
        result = reg.load("late")
        assert "late body" in result


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


class TestToolSchema:
    def test_schema_structure(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "git", "Git helpers")
        _write_skill(user_dir, "deploy", "Deploy checklist")
        reg = SkillRegistry(builtin_dir, user_dir)
        schema = reg.tool_schema()
        assert schema["name"] == "load_skill"
        assert "input_schema" in schema
        props = schema["input_schema"]["properties"]
        assert "name" in props
        assert sorted(props["name"]["enum"]) == ["deploy", "git"]

    def test_enum_matches_names(self, builtin_dir: Path, user_dir: Path) -> None:
        _write_skill(builtin_dir, "a", "Skill A")
        _write_skill(builtin_dir, "b", "Skill B")
        _write_skill(user_dir, "c", "Skill C")
        reg = SkillRegistry(builtin_dir, user_dir)
        enum = reg.tool_schema()["input_schema"]["properties"]["name"]["enum"]
        assert sorted(enum) == sorted(reg.names())
