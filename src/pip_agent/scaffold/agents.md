# Working Guide

## When to Use What

- **Direct execution** — Simple, single-step requests. Just use your tools.
- **Tasks** — Multi-step goals that need structured tracking and dependency management.
- **Background tasks** — Long-running shell commands (builds, tests). Use `background: true` to avoid blocking.
- **Agent Team** — Parallel work, specialized roles, or tasks too large for a single context.
- **notify_user** — Communicate with the user mid-task during long multi-step operations.

## Worktree Boundaries

When working with an agent team, subagents work in isolated worktrees (`.pip/.worktrees/{name}/`).

- **Do NOT** access or modify files inside `.pip/.worktrees/` directly.
- Wait for subagents to submit their work via `task_submit` (status becomes `in_review`).
- Review changes via `git diff`, then approve with `task_update(status="merged")`.
