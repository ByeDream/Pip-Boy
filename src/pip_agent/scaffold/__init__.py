"""Idempotent workspace scaffold with version-tracked templates.

Layout
------
The workspace root itself belongs to the default ``pip-boy`` agent:

    <workspace>/
      .pip/                 <- pip-boy's own state + workspace-shared state
        persona.md          <- pip-boy persona
        HEARTBEAT.md
        owner.md
        observations/
        users/
        incoming/
        credentials/
        bindings.json
        agents_registry.json
        sdk_sessions.json
        .scaffold_manifest.json
      <sub-agent>/          <- 0..N sub-agents
        .pip/
          persona.md
          HEARTBEAT.md
          observations/
          users/
          incoming/
      .env
      .gitignore
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pip_agent import __version__

logger = logging.getLogger(__name__)

_SCAFFOLD_DIR = Path(__file__).resolve().parent

_MANIFEST_NAME = ".scaffold_manifest.json"

# (target relative to workspace root, scaffold source file)
_SCAFFOLD_FILES: list[tuple[str, str]] = [
    (".pip/persona.md", "pip-boy.md"),
    (".pip/HEARTBEAT.md", "heartbeat.md"),
    (".pip/owner.md", "owner.md"),
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
    """Idempotent workspace initialization.

    Safe to call on every process start: existing state is left alone;
    only missing files and directories are created. Scaffold template
    updates are applied only to files that weren't locally modified
    (hash-tracked via ``.scaffold_manifest.json``).
    """
    _ensure_dirs(workdir)
    _ensure_registry(workdir, default_agent_id=default_agent_id)

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


def _ensure_dirs(workdir: Path) -> None:
    """Create the v2 directory skeleton for the root (pip-boy) agent."""
    # ``transcripts/`` is deliberately NOT created: Pip reflect reads
    # Claude Code's native JSONL under ``~/.claude/projects/`` and we
    # no longer maintain a per-agent transcript archive.
    dirs = [
        ".pip",
        ".pip/observations",
        ".pip/users",
        ".pip/incoming",
        ".pip/credentials",
    ]
    for rel in dirs:
        d = workdir / rel
        d.mkdir(parents=True, exist_ok=True)
        logger.debug("Directory ensured: %s", d)


def _ensure_registry(workdir: Path, *, default_agent_id: str) -> None:
    """Guarantee ``agents_registry.json`` has at least the root entry.

    We don't call :class:`AgentRegistry` here on purpose — the registry
    module imports yaml / dataclasses that pull in transitive deps, and
    scaffold must be runnable in a pre-import fast path.
    """
    registry_path = workdir / ".pip" / "agents_registry.json"
    if registry_path.is_file():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    if default_agent_id not in agents:
        agents[default_agent_id] = {
            "kind": "root",
            "cwd": ".",
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": "Default Pip-Boy agent",
        }
    data["agents"] = agents
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


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
