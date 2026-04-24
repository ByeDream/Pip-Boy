"""Memory subsystem: per-agent behavioural memory with three-tier pipeline.

Storage layout (v2 / post identity-redesign)
--------------------------------------------
Each agent's memory lives directly under its own ``.pip/`` directory:

    <agent_dir>/
        state.json
        observations/<date>.jsonl
        memories.json
        axioms.md
        users/<name>.md       (per-agent user profiles, tool-managed)

For the root ``pip-boy`` agent ``<agent_dir>`` is ``WORKDIR/.pip``.
For a sub-agent ``X`` it is ``WORKDIR/X/.pip``.

Workspace-level ACL state (``owner.md``) lives at
``<workspace_pip_dir>/owner.md`` and is shared by every agent — the
owner is a property of the human running Pip, not of any one agent.

Construction
~~~~~~~~~~~~
::

    MemoryStore(agent_dir=paths.pip_dir,
                workspace_pip_dir=paths.workspace_pip_dir,
                agent_id=paths.agent_id)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pip_agent.types import Memory, Observation

log = logging.getLogger(__name__)


class MemoryStore:
    """Facade for a single agent's memory storage.

    All file I/O is lazy — missing files are silently handled with defaults.
    """

    def __init__(
        self,
        *,
        agent_dir: Path,
        workspace_pip_dir: Path | None = None,
        agent_id: str = "",
    ) -> None:
        if workspace_pip_dir is None:
            workspace_pip_dir = agent_dir

        self.agent_id = agent_id
        self.agent_dir = agent_dir
        self.pip_dir = workspace_pip_dir  # shared workspace scope (owner.md)
        self._io_lock = threading.Lock()
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_dir / "observations").mkdir(exist_ok=True)
        (self.agent_dir / "users").mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        with self._io_lock:
            path = self.agent_dir / "state.json"
            if not path.is_file():
                return {}
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}

    def save_state(self, state: dict[str, Any]) -> None:
        from pip_agent.fileutil import atomic_write

        with self._io_lock:
            path = self.agent_dir / "state.json"
            atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Observations (L1)
    # ------------------------------------------------------------------

    def write_observations(self, observations: list[Observation]) -> None:
        with self._io_lock:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = self.agent_dir / "observations" / f"{date_str}.jsonl"
            # Self-heal the parent directory. ``MemoryStore.__init__``
            # already creates ``observations/`` once, but a long-lived
            # host caches the store across operations like
            # ``/subagent reset`` that blow away ``.pip/`` out from under
            # it — mirroring ``atomic_write``'s mkdir-on-write contract
            # keeps reflect working without a process restart.
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for obs in observations:
                    f.write(json.dumps(obs, ensure_ascii=False) + "\n")

    def write_single(
        self, text: str, category: str = "observation", source: str = "user",
    ) -> None:
        """Write a single observation (used by memory_write tool)."""
        obs = {
            "ts": time.time(),
            "text": text,
            "category": category,
            "source": source,
        }
        self.write_observations([obs])

    def load_all_observations(self) -> list[Observation]:
        obs_dir = self.agent_dir / "observations"
        if not obs_dir.is_dir():
            return []
        result: list[Observation] = []
        with self._io_lock:
            for fp in sorted(obs_dir.glob("*.jsonl")):
                try:
                    lines = fp.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return result

    def purge_observations_through(self, cutoff_ts: float) -> int:
        """Hard-delete observations with ``ts <= cutoff_ts``.

        Used by Dream after a successful consolidate/axiom pass:
        observations are an intermediate product — once their insight
        has been merged into ``memories.json`` they have no further
        value and keeping them around makes consolidate re-weight the
        same old signal every night (the regression that motivated
        H5).

        Strategy: rewrite each daily ``observations/*.jsonl`` minus the
        lines whose parsed ``ts`` is at or before the cutoff. Files that
        end up empty are unlinked. Files that fail to parse a given
        line are preserved (we never destroy bytes we can't understand
        — a malformed line is always better kept than silently lost).

        The cutoff approach (rather than deleting entire files) means
        observations written by a concurrent ``reflect`` call while
        Dream was running — whose ``ts`` is strictly greater than
        ``cutoff_ts`` — survive this purge intact.

        Returns the number of observation lines removed.
        """
        from pip_agent.fileutil import atomic_write

        obs_dir = self.agent_dir / "observations"
        if not obs_dir.is_dir():
            return 0
        purged = 0
        with self._io_lock:
            for fp in sorted(obs_dir.glob("*.jsonl")):
                try:
                    raw = fp.read_text(encoding="utf-8")
                except OSError:
                    continue
                kept_lines: list[str] = []
                for line in raw.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obs = json.loads(stripped)
                    except json.JSONDecodeError:
                        # Preserve unparseable bytes — they're not ours
                        # to delete on a semantic cutoff.
                        kept_lines.append(line)
                        continue
                    if isinstance(obs, dict) and float(obs.get("ts", 0)) <= cutoff_ts:
                        purged += 1
                        continue
                    kept_lines.append(line)
                try:
                    if kept_lines:
                        atomic_write(fp, "\n".join(kept_lines) + "\n")
                    else:
                        fp.unlink()
                except OSError as exc:
                    log.warning(
                        "purge_observations: failed to update %s: %s", fp, exc,
                    )
        return purged

    # ------------------------------------------------------------------
    # Memories (L2)
    # ------------------------------------------------------------------

    def load_memories(self) -> list[Memory]:
        path = self.agent_dir / "memories.json"
        if not path.is_file():
            return []
        with self._io_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                return []

    def save_memories(self, memories: list[Memory]) -> None:
        from pip_agent.fileutil import atomic_write

        with self._io_lock:
            path = self.agent_dir / "memories.json"
            atomic_write(
                path, json.dumps(memories, indent=2, ensure_ascii=False, default=str),
            )

    # ------------------------------------------------------------------
    # Axioms (L3)
    # ------------------------------------------------------------------

    def load_axioms(self) -> str:
        path = self.agent_dir / "axioms.md"
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def save_axioms(self, text: str) -> None:
        from pip_agent.fileutil import atomic_write

        with self._io_lock:
            path = self.agent_dir / "axioms.md"
            atomic_write(path, text)

    # ------------------------------------------------------------------
    # User profiles (owner.md read-only + users/*.md tool-managed)
    # ------------------------------------------------------------------

    _FIELD_MAP: dict[str, str] = {
        "name": "Name",
        "call_me": "What to call them",
        "timezone": "Timezone",
        "notes": "Notes",
    }

    def load_user_profile(self) -> str:
        """Load the owner profile (read-only, never modified by tools)."""
        path = self.pip_dir / "owner.md"
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _all_profile_paths(self) -> list[Path]:
        """Return owner.md + all per-agent users/*.md paths."""
        paths: list[Path] = []
        owner = self.pip_dir / "owner.md"
        if owner.is_file():
            paths.append(owner)
        users_dir = self.agent_dir / "users"
        if users_dir.is_dir():
            paths.extend(sorted(users_dir.glob("*.md")))
        return paths

    def find_profile_by_sender(
        self, channel: str, sender_id: str,
    ) -> Path | None:
        """Find which profile file contains this channel:sender_id."""
        if not sender_id:
            return None
        target = f"{channel}:{sender_id}"
        expected = f"- `{target}`"
        paths = self._all_profile_paths()
        log.debug(
            "find_profile_by_sender: expected=%r paths=%d", expected, len(paths),
        )
        for path in paths:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped == expected:
                        return path
                    if stripped.startswith("- `") and ":" in stripped:
                        log.debug(
                            "  candidate in %s: %r (match=%s)",
                            path.name, stripped, stripped == expected,
                        )
            except OSError:
                continue
        log.debug("find_profile_by_sender: no match for %r", expected)
        return None

    def _find_in_users(
        self, channel: str, sender_id: str,
    ) -> Path | None:
        """Find a sender_id only within per-agent users/*.md (excludes owner.md)."""
        if not sender_id:
            return None
        target = f"{channel}:{sender_id}"
        users_dir = self.agent_dir / "users"
        if not users_dir.is_dir():
            return None
        for path in sorted(users_dir.glob("*.md")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() == f"- `{target}`":
                        return path
            except OSError:
                continue
        return None

    def _find_user_by_name(self, name: str) -> Path | None:
        """Find a per-agent users/*.md profile whose Name or What to call them matches."""
        if not name:
            return None
        users_dir = self.agent_dir / "users"
        if not users_dir.is_dir():
            return None
        target_lower = name.strip().lower()
        for path in sorted(users_dir.glob("*.md")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    for prefix in ("- **Name:**", "- **What to call them:**"):
                        if stripped.startswith(prefix):
                            val = stripped[len(prefix):].strip().lower()
                            if val and val == target_lower:
                                return path
            except OSError:
                continue
        return None

    @staticmethod
    def extract_profile_name(path: Path) -> str:
        """Read 'What to call them' or 'Name' from a profile file."""
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("- **What to call them:**"):
                    val = stripped[len("- **What to call them:**"):].strip()
                    if val:
                        return val
                if stripped.startswith("- **Name:**"):
                    val = stripped[len("- **Name:**"):].strip()
                    if val:
                        return val
        except OSError:
            pass
        return ""

    def update_user_profile(
        self,
        *,
        sender_id: str = "",
        channel: str = "",
        **fields: str,
    ) -> str:
        """Create or update a user profile in users/. Returns confirmation.

        Only operates on per-agent users/ directory — never touches owner.md.
        A registered sender is locked to their own profile.
        An unregistered sender may join an existing profile by name or create new.
        """
        if sender_id and channel and sender_id.startswith(f"{channel}:"):
            sender_id = sender_id[len(channel) + 1:]

        if sender_id and channel and channel != "cli" and self.is_owner(channel, sender_id):
            return "This sender is the owner. Owner profile is read-only."

        new_id = f"{channel}:{sender_id}" if sender_id and channel else ""
        users_dir = self.agent_dir / "users"
        users_dir.mkdir(parents=True, exist_ok=True)

        registered_path = self._find_in_users(channel, sender_id) if new_id else None

        if registered_path:
            target_path = registered_path
        else:
            name = fields.get("name") or fields.get("call_me") or ""
            existing = self._find_user_by_name(name) if name else None
            if existing:
                target_path = existing
            else:
                safe = _sanitize_filename(name or sender_id or "unknown")
                target_path = users_dir / f"{safe}.md"

        current: dict[str, str] = {}
        current_ids: list[str] = []
        current_admin: str | None = None
        if target_path.is_file():
            try:
                in_ids = False
                for line in target_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    for key, label in self._FIELD_MAP.items():
                        prefix = f"- **{label}:**"
                        if stripped.startswith(prefix):
                            current[key] = stripped[len(prefix):].strip()
                    if stripped.startswith("- **Admin:**"):
                        current_admin = stripped[len("- **Admin:**"):].strip()
                    if stripped == "- **Identifiers:**":
                        in_ids = True
                        continue
                    if in_ids:
                        if stripped.startswith("- `") and stripped.endswith("`"):
                            current_ids.append(stripped[3:-1])
                        elif stripped.startswith("- **"):
                            in_ids = False
            except OSError:
                pass

        updated_keys: list[str] = []
        for key, value in fields.items():
            if key not in self._FIELD_MAP or not value:
                continue
            if key == "notes" and current.get("notes"):
                current[key] = current[key] + "; " + value
            else:
                current[key] = value
            updated_keys.append(key)

        if new_id and new_id not in current_ids:
            current_ids.append(new_id)
            updated_keys.append("identifier")

        if not updated_keys:
            return "No fields to update."

        display = current.get("call_me") or current.get("name") or "User"
        lines = [
            f"# {display}", "",
            "_Profile managed by Pip._", "",
        ]
        for key, label in self._FIELD_MAP.items():
            val = current.get(key, "")
            lines.append(f"- **{label}:** {val}")
        if current_admin is not None:
            lines.append(f"- **Admin:** {current_admin}")
        if current_ids:
            lines.append("- **Identifiers:**")
            for ident in current_ids:
                lines.append(f"  - `{ident}`")
        lines.append("")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-flush (Ctrl+C, OOM, power loss)
        # can't leave a user profile half-written. The file is the
        # source of truth for identity binding, admin bit, and the
        # aliases list — a truncated write would silently demote a
        # user to "unregistered" on the next load.
        from pip_agent.fileutil import atomic_write
        atomic_write(target_path, "\n".join(lines))
        return f"Updated user profile ({target_path.name}): {', '.join(updated_keys)}"

    # ------------------------------------------------------------------
    # ACL: owner / admin
    # ------------------------------------------------------------------

    def is_owner(self, channel: str, sender_id: str) -> bool:
        """Check if sender is the owner (CLI always True; otherwise match owner.md)."""
        if channel == "cli":
            return True
        if not sender_id:
            return False
        target = f"{channel}:{sender_id}"
        owner_path = self.pip_dir / "owner.md"
        if not owner_path.is_file():
            return False
        try:
            for line in owner_path.read_text(encoding="utf-8").splitlines():
                if line.strip() == f"- `{target}`":
                    return True
        except OSError:
            pass
        return False

    def is_admin(self, channel: str, sender_id: str) -> bool:
        """Check if sender has admin privileges in their user profile."""
        if not sender_id:
            return False
        profile = self._find_in_users(channel, sender_id)
        if not profile:
            return False
        try:
            for line in profile.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("- **Admin:**"):
                    val = stripped[len("- **Admin:**"):].strip().lower()
                    return val == "yes"
        except OSError:
            pass
        return False

    def set_admin(self, name: str, *, grant: bool) -> str:
        """Grant or revoke admin for a user identified by name."""
        profile = self._find_user_by_name(name)
        if not profile:
            return f"[error] User profile not found for '{name}'."
        try:
            content = profile.read_text(encoding="utf-8")
        except OSError:
            return f"[error] Cannot read profile for '{name}'."

        lines = content.splitlines()
        new_val = "yes" if grant else "no"
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("- **Admin:**"):
                lines[i] = f"- **Admin:** {new_val}"
                found = True
                break

        if not found:
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                if line.strip() == "- **Identifiers:**":
                    insert_idx = i
                    break
            lines.insert(insert_idx, f"- **Admin:** {new_val}")

        # Atomic write mirrors ``update_user_profile``: a torn write
        # here would revert the admin flag to its previous value (or
        # worse, corrupt the whole profile) on the next load.
        from pip_agent.fileutil import atomic_write
        atomic_write(profile, "\n".join(lines))
        action = "Granted" if grant else "Revoked"
        return f"{action} admin for '{name}'."

    def list_admins(self) -> list[str]:
        """Return names of all users with admin privileges."""
        admins: list[str] = []
        users_dir = self.agent_dir / "users"
        if not users_dir.is_dir():
            return admins
        for path in sorted(users_dir.glob("*.md")):
            try:
                is_admin = False
                name = ""
                for line in path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- **Admin:**"):
                        val = stripped[len("- **Admin:**"):].strip().lower()
                        is_admin = val == "yes"
                    if not name:
                        for prefix in ("- **What to call them:**", "- **Name:**"):
                            if stripped.startswith(prefix):
                                val = stripped[len(prefix):].strip()
                                if val:
                                    name = val
                if is_admin and name:
                    admins.append(name)
            except OSError:
                continue
        return admins

    # ------------------------------------------------------------------
    # Search / Recall
    # ------------------------------------------------------------------

    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        from pip_agent.memory.recall import search_memories
        memories = self.load_memories()
        if not memories:
            observations = self.load_all_observations()
            if not observations:
                return []
            search_pool: list[Memory] = [
                {
                    "text": o.get("text", ""),
                    "category": o.get("category", "observation"),
                    "last_reinforced": o.get("ts", 0),
                    "first_seen": o.get("ts", 0),
                    "count": 1,
                    "source": o.get("source", "auto"),
                }
                for o in observations
            ]
            return search_memories(query, search_pool, top_k=top_k)
        return search_memories(query, memories, top_k=top_k)

    def auto_recall(self, user_text: str, *, top_k: int = 3) -> str:
        """Return formatted string of recalled memories for prompt injection."""
        if not user_text.strip():
            return ""
        results = self.search(user_text, top_k=top_k)
        if not results:
            return ""
        lines: list[str] = []
        for r in results:
            lines.append(f"- {r.get('text', '')} (score: {r.get('score', 0)})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Prompt enrichment
    # ------------------------------------------------------------------

    def enrich_prompt(
        self,
        system_prompt: str,
        user_text: str,
        *,
        channel: str = "cli",
        agent_id: str = "",
        workdir: str = "",
        sender_id: str = "",
    ) -> str:
        """Inject dynamic context into the system prompt.

        Layers (injected in order):
          1. ## User — owner profile + all known user profiles
          2. ## Judgment Principles — per-agent axioms
          3. ## Recalled Context — TF-IDF matched memories
          4. ## Context — runtime metadata
          5. ## Channel — channel hints
        """
        sections: list[str] = []
        owner_text = self.load_user_profile()
        if owner_text:
            sections.append(owner_text)
        users_dir = self.agent_dir / "users"
        if users_dir.is_dir():
            for path in sorted(users_dir.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8").strip()
                    if text:
                        sections.append(text)
                except OSError:
                    continue
        if sections:
            system_prompt = _insert_after_identity(
                system_prompt,
                "## User\n\n" + "\n\n---\n\n".join(sections),
            )

        axioms = self.load_axioms()
        if axioms:
            wrapped = _wrap_axioms(axioms)
            system_prompt = _insert_before_rules(
                system_prompt, f"## Judgment Principles\n\n{wrapped}",
            )

        recalled = self.auto_recall(user_text)
        if recalled:
            system_prompt += f"\n\n## Recalled Context\n\n{recalled}"

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system_prompt += (
            f"\n\n## Context\n\n"
            f"Agent: {agent_id}\nWorking directory: {workdir}\nTime: {now}"
        )

        hints = {
            "cli": "You are responding via a terminal. Markdown is supported.",
            "wechat": "You are responding via WeChat. Keep messages concise. No markdown.",
            "wecom": "You are responding via WeCom. Keep messages under 2000 chars.",
        }
        if channel in hints:
            system_prompt += f"\n\n## Channel\n\n{hints[channel]}"

        return system_prompt

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        memories = self.load_memories()
        observations = self.load_all_observations()
        axioms = self.load_axioms()
        state = self.load_state()
        return {
            "agent_id": self.agent_id,
            "memories": len(memories),
            "observations": len(observations),
            "has_axioms": bool(axioms),
            "axiom_lines": len(axioms.splitlines()) if axioms else 0,
            "last_reflect_at": state.get("last_reflect_at"),
            "last_consolidate_at": state.get("last_consolidate_at"),
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-]")


def _sanitize_filename(name: str) -> str:
    """Turn a display name into a safe filename stem (no extension)."""
    s = _SAFE_RE.sub("_", name.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


# ----------------------------------------------------------------------
# Prompt section helpers
# ----------------------------------------------------------------------

# Scaffold personas use top-level ``# Identity`` headings; the builtin
# fallback and older tests use ``## Identity``. The injection logic
# must find either — otherwise the User/Axioms sections silently fall
# back to "prepend/append at the edges" and end up in the wrong part
# of the prompt.
_IDENTITY_RE = re.compile(r"^#+\s+Identity\b", re.MULTILINE)
_RULES_RE = re.compile(r"^#+\s+Rules\b", re.MULTILINE)
_ANY_HEADING_RE = re.compile(r"^#+\s+\S", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def _wrap_axioms(text: str) -> str:
    """Wrap each bullet of the axioms markdown list in ``<axiom>`` tags.

    L3 distillation writes ``axioms.md`` as a plain markdown list so
    humans can read it via ``/axioms`` and ``distill_axioms`` stays a
    simple text-in / text-out function. For prompt injection we want
    each principle to be *structurally* distinct so the model can
    recognize axioms as high-weight priors instead of ordinary list
    items — wrapping here (not in the LLM prompt) keeps the tagging
    deterministic and independent of model format adherence.

    Consecutive non-bullet lines are folded into the previous bullet
    so a rare multi-line principle still becomes one ``<axiom>``.
    If the text contains no bullets, returns it stripped as-is —
    safer than silently dropping content we don't recognize.
    """
    items: list[str] = []
    current: list[str] = []
    saw_bullet = False

    def _flush() -> None:
        if not current:
            return
        joined = " ".join(part.strip() for part in current).strip()
        if joined:
            items.append(joined)
        current.clear()

    for line in text.splitlines():
        match = _BULLET_RE.match(line)
        if match:
            _flush()
            current.append(match.group(1))
            saw_bullet = True
        elif line.strip():
            current.append(line.strip())
        else:
            _flush()
    _flush()

    if not saw_bullet or not items:
        return text.strip()
    return "\n".join(f"<axiom>{item}</axiom>" for item in items)


def _insert_after_identity(prompt: str, section: str) -> str:
    """Insert a section after the Identity heading. Falls back to prepend.

    The "next heading" search is level-agnostic: we cut off at the
    first heading of any depth that follows Identity, so personas
    using either ``# Identity`` + ``# Core Philosophy`` (scaffold
    style) or ``## Identity`` + ``## Rules`` (builtin / legacy)
    both work.
    """
    m = _IDENTITY_RE.search(prompt)
    if not m:
        return section + "\n\n" + prompt

    next_heading = _ANY_HEADING_RE.search(prompt, m.end())
    if next_heading:
        pos = next_heading.start()
    else:
        pos = len(prompt)

    return prompt[:pos].rstrip() + "\n\n" + section + "\n\n" + prompt[pos:].lstrip()


def _insert_before_rules(prompt: str, section: str) -> str:
    """Insert a section just before ## Rules. Falls back to append."""
    m = _RULES_RE.search(prompt)
    if not m:
        return prompt + "\n\n" + section
    return prompt[:m.start()].rstrip() + "\n\n" + section + "\n\n" + prompt[m.start():]
