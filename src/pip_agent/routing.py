"""Multi-agent routing: agents registry, binding table, session keys.

Layout model (v2)
-----------------
Pip-Boy runs a single service process rooted at ``WORKDIR``
(the Pip workspace). Inside it:

* The default agent ``pip-boy`` lives *at the workspace root itself*,
  with its state under ``WORKDIR/.pip/`` (``persona.md``,
  ``memories.json``, ``observations/``, …).
* User-created sub-agents live in subdirectories of the workspace:
  ``WORKDIR/<agent_id>/.pip/<...>``. Their ``cwd`` (as seen by the
  Claude Agent SDK subprocess) is ``WORKDIR/<agent_id>``.
* Workspace-level state shared by all agents (``owner.md``,
  ``bindings.json``, ``sdk_sessions.json``, ``agents_registry.json``,
  ``credentials/``) lives in ``WORKDIR/.pip/``.

Binding precedence (lower tier = more specific):
    T1 peer_id → T2 guild_id → T3 account_id → T4 channel → T5 default
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_DM_SCOPE = "per-guild"
DEFAULT_AGENT_ID = "pip-boy"

PIP_DIRNAME = ".pip"
REGISTRY_FILENAME = "agents_registry.json"
BINDINGS_FILENAME = "bindings.json"

REGISTRY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Agent ID validation
# ---------------------------------------------------------------------------

# Agent IDs double as directory names under the workspace root, so they
# must be safe on every supported OS (Windows in particular refuses
# control chars, ``<>:"/\\|?*``, trailing dots/spaces). We also want
# them legible in chat commands: ``/bind ProjectStella`` reads better
# than ``/bind project_stella_01``, so mixed case is allowed.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_INVALID_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]+")

_RESERVED_IDS = {"", ".", "..", PIP_DIRNAME, ".claude", ".git"}


def normalize_agent_id(value: str) -> str:
    """Return a filesystem-safe agent id, falling back to the default.

    ``normalize_agent_id`` is intentionally lenient: it takes arbitrary
    user input (e.g. ``"Project Stella!"``) and yields a safe directory
    name (``"Project-Stella"``). Callers that want to reject bad input
    outright should use :func:`is_valid_agent_id` instead.
    """
    trimmed = (value or "").strip()
    if not trimmed:
        return DEFAULT_AGENT_ID
    if _VALID_ID_RE.match(trimmed) and trimmed not in _RESERVED_IDS:
        return trimmed
    cleaned = _INVALID_CHARS_RE.sub("-", trimmed).strip("-")[:64]
    if not cleaned or cleaned in _RESERVED_IDS:
        return DEFAULT_AGENT_ID
    return cleaned


def is_valid_agent_id(value: str) -> bool:
    return bool(value) and bool(_VALID_ID_RE.match(value)) and value not in _RESERVED_IDS


# ---------------------------------------------------------------------------
# AgentConfig (persona + routing knobs)
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Persona + routing metadata for one agent.

    Token limits, compaction thresholds, and fallback-model chains are all
    delegated to Claude Code; Pip-Boy only stores persona-level knobs.
    """

    id: str
    name: str = ""
    system_body: str = ""
    model: str = ""
    dm_scope: str = ""

    @property
    def effective_model(self) -> str:
        return self.model or DEFAULT_MODEL

    @property
    def effective_dm_scope(self) -> str:
        return self.dm_scope or DEFAULT_DM_SCOPE

    def system_prompt(self, workdir: str = "") -> str:
        body = self.system_body or ""
        body = body.replace("{workdir}", workdir)
        body = body.replace("{model_name}", self.effective_model)
        return body


# ---------------------------------------------------------------------------
# AgentPaths (filesystem resolution for one agent)
# ---------------------------------------------------------------------------


AGENT_KIND_ROOT = "root"
AGENT_KIND_SUB = "sub"


@dataclass(frozen=True)
class AgentPaths:
    """Resolved filesystem paths for one agent.

    This is the single source of truth for "where does agent X live on
    disk" — every subsystem (scaffold, memory, scheduler, SDK dispatch)
    asks the registry for an :class:`AgentPaths` and derives its own
    paths from it. That keeps the root-vs-sub layout asymmetry
    (``WORKDIR/.pip`` vs ``WORKDIR/<id>/.pip``) in one place.
    """

    agent_id: str
    cwd: Path
    """Directory used as Claude Agent SDK ``cwd`` for this agent.

    For the root agent this is the workspace root itself; for sub-agents
    it's ``<workspace>/<agent_id>``.
    """

    pip_dir: Path
    """The agent's own ``.pip/`` directory (persona, memory, observations)."""

    workspace_pip_dir: Path
    """The workspace root's ``.pip/`` directory.

    Shared by all agents: ``owner.md``, ``bindings.json``,
    ``sdk_sessions.json``, ``agents_registry.json``, ``credentials/``.
    For the root agent it is the same path as :attr:`pip_dir`.
    """

    kind: str = AGENT_KIND_SUB
    description: str = ""

    @property
    def is_root(self) -> bool:
        return self.kind == AGENT_KIND_ROOT

    @property
    def persona_path(self) -> Path:
        return self.pip_dir / "persona.md"

    @property
    def observations_dir(self) -> Path:
        return self.pip_dir / "observations"

    @property
    def incoming_dir(self) -> Path:
        return self.pip_dir / "incoming"

    @property
    def users_dir(self) -> Path:
        return self.pip_dir / "users"


# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = _FM_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, match.group(2).strip()


def agent_config_from_file(path: Path, *, default_id: str = "") -> AgentConfig:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    fallback = default_id
    if not fallback:
        fallback = path.parent.name if path.name == "persona.md" else path.stem
    raw_id = meta.get("id") or fallback
    return AgentConfig(
        id=normalize_agent_id(str(raw_id)),
        name=meta.get("name", ""),
        system_body=body,
        model=meta.get("model", ""),
        dm_scope=meta.get("dm_scope", ""),
    )


# ---------------------------------------------------------------------------
# Binding & BindingTable (routing table)
# ---------------------------------------------------------------------------

@dataclass
class Binding:
    agent_id: str
    tier: int
    match_key: str
    match_value: str
    priority: int = 0
    overrides: dict[str, Any] = field(default_factory=dict)

    def display(self) -> str:
        names = {1: "peer", 2: "guild", 3: "account", 4: "channel", 5: "default"}
        label = names.get(self.tier, f"tier-{self.tier}")
        extra = ""
        if self.overrides:
            extra = f" overrides={self.overrides}"
        return (
            f"[{label}] {self.match_key}={self.match_value} "
            f"-> agent:{self.agent_id} (pri={self.priority}){extra}"
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "agent_id": self.agent_id,
            "tier": self.tier,
            "match_key": self.match_key,
            "match_value": self.match_value,
        }
        if self.priority:
            d["priority"] = self.priority
        if self.overrides:
            d["overrides"] = self.overrides
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Binding:
        return cls(
            agent_id=d["agent_id"],
            tier=int(d["tier"]),
            match_key=d["match_key"],
            match_value=d["match_value"],
            priority=int(d.get("priority", 0)),
            overrides=d.get("overrides") or {},
        )


class BindingTable:
    def __init__(self) -> None:
        self._bindings: list[Binding] = []

    def add(self, binding: Binding) -> None:
        self._bindings.append(binding)
        self._bindings.sort(key=lambda b: (b.tier, -b.priority))

    def remove(self, match_key: str, match_value: str) -> bool:
        before = len(self._bindings)
        self._bindings = [
            b for b in self._bindings
            if not (b.match_key == match_key and b.match_value == match_value)
        ]
        return len(self._bindings) < before

    def list_all(self) -> list[Binding]:
        return list(self._bindings)

    def resolve(
        self,
        channel: str = "",
        account_id: str = "",
        guild_id: str = "",
        peer_id: str = "",
    ) -> tuple[str | None, Binding | None]:
        """Walk tiers 1-5, return first match as (agent_id, binding)."""
        for b in self._bindings:
            if b.tier == 1 and b.match_key == "peer_id":
                if ":" in b.match_value:
                    if b.match_value == f"{channel}:{peer_id}":
                        return b.agent_id, b
                elif b.match_value == peer_id:
                    return b.agent_id, b
            elif b.tier == 2 and b.match_key == "guild_id":
                if ":" in b.match_value:
                    if b.match_value == f"{channel}:{guild_id}":
                        return b.agent_id, b
                elif b.match_value == guild_id and guild_id:
                    return b.agent_id, b
            elif b.tier == 3 and b.match_key == "account_id" and b.match_value == account_id:
                return b.agent_id, b
            elif b.tier == 4 and b.match_key == "channel" and b.match_value == channel:
                return b.agent_id, b
            elif b.tier == 5 and b.match_key == "default":
                return b.agent_id, b
        return None, None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [b.to_dict() for b in self._bindings]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                log.warning("bindings file %s: expected list, got %s", path, type(data).__name__)
                return
            new_bindings: list[Binding] = []
            for item in data:
                new_bindings.append(Binding.from_dict(item))
            self._bindings.clear()
            self._bindings.extend(new_bindings)
        except Exception as exc:
            log.warning("Failed to load bindings from %s: %s", path, exc)


# ---------------------------------------------------------------------------
# build_session_key
# ---------------------------------------------------------------------------

def build_session_key(
    agent_id: str,
    channel: str,
    peer_id: str,
    guild_id: str = "",
    is_group: bool = False,
    dm_scope: str = "per-guild",
) -> str:
    aid = normalize_agent_id(agent_id)
    ch = (channel or "unknown").strip().lower()
    pid = (peer_id or "unknown").strip().lower()
    gid = (guild_id or "").strip().lower()

    if dm_scope == "main":
        return f"agent:{aid}:{ch}:main"

    if is_group and gid:
        if dm_scope == "per-guild-peer":
            return f"agent:{aid}:{ch}:guild:{gid}:peer:{pid}"
        return f"agent:{aid}:{ch}:guild:{gid}"

    return f"agent:{aid}:{ch}:peer:{pid}"


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------

# Heading convention is **single hash at the top level** to match the
# shipped ``scaffold/pip-boy.md``. The memory store's prompt-injection
# regex accepts ``#+`` and therefore works with either, but keeping
# the builtin fallback aligned with the scaffold avoids drift between
# "scaffold never ran" (fallback) and "scaffold installed" (file)
# surfaces ending up with cosmetically different prompts.
_BUILTIN_DEFAULT = AgentConfig(
    id=DEFAULT_AGENT_ID,
    name="Pip-Boy",
    system_body=(
        "# Identity\n\n"
        "You are Pip-Boy, a personal assistant agent.\n"
        "Your working directory is {workdir}.\n"
    ),
    model=DEFAULT_MODEL,
    dm_scope=DEFAULT_DM_SCOPE,
)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentRegistry:
    """Directory + registry-backed catalog of all agents in the workspace.

    Two sources of truth, merged on load:

    * Disk discovery — every directory ``<workspace>/<id>/.pip/persona.md``
      plus the root ``<workspace>/.pip/persona.md`` is treated as an
      agent. Discovery is authoritative for "does this agent still
      exist".
    * Registry file (``<workspace>/.pip/agents_registry.json``) — carries
      provenance (``created_at``, ``description``, ``kind``) that the
      filesystem can't express on its own.

    When the two disagree, discovery wins for existence and the registry
    is treated as best-effort metadata. Missing registry entries are
    back-filled on load so future queries are consistent.
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
    ) -> None:
        self._agents: dict[str, AgentConfig] = {}
        self._paths: dict[str, AgentPaths] = {}
        self._metadata: dict[str, dict[str, Any]] = {}  # kind / cwd / created_at / description
        self._workspace_root = workspace_root.resolve() if workspace_root else None

        if self._workspace_root is not None:
            self._load_workspace(self._workspace_root)

        if DEFAULT_AGENT_ID not in self._agents:
            self._agents[DEFAULT_AGENT_ID] = _BUILTIN_DEFAULT
            if self._workspace_root is not None:
                self._paths[DEFAULT_AGENT_ID] = self._build_paths(
                    DEFAULT_AGENT_ID, AGENT_KIND_ROOT,
                )
                self._metadata[DEFAULT_AGENT_ID] = {
                    "kind": AGENT_KIND_ROOT,
                    "cwd": ".",
                    "description": "Default Pip-Boy agent",
                    "created_at": _iso_now(),
                }

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------

    @property
    def workspace_root(self) -> Path | None:
        return self._workspace_root

    @property
    def workspace_pip_dir(self) -> Path | None:
        if self._workspace_root is None:
            return None
        return self._workspace_root / PIP_DIRNAME

    @property
    def registry_path(self) -> Path | None:
        if self._workspace_root is None:
            return None
        return self._workspace_root / PIP_DIRNAME / REGISTRY_FILENAME

    @property
    def bindings_path(self) -> Path | None:
        if self._workspace_root is None:
            return None
        return self._workspace_root / PIP_DIRNAME / BINDINGS_FILENAME

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        return self._agents.get(agent_id) or self._agents.get(normalize_agent_id(agent_id))

    def default_agent(self) -> AgentConfig:
        return self._agents.get(DEFAULT_AGENT_ID, _BUILTIN_DEFAULT)

    def list_agents(self) -> list[AgentConfig]:
        return list(self._agents.values())

    def paths_for(self, agent_id: str) -> AgentPaths | None:
        if self._workspace_root is None:
            return None
        aid = agent_id if agent_id in self._paths else normalize_agent_id(agent_id)
        return self._paths.get(aid)

    def metadata_for(self, agent_id: str) -> dict[str, Any]:
        aid = agent_id if agent_id in self._metadata else normalize_agent_id(agent_id)
        return dict(self._metadata.get(aid, {}))

    # ------------------------------------------------------------------
    # Registry persistence
    # ------------------------------------------------------------------

    def _build_paths(self, agent_id: str, kind: str) -> AgentPaths:
        assert self._workspace_root is not None
        if kind == AGENT_KIND_ROOT:
            cwd = self._workspace_root
            pip_dir = self._workspace_root / PIP_DIRNAME
        else:
            cwd = self._workspace_root / agent_id
            pip_dir = cwd / PIP_DIRNAME
        return AgentPaths(
            agent_id=agent_id,
            cwd=cwd,
            pip_dir=pip_dir,
            workspace_pip_dir=self._workspace_root / PIP_DIRNAME,
            kind=kind,
            description=self._metadata.get(agent_id, {}).get("description", ""),
        )

    def _load_workspace(self, root: Path) -> None:
        """Discover agents from disk and merge registry metadata."""
        registry_data = self._read_registry(root)
        registered = registry_data.get("agents", {}) if isinstance(registry_data, dict) else {}
        if not isinstance(registered, dict):
            registered = {}

        root_persona = root / PIP_DIRNAME / "persona.md"
        if root_persona.is_file():
            self._register_from_persona(
                root_persona,
                agent_id=DEFAULT_AGENT_ID,
                kind=AGENT_KIND_ROOT,
                meta=registered.get(DEFAULT_AGENT_ID) or {},
            )

        for child in sorted(root.iterdir()) if root.is_dir() else []:
            if not child.is_dir():
                continue
            if child.name in _RESERVED_IDS:
                continue
            if not is_valid_agent_id(child.name):
                continue
            persona = child / PIP_DIRNAME / "persona.md"
            if not persona.is_file():
                continue
            self._register_from_persona(
                persona,
                agent_id=child.name,
                kind=AGENT_KIND_SUB,
                meta=registered.get(child.name) or {},
            )

        for aid, meta in registered.items():
            if aid in self._agents:
                continue
            if not isinstance(meta, dict):
                continue
            kind = meta.get("kind", AGENT_KIND_SUB)
            log.info(
                "Agent %r listed in registry but persona.md is missing; "
                "keeping metadata as stale entry", aid,
            )
            self._metadata[aid] = dict(meta)
            self._metadata[aid].setdefault("kind", kind)

    def _register_from_persona(
        self,
        persona_path: Path,
        *,
        agent_id: str,
        kind: str,
        meta: dict[str, Any],
    ) -> None:
        try:
            cfg = agent_config_from_file(persona_path, default_id=agent_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load persona %s: %s", persona_path, exc)
            return
        if cfg.id != agent_id:
            log.debug(
                "persona.md id=%r overrides directory name %r",
                cfg.id, agent_id,
            )
            agent_id = cfg.id
        self._agents[agent_id] = cfg
        self._metadata[agent_id] = {
            "kind": kind,
            "cwd": "." if kind == AGENT_KIND_ROOT else agent_id,
            "created_at": meta.get("created_at") or _iso_now(),
            "description": meta.get("description", ""),
        }
        self._paths[agent_id] = self._build_paths(agent_id, kind)

    @staticmethod
    def _read_registry(root: Path) -> dict[str, Any]:
        path = root / PIP_DIRNAME / REGISTRY_FILENAME
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def save_registry(self) -> None:
        """Persist the current metadata to ``agents_registry.json``."""
        if self._workspace_root is None:
            return
        path = self.registry_path
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_SCHEMA_VERSION,
            "agents": {
                aid: dict(meta)
                for aid, meta in sorted(self._metadata.items())
                if aid in self._agents
            },
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register_agent(
        self,
        cfg: AgentConfig,
        *,
        kind: str = AGENT_KIND_SUB,
        description: str = "",
    ) -> None:
        """Register (or update) an agent in the in-memory catalog."""
        self._agents[cfg.id] = cfg
        prev = self._metadata.get(cfg.id, {})
        meta = {
            "kind": kind,
            "cwd": "." if kind == AGENT_KIND_ROOT else cfg.id,
            "created_at": prev.get("created_at") or _iso_now(),
            "description": description or prev.get("description", ""),
        }
        self._metadata[cfg.id] = meta
        if self._workspace_root is not None:
            self._paths[cfg.id] = self._build_paths(cfg.id, kind)

    def remove_agent(
        self, agent_id: str, *, delete_files: bool = False,
    ) -> bool:
        """Remove an agent; optionally wipe only its ``.pip/`` metadata.

        The root (``pip-boy``) is protected — removing it would strip
        the workspace's own identity. Returns ``True`` if something was
        removed, ``False`` if the id wasn't known.

        Scope of ``delete_files=True``
        ------------------------------
        We only delete the agent's *identity surface*:

        * ``<agent_cwd>/.pip/`` (persona, memory, observations, etc.)
        * ``<agent_cwd>/.claude/`` — but **only if empty**, so we never
          destroy user-authored CC config.

        The agent's working directory itself and every non-Pip file
        inside it (``.git/``, source code, build artefacts, …) are
        left untouched. "Delete the agent" means "end its identity",
        not "nuke the project".
        """
        import shutil

        aid = agent_id if agent_id in self._agents else normalize_agent_id(agent_id)
        meta = self._metadata.get(aid, {})
        if meta.get("kind") == AGENT_KIND_ROOT or aid == DEFAULT_AGENT_ID:
            return False
        if aid not in self._agents:
            return False

        self._agents.pop(aid, None)
        self._metadata.pop(aid, None)
        paths = self._paths.pop(aid, None)

        if delete_files and paths is not None:
            if paths.pip_dir.is_dir():
                shutil.rmtree(paths.pip_dir, ignore_errors=True)
            claude_dir = paths.cwd / ".claude"
            if claude_dir.is_dir():
                try:
                    # Only prune the .claude dir if empty — presence of
                    # anything in it means the user put it there and we
                    # have no business deleting their CC config.
                    if not any(claude_dir.iterdir()):
                        claude_dir.rmdir()
                except OSError:
                    pass
        return True

    def archive_agent(self, agent_id: str) -> Path | None:
        """Move a sub-agent's ``.pip/`` into ``<workspace>/.pip/archived/``.

        Only the agent identity surface (``.pip/``) is relocated — the
        sub-agent's working directory and any project files inside it
        stay in place. Returns the destination path (inside
        ``archived/``), or ``None`` if the agent wasn't found / was the
        root / had no ``.pip/`` on disk.
        """
        import shutil

        aid = agent_id if agent_id in self._agents else normalize_agent_id(agent_id)
        meta = self._metadata.get(aid, {})
        if meta.get("kind") == AGENT_KIND_ROOT or aid == DEFAULT_AGENT_ID:
            return None
        paths = self._paths.get(aid)
        if paths is None or self._workspace_root is None:
            return None

        if not paths.pip_dir.is_dir():
            # Nothing to move — just drop the registry entry.
            self.remove_agent(aid, delete_files=False)
            return None

        archived_root = self._workspace_root / PIP_DIRNAME / "archived"
        archived_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest_dir = archived_root / f"{aid}-{stamp}"
        dest_dir.mkdir(parents=True, exist_ok=False)
        # Move the ``.pip/`` subtree into ``archived/<id>-<stamp>/.pip/``
        # so restoring is a straight rename back.
        shutil.move(str(paths.pip_dir), str(dest_dir / PIP_DIRNAME))

        self._agents.pop(aid, None)
        self._metadata.pop(aid, None)
        self._paths.pop(aid, None)
        return dest_dir


def resolve_effective_config(
    agent: AgentConfig,
    binding: Binding | None = None,
) -> AgentConfig:
    """Return agent config with binding overrides applied (shallow copy)."""
    if not binding or not binding.overrides:
        return agent
    kwargs: dict[str, Any] = {}
    ov = binding.overrides
    if "scope" in ov:
        kwargs["dm_scope"] = ov["scope"]
    if "model" in ov:
        kwargs["model"] = ov["model"]
    return replace(agent, **kwargs) if kwargs else agent
