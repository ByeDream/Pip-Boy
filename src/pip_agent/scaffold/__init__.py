"""Idempotent workspace scaffold with version-tracked templates.

Layout
------
The workspace root itself belongs to the default ``pip-boy`` agent:

    <workspace>/
      .pip/                 <- pip-boy's own state + workspace-shared state
        persona.md          <- pip-boy persona
        HEARTBEAT.md
        addressbook/        <- shared contacts (all agents read/write)
        observations/
        incoming/
        credentials/
        themes/             <- TUI themes; seeded on first boot from wheel
        bindings.json
        agents_registry.json
        sdk_sessions.json
        .scaffold_manifest.json
      <sub-agent>/          <- 0..N sub-agents
        .pip/
          persona.md
          HEARTBEAT.md
          observations/
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
from pip_agent.tui.themes import BUILTIN_THEMES_DIR

logger = logging.getLogger(__name__)

_SCAFFOLD_DIR = Path(__file__).resolve().parent

_MANIFEST_NAME = ".scaffold_manifest.json"

# (target relative to workspace root, scaffold source file)
_SCAFFOLD_FILES: list[tuple[str, str]] = [
    (".pip/persona.md", "pip-boy.md"),
    (".pip/HEARTBEAT.md", "heartbeat.md"),
    (".pip/themes/README.md", "themes_README.md"),
    (".env", "env.example"),
]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_hash(root: Path) -> str:
    """Stable combined hash over all files in a theme directory.

    The hash covers ``theme.toml`` / ``theme.tcss`` / ``art.txt`` (when
    present) so a template update to any of them triggers the
    "refresh seed" branch. We deliberately ignore sub-directories —
    themes are flat by contract — and ignore hidden files so a
    stray ``.DS_Store`` doesn't flip the hash.
    """
    h = hashlib.sha256()
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        h.update(entry.name.encode("utf-8"))
        h.update(b"\0")
        h.update(entry.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _load_manifest(workdir: Path) -> dict:
    mp = workdir / ".pip" / _MANIFEST_NAME
    if mp.is_file():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt scaffold manifest; rebuilding.")
    return {"version": "0.0.0", "files": {}, "themes": {}}


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

    themes_meta = manifest.get("themes")
    if not isinstance(themes_meta, dict):
        themes_meta = {}
    _seed_themes(workdir, themes_meta)

    manifest["version"] = __version__
    manifest["files"] = files_meta
    manifest["themes"] = themes_meta
    _save_manifest(workdir, manifest)

    _ensure_gitignore(workdir)
    _check_git(workdir)


def _seed_themes(workdir: Path, themes_meta: dict) -> None:
    """Copy package-seeded themes into ``<workspace>/.pip/themes/``.

    On first boot every seed directory is copied. On subsequent boots:

    * If the target is missing AND the manifest records we installed
      it before (``installed_once = True``), we respect the operator's
      deletion — no re-copy.
    * If the seed's combined hash matches the manifest, nothing to do.
    * If the seed hash changed AND the current workspace copy matches
      the previously-installed seed (user hasn't edited), refresh it.
    * If the seed hash changed AND the user has edited, skip with a
      WARNING so the operator can diff + merge manually.

    Seeded themes that no longer ship (e.g. renamed upstream) stay in
    the workspace untouched — we only clean the manifest entry so a
    future seed with the same slug doesn't surprise the operator.
    """
    if not BUILTIN_THEMES_DIR.is_dir():
        logger.warning(
            "Seed themes directory missing: %s", BUILTIN_THEMES_DIR,
        )
        return

    themes_root = workdir / ".pip" / "themes"
    themes_root.mkdir(parents=True, exist_ok=True)

    seeds: dict[str, Path] = {}
    for seed_dir in sorted(BUILTIN_THEMES_DIR.iterdir(), key=lambda p: p.name):
        if not seed_dir.is_dir():
            continue
        if seed_dir.name.startswith("."):
            continue
        if seed_dir.name == "__pycache__":
            continue
        seeds[seed_dir.name] = seed_dir

    for slug, seed_dir in seeds.items():
        target = themes_root / slug
        rel_key = f".pip/themes/{slug}/"
        seed_hash = _dir_hash(seed_dir)

        prev = themes_meta.get(rel_key)

        if not target.exists():
            if isinstance(prev, dict) and prev.get("installed_once"):
                logger.debug(
                    "Seed theme '%s' was deleted by operator; not re-seeding.",
                    slug,
                )
                continue
            shutil.copytree(seed_dir, target)
            themes_meta[rel_key] = {
                "seed_hash": seed_hash,
                "installed_once": True,
                "installed_version": __version__,
            }
            logger.info("Seeded theme: %s", target)
            continue

        if not isinstance(prev, dict):
            themes_meta[rel_key] = {
                "seed_hash": _dir_hash(target),
                "installed_once": True,
                "installed_version": __version__,
            }
            continue

        old_seed_hash = prev.get("seed_hash", "")
        if old_seed_hash == seed_hash:
            continue

        current_hash = _dir_hash(target)
        if current_hash == old_seed_hash:
            shutil.rmtree(target)
            shutil.copytree(seed_dir, target)
            themes_meta[rel_key] = {
                "seed_hash": seed_hash,
                "installed_once": True,
                "installed_version": __version__,
            }
            logger.info("Refreshed seed theme: %s", target)
        else:
            logger.warning(
                "Seed theme '%s' updated upstream but workspace copy has "
                "local edits; leaving alone. Diff against %s and merge "
                "manually if you want the new default.",
                slug, seed_dir,
            )

    tracked_keys = {f".pip/themes/{slug}/" for slug in seeds}
    for rel_key in list(themes_meta):
        if rel_key not in tracked_keys:
            logger.info(
                "Seed theme '%s' no longer ships; removing manifest entry "
                "(workspace copy left untouched).",
                rel_key,
            )
            del themes_meta[rel_key]


def _ensure_dirs(workdir: Path) -> None:
    """Create the v2 directory skeleton for the root (pip-boy) agent."""
    # ``transcripts/`` is deliberately NOT created: Pip reflect reads
    # Claude Code's native JSONL under ``~/.claude/projects/`` and we
    # no longer maintain a per-agent transcript archive.
    dirs = [
        ".pip",
        ".pip/observations",
        ".pip/addressbook",
        ".pip/incoming",
        ".pip/credentials",
        # TUI theme directory. ``_seed_themes`` copies the wheel's
        # bundled themes here on first boot; afterwards it's the
        # operator's sandbox (edit / add / delete freely — scaffold
        # respects deletions via ``installed_once`` in the manifest).
        ".pip/themes",
        # Per-run log files live here (``pip-boy.log`` + rotated
        # backups ``pip-boy.1.log`` / ``pip-boy.2.log``). See
        # :mod:`pip_agent.logging_setup`.
        ".pip/log",
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


# ---------------------------------------------------------------------------
# Claude Code settings — modelOverrides auto-sync
# ---------------------------------------------------------------------------

_MODEL_FAMILIES: tuple[str, ...] = ("haiku", "sonnet", "opus")


def _derive_model_override(name: str) -> tuple[str, str] | None:
    """Derive a ``modelOverrides`` entry for a non-canonical model name.

    Returns ``(canonical, actual)`` when *name* uses a non-canonical format
    (e.g. Venus ``claude-4-5-haiku-20251001`` vs canonical
    ``claude-haiku-4-5-20251001``). Returns ``None`` when the name is
    already canonical or unrecognisable.
    """
    parts = name.split("-")
    if not parts or parts[0] != "claude" or len(parts) < 3:
        return None
    for family in _MODEL_FAMILIES:
        if family not in parts:
            continue
        fi = parts.index(family)
        if fi == 1:
            return None
        without = parts[:fi] + parts[fi + 1 :]
        canonical_parts = [without[0], family] + without[1:]
        canonical = "-".join(canonical_parts)
        return (canonical, name)
    return None


def ensure_claude_model_overrides() -> None:
    """Sync ``modelOverrides`` in ``~/.claude/settings.json`` from ``.env``.

    Venus uses non-canonical model names (e.g. ``claude-4-5-haiku-*``
    instead of ``claude-haiku-4-5-*``). ``claude.exe`` cannot normalise
    these, so capability detection (thinking / effort) and sub-agent
    model resolution break.  ``modelOverrides`` is the native
    ``claude.exe`` setting that fixes both by teaching it the mapping.

    Only runs when ``ANTHROPIC_BASE_URL`` contains ``"venus"``; direct
    Anthropic API users are unaffected.
    """
    from pip_agent.config import settings

    base_url = (settings.anthropic_base_url or "").strip()
    if "venus" not in base_url.lower():
        return

    overrides: dict[str, str] = {}
    for model in (settings.model_t0, settings.model_t1, settings.model_t2):
        name = (model or "").strip()
        if not name:
            continue
        pair = _derive_model_override(name)
        if pair:
            overrides[pair[0]] = pair[1]

    if not overrides:
        return

    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}

    existing = data.get("modelOverrides")
    if not isinstance(existing, dict):
        existing = {}

    changed = False
    for canonical, actual in overrides.items():
        if existing.get(canonical) != actual:
            existing[canonical] = actual
            changed = True

    if not changed:
        return

    data["modelOverrides"] = existing
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "Synced modelOverrides to %s: %s",
        settings_path,
        overrides,
    )
