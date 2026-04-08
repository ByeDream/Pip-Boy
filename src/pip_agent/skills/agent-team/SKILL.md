---
name: agent-team
description: >-
  Agent Team: creating teammates, spawning with model/turn selection,
  communication, task coordination, and lifecycle.
  Load when working with teammates.
tags: [team, collaboration]
---

# Agent Team

## Teammate Definitions

Teammates are defined as `.md` files in `.pip/team/`. Each file contains:

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

- `team_create(name, description, system_prompt)` — create a new teammate
- `team_edit(name, description?, system_prompt?)` — update an existing teammate
- `team_delete(name)` — remove a teammate definition
- `team_status` — list all teammates and their current state

Create teammates whose roles match the work at hand. A good teammate
definition has a clear identity, specific expertise, and communication
instructions.

## Spawning

```
team_spawn(name, prompt, model, max_turns)
```

All four parameters are required:

- **name**: must exist in the roster (use `team_status` to check)
- **prompt**: project context and instructions for the teammate
- **model**: use `team_list_models` to see available models.
  Pick stronger models for complex reasoning, cheaper models for
  simple/repetitive tasks.
- **max_turns**: tool-use rounds budget. Allocate more turns for
  complex tasks.

The teammate begins working immediately on its own thread.

## Communication

### Lead to teammate
- `team_send(to, content)` — direct message
- `team_send(to, content, msg_type="broadcast")` — message all active teammates
- `team_send(to, content, msg_type="shutdown_request")` — ask a teammate to shut down

### Reading responses
- `team_read_inbox` — drain and read all pending messages from teammates
- Messages also appear automatically in your context between tool rounds

### Teammate to lead
Teammates use `send(to="lead", content)` to report back.

## Task Board

All agents (lead and teammates) share the same task workflow:

1. `task_board_overview` — see stories and ready tasks
2. `task_board_detail(story, task_id)` — inspect a task
3. `claim_task(story, task_id)` — take ownership (sets in_progress and owner)
4. Complete the work, then `task_update` status to completed

The system hints idle teammates when new claimable work appears.

## Lifecycle

```
Offline → [team_spawn] → Working → Idle ⇄ Working → Offline
```

- **Offline**: not running. All teammates start offline. Use `team_spawn` to activate them.
- **Working**: actively calling tools and the LLM
- **Idle**: waiting for inbox messages or task board changes (60s timeout)

A teammate goes offline when: task complete, max_turns exhausted,
idle timeout, or shutdown approved. Re-spawn to continue.

## Workflow Example

1. Plan the work with `task_create` (stories, tasks, dependencies)
2. Spawn teammates with project context
3. All agents (lead included) `claim_task`, work, and mark completed
4. Use `team_status` and `team_read_inbox` to coordinate
5. Story auto-cleans when all tasks complete
