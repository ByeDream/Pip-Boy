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

- **Shared addressbook, uuid-keyed** — Every contact lives at `<workspace>/.pip/addressbook/<user_id>.md` where `<user_id>` is an opaque 8-hex handle (e.g. `9c8b2a3e`). Root and every sub-agent read and write the same addressbook. There is no "owner" role; the local CLI user is just another entry registered through conversation.
- **Lazy loading, not eager injection** — Contact profiles are **not** dumped into the system prompt. Every `<user_query>` carries a `user_id` attribute (or the literal `unverified`), and the agent calls `lookup_user(user_id)` on demand when it needs the name / preferences / notes. Prompt tokens stay flat as the addressbook grows.
- **`remember_user` MCP tool** — Strictly self-directed:
  - An unverified caller creates a new entry; the tool mints a fresh `user_id` and records the current `channel:sender_id` as the first identifier.
  - A verified caller can only update their **own** record. Attempting to target another `user_id` is refused with an error the model sees, so it can switch to `memory_write` for facts about third parties.
- **`lookup_user` MCP tool** — Returns the raw markdown profile for a given `user_id`. The single read path for everything the model wants to know about who's talking.
- **ACL gate** — All commands are open on every channel except the `/subagent` lifecycle family and `/exit`, which are **CLI-only**. The `/help` output on remote channels (WeCom, WeChat) hides these commands entirely, so remote peers don't even learn they exist.

### Durable scheduling

Claude Code's native cron (`CronCreate` / `CronList` / `CronDelete`) lives **inside** the per-turn `claude.exe` subprocess, which exits on `end_turn` — jobs scheduled via it never fire in our subprocess-per-turn world. So we disable CC native cron (`CLAUDE_CODE_DISABLE_CRON=1`) and ship our own host-side scheduler instead.

- **Cron jobs** — `cron_add` / `cron_remove` / `cron_update` / `cron_list` MCP tools. Jobs persist to each agent's own `.pip/cron.json` (root agent at `<workspace>/.pip/cron.json`, sub-agents at `<workspace>/<id>/.pip/cron.json`), survive restarts, coalesce duplicate pending ticks, and auto-disable after repeated failures.
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
pip-boy                         # all configured channels (see rules below)
pip-boy --version
```

### Channel enablement rules

Pip-Boy picks which channels to start from what it sees on disk and in the
environment — there is no `--mode` anymore:

- **CLI** — always on.
- **WeCom** — enabled iff both `WECOM_BOT_ID` and `WECOM_BOT_SECRET` are set
  in `.env` (or the process env).
- **WeChat** — auto-started at boot iff at least one valid tier-3
  `account_id=...` binding already exists (i.e. an account scanned in on a
  previous run). Each account gets its own poll thread and an isolated
  conversation context per peer, so one host can serve multiple WeChat
  identities concurrently. First-time scans go through `/wechat add
  <agent_id>` from the CLI — the slash command lazily bootstraps the
  channel, so no restart is required.

Manage WeChat identities at runtime with `/wechat list`, `/wechat add
<agent_id>`, `/wechat cancel`, and `/wechat remove <account_id|agent_id>`
without restarting the host. The QR handshake from `/wechat add` is
non-blocking: it runs in a background daemon while the CLI stays
responsive. On first run after upgrading from a pre-multi-account build,
any legacy single-account `wechat_session.json` and tier-4
`channel=wechat` binding are dropped with a warning; re-scan with
`/wechat add <agent_id>` to rebuild bindings.

On first launch Pip-Boy scaffolds `.pip/` with defaults, including `.env` from the template. Fill in `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`) and run again. The agent uses `Path.cwd()` as its working directory.

## Configuration

### `.env`

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Conditional | — | Direct Anthropic credential; sent as `x-api-key` unless a proxy base URL promotes it. |
| `ANTHROPIC_AUTH_TOKEN` | Conditional | — | Proxy-style bearer token. Takes precedence over `ANTHROPIC_API_KEY`. |
| `ANTHROPIC_BASE_URL` | No | — | Custom API endpoint. Promotes any credential to bearer mode for proxy gateways. |
| `WECOM_BOT_ID` / `WECOM_BOT_SECRET` | No | — | WeCom enterprise bot credentials. |
| `MODEL_T0` / `MODEL_T1` / `MODEL_T2` | Yes | — | The three model tiers, ordered strongest → cheapest. Concrete model names live here only; every call site picks a tier and resolves through the table. Background tasks are pinned to fixed tiers in code. On a model-invalid error the chain steps DOWN to the next tier; never up. |
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

### Per-agent configuration (v2 layout)

The workspace root is the home of the default `pip-boy` agent. Additional sub-agents live in their own sibling directories, each with its own `.pip/` and optional `.claude/`:

```
<pip_boy_workspace>/
  .pip/                      # pip-boy's own persona + workspace-wide runtime
    persona.md
    bindings.json            # channel -> agent routing (workspace-wide)
    agents_registry.json     # known sub-agents
  ProjectA/                  # plain project; pip-boy operates on it directly
  stella/                    # a sub-agent with its own identity
    .pip/
      persona.md             # independent persona, memory, observations
```

Each `persona.md` carries YAML frontmatter:

```yaml
---
name: Pip-Boy
model: t0
dm_scope: main
---

## Identity
You are Pip-Boy, …
```

`model` is a **tier name** (`t0` / `t1` / `t2`), not a concrete model identifier. Concrete names live in `.env` (`MODEL_T0` / `MODEL_T1` / `MODEL_T2`) and are resolved at call time. Background tasks (heartbeat, cron, reflect, dream consolidation, axiom distillation) are pinned to fixed tiers in code and ignore the persona setting. On a model-invalid error the runtime steps DOWN the chain (`t0` → `t1` → `t2`) — never up. Other fields like token limits and compaction thresholds are **owned by Claude Code**, not Pip-Boy. To change them, use Claude Code's own config.

Claude Code's `.claude/` configuration is inherited automatically via the Agent SDK's native parent-directory walk-up — Pip itself does no merging. See [`docs/identity-model.md`](docs/identity-model.md) for the full three-tier model, sub-agent lifecycle, and `.claude/` override semantics.

### Slash commands

Two separate verb surfaces:

- **`/subagent`** — sibling lifecycle (create, archive, delete, reset, list). Pip-boy only. Git-style subcommands; no `--flag` options.
- **`/bind` / `/unbind`** — a symmetric routing pair for *this chat*. Works from any agent, including directly between sibling sub-agents without round-tripping through pip-boy. This is user navigation, not sibling management, so it's not gated to pip-boy.

ACL: every command is open to every sender on every channel with one exception — the `/subagent` lifecycle family and `/exit` are **CLI-only**, refused on remote channels and hidden from the remote `/help` output. There is no "owner" / "admin" concept anymore; identity is tracked in the shared `addressbook/` and recorded via the `remember_user` tool.

| Command | Description |
|---|---|
| `/help` | Show all available commands (CLI-only commands are hidden on remote channels). |
| `/status` | Current agent, session key, binding, and channel. |
| `/memory` | Memory statistics for the current agent. |
| `/axioms` | Current judgment principles (`axioms.md`). |
| `/recall <query>` | Search stored memories. |
| `/cron` | List scheduled cron jobs. |
| `/plugin` | Manage Claude Code plugins / marketplaces. See *Plugins and Marketplaces* below. |
| `/bind <id>` | Route this chat to sub-agent `<id>`. Works from any agent. `/bind pip-boy` is rejected with a redirect to `/unbind` — "on pip-boy" has exactly one canonical representation (no binding row). |
| `/unbind` | Clear this chat's binding so routing falls back to pip-boy. No-op when already on pip-boy. |
| `/subagent` | **pip-boy only, CLI-only.** List known sub-agents (alias for `/subagent list`). |
| `/subagent list` | **pip-boy only, CLI-only.** List known sub-agents. |
| `/subagent create <id>` | **pip-boy only, CLI-only.** Scaffold `<workspace>/<id>/.pip/` and register the sub-agent. |
| `/subagent archive <id>` | **pip-boy only, CLI-only.** Move the sub-agent's `.pip/` to `<workspace>/.pip/archived/` and drop its bindings. Project files in `<id>/` are untouched. |
| `/subagent delete <id> --yes` | **pip-boy only, CLI-only.** Wipe the sub-agent's `.pip/` and drop its bindings. Project files in `<id>/` are untouched. |
| `/subagent reset <id>` | **pip-boy only, CLI-only.** Rebuild sub-agent `<id>`'s `.pip/` from a minimal backup — preserves `persona.md` + `HEARTBEAT.md`; everything else is wiped and lazily re-created. Refused on the root agent (pip-boy can't safely self-surgery while running; stop the host and rebuild offline instead). |
| `/exit` | **CLI-only.** Quit Pip-Boy. |

`/subagent` is the pip-boy-only management console. From any sub-agent it returns a redirect to `/unbind` — sub-agents focus on their own work and don't manage siblings. Routing (`/bind` / `/unbind`) is a separate pair of commands that navigate *this chat* and work from anywhere.

Per-agent settings (`model`, `dm_scope`, description) have **no command-line flags** — edit the backing files directly:

- `<workspace>/<id>/.pip/persona.md` — YAML frontmatter controls `model` / `dm_scope`.
- `<workspace>/.pip/agents_registry.json` — descriptions and registry metadata.
- `<workspace>/.pip/bindings.json` — channel → agent routing with optional per-binding `overrides`.

Unknown slash commands (and unknown `/subagent` subcommands) fail fast with an `Unknown command` error plus a `Did you mean …?` hint for close matches. They are **not** forwarded to the model — typos should not cost an LLM turn.

### Workspace directory structure (v2)

```
<pip_boy_workspace>/
├── .pip/                        # pip-boy (root agent) + workspace runtime
│   ├── persona.md               # pip-boy persona + YAML frontmatter
│   ├── HEARTBEAT.md
│   ├── addressbook/             # Shared contacts — <user_id>.md per contact, loaded on demand via lookup_user
│   ├── cron.json                # pip-boy's scheduled jobs
│   ├── state.json               # Memory pipeline cursors
│   ├── memories.json            # L2 consolidated memories
│   ├── axioms.md                # L3 judgment principles
│   ├── observations/            # L1 observation files (.jsonl)
│   ├── incoming/                # Inbound attachments landing zone
│   ├── credentials/             # Channel keys (WeChat / WeCom)
│   ├── bindings.json            # Channel → agent routing (workspace-wide)
│   ├── agents_registry.json     # Known sub-agents
│   ├── sdk_sessions.json        # session_key → SDK session id
│   └── .scaffold_manifest.json  # Scaffold version tracking
├── ProjectA/                    # Plain project, pip-boy operates on it directly
└── <sub-agent-id>/              # Sub-agent with its own identity
    ├── .pip/                    # Independent persona + memory
    │   ├── persona.md
    │   ├── HEARTBEAT.md
    │   ├── state.json cron.json memories.json axioms.md
    │   ├── observations/ incoming/    # sub-agents share the root's addressbook/
    └── .claude/                 # Optional: local CC overrides (see below)
```

See [`docs/identity-model.md`](docs/identity-model.md) for the full three-tier identity model.

## Plugins and Marketplaces

Pip-Boy reuses Claude Code's native plugin system rather than re-implementing one. The Claude Agent SDK ships a full `claude` CLI inside its wheel, which owns the on-disk plugin state under `~/.claude/` (and per-project `.claude/`). Pip-Boy adds a thin chat surface on top so plugins can be discovered, installed, and used from inside a conversation.

### Why it just works

The agent runner loads all three Claude Code settings tiers (`setting_sources=["user", "project", "local"]`), so any plugin installed at any scope is automatically picked up by the next agent turn — no restart, no config merge code on Pip-Boy's side. Each `query()` spawns a fresh `claude.exe`, which reads the latest `settings.json` on the way in.

### Default marketplace bootstrap

A fresh Pip-Boy install ships with `BOOTSTRAP_MARKETPLACES=anthropics/claude-plugins-official` in `.env`, so the first cold-start auto-registers Anthropic's curated catalogue (~60 plugins covering web search, browser automation, IDE language servers, cloud SDKs, etc.). The bootstrap is idempotent — subsequent boots cost one `marketplace list --json` subprocess (~2 s) and short-circuit. Failures (offline, proxy down) are logged at WARNING but never block startup. Set the env var to empty to opt out and match Claude Code's own zero-marketplace default.

### Install scopes

| Scope | File | Visibility |
|---|---|---|
| `user` (default) | `~/.claude/settings.json` | Global; every agent and every cwd. |
| `project` | `<agent-cwd>/.claude/settings.json` | This agent only; gitable. |
| `local` | `<agent-cwd>/.claude/settings.local.json` | This agent only; gitignored. |

For sub-agents, "this agent's cwd" is the sub-agent's directory under the workspace root — so a per-`project` plugin install only affects that one sub-agent.

### Slash commands (host-driven)

```
/plugin list [--available]
/plugin search <query>
/plugin install <spec> [--scope user|project|local]
/plugin uninstall <name> [--scope user|project|local]
/plugin enable <name>  [--scope user|project|local]
/plugin disable <name> [--scope user|project|local]
/plugin marketplace list
/plugin marketplace add <gh-repo|url|path> [--scope user|project|local]
/plugin marketplace remove <name>
/plugin marketplace update [name]
/plugin help
```

`<spec>` is `<name>` or `<name>@<marketplace>` (use the latter to disambiguate when the same name appears in multiple sources). With the default `BOOTSTRAP_MARKETPLACES`, `anthropics/claude-plugins-official` is already registered on first boot, so the typical flow collapses to:

```
/plugin search <query>
/plugin install <name>
```

If you cleared the bootstrap env var (or want to add a third-party catalogue), `marketplace add` accepts `owner/repo`, an HTTPS git URL, or a local path:

```
/plugin marketplace add anthropics/claude-plugins-official
/plugin list --available
```

### Agent-driven (MCP tools)

The agent has self-service surface for additive operations only:

- `plugin_list(available=false|true)`
- `plugin_search(query)`
- `plugin_install(spec, scope=...)`
- `plugin_marketplace_list()`
- `plugin_marketplace_add(source, scope=...)`

Destructive operations (`uninstall`, `disable`, `marketplace remove`) are intentionally **not** exposed as tools — those decisions stay with the human via `/plugin`.

## TUI & Themes

Pip-Boy's CLI is a Textual + Rich TUI by default — a three-pane layout with
streaming agent output, a side art / app-log column, and a status bar — and
falls back to plain line mode only when the terminal can't host it. The
fallback decision is logged to `<workspace>/.pip/tui_capability.log` so
you can see exactly which probe failed.

```text
pip-boy            # TUI by default (line mode if the terminal can't host it)
pip-boy --no-tui   # force line mode (CI, redirected pipes, broken CJK)
pip-boy doctor     # one-shot env + capability + theme report
```

Themes are data-driven: a `theme.toml` manifest, a `theme.tcss` Textual CSS
file, and an optional `art.txt`. Two themes ship in the wheel
(`wasteland` and `vault-amber`); local themes drop into
`<workspace>/.pip/themes/<slug>/` and override built-ins of the same name.

```text
/theme list                # installed themes (built-in + local)
/theme show                # active theme + persisted preference
/theme set <slug>          # persist <slug> for the next boot
```

Selection precedence: `PIP_TUI_THEME` env var → `host_state.json` (set via
`/theme set`) → package default `wasteland`. Live reload is intentionally
out of scope in v1; restart pip-boy after `/theme set` to apply. Themes
own appearance only — widget topology, layout fractions, and pump wiring
are framework invariants guarded by SVG snapshot tests.

See [`docs/themes.md`](docs/themes.md) for the full author guide,
including the locked palette tokens, widget IDs you can style, the
starter theme template, and known v1 constraints.

### Troubleshooting

* **`pip-boy` exits immediately with a Windows runtime error dialog** —
  this is the Textual win32 driver tripping on a stale `__stdout__`
  alignment after the UTF-8 console rewrap. Pip-Boy v0.5+ aligns
  `sys.__stdout__` / `sys.__stdin__` atomically inside
  `force_utf8_console()` (regression-tested in
  `tests/test_console_utf8.py`); if you still see the dialog, run
  `pip-boy doctor` to confirm the `textual` version is `>=1`.
* **`AssertionError: Driver must be in application mode`** — same
  root cause as above; upgrade `pip-boy` to a build that includes
  the alignment fix.
* **CLI shows `[builtin] wasteland` but you copied a custom theme** —
  the local theme directory must match the slug (e.g.
  `.pip/themes/wasteland/theme.toml` with `name = "wasteland"`).
  Mismatched slugs are listed under the `Skipped` section of
  `/theme list` and `pip-boy doctor`.
* **TUI stays disabled on a clearly capable terminal** — check
  `<workspace>/.pip/tui_capability.log`. The first failing stage
  (`tty`, `driver`, or `encoding`) tells you which probe disagreed
  with the environment. Forcing `--no-tui` short-circuits at
  `user_optout`.
* **Updating dependencies fails on Windows** — close any running
  `pip-boy` first. The bundled `claude.exe` (shipped by
  `claude-agent-sdk`) keeps a write lock on its directory while a
  host process is alive, so `pip install -U pip-boy` aborts mid-write.
  Stop the host, retry the install, and start it again.

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
       │ │  — short-circuits /help, /status, /subagent … —│  │
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
                │   — settings: user+proj+loc│ ─ plugin tools
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

- [`docs/themes.md`](docs/themes.md) — TUI theme author guide + starter theme.
- [`docs/releasing.md`](docs/releasing.md) — Release workflow.

## License

MIT. See [LICENSE](LICENSE).
