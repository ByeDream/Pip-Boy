# Pip-Boy

<p align="center">
  <img src="docs/Imgs/Pip-BoyAdArtPrint.jpg" width="480" alt="Pip-Boy 3000 Mark IV" />
</p>

A personal assistant agent with persistent memory and a configurable persona, providing a chat-based interface for workstation interaction. Built on Anthropic's Claude API, it supports multi-agent teamwork, task planning, git worktree isolation, and extensible skills.

## Features

### Core

- **Conversational REPL** — Interactive chat loop with readline history and UTF-8 support
- **Persona System** — Fixed lead persona ("Pip-Boy") with customizable teammate personas via Markdown files
- **Web Search** — Tavily integration with automatic DuckDuckGo fallback

### Tools

- **Filesystem** — `read`, `write`, `edit`, `glob` (sandboxed to working directory)
- **Shell** — `bash` execution with optional **background mode** for long-running commands
- **Web** — `web_search` and `web_fetch` for real-time information retrieval
- **Skills** — `load_skill` dynamically loads built-in and user-defined skill guides (Markdown with YAML frontmatter)

### Task Planning

- **Story / Task DAG** — Two-level planning: stories (epics) contain tasks with dependency tracking
- **Kanban Board** — `task_board_overview`, `task_board_detail` for status visualization
- **State Machine** — Tasks flow through `pending` → `in_progress` → `in_review` → `completed` / `failed`
- **Persistent Storage** — JSON files under `.pip/tasks/` survive across sessions

### Multi-Agent Team

- **Teammate Spawning** — `team_spawn` creates daemon threads with per-session model and turn limits
- **Inbox Messaging** — JSONL-based message bus (`send`, `read_inbox`) between lead and teammates
- **Model Selection** — Per-project `.pip/models.json` defines available models for teammate assignment
- **Protocol Tracking** — Structured shutdown and plan approval flows
- **CLI Commands** — `/team` for roster status, `/inbox` to peek the lead inbox

### Git Worktree Isolation

- **Isolated Branches** — Each subagent works in its own git worktree (`.pip/.worktrees/{name}/`, branch `wt/{name}`)
- **Sync / Integrate / Cleanup** — Worktree lifecycle management with merge conflict detection
- **Task Submission** — `task_submit` syncs work and transitions task status automatically

### Context Management

- **Micro-Compaction** — Old tool results replaced with placeholders, keeping the last N rounds intact
- **Auto-Compaction** — When token count exceeds threshold, full transcript is saved and replaced with an LLM-generated summary
- **Transcript Persistence** — Timestamped JSON transcripts stored under `.pip/transcripts/`

### Skills (Built-in)

| Skill | Purpose |
|-------|---------|
| `task-planning` | Structured planning with story/task breakdown |
| `agent-team` | Multi-agent coordination and delegation |
| `git` | Git operations and workflow guidance |
| `code-review` | Code review methodology |
| `create-skill` | Authoring new custom skills |

### Workspace Scaffold

On first run in a new project, the agent automatically creates:
- `.pip/` directory structure (tasks, transcripts, team, skills)
- `AGENTS.md` with injected working guide (idempotent)
- `.pip/models.json` with default model catalog
- `.env` from template (if missing)
- `.gitignore` entries for `.pip/` and related paths

## Quick Start

**Prerequisites:** Python ≥ 3.11

```bash
# 1. Clone and install
git clone https://github.com/ByeDream/Pip-Boy.git
cd Pip-Boy
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
# source .venv/bin/activate    # macOS / Linux
pip install -e .

# 2. Navigate to your target project and run
cd /path/to/your/project
python -m pip_agent
```

On first launch, the scaffold automatically creates `.pip/` directory structure, `.env` (from template), `AGENTS.md`, and `.gitignore` entries in the target project. Edit the generated `.env` to fill in your `ANTHROPIC_API_KEY`, then run again.

The agent uses `Path.cwd()` as its working directory — always run it from the project you want to interact with.

## Configuration

All configuration is done via environment variables or `.env` file.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | — | Anthropic API key |
| `ANTHROPIC_BASE_URL` | No | *(api.anthropic.com)* | Custom API endpoint (proxy support) |
| `MODEL` | No | `claude-sonnet-4-6` | Default model for the lead agent |
| `MAX_TOKENS` | No | `8096` | Max response tokens |
| `SEARCH_API_KEY` | No | — | Tavily API key; falls back to DuckDuckGo |
| `COMPACT_THRESHOLD` | No | `50000` | Token estimate to trigger auto-compaction |
| `COMPACT_MICRO_AGE` | No | `3` | Micro-compaction: rounds of tool results to preserve |
| `TRANSCRIPTS_DIR` | No | `.pip/transcripts` | Transcript storage path (relative to working dir) |
| `TASKS_DIR` | No | `.pip/tasks` | Task storage path (relative to working dir) |
| `VERBOSE` | No | `true` | Verbose output |
| `PROFILER_ENABLED` | No | `false` | Enable performance profiling |

### Project-Level Files

| File | Location | Purpose |
|---|---|---|
| `models.json` | `.pip/` | Model catalog for teammate spawning |
| `*.md` | `.pip/team/` | Teammate persona definitions (YAML frontmatter + body) |
| `_meta.json` + `*.json` | `.pip/tasks/{story}/` | Task board state |
| `*.md` | `.pip/skills/` | User-defined skills |
| `AGENTS.md` | Project root | Project-specific instructions (auto-scaffolded) |

## Dependencies

- [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) — Claude API client
- [`pydantic-settings`](https://github.com/pydantic/pydantic-settings) — Configuration management
- [`tavily-python`](https://github.com/tavily-ai/tavily-python) — Web search API
- [`ddgs`](https://github.com/deedy5/duckduckgo_search) — DuckDuckGo fallback search
- [`pyyaml`](https://github.com/yaml/pyyaml) — YAML parsing for skills and team files
- [`pyreadline3`](https://github.com/pyreadline3/pyreadline3) — Readline for Windows

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
