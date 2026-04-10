---
name: agent-team
description: >-
  Agent Team: creating subagents, spawning with model/turn selection,
  communication, worktree-based task isolation, and lifecycle.
  Load when working with subagents.
tags: [team, collaboration]
---

# Agent Team

## Subagent Definitions

Subagents are defined as `.md` files in `.pip/team/`. Each file contains:

```markdown
---
name: alice
description: "Python developer. Writes clean code and tests."
---

You are Alice, a Python developer on a collaborative agent team.

### Expertise
- Writing idiomatic Python
- Unit testing with pytest

### Communication
- Use `send` to report results to `lead` when done.
- Use `read_inbox` to check for messages during long tasks.
```

### Managing Definitions

- `team_create(name, description, system_prompt)` — create a new subagent
- `team_edit(name, description?, system_prompt?)` — update an existing subagent
- `team_delete(name)` — remove a subagent definition
- `team_status` — list all subagents and their current state

## Spawning

```
team_spawn(name, prompt, model, max_turns)
```

All four parameters are required:

- **name**: must exist in the roster (use `team_status` to check)
- **prompt**: project context and instructions for the subagent
- **model**: use `team_list_models` to see available models.
  Pick stronger models for complex reasoning, cheaper models for
  simple/repetitive tasks.
- **max_turns**: tool-use rounds budget. Allocate more turns for
  complex tasks.

The subagent begins working immediately on its own thread.

## Communication

### Lead to subagent
- `team_send(to, content)` — direct message
- `team_send(to, content, msg_type="broadcast")` — message all active subagents
- `team_send(to, content, msg_type="shutdown_request")` — ask a subagent to shut down

### Reading responses
- `team_read_inbox` — drain and read all pending messages from subagents
- Messages also appear automatically in your context between tool rounds

### Subagent to lead
Subagents use `send(to="lead", content)` to report back.

## Worktree Isolation

Each subagent works in an isolated git worktree:

- **Location**: `.pip/.worktrees/{name}/` (feature branch `wt/{name}`)
- **Created automatically** when a subagent calls `claim_task`
- **Cleaned up** when Lead marks the task `completed`
- Lead always works in the main WORKDIR on the current branch

This means subagents never touch WORKDIR files, and Lead never
touches worktree files. Changes are integrated via git merge.

## Task Board

### Subagent workflow (5 states):

1. `task_board_overview` — see stories and ready tasks
2. `task_board_detail(story, task_id)` — inspect a task
3. `claim_task(story, task_id)` — take ownership (creates worktree)
4. Do the work in the worktree
5. `task_submit(story, task_id)` — submit for Lead review

### Lead workflow for subagent tasks:

1. See `in_review` tasks (subagent submitted)
2. Review the diff, then `task_update(status="merged")` — integrates code into main
3. Verify code in WORKDIR, then `task_update(status="completed")` — cleans up worktree

### Three-stage merge flow:

```
Subagent calls task_submit:
  → System syncs feature branch with main
  → If conflicts: task → "failed", subagent resolves and resubmits
  → If clean: task → "in_review", Lead notified

Lead calls task_update(status="merged"):
  → System checks WORKDIR is clean (Lead must commit WIP first)
  → System re-syncs feature branch
  → System merges feature into main (--no-ff)
  → If conflicts: task → "failed", subagent resolves
  → If clean: task → "merged", code now in WORKDIR

Lead calls task_update(status="completed"):
  → System removes worktree and feature branch
  → Downstream tasks unblocked
```

### Lead's own tasks (3 states):

Lead can also claim and complete tasks directly:

1. `claim_task(story, task_id)` — no worktree needed
2. Work directly in WORKDIR
3. `task_update(status="completed")` — done

The system hints idle subagents when new claimable work appears.

## Lifecycle

```
Offline → [team_spawn] → Working → Idle ⇄ Working → Offline
```

- **Offline**: not running. Use `team_spawn` to activate.
- **Working**: actively calling tools and the LLM
- **Idle**: waiting for inbox messages or task board changes (60s timeout)

A subagent goes offline when: task complete, max_turns exhausted,
idle timeout, or shutdown approved. Re-spawn to continue.

## Workflow Example

1. Plan the work with `task_create` (stories, tasks, dependencies)
2. Spawn subagents with project context
3. All agents (lead included) `claim_task` and work
4. Subagents use `task_submit` when done; Lead reviews and merges
5. Use `team_status` and `team_read_inbox` to coordinate
6. Story auto-cleans when all tasks complete
