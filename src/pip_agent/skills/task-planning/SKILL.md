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
  Created as a directory under `.pip/tasks/`.
- **Task** = a step within a story (e.g. "Design database schema").
  Created as a JSON file inside the story directory.
- **blocked_by** = dependency list. Stories can block other stories;
  tasks can block other tasks *within the same story*.
  Cross-story task dependencies are not allowed.
  When a task completes, its ID is automatically removed from
  siblings' blocked_by lists (write-time normalization).
- **owner** = set automatically by `claim_task` to identify who is working on it.
- **Story status** is automatic:
  - Any task in_progress/in_review/merged/failed -> story is in_progress
  - All tasks completed -> story is completed -> **directory deleted**
  - Otherwise -> story is pending

## Task States

### Lead's own tasks (3 states):
```
pending → in_progress → completed
```

### Subagent tasks (5 states + failed):
```
pending → in_progress → in_review → merged → completed
                          ↑           |
                          +-- failed --+
```

State meanings:

| Status       | Meaning                                                 |
|-------------|----------------------------------------------------------|
| `pending`    | Not started, waiting to be claimed                      |
| `in_progress`| Being worked on                                         |
| `in_review`  | Subagent submitted work, synced with main, awaiting Lead review |
| `merged`     | Lead approved, code merged into main WORKDIR            |
| `failed`     | Merge conflict or issue, subagent needs to resolve      |
| `completed`  | Lead confirmed, worktree cleaned up, downstream unblocked |

Only `completed` unblocks downstream tasks.

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
owner to the caller. For subagents, also creates a worktree.

```
claim_task(story="setup-infra", task_id="configure-db")
```

### task_update (Lead only)

Update stories (title/blocked_by only) or tasks (status/title/blocked_by).

```
# Update a story's dependencies:
task_update(tasks=[{"id": "build-api", "blocked_by": ["setup-infra"]}])

# Approve merge (subagent task in_review → merged):
task_update(story="s1", tasks=[{"id": "t1", "status": "merged"}])

# Confirm completion (merged → completed, cleans up worktree):
task_update(story="s1", tasks=[{"id": "t1", "status": "completed"}])

# Reject / send back (→ failed):
task_update(story="s1", tasks=[{"id": "t1", "status": "failed"}])

# Complete Lead's own task:
task_update(story="s1", tasks=[{"id": "t2", "status": "completed"}])
```

Story status cannot be set manually -- it is derived from task statuses.

### task_submit (Subagent only)

Submit completed work for Lead review. Syncs branch with main first.

```
task_submit(story="setup-infra", task_id="configure-db")
```

If there are merge conflicts, the task goes to `failed`. Resolve the
conflicts, commit, then call `task_submit` again.

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

### Lead working alone:
1. **Plan**: Create stories and tasks with dependencies.
2. **Work**: `claim_task` → do work → `task_update(status="completed")`
3. **Auto-cleanup**: Story directories deleted when all tasks complete.

### Lead + subagent team:
1. **Plan**: Create stories and tasks with dependencies.
2. **Spawn**: Create subagents and give them project context.
3. **Subagents work**: `claim_task` → work in worktree → `task_submit`
4. **Lead reviews**: See `in_review` → `task_update(status="merged")` → verify → `task_update(status="completed")`
5. **Lead's own tasks**: `claim_task` → work in WORKDIR → `task_update(status="completed")`
6. **Auto-cleanup**: Stories auto-delete when complete.

## Rules

- Task IDs: alphanumeric, dashes, underscores, 1-64 characters.
- A task in a blocked story cannot be started.
- blocked_by references must exist at the same level.
- No cycles allowed in dependency graphs.
- Do NOT set story status manually -- it is always derived.
- Subagents cannot directly mark tasks `completed` -- they use `task_submit`.
- Lead cannot mark tasks `in_review` -- that's done by `task_submit`.
- Lead must commit WORKDIR changes before approving `merged`.
