"""
Multi-agent routing: binding table, agent config, session key generation.

Inbound messages are resolved through a 5-tier binding table to determine
which agent handles them. Each agent carries its own config (model, system
prompt, dm_scope, etc.) loaded from .pip/agents/*.md files.

Tier priority (lower = more specific, matched first):
  T1  peer_id      — route a specific user to an agent
  T2  guild_id     — route a specific group/guild to an agent
  T3  account_id   — route by bot account
  T4  channel      — route an entire platform (e.g. "wecom")
  T5  default      — catch-all fallback
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_DM_SCOPE = "per-guild"
DEFAULT_COMPACT_THRESHOLD = 50_000
DEFAULT_COMPACT_MICRO_AGE = 8
DEFAULT_AGENT_ID = "pip-boy"

# ---------------------------------------------------------------------------
# Agent ID normalisation
# ---------------------------------------------------------------------------

_VALID_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_INVALID_CHARS_RE = re.compile(r"[^a-z0-9_-]+")


def normalize_agent_id(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return DEFAULT_AGENT_ID
    low = trimmed.lower()
    if _VALID_ID_RE.match(low):
        return low
    cleaned = _INVALID_CHARS_RE.sub("-", low).strip("-")[:64]
    return cleaned or DEFAULT_AGENT_ID


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    id: str
    name: str = ""
    system_body: str = ""
    model: str = ""
    max_tokens: int = 0
    dm_scope: str = ""
    compact_threshold: int = 0
    compact_micro_age: int = 0
    fallback_models: list[str] = field(default_factory=list)

    @property
    def effective_model(self) -> str:
        return self.model or DEFAULT_MODEL

    @property
    def effective_max_tokens(self) -> int:
        return self.max_tokens or DEFAULT_MAX_TOKENS

    @property
    def effective_dm_scope(self) -> str:
        return self.dm_scope or DEFAULT_DM_SCOPE

    @property
    def effective_compact_threshold(self) -> int:
        return self.compact_threshold or DEFAULT_COMPACT_THRESHOLD

    @property
    def effective_compact_micro_age(self) -> int:
        return self.compact_micro_age or DEFAULT_COMPACT_MICRO_AGE

    def system_prompt(self, workdir: str = "") -> str:
        body = self.system_body if self.system_body else ""
        body = body.replace("{workdir}", workdir)
        body = body.replace("{model_name}", self.effective_model)
        return body


# ---------------------------------------------------------------------------
# YAML frontmatter parsing (shared pattern with team module)
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


def agent_config_from_file(path: Path) -> AgentConfig:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    # For <agents_dir>/<id>/persona.md, derive id from parent dir name
    if path.name == "persona.md":
        default_id = path.parent.name
    else:
        default_id = path.stem
    fb_raw = meta.get("fallback_models") or []
    if isinstance(fb_raw, str):
        fallback_models = [s.strip() for s in fb_raw.split(",") if s.strip()]
    elif isinstance(fb_raw, list):
        fallback_models = [str(s).strip() for s in fb_raw if str(s).strip()]
    else:
        fallback_models = []

    return AgentConfig(
        id=normalize_agent_id(meta.get("id", default_id)),
        name=meta.get("name", ""),
        system_body=body,
        model=meta.get("model", ""),
        max_tokens=int(meta["max_tokens"]) if "max_tokens" in meta else 0,
        dm_scope=meta.get("dm_scope", ""),
        compact_threshold=int(meta["compact_threshold"]) if "compact_threshold" in meta else 0,
        compact_micro_age=int(meta["compact_micro_age"]) if "compact_micro_age" in meta else 0,
        fallback_models=fallback_models,
    )


# ---------------------------------------------------------------------------
# Binding & BindingTable
# ---------------------------------------------------------------------------

@dataclass
class Binding:
    agent_id: str
    tier: int                # 1-5
    match_key: str           # "peer_id" | "guild_id" | "account_id" | "channel" | "default"
    match_value: str         # e.g. "wecom:admin-001", "wecom", "*"
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

    # -- persistence --

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
        # per-guild (default for group)
        return f"agent:{aid}:{ch}:guild:{gid}"

    # private message
    return f"agent:{aid}:{ch}:peer:{pid}"


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------

_BUILTIN_DEFAULT = AgentConfig(
    id=DEFAULT_AGENT_ID,
    name="Pip-Boy",
    system_body=(
        "## Identity\n\n"
        "You are Pip-Boy, a personal assistant agent.\n"
        "Your working directory is {workdir}.\n"
        "If AGENTS.md exists in your working directory, read it for project context.\n\n"
        "## Rules\n\n"
        "- **Direct execution** — Simple, single-step requests. Just use your tools.\n"
        "- **Tasks** — Multi-step goals that need structured tracking.\n"
        "- **Background tasks** — Long-running shell commands. Use `background: true`.\n"
        "- **Agent Team** — Parallel work, specialized roles, "
        "or tasks too large for a single context."
    ),
    model=DEFAULT_MODEL,
    max_tokens=DEFAULT_MAX_TOKENS,
    dm_scope=DEFAULT_DM_SCOPE,
    compact_threshold=DEFAULT_COMPACT_THRESHOLD,
    compact_micro_age=DEFAULT_COMPACT_MICRO_AGE,
)


class AgentRegistry:
    def __init__(self, agents_dir: Path | None = None) -> None:
        self._agents: dict[str, AgentConfig] = {}
        self._agents_dir = agents_dir
        if agents_dir:
            self._load_dir(agents_dir)
        if not self._agents:
            self._agents[DEFAULT_AGENT_ID] = _BUILTIN_DEFAULT

    @property
    def agents_dir(self) -> Path | None:
        return self._agents_dir

    def _load_dir(self, agents_dir: Path) -> None:
        if not agents_dir.is_dir():
            return
        # New layout: <agents_dir>/<agent-id>/persona.md
        for persona in sorted(agents_dir.glob("*/persona.md")):
            try:
                cfg = agent_config_from_file(persona)
                self._agents[cfg.id] = cfg
                log.debug("Loaded agent config: %s from %s", cfg.id, persona)
            except Exception as exc:
                log.warning("Failed to load agent config %s: %s", persona, exc)
        # Legacy layout: <agents_dir>/<agent-id>.md (flat files)
        for path in sorted(agents_dir.glob("*.md")):
            try:
                cfg = agent_config_from_file(path)
                if cfg.id not in self._agents:
                    self._agents[cfg.id] = cfg
                    log.debug("Loaded legacy agent config: %s from %s", cfg.id, path)
            except Exception as exc:
                log.warning("Failed to load agent config %s: %s", path, exc)

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        return self._agents.get(normalize_agent_id(agent_id))

    def register_agent(self, cfg: AgentConfig) -> None:
        self._agents[cfg.id] = cfg

    def remove_agent(self, agent_id: str, *, delete_file: bool = False) -> bool:
        """Remove an agent from the registry and optionally delete its data."""
        import shutil

        agent_id = normalize_agent_id(agent_id)
        if agent_id == DEFAULT_AGENT_ID:
            return False
        if agent_id not in self._agents:
            return False
        del self._agents[agent_id]
        if delete_file and self._agents_dir:
            agent_subtree = self._agents_dir / agent_id
            if agent_subtree.is_dir():
                shutil.rmtree(agent_subtree, ignore_errors=True)
            legacy_md = self._agents_dir / f"{agent_id}.md"
            legacy_md.unlink(missing_ok=True)
        return True

    def list_agents(self) -> list[AgentConfig]:
        return list(self._agents.values())

    def default_agent(self) -> AgentConfig:
        return self._agents.get(DEFAULT_AGENT_ID, _BUILTIN_DEFAULT)


def resolve_effective_config(
    agent: AgentConfig,
    binding: Binding | None = None,
) -> AgentConfig:
    """Return agent config with binding overrides applied (shallow copy)."""
    if not binding or not binding.overrides:
        return agent
    from dataclasses import replace
    kwargs: dict[str, Any] = {}
    ov = binding.overrides
    if "scope" in ov:
        kwargs["dm_scope"] = ov["scope"]
    if "model" in ov:
        kwargs["model"] = ov["model"]
    for key in ("max_tokens", "compact_threshold", "compact_micro_age"):
        if key in ov:
            try:
                kwargs[key] = int(ov[key])
            except (ValueError, TypeError):
                log.warning("Invalid override %s=%r, skipping", key, ov[key])
    if "fallback_models" in ov:
        raw = ov["fallback_models"]
        if isinstance(raw, str):
            kwargs["fallback_models"] = [s.strip() for s in raw.split(",") if s.strip()]
        elif isinstance(raw, list):
            kwargs["fallback_models"] = [str(s).strip() for s in raw if str(s).strip()]
    return replace(agent, **kwargs) if kwargs else agent
