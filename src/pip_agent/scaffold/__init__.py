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
    (".pip/agents/pip-boy/persona.md", "pip-boy.md"),
    (".pip/agents/pip-boy/HEARTBEAT.md", "heartbeat.md"),
    (".pip/owner.md", "owner.md"),
    (".pip/keys.json", "keys.json"),
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
    _migrate_legacy_layout(workdir, default_agent_id=default_agent_id)

    manifest = _load_manifest(workdir)
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
        ".pip",
        ".pip/agents",
        f".pip/agents/{default_agent_id}",
        f".pip/agents/{default_agent_id}/observations",
        f".pip/agents/{default_agent_id}/users",
        f".pip/agents/{default_agent_id}/transcripts",
        f".pip/agents/{default_agent_id}/tasks",
        f".pip/agents/{default_agent_id}/downloads",
        f".pip/agents/{default_agent_id}/team",
        f".pip/agents/{default_agent_id}/team/inbox",
    ]
    for rel in dirs:
        d = workdir / rel
        d.mkdir(parents=True, exist_ok=True)
        logger.debug("Directory ensured: %s", d)


def _migrate_legacy_layout(
    workdir: Path, *, default_agent_id: str = "pip-boy",
) -> None:
    """One-time migration from old flat layout to per-agent subdirectories."""
    pip = workdir / ".pip"
    agents = pip / "agents"
    default_dir = agents / default_agent_id

    # Legacy flat agent .md → per-agent persona.md
    for md in sorted(agents.glob("*.md")):
        agent_id = md.stem
        persona = agents / agent_id / "persona.md"
        if not persona.exists():
            persona.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md, persona)
            logger.info("Migrated %s → %s", md, persona)

    # Legacy .pip/memory/<id>/ → .pip/agents/<id>/
    legacy_memory = pip / "memory"
    if legacy_memory.is_dir():
        for sub in sorted(legacy_memory.iterdir()):
            if not sub.is_dir():
                continue
            target = agents / sub.name
            target.mkdir(parents=True, exist_ok=True)
            for item in sub.iterdir():
                dest = target / item.name
                if not dest.exists():
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                elif item.is_dir() and dest.is_dir():
                    for child in item.rglob("*"):
                        if child.is_file():
                            child_dest = dest / child.relative_to(item)
                            if not child_dest.exists():
                                child_dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(child, child_dest)
            logger.info("Migrated memory/%s → agents/%s", sub.name, sub.name)

    # Legacy .pip/users/ → default agent's users/
    legacy_users = pip / "users"
    if legacy_users.is_dir() and any(legacy_users.glob("*.md")):
        target_users = default_dir / "users"
        target_users.mkdir(parents=True, exist_ok=True)
        for md in sorted(legacy_users.glob("*.md")):
            dest = target_users / md.name
            if not dest.exists():
                shutil.copy2(md, dest)
        logger.info("Migrated .pip/users/ → agents/%s/users/", default_agent_id)

    # Legacy .pip/tasks/ → default agent's tasks/
    legacy_tasks = pip / "tasks"
    if legacy_tasks.is_dir() and any(legacy_tasks.iterdir()):
        target_tasks = default_dir / "tasks"
        target_tasks.mkdir(parents=True, exist_ok=True)
        for item in legacy_tasks.iterdir():
            dest = target_tasks / item.name
            if not dest.exists():
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
        logger.info("Migrated .pip/tasks/ → agents/%s/tasks/", default_agent_id)

    # Legacy .pip/team/ → default agent's team/
    legacy_team = pip / "team"
    if legacy_team.is_dir() and any(legacy_team.iterdir()):
        target_team = default_dir / "team"
        target_team.mkdir(parents=True, exist_ok=True)
        for item in legacy_team.iterdir():
            dest = target_team / item.name
            if not dest.exists():
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
        logger.info("Migrated .pip/team/ → agents/%s/team/", default_agent_id)

    # Legacy .pip/transcripts/ → default agent's transcripts/
    legacy_transcripts = pip / "transcripts"
    if legacy_transcripts.is_dir() and any(legacy_transcripts.glob("*.json")):
        target_transcripts = default_dir / "transcripts"
        target_transcripts.mkdir(parents=True, exist_ok=True)
        for f in legacy_transcripts.glob("*.json"):
            dest = target_transcripts / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
        logger.info(
            "Migrated .pip/transcripts/ → agents/%s/transcripts/", default_agent_id,
        )

    # Remove legacy directories after migration
    for legacy in (legacy_memory, legacy_users, legacy_tasks, legacy_team, legacy_transcripts):
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)
            logger.info("Removed legacy directory: %s", legacy)


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
