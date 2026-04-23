"""Idempotent workspace scaffold with version-tracked migration.

Layout v2 (identity redesign)
-----------------------------
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

Migration from v1 (flat ``.pip/agents/<id>/`` layout):
    * ``.pip/agents/pip-boy/*``    -> ``.pip/*``         (content bubbled up)
    * ``.pip/agents/<other>/*``    -> ``<other>/.pip/*`` (each becomes a sub-agent)
    * ``.pip/agents/bindings.json``-> ``.pip/bindings.json``
    * ``.pip/agents/`` is removed once it's empty.
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

# Legacy paths we recognise and migrate from (v1 layout).
_LEGACY_AGENTS_DIRNAME = ".pip/agents"
_LEGACY_MANIFEST_KEYS = {
    ".pip/agents/pip-boy/persona.md": ".pip/persona.md",
    ".pip/agents/pip-boy/HEARTBEAT.md": ".pip/HEARTBEAT.md",
}


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
    """Idempotent workspace initialization with scaffold migration.

    Safe to call on every process start: existing state is left alone;
    only missing files are created and legacy layouts are migrated in
    place on first run.
    """
    _migrate_v1_to_v2(workdir)
    _ensure_dirs(workdir)
    _ensure_registry(workdir, default_agent_id=default_agent_id)
    _migrate_legacy_nested(workdir, default_agent_id=default_agent_id)

    manifest = _load_manifest(workdir)
    files_meta = manifest.get("files", {})

    # Forward-compat: older manifests referenced ``.pip/agents/pip-boy/``
    # paths that have since moved up to ``.pip/``. Rewrite their keys
    # in place so scaffold-upgrade detection still works after the
    # v1 -> v2 migration.
    for old_key, new_key in _LEGACY_MANIFEST_KEYS.items():
        if old_key in files_meta and new_key not in files_meta:
            files_meta[new_key] = files_meta.pop(old_key)

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


def _migrate_v1_to_v2(workdir: Path) -> None:
    """One-shot migration from the flat ``.pip/agents/<id>/`` layout."""
    pip = workdir / ".pip"
    agents = pip / _LEGACY_AGENTS_DIRNAME.split("/", 1)[1]  # .pip/agents/
    if not agents.is_dir():
        return

    logger.info("Detected v1 layout at %s — migrating to v2.", agents)

    # 1. Bubble pip-boy's own state up into .pip/
    pipboy_old = agents / "pip-boy"
    if pipboy_old.is_dir():
        _merge_tree_into(pipboy_old, pip)

    # 2. Each other <id>/ becomes <workspace>/<id>/.pip/
    registry_agents: dict[str, dict] = {}
    for child in sorted(agents.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "pip-boy":
            continue
        dest_pip = workdir / child.name / ".pip"
        dest_pip.mkdir(parents=True, exist_ok=True)
        _merge_tree_into(child, dest_pip)
        registry_agents[child.name] = {
            "kind": "sub",
            "cwd": child.name,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": f"Migrated from v1 {_LEGACY_AGENTS_DIRNAME}/{child.name}",
        }
        logger.info(
            "Migrated v1 agent %r to %s/.pip/",
            child.name, dest_pip.parent,
        )

    # 3. Promote bindings.json to .pip/ root (if it wasn't already).
    legacy_bindings = agents / "bindings.json"
    new_bindings = pip / "bindings.json"
    if legacy_bindings.is_file() and not new_bindings.is_file():
        shutil.move(str(legacy_bindings), str(new_bindings))
        logger.info("Migrated bindings.json to %s", new_bindings)

    # 4. Record migrated agents in the registry (non-destructive merge).
    if registry_agents:
        registry_path = pip / "agents_registry.json"
        if registry_path.is_file():
            try:
                data = json.loads(registry_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        existing = data.get("agents") if isinstance(data.get("agents"), dict) else {}
        merged = {**registry_agents, **existing}  # existing wins if present
        data["version"] = 1
        data["agents"] = merged
        registry_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # 5. Drop the now-empty .pip/agents/.
    try:
        remaining = list(agents.iterdir())
    except OSError:
        remaining = []
    if not remaining:
        try:
            agents.rmdir()
            logger.info("Removed empty legacy directory %s", agents)
        except OSError:
            pass
    else:
        logger.warning(
            "%s still has %d entries after migration; leaving in place for "
            "manual inspection.", agents, len(remaining),
        )


def _merge_tree_into(src: Path, dest: Path) -> None:
    """Move everything from ``src`` into ``dest``, preferring ``dest`` on conflict."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in list(src.iterdir()):
        target = dest / item.name
        if target.exists():
            if item.is_dir() and target.is_dir():
                _merge_tree_into(item, target)
                try:
                    item.rmdir()
                except OSError:
                    pass
            else:
                logger.debug(
                    "Skipping %s: %s already exists at destination",
                    item, target,
                )
        else:
            shutil.move(str(item), str(target))
    try:
        src.rmdir()
    except OSError:
        pass


def _migrate_legacy_nested(workdir: Path, *, default_agent_id: str) -> None:
    """Migrate pre-v1 quirks that may still exist inside ``.pip/``.

    Historically some installs had ``.pip/memory/<id>/`` and
    ``.pip/users/`` lying around from much older versions. These are
    merged into the new layout defensively — the cost of looking is
    negligible and it keeps upgrades from stranded legacy data.
    """
    pip = workdir / ".pip"

    # .pip/memory/<id>/ → pip-boy's .pip/ (or <id>/.pip/ for non-default)
    legacy_memory = pip / "memory"
    if legacy_memory.is_dir():
        for sub in sorted(legacy_memory.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name == default_agent_id:
                _merge_tree_into(sub, pip)
            else:
                dest = workdir / sub.name / ".pip"
                _merge_tree_into(sub, dest)
        try:
            legacy_memory.rmdir()
        except OSError:
            pass

    # Ancient ``.pip/users/`` at the ROOT (pre-multi-agent) went to the
    # default agent's users/. Under v2 pip-boy's users/ lives at
    # ``.pip/users/`` anyway, so the move is a no-op for fresh installs
    # and only fires if an install was downgraded at some point.
    legacy_transcripts = pip / "transcripts"
    if legacy_transcripts.is_dir():
        shutil.rmtree(legacy_transcripts, ignore_errors=True)
        logger.info("Removed stale %s", legacy_transcripts)


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
