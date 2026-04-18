from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class _SkillEntry:
    name: str
    description: str
    path: Path
    tags: list[str] = field(default_factory=list)


class SkillRegistry:
    """Two-source skill registry: built-in + user, user wins on name collision."""

    def __init__(self, builtin_dir: Path, user_dir: Path) -> None:
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._skills: dict[str, _SkillEntry] = {}
        self._scan_dir(builtin_dir)
        self._scan_dir(user_dir)

    def _scan_dir(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for skill_md in sorted(directory.glob("*/SKILL.md")):
            try:
                meta, body = self._parse_frontmatter(skill_md)
            except OSError:
                continue
            name = meta.get("name", skill_md.parent.name)
            description = meta.get("description", "").strip()
            if not description:
                description = self._heading_fallback(body, fallback=name)
            raw_tags = meta.get("tags", [])
            tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
            self._skills[name] = _SkillEntry(
                name=name, description=description, path=skill_md, tags=tags,
            )

    @staticmethod
    def _parse_frontmatter(path: Path) -> tuple[dict, str]:
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, match.group(2).strip()

    @staticmethod
    def _heading_fallback(text: str, fallback: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return fallback

    @property
    def available(self) -> bool:
        return bool(self._skills)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def catalog_prompt(self) -> str:
        if not self._skills:
            return ""
        lines = ["Skills available (use load_skill to activate):"]
        for name in sorted(self._skills):
            entry = self._skills[name]
            line = f"  - {name}: {entry.description}"
            if entry.tags:
                line += f" [{', '.join(entry.tags)}]"
            lines.append(line)
        return "\n".join(lines)

    def _rescan(self) -> None:
        self._skills.clear()
        self._scan_dir(self._builtin_dir)
        self._scan_dir(self._user_dir)

    def load(self, name: str) -> str:
        entry = self._skills.get(name)
        if entry is None:
            self._rescan()
            entry = self._skills.get(name)
        if entry is None:
            available = ", ".join(sorted(self._skills))
            return f"Unknown skill: {name}. Available: {available}"
        try:
            _, body = self._parse_frontmatter(entry.path)
        except OSError as exc:
            return f"[error] Failed to read skill '{name}': {exc}"
        return f'<skill name="{name}">\n{body}\n</skill>'

    def tool_schema(self) -> dict:
        return {
            "name": "load_skill",
            "description": (
                "Load detailed domain instructions for a specific skill. "
                "Call this before starting domain-specific work."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill to load.",
                        "enum": self.names(),
                    },
                },
                "required": ["name"],
            },
        }
