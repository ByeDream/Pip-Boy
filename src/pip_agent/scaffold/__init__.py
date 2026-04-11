"""Idempotent workspace scaffold for target repositories."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_SCAFFOLD_DIR = Path(__file__).resolve().parent
_SENTINEL = "<!-- pip-agent:begin -->"
_SENTINEL_END = "<!-- pip-agent:end -->"


def ensure_workspace(workdir: Path) -> None:
    """Idempotent workspace initialization. Safe to call on every startup."""
    _ensure_dirs(workdir)
    _ensure_agents_md(workdir)
    _ensure_copy(workdir / ".pip" / "models.json", "models.json")
    _ensure_copy(workdir / ".env", "env.example")
    _ensure_gitignore(workdir)
    _check_git(workdir)


def _ensure_dirs(workdir: Path) -> None:
    for rel in (".pip", ".pip/team", ".pip/skills"):
        d = workdir / rel
        d.mkdir(parents=True, exist_ok=True)
        logger.debug("Directory ensured: %s", d)


def _ensure_agents_md(workdir: Path) -> None:
    src = _SCAFFOLD_DIR / "agents.md"
    if not src.is_file():
        logger.warning("Scaffold template missing: %s", src)
        return
    template = src.read_text(encoding="utf-8")
    block = f"{_SENTINEL}\n{template}\n{_SENTINEL_END}\n"
    target = workdir / "AGENTS.md"

    if not target.exists():
        target.write_text(block, encoding="utf-8")
        logger.info("Created %s", target)
        return

    existing = target.read_text(encoding="utf-8")
    if _SENTINEL in existing:
        logger.debug("Sentinel found in %s, skipping", target)
        return

    separator = "" if existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n"
    target.write_text(existing + separator + block, encoding="utf-8")
    logger.info("Appended working guide to %s", target)


def _ensure_copy(target: Path, scaffold_name: str) -> None:
    if target.exists():
        logger.debug("Already exists: %s", target)
        return
    src = _SCAFFOLD_DIR / scaffold_name
    if not src.is_file():
        logger.warning("Scaffold resource missing: %s", src)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    logger.info("Created %s", target)


def _ensure_gitignore(workdir: Path) -> None:
    src = _SCAFFOLD_DIR / "gitignore_entries.txt"
    if not src.is_file():
        logger.warning("Scaffold resource missing: %s", src)
        return
    entries_text = src.read_text(encoding="utf-8")
    required = [line for line in entries_text.splitlines() if line.strip()]

    target = workdir / ".gitignore"
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        existing_lines = set(existing.splitlines())
    else:
        existing = ""
        existing_lines = set()

    missing = [e for e in required if e not in existing_lines]
    if not missing:
        logger.debug(".gitignore already contains all required entries")
        return

    addition = "\n# Pip-Boy\n" + "\n".join(missing) + "\n"
    if not target.exists():
        target.write_text(addition.lstrip("\n"), encoding="utf-8")
        logger.info("Created %s", target)
    else:
        with target.open("a", encoding="utf-8") as f:
            if not existing.endswith("\n"):
                f.write("\n")
            f.write(addition)
        logger.info("Appended missing entries to %s", target)


def _check_git(workdir: Path) -> None:
    if not (workdir / ".git").is_dir():
        logger.warning(
            "Not a git repository: %s. Worktree features will be unavailable.",
            workdir,
        )
