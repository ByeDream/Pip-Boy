"""Idempotent workspace scaffold with version-tracked migration."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

from pip_agent import __version__

logger = logging.getLogger(__name__)

_SCAFFOLD_DIR = Path(__file__).resolve().parent

_MANIFEST_NAME = ".scaffold_manifest.json"

_SCAFFOLD_FILES: list[tuple[str, str]] = [
    (".pip/models.json", "models.json"),
    (".pip/agents/pip-boy.md", "pip-boy.md"),
    (".pip/user.md", "user.md"),
    (".env", "env.example"),
]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest(workdir: Path) -> dict:
    mp = workdir / ".pip" / _MANIFEST_NAME
    if mp.is_file():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt scaffold manifest; rebuilding.")
    return {"version": "0.0.0", "files": {}}


def _save_manifest(workdir: Path, manifest: dict) -> None:
    mp = workdir / ".pip" / _MANIFEST_NAME
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def ensure_workspace(workdir: Path, *, default_agent_id: str = "pip-boy") -> None:
    """Idempotent workspace initialization with scaffold migration."""
    _ensure_dirs(workdir, default_agent_id=default_agent_id)

    manifest = _load_manifest(workdir)
    old_version = manifest.get("version", "0.0.0")
    files_meta = manifest.get("files", {})

    for rel_target, scaffold_name in _SCAFFOLD_FILES:
        target = workdir / rel_target
        src = _SCAFFOLD_DIR / scaffold_name
        if not src.is_file():
            logger.warning("Scaffold resource missing: %s", src)
            continue

        src_hash = _file_hash(src)

        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            files_meta[rel_target] = {
                "scaffold_hash": src_hash,
                "installed_version": __version__,
            }
            logger.info("Created %s", target)
            continue

        prev = files_meta.get(rel_target)
        if prev is None:
            files_meta[rel_target] = {
                "scaffold_hash": src_hash,
                "installed_version": __version__,
            }
            continue

        old_scaffold_hash = prev.get("scaffold_hash", "")
        if old_scaffold_hash == src_hash:
            continue

        current_hash = _file_hash(target)
        if current_hash == old_scaffold_hash:
            shutil.copy2(src, target)
            files_meta[rel_target] = {
                "scaffold_hash": src_hash,
                "installed_version": __version__,
            }
            logger.info("Updated %s (scaffold template changed)", target)
        else:
            logger.warning(
                "Scaffold template for %s changed but file was locally modified; skipping.",
                rel_target,
            )

    tracked_keys = {rel for rel, _ in _SCAFFOLD_FILES}
    for rel in list(files_meta):
        if rel not in tracked_keys:
            target = workdir / rel
            if target.exists():
                logger.warning(
                    "Scaffold file %s was removed upstream. Consider deleting it manually.",
                    rel,
                )
            del files_meta[rel]

    manifest["version"] = __version__
    manifest["files"] = files_meta
    _save_manifest(workdir, manifest)

    _ensure_gitignore(workdir)
    _check_git(workdir)


def _ensure_dirs(workdir: Path, *, default_agent_id: str = "pip-boy") -> None:
    dirs = [
        ".pip", ".pip/team", ".pip/skills", ".pip/agents",
        f".pip/memory/{default_agent_id}",
        f".pip/memory/{default_agent_id}/observations",
    ]
    for rel in dirs:
        d = workdir / rel
        d.mkdir(parents=True, exist_ok=True)
        logger.debug("Directory ensured: %s", d)


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
