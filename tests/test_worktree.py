"""Tests for the WorktreeManager."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pip_agent.worktree import WorktreeManager


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit and .pip/ ignored."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=path)
    _git(["config", "user.email", "test@test.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / ".gitignore").write_text(".pip/\n")
    (path / "README.md").write_text("# Test\n")
    _git(["add", "."], cwd=path)
    _git(["commit", "-m", "initial"], cwd=path)
    return path


class TestWorktreeManager:
    def test_create_and_exists(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)

        assert not mgr.exists("coder")
        wt = mgr.create("coder")
        assert wt == mgr.worktree_path("coder")
        assert wt.is_dir()
        assert mgr.exists("coder")
        assert (wt / "README.md").is_file()

    def test_create_idempotent(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        wt1 = mgr.create("coder")
        wt2 = mgr.create("coder")
        assert wt1 == wt2

    def test_branch_name(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        assert mgr.branch_name("coder") == "wt/coder"

    def test_create_makes_feature_branch(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        mgr.create("coder")
        r = _git(["branch", "--list", "wt/coder"], cwd=repo)
        assert "wt/coder" in r.stdout

    def test_remove_cleans_up(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        mgr.create("coder")
        assert mgr.exists("coder")
        mgr.remove("coder")
        assert not mgr.exists("coder")
        r = _git(["branch", "--list", "wt/coder"], cwd=repo)
        assert "wt/coder" not in r.stdout

    def test_remove_nonexistent_is_noop(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        mgr.remove("ghost")

    def test_sync_no_conflicts(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        wt = mgr.create("coder")

        (repo / "main_file.txt").write_text("from main\n")
        _git(["add", "."], cwd=repo)
        _git(["commit", "-m", "main work"], cwd=repo)

        result = mgr.sync("coder")
        assert result.ok
        assert (wt / "main_file.txt").is_file()

    def test_sync_with_conflict(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        wt = mgr.create("coder")

        (repo / "README.md").write_text("main version\n")
        _git(["add", "."], cwd=repo)
        _git(["commit", "-m", "main edit"], cwd=repo)

        (wt / "README.md").write_text("coder version\n")
        _git(["add", "."], cwd=wt)
        _git(["commit", "-m", "coder edit"], cwd=wt)

        result = mgr.sync("coder")
        assert not result.ok
        assert result.conflict_files
        assert "README.md" in result.conflict_files

    def test_sync_nonexistent_worktree(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        result = mgr.sync("ghost")
        assert not result.ok

    def test_workdir_clean(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        assert mgr.workdir_clean()

        (repo / "dirty.txt").write_text("dirty\n")
        assert not mgr.workdir_clean()

    def test_integrate_success(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        wt = mgr.create("coder")

        (wt / "feature.txt").write_text("feature work\n")
        _git(["add", "."], cwd=wt)
        _git(["commit", "-m", "feature work"], cwd=wt)

        result = mgr.integrate("coder")
        assert result.ok
        assert (repo / "feature.txt").is_file()

    def test_integrate_rejects_dirty_workdir(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        wt = mgr.create("coder")

        (wt / "feature.txt").write_text("feature\n")
        _git(["add", "."], cwd=wt)
        _git(["commit", "-m", "feat"], cwd=wt)

        (repo / "uncommitted.txt").write_text("wip\n")
        result = mgr.integrate("coder")
        assert not result.ok
        assert "uncommitted" in result.message.lower()

    def test_integrate_with_conflict(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        wt = mgr.create("coder")

        (repo / "README.md").write_text("main version\n")
        _git(["add", "."], cwd=repo)
        _git(["commit", "-m", "main edit"], cwd=repo)

        (wt / "README.md").write_text("coder version\n")
        _git(["add", "."], cwd=wt)
        _git(["commit", "-m", "coder edit"], cwd=wt)

        result = mgr.integrate("coder")
        assert not result.ok
        assert result.conflict_files

    def test_worktrees_root_path(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)
        assert mgr.worktrees_root == repo / ".pip" / ".worktrees"

    def test_agent_id_scopes_root(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo, agent_id="pip-boy")
        assert mgr.worktrees_root == repo / ".pip" / "agents" / "pip-boy" / "worktrees"

    def test_agent_id_scopes_branch_name(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo, agent_id="pip-boy")
        assert mgr.branch_name("coder") == "wt/pip-boy/coder"

    def test_two_agents_same_teammate_no_collision(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        mgr_a = WorktreeManager(repo, agent_id="agent-a")
        mgr_b = WorktreeManager(repo, agent_id="agent-b")

        wt_a = mgr_a.create("coder")
        wt_b = mgr_b.create("coder")

        assert wt_a != wt_b
        assert wt_a.is_dir()
        assert wt_b.is_dir()
        assert mgr_a.exists("coder")
        assert mgr_b.exists("coder")

        r = _git(["branch", "--list", "wt/agent-a/coder"], cwd=repo)
        assert "wt/agent-a/coder" in r.stdout
        r = _git(["branch", "--list", "wt/agent-b/coder"], cwd=repo)
        assert "wt/agent-b/coder" in r.stdout

        mgr_a.remove("coder")
        assert not mgr_a.exists("coder")
        assert mgr_b.exists("coder")
        mgr_b.remove("coder")

    def test_agent_id_full_lifecycle(self, tmp_path: Path):
        """Create -> work -> sync -> integrate -> remove with agent_id."""
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo, agent_id="my-agent")

        wt = mgr.create("coder")
        assert wt == repo / ".pip" / "agents" / "my-agent" / "worktrees" / "coder"
        (wt / "new_feature.py").write_text("def hello(): pass\n")
        _git(["add", "."], cwd=wt)
        _git(["commit", "-m", "add feature"], cwd=wt)

        assert mgr.sync("coder").ok
        assert mgr.integrate("coder").ok
        assert (repo / "new_feature.py").is_file()

        mgr.remove("coder")
        assert not mgr.exists("coder")

    def test_full_lifecycle(self, tmp_path: Path):
        """Create -> work -> sync -> integrate -> remove."""
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo)

        wt = mgr.create("coder")
        (wt / "new_feature.py").write_text("def hello(): pass\n")
        _git(["add", "."], cwd=wt)
        _git(["commit", "-m", "add feature"], cwd=wt)

        assert mgr.sync("coder").ok
        assert mgr.integrate("coder").ok
        assert (repo / "new_feature.py").is_file()

        mgr.remove("coder")
        assert not mgr.exists("coder")
