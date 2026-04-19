from __future__ import annotations

import json
import threading

import pytest

from pip_agent.task_graph import PlanManager, _NodeGraph

# ======================================================================
# _NodeGraph (low-level DAG engine)
# ======================================================================

class TestNodeGraphCreate:
    def test_create_single(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        assert "a" in ng.load_all()

    def test_create_batch_cross_ref(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "blocked_by": ["a"]},
        ])
        assert ng.load_all()["b"].blocked_by == ["a"]

    def test_create_duplicate_fails(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        with pytest.raises(ValueError, match="already exists"):
            ng.create([{"id": "a", "title": "A2"}])

    def test_create_dangling_ref_fails(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        with pytest.raises(ValueError, match="non-existent"):
            ng.create([{"id": "a", "title": "A", "blocked_by": ["nope"]}])

    def test_cycle_fails(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        with pytest.raises(ValueError, match="Cycle"):
            ng.create([
                {"id": "a", "title": "A", "blocked_by": ["b"]},
                {"id": "b", "title": "B", "blocked_by": ["a"]},
            ])


class TestNodeGraphUpdate:
    def test_update_status(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        ng.update([{"id": "a", "status": "in_progress"}])
        assert ng.load_all()["a"].status == "in_progress"

    def test_start_blocked_fails(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "blocked_by": ["a"]},
        ])
        with pytest.raises(ValueError, match="blocked by"):
            ng.update([{"id": "b", "status": "in_progress"}])

    def test_story_blocked_prevents_start(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        with pytest.raises(ValueError, match="parent story is blocked"):
            ng.update([{"id": "a", "status": "in_progress"}], story_blocked=True)

    def test_owner_field(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        ng.update([{"id": "a", "owner": "agent-1", "status": "in_progress"}])
        task = ng.load_all()["a"]
        assert task.owner == "agent-1"
        assert task.status == "in_progress"

    def test_owner_persists_on_disk(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        ng.update([{"id": "a", "owner": "bot"}])
        ng2 = _NodeGraph(tmp_path)
        assert ng2.load_all()["a"].owner == "bot"

    def test_write_time_normalization(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "blocked_by": ["a"]},
            {"id": "c", "title": "C", "blocked_by": ["a"]},
        ])
        ng.update([{"id": "a", "status": "in_progress"}])
        ng.update([{"id": "a", "status": "completed"}])
        tasks = ng.load_all()
        assert tasks["b"].blocked_by == []
        assert tasks["c"].blocked_by == []

    def test_write_time_norm_persisted(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "blocked_by": ["a"]},
        ])
        ng.update([{"id": "a", "status": "in_progress"}])
        ng.update([{"id": "a", "status": "completed"}])
        ng2 = _NodeGraph(tmp_path)
        assert ng2.load_all()["b"].blocked_by == []

    def test_write_time_norm_unblocks_start(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "blocked_by": ["a"]},
        ])
        ng.update([{"id": "a", "status": "in_progress"}])
        ng.update([{"id": "a", "status": "completed"}])
        ng.update([{"id": "b", "status": "in_progress"}])
        assert ng.load_all()["b"].status == "in_progress"

    def test_add_blocked_by(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B"},
            {"id": "c", "title": "C"},
        ])
        ng.update([{"id": "c", "add_blocked_by": ["a", "b"]}])
        assert set(ng.load_all()["c"].blocked_by) == {"a", "b"}

    def test_remove_blocked_by(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B"},
            {"id": "c", "title": "C", "blocked_by": ["a", "b"]},
        ])
        ng.update([{"id": "c", "remove_blocked_by": ["a"]}])
        assert ng.load_all()["c"].blocked_by == ["b"]

    def test_blocked_by_full_replace_wins(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B"},
            {"id": "c", "title": "C", "blocked_by": ["a"]},
        ])
        ng.update([{"id": "c", "blocked_by": ["b"], "add_blocked_by": ["a"]}])
        assert ng.load_all()["c"].blocked_by == ["b"]

    def test_update_returns_affected_tasks(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
        result = ng.update([{"id": "a", "status": "in_progress"}])
        assert len(result) == 1
        assert result[0].id == "a"


class TestNodeGraphRemove:
    def test_remove_leaf(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([{"id": "a", "title": "A"}])
        ng.remove(["a"])
        assert not ng.has_tasks()

    def test_remove_with_dependent_fails(self, tmp_path):
        ng = _NodeGraph(tmp_path)
        ng.create([
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "blocked_by": ["a"]},
        ])
        with pytest.raises(ValueError, match="depended on by"):
            ng.remove(["a"])


# ======================================================================
# PlanManager -- Story CRUD
# ======================================================================

class TestStoryCreate:
    def test_create_single_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "Story 1"}])
        assert pm.has_tasks()

    def test_create_stories_with_deps(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        meta = pm._load_meta("s2")
        assert meta.blocked_by == ["s1"]

    def test_create_duplicate_story_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        with pytest.raises(ValueError, match="already exists"):
            pm.create(None, [{"id": "s1", "title": "S1 again"}])

    def test_create_story_dangling_ref_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        with pytest.raises(ValueError, match="non-existent"):
            pm.create(None, [{"id": "s1", "title": "S1", "blocked_by": ["nope"]}])

    def test_create_story_cycle_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        with pytest.raises(ValueError, match="Cycle"):
            pm.create(None, [
                {"id": "s1", "title": "S1", "blocked_by": ["s2"]},
                {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
            ])


class TestStoryUpdate:
    def test_update_story_title(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "Old"}])
        pm.update(None, [{"id": "s1", "title": "New"}])
        assert pm._load_meta("s1").title == "New"

    def test_update_story_blocked_by(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2"},
        ])
        pm.update(None, [{"id": "s2", "blocked_by": ["s1"]}])
        assert pm._load_meta("s2").blocked_by == ["s1"]

    def test_update_story_status_rejected(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        with pytest.raises(ValueError, match="auto-derived"):
            pm.update(None, [{"id": "s1", "status": "completed"}])

    def test_update_nonexistent_story_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            pm.update(None, [{"id": "nope", "title": "X"}])


class TestStoryRemove:
    def test_remove_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.remove(None, ["s1"])
        assert not pm.has_tasks()

    def test_remove_story_with_dependent_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        with pytest.raises(ValueError, match="depended on by"):
            pm.remove(None, ["s1"])

    def test_remove_both_stories_together(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.remove(None, ["s1", "s2"])
        assert not pm.has_tasks()


# ======================================================================
# PlanManager -- Task CRUD within stories
# ======================================================================

class TestTaskCreate:
    def test_create_task_in_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "Task 1"}])
        ng = pm._task_graph("s1")
        assert "t1" in ng.load_all()

    def test_create_task_in_nonexistent_story_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            pm.create("nope", [{"id": "t1", "title": "T1"}])

    def test_task_cross_ref_within_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": "t1", "title": "T1"},
            {"id": "t2", "title": "T2", "blocked_by": ["t1"]},
        ])
        ng = pm._task_graph("s1")
        assert ng.load_all()["t2"].blocked_by == ["t1"]


class TestTaskUpdate:
    def test_update_task_status(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        ng = pm._task_graph("s1")
        assert ng.load_all()["t1"].status == "in_progress"

    def test_start_task_in_blocked_story_fails(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.create("s2", [{"id": "t1", "title": "T1"}])
        with pytest.raises(ValueError, match="parent story is blocked"):
            pm.update("s2", [{"id": "t1", "status": "in_progress"}])

    def test_start_task_after_story_unblocked(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.create("s2", [{"id": "t2", "title": "T2"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        pm.update("s1", [{"id": "t1", "status": "completed"}])
        pm.update("s2", [{"id": "t2", "status": "in_progress"}])
        ng = pm._task_graph("s2")
        assert ng.load_all()["t2"].status == "in_progress"


class TestTaskRemove:
    def test_remove_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.remove("s1", ["t1"])
        ng = pm._task_graph("s1")
        assert not ng.has_tasks()


# ======================================================================
# Story status derivation
# ======================================================================

class TestStoryStatus:
    def test_pending_when_no_tasks(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        assert pm._derive_status("s1") == "pending"

    def test_pending_when_tasks_pending(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        assert pm._derive_status("s1") == "pending"

    def test_in_progress_when_task_started(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        assert pm._derive_status("s1") == "in_progress"

    def test_completed_when_all_tasks_done(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        pm.update("s1", [{"id": "t1", "status": "completed"}])
        assert not pm._story_exists("s1")


# ======================================================================
# Auto-prune: story completion deletes directory
# ======================================================================

class TestAutoprune:
    def test_single_story_pruned(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        pm.update("s1", [{"id": "t1", "status": "completed"}])
        assert not pm.has_tasks()

    def test_chain_prune(self, tmp_path):
        """S1 -> S2. When S1 completes, S2 is unblocked. Complete S2 -> both gone."""
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.create("s2", [{"id": "t2", "title": "T2"}])

        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        pm.update("s1", [{"id": "t1", "status": "completed"}])
        assert not pm._story_exists("s1")
        assert pm._story_exists("s2")

        pm.update("s2", [{"id": "t2", "status": "in_progress"}])
        pm.update("s2", [{"id": "t2", "status": "completed"}])
        assert not pm.has_tasks()

    def test_multi_task_story_not_pruned_early(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": "t1", "title": "T1"},
            {"id": "t2", "title": "T2"},
        ])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        pm.update("s1", [{"id": "t1", "status": "completed"}])
        assert pm._story_exists("s1")

    def test_diamond_prune(self, tmp_path):
        """S1 -> S2, S1 -> S3, S2+S3 -> S4. All complete -> all gone."""
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
            {"id": "s3", "title": "S3", "blocked_by": ["s1"]},
            {"id": "s4", "title": "S4", "blocked_by": ["s2", "s3"]},
        ])
        for sid in ("s1", "s2", "s3", "s4"):
            pm.create(sid, [{"id": f"{sid}-t", "title": f"{sid} task"}])

        for sid in ("s1", "s2", "s3", "s4"):
            pm.update(sid, [{"id": f"{sid}-t", "status": "in_progress"}])
            pm.update(sid, [{"id": f"{sid}-t", "status": "completed"}])

        assert not pm.has_tasks()


# ======================================================================
# Render
# ======================================================================

class TestRender:
    def test_empty(self, tmp_path):
        pm = PlanManager(tmp_path)
        assert pm.render() == "(no stories)"

    def test_overview_shows_stories(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "Ready story"},
            {"id": "s2", "title": "Blocked story", "blocked_by": ["s1"]},
        ])
        pm.create("s1", [{"id": "t1", "title": "Task 1"}])
        text = pm.render()
        assert "STORIES READY:" in text
        assert "Ready story" in text
        assert "STORIES BLOCKED:" in text
        assert "Blocked story" in text

    def test_overview_shows_kanban(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "Do stuff"}])
        text = pm.render()
        assert "KANBAN" in text
        assert "Do stuff" in text
        assert "story: s1" in text

    def test_story_detail(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "My Story"}])
        pm.create("s1", [
            {"id": "t1", "title": "Task A"},
            {"id": "t2", "title": "Task B", "blocked_by": ["t1"]},
        ])
        text = pm.render("s1")
        assert "Story: My Story" in text
        assert "READY:" in text
        assert "Task A" in text
        assert "BLOCKED:" in text
        assert "Task B" in text

    def test_in_progress_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "WIP Story"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        text = pm.render()
        assert "STORIES IN PROGRESS:" in text
        assert "WIP Story" in text

    def test_kanban_excludes_blocked_story_tasks(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.create("s1", [{"id": "t1", "title": "Task in S1"}])
        pm.create("s2", [{"id": "t2", "title": "Task in S2"}])
        text = pm.render()
        assert "Task in S1" in text
        assert "Task in S2" not in text.split("KANBAN")[1] if "KANBAN" in text else True


# ======================================================================
# Persistence
# ======================================================================

class TestStoryIncrementalDeps:
    def test_add_blocked_by_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2"},
        ])
        pm.update(None, [{"id": "s2", "add_blocked_by": ["s1"]}])
        assert pm._load_meta("s2").blocked_by == ["s1"]

    def test_remove_blocked_by_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.update(None, [{"id": "s2", "remove_blocked_by": ["s1"]}])
        assert pm._load_meta("s2").blocked_by == []


class TestLeanReturns:
    def test_create_story_returns_json(self, tmp_path):
        pm = PlanManager(tmp_path)
        result = pm.create(None, [{"id": "s1", "title": "S1"}])
        assert "<notice>" in result
        data = json.loads(result.split("\n", 1)[1])
        assert data[0]["id"] == "s1"

    def test_create_task_returns_json(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        result = pm.create("s1", [{"id": "t1", "title": "T1"}])
        assert "<notice>" in result
        data = json.loads(result.split("\n", 1)[1])
        assert data[0]["id"] == "t1"
        assert data[0]["status"] == "pending"

    def test_update_task_returns_json(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        result = pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        data = json.loads(result)
        assert data[0]["id"] == "t1"
        assert data[0]["status"] == "in_progress"

    def test_update_story_returns_json(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "Old"}])
        result = pm.update(None, [{"id": "s1", "title": "New"}])
        data = json.loads(result)
        assert data[0]["title"] == "New"

    def test_remove_story_returns_confirmation(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        result = pm.remove(None, ["s1"])
        assert "Removed" in result
        assert "s1" in result

    def test_remove_task_returns_confirmation(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        result = pm.remove("s1", ["t1"])
        assert "Removed" in result
        assert "t1" in result

    def test_task_list_still_renders(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        result = pm.render()
        assert "KANBAN" in result or "STORIES" in result


class TestOwnerInRender:
    def test_owner_shown_in_progress(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress", "owner": "bot-1"}])
        text = pm.render("s1")
        assert "owner: bot-1" in text

    def test_no_owner_tag_when_empty(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        text = pm.render("s1")
        assert "owner:" not in text


class TestNotices:
    def test_story_create_notice(self, tmp_path):
        pm = PlanManager(tmp_path)
        result = pm.create(None, [{"id": "s1", "title": "S1"}])
        assert "<notice>Story 's1' created.</notice>" in result

    def test_task_create_notice(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        result = pm.create("s1", [{"id": "t1", "title": "T1"}])
        assert "<notice>Tasks added to story 's1'.</notice>" in result

    def test_story_completed_notice(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        result = pm.update("s1", [{"id": "t1", "status": "completed"}])
        assert "<notice>Story 's1' completed and removed.</notice>" in result

    def test_no_notice_on_regular_update(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}, {"id": "t2", "title": "T2"}])
        result = pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        assert "<notice>" not in result


class TestPersistence:
    def test_survives_reload(self, tmp_path):
        pm1 = PlanManager(tmp_path)
        pm1.create(None, [{"id": "s1", "title": "S1"}])
        pm1.create("s1", [{"id": "t1", "title": "T1"}])
        pm1.update("s1", [{"id": "t1", "status": "in_progress"}])

        pm2 = PlanManager(tmp_path)
        ng = pm2._task_graph("s1")
        assert ng.load_all()["t1"].status == "in_progress"

    def test_meta_json_format(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "Test Story"}])
        data = json.loads((tmp_path / "s1" / "_meta.json").read_text())
        assert data["id"] == "s1"
        assert data["title"] == "Test Story"
        assert data["blocked_by"] == []
        assert "status" not in data


# ======================================================================
# PlanManager.claim_next
# ======================================================================

class TestClaimNext:
    def test_claims_first_ready_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": "t1", "title": "Task 1"},
            {"id": "t2", "title": "Task 2"},
        ])
        result = pm.claim_next("alice")
        assert result is not None
        assert result["story"] == "s1"
        assert result["id"] == "t1"
        assert result["title"] == "Task 1"

        task = pm._task_graph("s1").load_all()["t1"]
        assert task.status == "in_progress"
        assert task.owner == "alice"

    def test_skips_blocked_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": "t1", "title": "Blocker"},
            {"id": "t2", "title": "Blocked", "blocked_by": ["t1"]},
        ])
        result = pm.claim_next("alice")
        assert result["id"] == "t1"

        result2 = pm.claim_next("bob")
        assert result2 is None

    def test_skips_owned_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "Task 1"}])
        pm.claim_next("alice")

        result = pm.claim_next("bob")
        assert result is None

    def test_skips_completed_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": "t1", "title": "Task 1"},
            {"id": "t2", "title": "Task 2"},
        ])
        pm.update("s1", [{"id": "t1", "status": "in_progress"}])
        pm.update("s1", [{"id": "t1", "status": "completed"}])

        result = pm.claim_next("bob")
        assert result is not None
        assert result["id"] == "t2"

    def test_skips_blocked_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.create("s2", [{"id": "t2", "title": "T2"}])

        result = pm.claim_next("alice")
        assert result["story"] == "s1"

    def test_returns_none_when_empty(self, tmp_path):
        pm = PlanManager(tmp_path)
        assert pm.claim_next("alice") is None

    def test_returns_none_when_all_claimed(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.claim_next("alice")
        assert pm.claim_next("bob") is None

    def test_concurrent_claims_no_duplicates(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": f"t{i}", "title": f"Task {i}"} for i in range(10)
        ])

        results: list[dict | None] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def claimer(name: str) -> None:
            try:
                while True:
                    r = pm.claim_next(name)
                    if r is None:
                        break
                    with lock:
                        results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=claimer, args=(f"agent-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        claimed_ids = [r["id"] for r in results]
        assert len(claimed_ids) == 10
        assert len(set(claimed_ids)) == 10


# ======================================================================
# PlanManager.board_revision, has_claimable_work, format_task
# ======================================================================


class TestBoardRevision:
    def test_starts_at_zero(self, tmp_path):
        pm = PlanManager(tmp_path)
        assert pm.board_revision == 0

    def test_bumps_on_create_story_tasks_and_claim(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        assert pm.board_revision == 1
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        assert pm.board_revision == 2
        pm.claim_next("alice")
        assert pm.board_revision == 3


class TestHasClaimableWork:
    def test_true_when_unclaimed_ready_exists(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        assert pm.has_claimable_work() is True

    def test_false_when_none_ready(self, tmp_path):
        pm = PlanManager(tmp_path)
        assert pm.has_claimable_work() is False

    def test_false_after_claim_next_claims_all(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        pm.claim_next("alice")
        assert pm.has_claimable_work() is False

    def test_matches_claim_next_skips_blocked(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [
            {"id": "t1", "title": "Blocker"},
            {"id": "t2", "title": "Blocked", "blocked_by": ["t1"]},
        ])
        assert pm.has_claimable_work() is True
        pm.claim_next("alice")
        assert pm.has_claimable_work() is False


class TestFormatTask:
    def test_renders_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "Hello"}])
        text = pm.format_task("s1", "t1")
        assert "Hello" in text
        assert "t1" in text
        assert "pending" in text

    def test_unknown_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        assert pm.format_task("nope", "t1").startswith("[error]")

    def test_unknown_task(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [{"id": "s1", "title": "S1"}])
        pm.create("s1", [{"id": "t1", "title": "T1"}])
        assert pm.format_task("s1", "missing").startswith("[error]")

    def test_blocked_story(self, tmp_path):
        pm = PlanManager(tmp_path)
        pm.create(None, [
            {"id": "s1", "title": "S1"},
            {"id": "s2", "title": "S2", "blocked_by": ["s1"]},
        ])
        pm.create("s2", [{"id": "t1", "title": "T1"}])
        assert pm.format_task("s2", "t1").startswith("[error]")
