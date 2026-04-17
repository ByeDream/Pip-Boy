# Pip-Boy

[![CI](https://github.com/ByeDream/Pip-Boy/actions/workflows/ci.yml/badge.svg)](https://github.com/ByeDream/Pip-Boy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pip-boy)](https://pypi.org/project/pip-boy/)
[![Python](https://img.shields.io/pypi/pyversions/pip-boy)](https://pypi.org/project/pip-boy/)
[![License](https://img.shields.io/github/license/ByeDream/Pip-Boy)](LICENSE)

<p align="center">
  <img src="docs/Imgs/Pip-BoyAdArtPrint.jpg" width="480" alt="Pip-Boy 3000 Mark IV" />
</p>

A personal assistant agent with persistent memory, multi-channel support, and a configurable persona. Built on Anthropic's Claude API, it supports multi-agent teamwork, task planning, git worktree isolation, and extensible skills — accessible via CLI, WeChat, or WeCom.

## Features

### Core

- **Conversational REPL** — Interactive chat loop with readline history and UTF-8 support
- **Persona System** — Lead persona ("Pip-Boy") with customizable teammate personas via Markdown + YAML frontmatter
- **Multi-Channel** — CLI, WeChat (personal), and WeCom (enterprise) channels with unified message routing
- **Web Search** — Tavily integration with automatic DuckDuckGo fallback

### Memory System

A three-tier pipeline that learns from conversations automatically:

- **L1 Reflect** — Extracts behavioral observations (user preferences, decision patterns) and objective experience (technical lessons, API insights, reusable patterns) from conversation transcripts
- **L2 Consolidate** — Merges observations into memories with reinforcement, decay, and conflict resolution
- **L3 Axiom Distillation** — Promotes high-stability memories into judgment principles (`axioms.md`)
- **Dream Cycle** — L2 + L3 run together at a configurable hour when the system is idle and enough observations have accumulated
- **Memory Recall** — TF-IDF search with temporal decay injects relevant memories into the system prompt
- **Reflect Tool** — The agent can proactively trigger reflection when meaningful work is completed
- **SOP-Driven Prompts** — Memory pipeline rules are maintained in an external [SOP document](src/pip_agent/memory/sops/memory_pipeline_sop.md) for easy tuning

### User Identity

- **Owner Profile** — `owner.md` is read-only and defines the workspace owner with channel identifiers
- **User Profiles** — `remember_user` tool creates and updates profiles for other users (`users/*.md`)
- **ACL** — Owner and admin roles control access to sensitive operations (e.g., `/clean`, `/reset`)
- **Multi-Channel Identity** — Users are tracked by channel-specific identifiers (WeChat ID, WeCom ID, CLI)

### Tools

- **Filesystem** — `read`, `write`, `edit`, `glob` (sandboxed to working directory)
- **Shell** — `bash` execution with optional **background mode** for long-running commands
- **Web** — `web_search` and `web_fetch` for real-time information retrieval
- **Memory** — `memory_search` for explicit recall, `reflect` for on-demand reflection
- **Skills** — `load_skill` dynamically loads built-in and user-defined skill guides

### Task Planning

- **Story / Task DAG** — Two-level planning: stories (epics) contain tasks with dependency tracking
- **Kanban Board** — `task_board_overview`, `task_board_detail` for status visualization
- **State Machine** — Tasks flow through `pending` → `in_progress` → `in_review` → `completed` / `failed`
- **Persistent Storage** — JSON files under `.pip/agents/<id>/tasks/` survive across sessions

### Multi-Agent Team

- **Teammate Spawning** — `team_spawn` creates daemon threads with per-session model and turn limits
- **Inbox Messaging** — JSONL-based message bus (`send`, `read_inbox`) between lead and teammates
- **Model Selection** — Per-project `.pip/models.json` defines available models for teammate assignment
- **Per-Agent Isolation** — Each agent has its own data directory, TeamManager, and WorktreeManager
- **CLI Commands** — `/team` for roster status, `/inbox` to peek the lead inbox

### Git Worktree Isolation

- **Isolated Branches** — Each subagent works in its own git worktree (`.pip/.worktrees/{name}/`, branch `wt/{name}`)
- **Sync / Integrate / Cleanup** — Worktree lifecycle management with merge conflict detection
- **Task Submission** — `task_submit` syncs work and transitions task status automatically

### Context Management

- **Micro-Compaction** — Old tool results replaced with placeholders, keeping the last N rounds intact
- **Auto-Compaction** — When token count exceeds threshold, conversation is replaced with an LLM-generated summary
- **Transcript Persistence** — Every conversation turn saves a timestamped JSON transcript; old transcripts are cleaned up after reflection

### Built-in Skills

| Skill | Purpose |
|-------|---------|
| `task-planning` | Structured planning with story/task breakdown |
| `agent-team` | Multi-agent coordination and delegation |
| `git` | Git operations and workflow guidance |
| `code-review` | Code review methodology |
| `create-skill` | Authoring new custom skills |

## Installation

**Prerequisites:** Python >= 3.11

```bash
pip install pip-boy
```

### Development (from source)

```bash
git clone https://github.com/ByeDream/Pip-Boy.git
cd Pip-Boy
pip install -e ".[dev]"
```

## Usage

```bash
# Navigate to your target project and run
cd /path/to/your/project
pip-boy

# CLI-only mode (no WeChat/WeCom channels)
pip-boy --cli

# Force WeChat QR login
pip-boy --scan

# Show version
pip-boy --version
```

On first launch, the scaffold automatically creates the `.pip/` directory structure, `.env` (from template), and `.gitignore` entries. Edit the generated `.env` to fill in your `ANTHROPIC_API_KEY`, then run again.

The agent uses `Path.cwd()` as its working directory — always run it from the project you want to interact with.

### Updating

From within a running session:

```
/update
```

Or manually:

```bash
pip install --upgrade pip-boy
```

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | — | Anthropic API key |
| `ANTHROPIC_BASE_URL` | No | *(api.anthropic.com)* | Custom API endpoint (proxy support) |
| `SEARCH_API_KEY` | No | — | Tavily API key; falls back to DuckDuckGo |
| `WECOM_BOT_ID` | No | — | WeCom bot ID for enterprise WeChat channel |
| `WECOM_BOT_SECRET` | No | — | WeCom bot secret |
| `VERBOSE` | No | `true` | Verbose output |
| `PROFILER_ENABLED` | No | `false` | Enable performance profiling |

#### Memory Pipeline

| Variable | Default | Description |
|---|---|---|
| `REFLECT_TRANSCRIPT_THRESHOLD` | `10` | New transcripts needed to trigger reflection |
| `TRANSCRIPT_RETENTION_DAYS` | `7` | Days to keep reflected transcripts |
| `DREAM_HOUR` | `2` | Local hour (0-23) for the Dream cycle |
| `DREAM_MIN_OBSERVATIONS` | `20` | Minimum observations before Dream can run |
| `DREAM_INACTIVE_MINUTES` | `30` | Agent idle time (minutes) required for Dream |

### Per-Agent Configuration

Model, token limits, and compaction settings are configured per-agent via YAML frontmatter in `.pip/agents/<id>/persona.md`:

```yaml
---
model: claude-sonnet-4-6
max_tokens: 8192
compact_threshold: 50000
compact_micro_age: 3
---
```

### Project Directory Structure

```
.pip/
├── owner.md                     # Owner profile (read-only)
├── models.json                  # Model catalog for team spawning
├── .scaffold_manifest.json      # Scaffold version tracking
├── agents/
│   ├── bindings.json            # Channel → agent routing
│   └── pip-boy/                 # Per-agent directory
│       ├── persona.md           # Agent persona + config (YAML frontmatter)
│       ├── state.json           # Memory pipeline state
│       ├── memories.json        # L2 consolidated memories
│       ├── axioms.md            # L3 judgment principles
│       ├── observations/        # L1 observation files (.jsonl)
│       ├── transcripts/         # Conversation transcripts (.json)
│       ├── users/               # User profiles (.md)
│       ├── tasks/               # Task board state
│       └── team/                # Teammate data + inbox
└── .worktrees/                  # Git worktree isolation
```

## Dependencies

- [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) — Claude API client
- [`pydantic-settings`](https://github.com/pydantic/pydantic-settings) — Configuration management
- [`tavily-python`](https://github.com/tavily-ai/tavily-python) — Web search API
- [`ddgs`](https://github.com/deedy5/duckduckgo_search) — DuckDuckGo fallback search
- [`pyyaml`](https://github.com/yaml/pyyaml) — YAML parsing for skills and personas
- [`httpx`](https://github.com/encode/httpx) — HTTP client for channel communication
- [`wecom-aibot-python-sdk`](https://pypi.org/project/wecom-aibot-python-sdk/) — WeCom enterprise bot SDK
- [`qrcode`](https://github.com/lincolnloop/python-qrcode) — Terminal QR code rendering for WeChat login
- [`pyreadline3`](https://github.com/pyreadline3/pyreadline3) — Readline for Windows

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
