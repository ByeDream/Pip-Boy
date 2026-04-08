---
name: task-planning
description: >-
  Guide for the two-level Story/Task planning system.
  Load this skill when you need to break down goals into structured plans.
tags: [planning, tasks, stories]
---

# Task Planning System

Pip-Boy uses a two-level **Story / Task** system to track goals on disk.
Stories and tasks survive across sessions and are automatically cleaned up
when completed.

## Concepts

- **Story** = a big goal (e.g. "Build user authentication").
  Created as a directory under `.tasks/`.
- **Task** = a step within a story (e.g. "Design database schema").
  Created as a JSON file inside the story directory.
- **blocked_by** = dependency list. Stories can block other stories;
  tasks can block other tasks *within the same story*.
  Cross-story task dependencies are not allowed.
  When a task completes, its ID is automatically removed from
  siblings' blocked_by lists (write-time normalization).
- **owner** = set automatically by `claim_task` to identify who is working on it.
- **Story status** is automatic:
  - Any task in_progress -> story is in_progress
  - All tasks completed  -> story is completed -> **directory deleted**
  - Otherwise            -> story is pending

## Tools

### task_create

Create stories or tasks.

```
# Create stories (no "story" param):
task_create(tasks=[
  {"id": "setup-infra", "title": "Set up infrastructure"},
  {"id": "build-api", "title": "Build REST API", "blocked_by": ["setup-infra"]},
])

# Create tasks within a story:
task_create(story="setup-infra", tasks=[
  {"id": "configure-db", "title": "Configure database"},
  {"id": "setup-ci", "title": "Set up CI pipeline", "blocked_by": ["configure-db"]},
])
```

### claim_task

Claim a task to start working on it. Sets status to `in_progress` and
owner to the caller.

```
claim_task(story="setup-infra", task_id="configure-db")
```

### task_update

Update stories (title/blocked_by only) or tasks (status/title/blocked_by).
Use `claim_task` to start work on a task.

```
# Update a story's dependencies (full replace):
task_update(tasks=[{"id": "build-api", "blocked_by": ["setup-infra"]}])

# Incremental dependency changes:
task_update(tasks=[{"id": "build-api", "add_blocked_by": ["setup-infra"]}])
task_update(tasks=[{"id": "build-api", "remove_blocked_by": ["old-dep"]}])

# Complete a task:
task_update(story="setup-infra", tasks=[{"id": "configure-db", "status": "completed"}])
```

Story status cannot be set manually -- it is derived from task statuses.

Create/update tools return JSON of the affected items only.
Use `task_list` to see the full graph.

### task_list

View the plan.

```
# Kanban overview (all stories + ready tasks across stories):
task_list()

# Detailed view of one story's tasks:
task_list(story="setup-infra")
```

### task_remove

Remove stories or tasks. Fails if other items depend on them.

```
# Remove a story (and all its tasks):
task_remove(task_ids=["build-api"])

# Remove tasks from a story:
task_remove(story="setup-infra", task_ids=["setup-ci"])
```

## Typical workflow

1. **Plan**: Create stories for each major goal, with inter-story dependencies.
2. **Decompose**: Add tasks to each story, with intra-story dependencies.
3. **Work**: Use `task_list()` to see what's ready. `claim_task` to start,
   do the work, `task_update` to mark `completed`.
4. **Auto-cleanup**: When all tasks in a story complete, the story
   directory is automatically deleted from disk.

## Rules

- Task IDs: alphanumeric, dashes, underscores, 1-64 characters.
- A task in a blocked story cannot be started.
- blocked_by references must exist at the same level (story-to-story or task-to-task within one story).
- No cycles allowed in either story or task dependency graphs.
- Do NOT set story status manually -- it is always derived.
- `blocked_by` (full replace) takes precedence over `add_blocked_by`/`remove_blocked_by`.
