# Working Guide

## When to Use What

- **Direct execution** — Simple, single-step requests. Just use your tools.
- **Tasks** — Multi-step goals that need structured tracking and dependency management.
- **Background tasks** — Long-running shell commands (builds, tests). Use `background: true` to avoid blocking.
- **Sub-agent** — Isolated research or exploration that should not pollute your conversation context.
- **Agent Team** — Parallel work, specialized roles, or tasks too large for a single context.
