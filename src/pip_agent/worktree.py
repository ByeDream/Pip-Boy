"""Git worktree manager for subagent task isolation.

Each subagent gets its own worktree at ``.pip/.worktrees/{name}/``
with a feature branch ``wt/{name}``.  The manager handles creation,
syncing (merge main into feature), integration (merge feature into
main), and cleanup.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_name(name: str) -> None:
    """Raise ValueError if *name* is unsafe for use as a path component."""
    if not name or not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid worktree name {name!r}: only [a-zA-Z0-9_-] allowed"
        )


@dataclass
class MergeResult:
    ok: bool
    message: str
    conflict_files: list[str] | None = None


class WorktreeManager:
    """Manages git worktrees for subagent isolation."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._worktrees_root = workdir / ".pip" / ".worktrees"
        self._lock = threading.RLock()

    @property
    def worktrees_root(self) -> Path:
        return self._worktrees_root

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git"] + args
        return subprocess.run(
            cmd,
            cwd=cwd or self._workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def _main_branch(self) -> str:
        """Return the name of the current branch in WORKDIR."""
        r = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        return r.stdout.strip()

    def _conflict_files(self, cwd: Path) -> list[str]:
        r = self._git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd, check=False)
        return [f for f in r.stdout.strip().splitlines() if f]

    def worktree_path(self, name: str) -> Path:
        return self._worktrees_root / name

    def branch_name(self, name: str) -> str:
        return f"wt/{name}"

    def exists(self, name: str) -> bool:
        return self.worktree_path(name).is_dir()

    # -- lifecycle -----------------------------------------------------------

    def create(self, name: str) -> Path:
        """Create a worktree + feature branch for a subagent.

        Returns the worktree path.
        """
        _validate_name(name)
        wt_path = self.worktree_path(name)
        branch = self.branch_name(name)

        with self._lock:
            if wt_path.exists():
                return wt_path

            self._worktrees_root.mkdir(parents=True, exist_ok=True)
            main = self._main_branch()

            self._git([
                "worktree", "add",
                "-b", branch,
                str(wt_path),
                main,
            ])

        return wt_path

    def remove(self, name: str) -> None:
        """Remove worktree and delete the feature branch."""
        _validate_name(name)
        wt_path = self.worktree_path(name)
        branch = self.branch_name(name)

        with self._lock:
            if wt_path.exists():
                self._git(["worktree", "remove", "--force", str(wt_path)], check=False)
            self._git(["branch", "-D", branch], check=False)

    # -- sync / integrate ----------------------------------------------------

    def sync(self, name: str) -> MergeResult:
        """Merge main into the feature branch (run in worktree).

        This ensures the feature branch is up to date before review.
        """
        _validate_name(name)
        wt_path = self.worktree_path(name)

        with self._lock:
            if not wt_path.exists():
                return MergeResult(ok=False, message=f"Worktree '{name}' not found")

            main = self._main_branch()
            r = self._git(["merge", main], cwd=wt_path, check=False)

            if r.returncode != 0:
                conflicts = self._conflict_files(wt_path)
                if conflicts:
                    self._git(["merge", "--abort"], cwd=wt_path, check=False)
                    return MergeResult(
                        ok=False,
                        message=f"Merge conflicts with {main}: {', '.join(conflicts)}",
                        conflict_files=conflicts,
                    )
                return MergeResult(ok=False, message=f"Merge failed: {r.stderr.strip()}")

            return MergeResult(ok=True, message=f"Synced with {main}")

    def workdir_clean(self) -> bool:
        """Check if WORKDIR has no uncommitted changes."""
        r = self._git(["status", "--porcelain"])
        return not r.stdout.strip()

    def integrate(self, name: str) -> MergeResult:
        """Merge feature branch into main (three-stage: check clean, re-sync, merge).

        Must be called from the Lead's context (WORKDIR).
        """
        _validate_name(name)
        if not self.workdir_clean():
            return MergeResult(
                ok=False,
                message="WORKDIR has uncommitted changes. Commit first.",
            )

        sync_result = self.sync(name)
        if not sync_result.ok:
            return sync_result

        branch = self.branch_name(name)
        task_id = name

        with self._lock:
            r = self._git(
                ["merge", "--no-ff", branch, "-m", f"merge: {task_id} by {name}"],
                check=False,
            )
            if r.returncode != 0:
                conflicts = self._conflict_files(self._workdir)
                if conflicts:
                    self._git(["merge", "--abort"], check=False)
                    return MergeResult(
                        ok=False,
                        message=f"Integration conflicts: {', '.join(conflicts)}",
                        conflict_files=conflicts,
                    )
                return MergeResult(
                    ok=False,
                    message=f"Integration failed: {r.stderr.strip()}",
                )

        return MergeResult(ok=True, message=f"Merged {branch} into {self._main_branch()}")
