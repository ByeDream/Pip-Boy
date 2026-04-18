from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

TaskStatus = Literal[
    "pending", "in_progress", "in_review", "merged", "failed", "completed",
]

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_META_FILE = "_meta.json"


@dataclass
class Task:
    id: str
    title: str
    status: TaskStatus = "pending"
    blocked_by: list[str] = field(default_factory=list)
    owner: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "blocked_by": self.blocked_by,
            "owner": self.owner,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        return cls(
            id=data["id"],
            title=data["title"],
            status=data.get("status", "pending"),
            blocked_by=data.get("blocked_by", []),
            owner=data.get("owner", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class StoryMeta:
    id: str
    title: str
    blocked_by: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "blocked_by": self.blocked_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StoryMeta:
        return cls(
            id=data["id"],
            title=data["title"],
            blocked_by=data.get("blocked_by", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _items_json(items: list[Task] | list[StoryMeta]) -> str:
    return json.dumps(
        [i.to_dict() for i in items], indent=2, ensure_ascii=False,
    )


def _validate_id(node_id: str) -> None:
    if not _SAFE_ID.match(node_id):
        raise ValueError(
            f"Invalid id '{node_id}': must be 1-64 chars, "
            "alphanumeric/dash/underscore, starting with alphanumeric"
        )


# ======================================================================
# DAG helpers (pure functions, reused at both story and task level)
# ======================================================================

def _check_no_cycle(nodes: dict[str, list[str]]) -> None:
    """Detect cycles in a {id: blocked_by} graph via DFS."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in nodes}

    def dfs(nid: str) -> None:
        color[nid] = GRAY
        for dep in nodes[nid]:
            if dep not in color:
                continue
            if color[dep] == GRAY:
                raise ValueError(f"Cycle detected: '{nid}' -> '{dep}'")
            if color[dep] == WHITE:
                dfs(dep)
        color[nid] = BLACK

    for nid in nodes:
        if color[nid] == WHITE:
            dfs(nid)


def _check_refs(nodes: dict[str, list[str]], all_ids: set[str]) -> None:
    """Ensure all blocked_by references point to existing IDs."""
    for nid, deps in nodes.items():
        missing = set(deps) - all_ids
        if missing:
            raise ValueError(
                f"'{nid}' references non-existent: "
                f"{', '.join(sorted(missing))}"
            )


def _is_blocked_by_status(
    blocked_by: list[str], statuses: dict[str, TaskStatus],
) -> bool:
    """Return True if any dependency is not completed."""
    for dep_id in blocked_by:
        st = statuses.get(dep_id)
        if st is not None and st != "completed":
            return True
    return False


# ======================================================================
# _NodeGraph -- flat DAG engine for one directory of JSON files
# ======================================================================

class _NodeGraph:
    """Manages Task JSON files in a single directory (excludes _meta.json)."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def load_all(self) -> dict[str, Task]:
        tasks: dict[str, Task] = {}
        for p in sorted(self._dir.glob("*.json")):
            if p.name == _META_FILE:
                continue
            try:
                t = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
                if t.id != p.stem and t.id in tasks:
                    log.warning(
                        "Task file %s has id '%s' which conflicts with "
                        "an already loaded task; skipping",
                        p.name, t.id,
                    )
                    continue
                tasks[t.id] = t
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("Skipping corrupted task file %s: %s", p.name, exc)
                continue
        return tasks

    def save(self, task: Task) -> None:
        task.updated_at = _now_iso()
        self._path(task.id).write_text(
            json.dumps(task.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def delete(self, task_id: str) -> None:
        p = self._path(task_id)
        if p.is_file():
            p.unlink()

    def has_tasks(self) -> bool:
        return any(
            p for p in self._dir.glob("*.json") if p.name != _META_FILE
        )

    def create(self, items: list[dict]) -> None:
        if not items:
            raise ValueError("No tasks provided")

        all_existing = self.load_all()
        new_tasks: list[Task] = []

        for entry in items:
            tid = entry.get("id", "")
            _validate_id(tid)
            if tid in all_existing:
                raise ValueError(f"Task '{tid}' already exists")

            title = entry.get("title", "").strip()
            if not title:
                raise ValueError(f"Task '{tid}': title is required")

            now = _now_iso()
            task = Task(
                id=tid,
                title=title,
                status="pending",
                blocked_by=entry.get("blocked_by", []),
                created_at=now,
                updated_at=now,
            )
            new_tasks.append(task)
            all_existing[tid] = task

        all_ids = set(all_existing.keys())
        _check_refs(
            {t.id: t.blocked_by for t in all_existing.values()}, all_ids,
        )
        _check_no_cycle(
            {t.id: t.blocked_by for t in all_existing.values()},
        )

        for task in new_tasks:
            self.save(task)

    def update(
        self, items: list[dict], *, story_blocked: bool = False,
    ) -> list[Task]:
        if not items:
            raise ValueError("No tasks provided")

        all_tasks = self.load_all()
        modified: list[Task] = []

        for entry in items:
            tid = entry.get("id", "")
            if tid not in all_tasks:
                raise ValueError(f"Task '{tid}' not found")

            task = all_tasks[tid]

            if "title" in entry:
                title = entry["title"].strip()
                if not title:
                    raise ValueError(f"Task '{tid}': title cannot be empty")
                task.title = title

            if "blocked_by" in entry:
                task.blocked_by = entry["blocked_by"]
            elif "add_blocked_by" in entry or "remove_blocked_by" in entry:
                to_add = set(entry.get("add_blocked_by", []))
                to_remove = set(entry.get("remove_blocked_by", []))
                task.blocked_by = list(
                    (set(task.blocked_by) | to_add) - to_remove
                )

            if "owner" in entry:
                task.owner = entry["owner"]

            if "status" in entry:
                new_status: TaskStatus = entry["status"]
                _VALID_STATUSES = {
                    "pending", "in_progress", "in_review",
                    "merged", "failed", "completed",
                }
                if new_status not in _VALID_STATUSES:
                    raise ValueError(
                        f"Task '{tid}': invalid status '{new_status}'"
                    )
                if new_status == "in_progress":
                    if story_blocked:
                        raise ValueError(
                            f"Task '{tid}': parent story is blocked"
                        )
                    if task.blocked_by:
                        raise ValueError(
                            f"Task '{tid}' is blocked by: "
                            f"{', '.join(task.blocked_by)}"
                        )
                task.status = new_status

                if new_status == "completed":
                    for sibling in all_tasks.values():
                        if tid in sibling.blocked_by:
                            sibling.blocked_by.remove(tid)
                            if sibling not in modified:
                                modified.append(sibling)

            if task not in modified:
                modified.append(task)

        all_ids = set(all_tasks.keys())
        _check_refs(
            {t.id: t.blocked_by for t in all_tasks.values()}, all_ids,
        )
        _check_no_cycle(
            {t.id: t.blocked_by for t in all_tasks.values()},
        )

        for task in modified:
            self.save(task)

        requested_ids = {e.get("id", "") for e in items}
        return [t for t in modified if t.id in requested_ids]

    def remove(self, task_ids: list[str]) -> None:
        if not task_ids:
            raise ValueError("No task IDs provided")

        all_tasks = self.load_all()
        remove_set = set(task_ids)

        for tid in task_ids:
            if tid not in all_tasks:
                raise ValueError(f"Task '{tid}' not found")
            dependents = [
                t.id for t in all_tasks.values()
                if tid in t.blocked_by and t.id not in remove_set
            ]
            if dependents:
                raise ValueError(
                    f"Cannot remove '{tid}': depended on by "
                    f"{', '.join(dependents)}"
                )

        for tid in task_ids:
            self.delete(tid)

    def render(self) -> str:
        all_tasks = self.load_all()
        if not all_tasks:
            return "(no tasks)"

        ready: list[Task] = []
        blocked_list: list[Task] = []
        wip: list[Task] = []
        in_review: list[Task] = []
        merged: list[Task] = []
        failed: list[Task] = []
        done: list[Task] = []

        for t in all_tasks.values():
            if t.status == "completed":
                done.append(t)
            elif t.status == "in_review":
                in_review.append(t)
            elif t.status == "merged":
                merged.append(t)
            elif t.status == "failed":
                failed.append(t)
            elif t.status == "in_progress":
                wip.append(t)
            elif t.blocked_by:
                blocked_list.append(t)
            else:
                ready.append(t)

        lines: list[str] = []

        if wip:
            lines.append("  IN PROGRESS:")
            for t in wip:
                owner_tag = f", owner: {t.owner}" if t.owner else ""
                lines.append(f"    [>] {t.title}  (id: {t.id}{owner_tag})")
        if in_review:
            lines.append("  IN REVIEW:")
            for t in in_review:
                owner_tag = f", owner: {t.owner}" if t.owner else ""
                lines.append(f"    [R] {t.title}  (id: {t.id}{owner_tag})")
        if merged:
            lines.append("  MERGED:")
            for t in merged:
                owner_tag = f", owner: {t.owner}" if t.owner else ""
                lines.append(f"    [M] {t.title}  (id: {t.id}{owner_tag})")
        if failed:
            lines.append("  FAILED:")
            for t in failed:
                owner_tag = f", owner: {t.owner}" if t.owner else ""
                lines.append(f"    [!] {t.title}  (id: {t.id}{owner_tag})")
        if ready:
            lines.append("  READY:")
            for t in ready:
                lines.append(f"    [ ] {t.title}  (id: {t.id})")
        if blocked_list:
            lines.append("  BLOCKED:")
            for t in blocked_list:
                lines.append(
                    f"    [x] {t.title}  (id: {t.id}, waiting: {', '.join(t.blocked_by)})"
                )
        if done:
            lines.append("  COMPLETED:")
            for t in done:
                lines.append(f"    [v] {t.title}  (id: {t.id})")

        total = len(all_tasks)
        lines.append(
            f"  ({len(done)}/{total} completed, "
            f"{len(wip)} in progress, {len(ready)} ready, "
            f"{len(blocked_list)} blocked)"
        )
        return "\n".join(lines)


# ======================================================================
# PlanManager -- two-level Story / Task graph
# ======================================================================

class PlanManager:
    """Two-level Story/Task graph persisted under a root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._claim_lock = threading.RLock()
        self._board_revision = 0

    @property
    def board_revision(self) -> int:
        with self._claim_lock:
            return self._board_revision

    def _bump_board_revision(self) -> None:
        with self._claim_lock:
            self._board_revision += 1

    # ------------------------------------------------------------------
    # Story I/O
    # ------------------------------------------------------------------

    def _story_dir(self, story_id: str) -> Path:
        return self._root / story_id

    def _meta_path(self, story_id: str) -> Path:
        return self._story_dir(story_id) / _META_FILE

    def _load_meta(self, story_id: str) -> StoryMeta:
        p = self._meta_path(story_id)
        if not p.is_file():
            raise ValueError(f"Story '{story_id}' not found")
        return StoryMeta.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def _save_meta(self, meta: StoryMeta) -> None:
        d = self._story_dir(meta.id)
        d.mkdir(parents=True, exist_ok=True)
        meta.updated_at = _now_iso()
        self._meta_path(meta.id).write_text(
            json.dumps(meta.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _story_exists(self, story_id: str) -> bool:
        return self._meta_path(story_id).is_file()

    def _load_all_metas(self) -> dict[str, StoryMeta]:
        metas: dict[str, StoryMeta] = {}
        for d in sorted(self._root.iterdir()):
            mp = d / _META_FILE
            if d.is_dir() and mp.is_file():
                try:
                    m = StoryMeta.from_dict(
                        json.loads(mp.read_text(encoding="utf-8"))
                    )
                    metas[m.id] = m
                except (json.JSONDecodeError, KeyError):
                    continue
        return metas

    def _task_graph(self, story_id: str) -> _NodeGraph:
        return _NodeGraph(self._story_dir(story_id))

    # ------------------------------------------------------------------
    # Story status derivation
    # ------------------------------------------------------------------

    def _derive_status(self, story_id: str) -> TaskStatus:
        ng = self._task_graph(story_id)
        tasks = ng.load_all()
        if not tasks:
            return "pending"
        statuses = {t.status for t in tasks.values()}
        if statuses == {"completed"}:
            return "completed"
        active = {"in_progress", "in_review", "merged", "failed"}
        if statuses & active:
            return "in_progress"
        return "pending"

    def _is_story_blocked(self, story_id: str) -> bool:
        meta = self._load_meta(story_id)
        for dep_id in meta.blocked_by:
            if self._story_exists(dep_id):
                dep_status = self._derive_status(dep_id)
                if dep_status != "completed":
                    return True
        return False

    # ------------------------------------------------------------------
    # Auto-prune completed stories
    # ------------------------------------------------------------------

    def _prune_completed_stories(self) -> list[str]:
        """Delete completed story directories. Returns list of pruned story IDs."""
        pruned: list[str] = []
        changed = True
        while changed:
            changed = False
            for sid, _meta in list(self._load_all_metas().items()):
                if self._derive_status(sid) == "completed":
                    shutil.rmtree(self._story_dir(sid))
                    pruned.append(sid)
                    changed = True
        return pruned

    # ------------------------------------------------------------------
    # CRUD: story=None -> story level, story=str -> task level
    # ------------------------------------------------------------------

    def create(self, story: str | None, items: list[dict]) -> str:
        if not items:
            raise ValueError("No items provided")
        with self._claim_lock:
            if story is None:
                return self._create_stories(items)
            return self._create_tasks(story, items)

    def _create_stories(self, items: list[dict]) -> str:
        all_metas = self._load_all_metas()

        new_metas: list[StoryMeta] = []
        for entry in items:
            sid = entry.get("id", "")
            _validate_id(sid)
            if sid in all_metas:
                raise ValueError(f"Story '{sid}' already exists")

            title = entry.get("title", "").strip()
            if not title:
                raise ValueError(f"Story '{sid}': title is required")

            now = _now_iso()
            meta = StoryMeta(
                id=sid,
                title=title,
                blocked_by=entry.get("blocked_by", []),
                created_at=now,
                updated_at=now,
            )
            new_metas.append(meta)
            all_metas[sid] = meta

        all_ids = set(all_metas.keys())
        _check_refs(
            {m.id: m.blocked_by for m in all_metas.values()}, all_ids,
        )
        _check_no_cycle(
            {m.id: m.blocked_by for m in all_metas.values()},
        )

        for meta in new_metas:
            self._save_meta(meta)

        self._bump_board_revision()
        names = ", ".join(f"'{m.id}'" for m in new_metas)
        notice = f"<notice>Story {names} created.</notice>\n"
        return notice + _items_json(new_metas)

    def _create_tasks(self, story_id: str, items: list[dict]) -> str:
        if not self._story_exists(story_id):
            raise ValueError(f"Story '{story_id}' not found")
        ng = self._task_graph(story_id)
        ng.create(items)
        created_ids = {e["id"] for e in items}
        all_tasks = ng.load_all()
        created = [all_tasks[tid] for tid in created_ids if tid in all_tasks]
        self._bump_board_revision()
        notice = f"<notice>Tasks added to story '{story_id}'.</notice>\n"
        return notice + _items_json(created)

    def update(self, story: str | None, items: list[dict]) -> str:
        if not items:
            raise ValueError("No items provided")
        with self._claim_lock:
            if story is None:
                return self._update_stories(items)
            return self._update_tasks(story, items)

    def _update_stories(self, items: list[dict]) -> str:
        all_metas = self._load_all_metas()

        modified: list[StoryMeta] = []
        for entry in items:
            sid = entry.get("id", "")
            if sid not in all_metas:
                raise ValueError(f"Story '{sid}' not found")

            if "status" in entry:
                raise ValueError(
                    f"Story '{sid}': status is auto-derived and cannot be set"
                )

            meta = all_metas[sid]

            if "title" in entry:
                title = entry["title"].strip()
                if not title:
                    raise ValueError(f"Story '{sid}': title cannot be empty")
                meta.title = title

            if "blocked_by" in entry:
                meta.blocked_by = entry["blocked_by"]
            elif "add_blocked_by" in entry or "remove_blocked_by" in entry:
                to_add = set(entry.get("add_blocked_by", []))
                to_remove = set(entry.get("remove_blocked_by", []))
                meta.blocked_by = list(
                    (set(meta.blocked_by) | to_add) - to_remove
                )

            modified.append(meta)

        all_ids = set(all_metas.keys())
        _check_refs(
            {m.id: m.blocked_by for m in all_metas.values()}, all_ids,
        )
        _check_no_cycle(
            {m.id: m.blocked_by for m in all_metas.values()},
        )

        for meta in modified:
            self._save_meta(meta)

        self._bump_board_revision()
        return _items_json(modified)

    def _update_tasks(self, story_id: str, items: list[dict]) -> str:
        if not self._story_exists(story_id):
            raise ValueError(f"Story '{story_id}' not found")

        blocked = self._is_story_blocked(story_id)
        ng = self._task_graph(story_id)
        updated = ng.update(items, story_blocked=blocked)

        pruned = self._prune_completed_stories()

        self._bump_board_revision()
        notices: list[str] = []
        for sid in pruned:
            notices.append(f"<notice>Story '{sid}' completed and removed.</notice>")
        if notices:
            return "\n".join(notices) + "\n" + self.render()
        return _items_json(updated)

    def remove(self, story: str | None, ids: list[str]) -> str:
        if not ids:
            raise ValueError("No IDs provided")
        with self._claim_lock:
            if story is None:
                return self._remove_stories(ids)
            return self._remove_tasks(story, ids)

    def _remove_stories(self, story_ids: list[str]) -> str:
        all_metas = self._load_all_metas()
        remove_set = set(story_ids)

        for sid in story_ids:
            if sid not in all_metas:
                raise ValueError(f"Story '{sid}' not found")
            dependents = [
                m.id for m in all_metas.values()
                if sid in m.blocked_by and m.id not in remove_set
            ]
            if dependents:
                raise ValueError(
                    f"Cannot remove story '{sid}': depended on by "
                    f"{', '.join(dependents)}"
                )

        for sid in story_ids:
            shutil.rmtree(self._story_dir(sid))

        self._bump_board_revision()
        return f"Removed: {', '.join(story_ids)}"

    def _remove_tasks(self, story_id: str, task_ids: list[str]) -> str:
        if not self._story_exists(story_id):
            raise ValueError(f"Story '{story_id}' not found")
        ng = self._task_graph(story_id)
        ng.remove(task_ids)
        self._bump_board_revision()
        return f"Removed from '{story_id}': {', '.join(task_ids)}"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_tasks(self) -> bool:
        return any(
            d.is_dir() and (d / _META_FILE).is_file()
            for d in self._root.iterdir()
        ) if self._root.exists() else False

    def _first_claimable_slot(self) -> tuple[str, Task] | None:
        """First pending, unblocked, unowned task. Caller must hold ``_claim_lock``."""
        if not self._root.exists():
            return None
        all_metas = self._load_all_metas()
        if not all_metas:
            return None

        story_statuses: dict[str, TaskStatus] = {
            sid: self._derive_status(sid) for sid in all_metas
        }

        for sid, meta in all_metas.items():
            if story_statuses[sid] == "completed":
                continue
            if _is_blocked_by_status(meta.blocked_by, story_statuses):
                continue

            ng = self._task_graph(sid)
            tasks = ng.load_all()
            for t in tasks.values():
                if (
                    t.status == "pending"
                    and not t.blocked_by
                    and not t.owner
                ):
                    return (sid, t)
        return None

    def has_claimable_work(self) -> bool:
        """True if some task matches ``claim_next`` eligibility (read-only)."""
        with self._claim_lock:
            return self._first_claimable_slot() is not None

    def claim_next(self, owner: str) -> dict | None:
        """Atomically find and claim the next ready, unowned task.

        Scans stories (skipping completed/blocked) and within each finds
        the first task that is pending, unblocked, and unowned.
        Returns ``{"story": ..., "id": ..., "title": ...}`` or ``None``.
        """
        with self._claim_lock:
            slot = self._first_claimable_slot()
            if slot is None:
                return None
            sid, t = slot
            t.status = "in_progress"
            t.owner = owner
            ng = self._task_graph(sid)
            ng.save(t)
            self._bump_board_revision()
            return {"story": sid, "id": t.id, "title": t.title}

    def format_task(self, story_id: str, task_id: str) -> str:
        """Single-task summary for tooling. Returns ``[error] ...`` on failure."""
        with self._claim_lock:
            if not self._story_exists(story_id):
                return f"[error] Story '{story_id}' not found"
            if self._is_story_blocked(story_id):
                return f"[error] Story '{story_id}' is blocked by dependencies"
            ng = self._task_graph(story_id)
            tasks = ng.load_all()
            if task_id not in tasks:
                return f"[error] Task '{task_id}' not found in story '{story_id}'"
            t = tasks[task_id]
        lines = [
            f"Task: {t.title}",
            f"  id: {t.id}",
            f"  status: {t.status}",
            f"  owner: {t.owner or '(none)'}",
        ]
        if t.blocked_by:
            lines.append(f"  blocked_by: {', '.join(t.blocked_by)}")
        if t.created_at:
            lines.append(f"  created_at: {t.created_at}")
        if t.updated_at:
            lines.append(f"  updated_at: {t.updated_at}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, story: str | None = None) -> str:
        with self._claim_lock:
            if story is not None:
                return self._render_story(story)
            return self._render_overview()

    def _render_story(self, story_id: str) -> str:
        if not self._story_exists(story_id):
            raise ValueError(f"Story '{story_id}' not found")
        meta = self._load_meta(story_id)
        status = self._derive_status(story_id)
        ng = self._task_graph(story_id)

        lines = [f"Story: {meta.title}  (id: {meta.id}, status: {status})"]
        if meta.blocked_by:
            lines.append(f"  blocked_by: {', '.join(meta.blocked_by)}")
        lines.append(ng.render())
        return "\n".join(lines)

    def _render_overview(self) -> str:
        all_metas = self._load_all_metas()
        if not all_metas:
            return "(no stories)"

        story_statuses: dict[str, TaskStatus] = {}
        for sid in all_metas:
            story_statuses[sid] = self._derive_status(sid)

        ready_stories: list[StoryMeta] = []
        blocked_stories: list[StoryMeta] = []
        wip_stories: list[StoryMeta] = []

        for sid, meta in all_metas.items():
            st = story_statuses[sid]
            if st == "in_progress":
                wip_stories.append(meta)
            elif st == "pending":
                s_blocked = _is_blocked_by_status(
                    meta.blocked_by, story_statuses,
                )
                if s_blocked:
                    blocked_stories.append(meta)
                else:
                    ready_stories.append(meta)

        lines: list[str] = []

        if wip_stories:
            lines.append("STORIES IN PROGRESS:")
            for m in wip_stories:
                lines.append(f"  [>] {m.title}  (id: {m.id})")
        if ready_stories:
            lines.append("STORIES READY:")
            for m in ready_stories:
                lines.append(f"  [ ] {m.title}  (id: {m.id})")
        if blocked_stories:
            lines.append("STORIES BLOCKED:")
            for m in blocked_stories:
                waiting = [
                    d for d in m.blocked_by
                    if story_statuses.get(d, "completed") != "completed"
                ]
                lines.append(
                    f"  [x] {m.title}  (id: {m.id}, waiting: {', '.join(waiting)})"
                )

        kanban_ready: list[tuple[str, Task]] = []
        for sid, meta in all_metas.items():
            st = story_statuses[sid]
            if st == "completed":
                continue
            s_blocked = _is_blocked_by_status(
                meta.blocked_by, story_statuses,
            )
            if s_blocked:
                continue
            ng = self._task_graph(sid)
            tasks = ng.load_all()
            for t in tasks.values():
                if t.status == "pending" and not t.blocked_by:
                    kanban_ready.append((sid, t))

        if kanban_ready:
            lines.append("\nKANBAN (ready tasks across stories):")
            for sid, t in kanban_ready:
                lines.append(f"  [ ] {t.title}  (story: {sid}, id: {t.id})")

        return "\n".join(lines) if lines else "(no active stories)"
