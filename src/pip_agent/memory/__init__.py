"""Memory subsystem: per-agent behavioural memory with three-tier pipeline.

Storage layout
--------------
Each agent's memory lives directly under its own ``.pip/`` directory:

    <agent_dir>/
        state.json
        observations/<date>.jsonl
        memories.json
        axioms.md

For the root ``pip-boy`` agent ``<agent_dir>`` is ``WORKDIR/.pip``.
For a sub-agent ``X`` it is ``WORKDIR/X/.pip``.

Addressbook (user profiles) is **workspace-shared** — one flat directory
under the root ``.pip`` that every agent reads and writes. Every contact
is keyed by a stable opaque ``user_id`` (8-char hex) so the model can
pass identities around without confusing them for human names:

    <workspace_pip_dir>/addressbook/<user_id>.md

There is no "owner" concept. Whoever is using Pip is just another
contact the agent learns through conversation and records via
``remember_user``. Sub-agents use the same tool and see the same
addressbook as the root agent. Profile *content* is never auto-injected
into the system prompt — the agent loads it on demand via
``lookup_user`` using the ``user_id`` carried on each ``<user_query>``.

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
        # Workspace-shared scope: addressbook/ (user profiles) lives
        # here, visible to every agent in the workspace.
        self.pip_dir = workspace_pip_dir
        self._io_lock = threading.Lock()
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_dir / "observations").mkdir(exist_ok=True)
        # Addressbook is shared — only the root's workspace_pip_dir
        # actually owns the directory on disk, but every MemoryStore
        # self-heals it so a pre-root sub-agent invocation still works.
        self.pip_dir.mkdir(parents=True, exist_ok=True)
        (self.pip_dir / "addressbook").mkdir(exist_ok=True)

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
    # Addressbook (user profiles, workspace-shared, tool-managed)
    #
    # Each contact is keyed by an opaque 8-char hex ``user_id`` that
    # doubles as its filename (``<user_id>.md``). The agent passes this
    # id around in ``<user_query user_id=...>`` wrappers and retrieves
    # details on demand via ``lookup_user``. Profile *content* is never
    # injected into the system prompt — lazy loading keeps token cost
    # flat no matter how many contacts accumulate.
    # ------------------------------------------------------------------

    _FIELD_MAP: dict[str, str] = {
        "name": "Name",
        "call_me": "What to call them",
        "timezone": "Timezone",
        "notes": "Notes",
    }

    @property
    def addressbook_dir(self) -> Path:
        return self.pip_dir / "addressbook"

    def _all_profile_paths(self) -> list[Path]:
        ab = self.addressbook_dir
        if not ab.is_dir():
            return []
        return sorted(ab.glob("*.md"))

    @staticmethod
    def _name_to_user_id(name: str) -> str:
        """Deterministic 16-char hex id from a display name.

        ``sha256(NFKC(name.strip().lower()))[:16]`` — stable across
        platforms, case-insensitive, Unicode-normalised.
        """
        import hashlib
        import unicodedata

        normalised = unicodedata.normalize(
            "NFKC", name.strip().lower(),
        )
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def extract_user_id(path: Path) -> str:
        """Derive ``user_id`` from a contact file path (filename stem)."""
        return path.stem

    def find_profile_by_sender(
        self, channel: str, sender_id: str,
    ) -> Path | None:
        """Find which contact file contains this channel:sender_id."""
        if not sender_id:
            return None
        target = f"{channel}:{sender_id}"
        expected = f"- `{target}`"
        for path in self._all_profile_paths():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() == expected:
                        return path
            except OSError:
                continue
        return None

    def load_profile_by_id(self, user_id: str) -> str | None:
        """Return the markdown body of ``<user_id>.md``, or None."""
        if not user_id or not _USER_ID_RE.fullmatch(user_id):
            return None
        path = self.addressbook_dir / f"{user_id}.md"
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _read_profile_fields(
        self, path: Path,
    ) -> tuple[dict[str, str], list[str]]:
        """Parse an existing contact file into (fields, identifiers)."""
        current: dict[str, str] = {}
        current_ids: list[str] = []
        if not path.is_file():
            return current, current_ids
        try:
            in_ids = False
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                for key, label in self._FIELD_MAP.items():
                    prefix = f"- **{label}:**"
                    if stripped.startswith(prefix):
                        current[key] = stripped[len(prefix):].strip()
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
        return current, current_ids

    def _write_profile(
        self,
        user_id: str,
        fields: dict[str, str],
        identifiers: list[str],
    ) -> Path:
        display = fields.get("call_me") or fields.get("name") or "User"
        lines = [
            f"# {display}", "",
            "_Profile managed by Pip._", "",
            f"- **ID:** {user_id}",
        ]
        for key, label in self._FIELD_MAP.items():
            lines.append(f"- **{label}:** {fields.get(key, '')}")
        if identifiers:
            lines.append("- **Identifiers:**")
            for ident in identifiers:
                lines.append(f"  - `{ident}`")
        lines.append("")

        target = self.addressbook_dir / f"{user_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-flush can't leave a contact
        # half-written — the file is the source of truth for identity
        # binding and the aliases list.
        from pip_agent.fileutil import atomic_write
        atomic_write(target, "\n".join(lines))
        return target

    def upsert_contact(
        self,
        *,
        sender_id: str = "",
        channel: str = "",
        **fields: str,
    ) -> tuple[str, str]:
        """Create or update a contact keyed by name hash.

        The ``name`` field is required — ``sha256(NFKC(name.lower()))[:16]``
        produces a deterministic ``user_id`` so the same person calling
        from different channels converges on a single profile.

        Returns ``(user_id, message)``.
        """
        name = fields.get("name", "")
        if not name:
            raise ValueError("'name' is required to derive user_id")

        if sender_id and channel and sender_id.startswith(f"{channel}:"):
            sender_id = sender_id[len(channel) + 1:]

        uid = self._name_to_user_id(name)
        path = self.addressbook_dir / f"{uid}.md"

        if path.is_file():
            return uid, self.update_contact(
                uid, sender_id=sender_id, channel=channel, **fields,
            )

        new_fields: dict[str, str] = {
            k: v for k, v in fields.items() if k in self._FIELD_MAP and v
        }
        identifiers: list[str] = []
        if sender_id and channel:
            identifiers.append(f"{channel}:{sender_id}")

        self._write_profile(uid, new_fields, identifiers)
        return uid, f"Created contact {uid}."

    def update_contact(
        self,
        user_id: str,
        *,
        sender_id: str = "",
        channel: str = "",
        **fields: str,
    ) -> str:
        """Update an existing contact by ``user_id``.

        Optionally records a newly-seen ``channel:sender_id`` pair if
        the caller is reaching this contact from an identifier the
        profile hasn't seen before.
        """
        if not _USER_ID_RE.fullmatch(user_id):
            return f"Invalid user_id: {user_id!r}."

        path = self.addressbook_dir / f"{user_id}.md"
        if not path.is_file():
            return f"No contact with user_id={user_id}."

        if sender_id and channel and sender_id.startswith(f"{channel}:"):
            sender_id = sender_id[len(channel) + 1:]
        new_sender_key = (
            f"{channel}:{sender_id}" if sender_id and channel else ""
        )

        current, current_ids = self._read_profile_fields(path)
        updated_keys: list[str] = []
        for key, value in fields.items():
            if key not in self._FIELD_MAP or not value:
                continue
            if key == "notes" and current.get("notes"):
                current[key] = current[key] + "; " + value
            else:
                current[key] = value
            updated_keys.append(key)

        if new_sender_key and new_sender_key not in current_ids:
            current_ids.append(new_sender_key)
            updated_keys.append("identifier")

        if not updated_keys:
            return "No fields to update."

        self._write_profile(user_id, current, current_ids)
        return (
            f"Updated addressbook entry ({user_id}): {', '.join(updated_keys)}"
        )

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
          1. ## Judgment Principles — per-agent axioms
          2. ## Recalled Context — TF-IDF matched memories
          3. ## Context — runtime metadata
          4. ## Channel — channel hints

        Note: addressbook content is **not** injected here. The agent
        receives the caller's ``user_id`` on each ``<user_query>`` and
        loads the profile on demand via the ``lookup_user`` tool. This
        keeps the prompt token cost flat as the addressbook grows.
        """
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

# Addressbook user_id: lowercase 16-hex, derived from
# sha256(NFKC(name.lower().strip())).  Deterministic so the same
# display name always maps to the same profile, enabling cross-channel
# identity linking without a search tool.
# Also accepts legacy 8-hex ids for backward compatibility.
_USER_ID_RE = re.compile(r"[0-9a-f]{8}(?:[0-9a-f]{8})?")


# ----------------------------------------------------------------------
# Prompt section helpers
# ----------------------------------------------------------------------

# Axioms inject *before* the Rules heading (both scaffold ``# Rules``
# and legacy ``## Rules``). Addressbook injection was removed when
# contact profiles moved to lazy ``lookup_user`` loading.
_RULES_RE = re.compile(r"^#+\s+Rules\b", re.MULTILINE)
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


def _insert_before_rules(prompt: str, section: str) -> str:
    """Insert a section just before ## Rules. Falls back to append."""
    m = _RULES_RE.search(prompt)
    if not m:
        return prompt + "\n\n" + section
    return prompt[:m.start()].rstrip() + "\n\n" + section + "\n\n" + prompt[m.start():]
