# Pip-Boy

[![CI](https://github.com/ByeDream/Pip-Boy/actions/workflows/ci.yml/badge.svg)](https://github.com/ByeDream/Pip-Boy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pip-boy)](https://pypi.org/project/pip-boy/)
[![Python](https://img.shields.io/pypi/pyversions/pip-boy)](https://pypi.org/project/pip-boy/)
[![License](https://img.shields.io/github/license/ByeDream/Pip-Boy)](LICENSE)

<p align="center">
  <img src="docs/Imgs/Pip-BoyAdArtPrint.jpg" width="480" alt="Pip-Boy 3000 Mark IV" />
</p>

A **lean host for Claude Code** that adds persistent cross-session memory, multi-channel delivery (CLI / WeChat / WeCom), user identity, and durable scheduling on top of what Claude Code already ships. Pip-Boy does **not** re-implement the agent loop, tool dispatch, web search, context compaction, or session resume — those are owned by the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). Pip-Boy owns what the SDK does not.

## What Pip-Boy adds to Claude Code

### Memory pipeline (cross-session)

Claude Code's JSONL session resume covers an in-flight conversation. Pip-Boy covers **across** conversations:

- **L1 Reflect** — Extracts ≤ 5 high-signal observations per pass from a session's JSONL transcript. Triggered by (a) Claude Code's own `PreCompact` hook (when the context boundary is about to be discarded) and (b) `/exit` (to catch sessions that never hit compact).
- **L2 Consolidate** — Merges observations into memories with reinforcement, decay, and conflict resolution.
- **L3 Axiom Distillation** — Promotes high-stability memories into persona principles (`axioms.md`).
- **Dream cycle** — L2 + L3 run together once per idle-hour window when enough observations have accumulated. Scheduler-driven, not agent-driven.
- **Prompt enrichment** — Axioms and relevant memories are injected into the system prompt on every turn via `system_prompt_append`.
- **`reflect` / `memory_search` / `memory_write` MCP tools** — The model can drive reflection and recall on demand.

### Multi-channel host

One Pip-Boy host, many surfaces. All channels feed into the same inbound message queue routed through the same Claude Code agent:

- **CLI** — Interactive REPL with streaming output and UTF-8-safe input on Windows.
- **WeChat** — Personal WeChat via WebSocket. Images, files, and voice transcriptions are passed to the model as multimodal content blocks.
- **WeCom** — Enterprise WeCom bots. Same multimodal path as WeChat.

### User identity & ACL

- **Owner profile** — `owner.md` is the source of truth for who owns this workspace. CLI is always owner.
- **User profiles** — The `remember_user` MCP tool lets the agent record identity / preferences about whoever is talking to it (`users/*.md`).
- **ACL gate** — `/admin` is owner-only; other mutating slash commands require admin or owner. Gate is enforced in the host dispatcher, not in individual handlers.

### Durable scheduling

Claude Code's native cron (`CronCreate` / `CronList` / `CronDelete`) lives **inside** the per-turn `claude.exe` subprocess, which exits on `end_turn` — jobs scheduled via it never fire in our subprocess-per-turn world. So we disable CC native cron (`CLAUDE_CODE_DISABLE_CRON=1`) and ship our own host-side scheduler instead.

- **Cron jobs** — `cron_add` / `cron_remove` / `cron_update` / `cron_list` MCP tools. Jobs persist to `.pip/agents/<id>/cron.json`, survive restarts, coalesce duplicate pending ticks, and auto-disable after repeated failures.
- **Heartbeat** — Periodic proactive turn during configured active hours. `HEARTBEAT.md` per agent drives what the model does; `HEARTBEAT_OK` is a sentinel for "nothing to report" (silenced to avoid CLI noise).
- **Dream trigger** — Same scheduler fires the L2/L3 memory pipeline on the configured idle-hour window.

### Delivery out-of-band

- **`send_file` MCP tool** — The model can ship a local file through the active messaging channel (e.g. "here's the report"). CLI returns a friendly refusal; messaging channels use their native file-upload path.

## Installation

**Prerequisites:** Python ≥ 3.11. No separate `claude` CLI install needed — the Claude Agent SDK wheel carries a bundled executable.

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
cd /path/to/your/project
pip-boy                 # all available channels (CLI + WeChat/WeCom if configured)
pip-boy --cli           # CLI only
pip-boy --scan          # force WeChat QR login
pip-boy --version
```

On first launch Pip-Boy scaffolds `.pip/` with defaults, including `.env` from the template. Fill in `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`) and run again. The agent uses `Path.cwd()` as its working directory.

## Configuration

### `.env`

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Conditional | — | Direct Anthropic credential; sent as `x-api-key` unless a proxy base URL promotes it. |
| `ANTHROPIC_AUTH_TOKEN` | Conditional | — | Proxy-style bearer token. Takes precedence over `ANTHROPIC_API_KEY`. |
| `ANTHROPIC_BASE_URL` | No | — | Custom API endpoint. Promotes any credential to bearer mode for proxy gateways. |
| `WECOM_BOT_ID` / `WECOM_BOT_SECRET` | No | — | WeCom enterprise bot credentials. |
| `VERBOSE` | No | `false` | Open the internal log firehose: root at `INFO`, `pip_agent.*` at `DEBUG`. Streaming agent output and `[tool: ...]` traces always print regardless. |

At least one of `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` must be set, or Claude Code will fall back to its own auth (`claude login`).

### Heartbeat

| Variable | Default | Description |
|---|---|---|
| `HEARTBEAT_INTERVAL` | `1800` | Seconds between heartbeat injections. `0` disables. |
| `HEARTBEAT_ACTIVE_START` | `9` | Local hour (0-23) when heartbeats begin. |
| `HEARTBEAT_ACTIVE_END` | `22` | Local hour (0-23) when heartbeats stop. |

### Dream cycle (L2 / L3 memory)

| Variable | Default | Description |
|---|---|---|
| `DREAM_HOUR_START` | `2` | Local hour when the Dream window opens. |
| `DREAM_HOUR_END` | `5` | Local hour when the Dream window closes. Setting `start == end` disables Dream. |
| `DREAM_MIN_OBSERVATIONS` | `20` | Minimum unconsolidated observations before Dream fires. |
| `DREAM_INACTIVE_MINUTES` | `30` | Minimum minutes of user silence before Dream fires. |

### Per-agent configuration

Each agent lives under `.pip/agents/<id>/persona.md` with YAML frontmatter:

```yaml
---
name: Pip-Boy
model: claude-opus-4-6
dm_scope: main
---

## Identity
You are Pip-Boy, …
```

Only `model` and `dm_scope` are effective overrides; other fields (token limits, compaction thresholds, fallback-model chains) are **owned by Claude Code**, not Pip-Boy. To change them, use Claude Code's own config.

### Slash commands

All commands are flat (single-level). ACL: `/help` and `/status` are open, `/admin` is owner-only, the rest require owner or admin. CLI is always owner.

| Command | Description |
|---|---|
| `/help` | Show all available commands. |
| `/status` | Current agent, session key, binding, and channel. |
| `/memory` | Memory statistics for the current agent. |
| `/axioms` | Current judgment principles (`axioms.md`). |
| `/recall <query>` | Search stored memories. |
| `/cron` | List scheduled cron jobs. |
| `/bind <agent-id> [--scope s] [--model m]` | Bind current chat to an agent (auto-creates the agent if needed). |
| `/unbind` | Remove current chat's binding. |
| `/name <display_name>` | Rename the current agent. |
| `/reset` | Factory-reset memory for the current agent (keeps binding + persona). |
| `/admin grant\|revoke\|list [name]` | Manage admin privileges (owner only). |
| `/exit` | Quit Pip-Boy (CLI only). |

Unknown slash commands are passed through to the model for interpretation — no "unknown command" error.

### Project directory structure

```
.pip/
├── owner.md                     # Owner profile (read-only)
├── .scaffold_manifest.json      # Scaffold version tracking
├── agents/
│   ├── bindings.json            # Channel → agent routing
│   └── pip-boy/                 # Per-agent directory
│       ├── persona.md           # Agent persona + config (YAML frontmatter)
│       ├── HEARTBEAT.md         # Heartbeat prompt template
│       ├── cron.json            # Scheduled jobs
│       ├── state.json           # Memory pipeline cursors
│       ├── memories.json        # L2 consolidated memories
│       ├── axioms.md            # L3 judgment principles
│       ├── observations/        # L1 observation files (.jsonl)
│       ├── sessions.json        # Session-key → SDK session id map
│       └── users/               # User profiles (.md)
```

## Architecture, in one diagram

```
     ┌───────────────┐    ┌──────────────┐    ┌──────────────┐
     │   CLI / WS    │    │   WeChat     │    │    WeCom     │
     └──────┬────────┘    └──────┬───────┘    └──────┬───────┘
            │                    │                   │
            ▼                    ▼                   ▼
          ┌─────────────────────────────────────────────┐
          │            InboundMessage queue             │
          └────────────────────┬────────────────────────┘
                               │
                               ▼
       ┌───────────────────────────────────────────────────┐
       │              AgentHost.process_inbound            │
       │ ┌─────────────────────────────────────────────┐   │
       │ │  Slash dispatch (host_commands.py)          │   │
       │ │  — short-circuits /help, /status, /bind … —│   │
       │ └────────────────────┬────────────────────────┘   │
       │                      │ (unknown or non-slash)     │
       │                      ▼                            │
       │ ┌─────────────────────────────────────────────┐   │
       │ │  Memory enrichment → system_prompt_append   │   │
       │ │  Prompt formatting (str | content blocks)   │   │
       │ │  Per-session lock + global semaphore        │   │
       │ └────────────────────┬────────────────────────┘   │
       └──────────────────────┼────────────────────────────┘
                              ▼
                ┌─────────────────────────────┐
                │  claude_agent_sdk.query()   │   MCP server:
                │   — spawns claude.exe —     │ ─ memory tools
                │   — streams messages —      │ ─ cron tools
                │   — PreCompact hook → L1 ─┐ │ ─ send_file
                └─────────────────────────────┘
                              │
                              ▼ reply
                        dispatch back
                      to originating channel
```

## Dependencies

- [`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python) — Claude Code runtime and MCP server scaffold.
- [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) — Used only by the `reflect` pipeline for direct Messages API calls (delta-cursor extraction).
- [`pydantic-settings`](https://github.com/pydantic/pydantic-settings) — `.env` configuration binding.
- [`pyyaml`](https://github.com/yaml/pyyaml) — YAML frontmatter parsing for personas.
- [`httpx`](https://github.com/encode/httpx) — HTTP client for channel communication.
- [`wecom-aibot-python-sdk`](https://pypi.org/project/wecom-aibot-python-sdk/) — WeCom enterprise bot SDK.
- [`qrcode`](https://github.com/lincolnloop/python-qrcode) — Terminal QR code rendering for WeChat login.
- [`pyreadline3`](https://github.com/pyreadline3/pyreadline3) — Readline for Windows.

## Further reading

- [`docs/sdk-contract-notes.md`](docs/sdk-contract-notes.md) — Observed SDK behaviour, hook contracts, memory pipeline design decisions, and design journal for every phase of the 0.4 rewrite.
- [`docs/system-prompt-anatomy.md`](docs/system-prompt-anatomy.md) — How the system prompt is assembled each turn.
- [`docs/releasing.md`](docs/releasing.md) — Release workflow.

## License

MIT. See [LICENSE](LICENSE).
