# Identity & Configuration Model (v2)

Pip-Boy v2 aligns with Claude Code's `.claude/` layout so a single Pip host
can operate inside a workspace that hosts many projects, while still
letting selected projects carry an independent agent persona and memory.

## Three tiers

Pip's own config (persona, memory, host settings) is stacked in three
layers; each inherits from the one above:

| Layer | Location | Scope | Notes |
| --- | --- | --- | --- |
| System (lowest) | `~/.pip/` | Per-user system settings | Only CLI preferences and `settings.json` (logging / profiler / network defaults). No channel credentials, no bindings. |
| Workspace | `<pip_boy_workspace>/.pip/` | The root `pip-boy` agent's home + shared runtime state | Holds `bindings.json`, `agents_registry.json`, `credentials/`, `sdk_sessions.json`. |
| Sub-agent | `<pip_boy_workspace>/<id>/.pip/` | An individual sub-agent | Independent `persona.md`, `memory`, `observations/`, `users/`. |

Scalar settings from higher tiers override lower tiers; array-valued
settings (like permission allow-lists) are unioned with dedup.

**Persona, memory, observations, axioms, and per-user profiles never
merge.** Each agent — including `pip-boy` itself — owns its own.

## Directory layout

```
~/.pip/
  settings.json
  cli.json

<pip_boy_workspace>/
  .pip/                     # pip-boy's home + workspace runtime state
    persona.md
    HEARTBEAT.md
    state.json cron.json memories.json axioms.md
    observations/*.jsonl
    users/*.md
    incoming/
    credentials/             # channel keys (WeChat / WeCom)
    bindings.json            # global channel -> agent routing table
    agents_registry.json     # known sub-agents
    sdk_sessions.json        # session_key -> CC session id
    .scaffold_manifest.json
  .claude/                   # optional: CC-native project config
    settings.json agents/ skills/

  ProjectA/                  # plain project, driven by pip-boy directly
  ProjectB/
  sub-agent-X/               # a sub-agent lives here
    .pip/                    # independent persona + memory
      persona.md HEARTBEAT.md ...
    .claude/                 # optional: local CC overrides (see below)
```

## Sub-agent lifecycle

Agent-lifecycle management lives under a single `/agent` verb — **the
pip-boy-exclusive management console**. From a sub-agent, `/agent`
returns a redirect to `/home`; sub-agents focus on their own work and
don't manage siblings. Routing direction is symmetric:

- `/agent switch <id>` — pip-boy → sub-agent (only available from pip-boy).
- `/home` — sub-agent → pip-boy (available everywhere; no-op on pip-boy).

The subcommand style is git-like; no `--flag` options for
configuration.

| Command | Effect | ACL |
| --- | --- | --- |
| `/home` | Clear this chat's binding so routing falls back to pip-boy. No-op when already on pip-boy. | owner-or-admin |
| `/agent` | **pip-boy only.** Show pip-boy's detail + memory summary. | owner-or-admin |
| `/agent list` | **pip-boy only.** List known agents. | owner-or-admin |
| `/agent create <id>` | **pip-boy only.** Scaffold `<workspace>/<id>/.pip/` with a cloned persona + HEARTBEAT and register it. | owner |
| `/agent archive <id>` | **pip-boy only.** Move `<id>/.pip/` → `<workspace>/.pip/archived/<id>-<ts>/.pip/`, drop bindings. Project files in `<id>/` untouched. | owner |
| `/agent delete <id> --yes` | **pip-boy only.** `rmtree(<id>/.pip/)` and drop bindings. Project files in `<id>/` untouched. | owner |
| `/agent switch <id>` | **pip-boy only.** Route this chat to sub-agent `<id>`. Use `/home` to come back. | owner-or-admin |
| `/agent reset <id>` | **pip-boy only.** Rebuild `<id>`'s `.pip/` from a minimal backup (see below). | owner |

There are no CLI flags for `model` / `dm_scope` / `description`. Edit
the relevant file directly:

- `<workspace>/<id>/.pip/persona.md` — YAML frontmatter for `model` and
  `dm_scope`.
- `<workspace>/.pip/agents_registry.json` — description and registry
  metadata.
- `<workspace>/.pip/bindings.json` — channel → agent routing with
  optional per-binding `overrides` (`scope`, `model`).

Destructive commands (`create`, `archive`, `delete`, `reset`) are
**scoped to the agent identity surface** (`.pip/`). Project files,
`.git/`, build artefacts, etc. inside the sub-agent's directory are
never touched. "Delete the agent" means "end its identity", not "nuke
the project".

### `/agent reset <id>` — backup, delete, rebuild

`reset` is implemented as a clean four-step dance, not as surgical
field-by-field clearing:

1. **Stash** the identity files (`persona.md`, `HEARTBEAT.md`) to a
   sibling temp directory. For the root agent (`pip-boy`), the
   workspace-shared state is stashed too: `owner.md`, `bindings.json`,
   `agents_registry.json`, `credentials/`, `archived/`. These are
   the files you lose channel access / ACL / sub-agent inventory
   over if they vanish — they're preserved by definition.
2. **Delete** the agent's entire `.pip/` directory.
3. **Rebuild** an empty `.pip/` and restore the stash into it.
4. **Drop** the temp stash.

As a side-step, entries keyed `agent:<id>:…` in the workspace-shared
`sdk_sessions.json` are also removed, so the next turn opens a fresh
SDK session.

What survives: identity (persona + HEARTBEAT) and, for pip-boy, the
five workspace-shared artefacts above.
What's wiped: observations, memories.json, axioms.md, state.json,
users/, incoming/, cron.json, sdk_sessions entries for this agent,
scaffold manifest, etc. These are all lazily re-created by the host
on the next turn.

## How `.claude/` inheritance works (zero bridging)

Pip does **not** merge, republish, or bridge `.claude/` configs. The
Claude Agent SDK's `setting_sources=["project","user"]` handles this
natively:

- `"user"` always loads `~/.claude/` — plugins, skills, and user-level
  subagents are globally visible. Nothing Pip does can hide them.
- `"project"` starts from the SDK subprocess `cwd` and walks up parent
  directories, stopping at the **first** `.claude/` it finds.

Because Pip launches each turn with `cwd=<agent's own directory>`:

| Situation | `"project"` finds | `"user"` finds |
| --- | --- | --- |
| Sub-agent has **no** `.claude/` (default) | `pip_boy_workspace/.claude/` (walk-up) | `~/.claude/` |
| Sub-agent has its own `.claude/` | `sub-agent/.claude/` (walk stops here) | `~/.claude/` |

### All-or-nothing override

When a sub-agent creates its own `.claude/`, the walk-up stops there.
The SDK will **not** also load `pip_boy_workspace/.claude/`. This is a
deliberate upstream choice: project-scope configuration is either
inherited wholesale from the nearest ancestor or owned wholesale by the
sub-agent. Partial overrides (e.g. "inherit the workspace MCP servers
but swap one skill") are not supported at this layer.

Practical guidance:

- **Share across everything (inside Pip and outside):** put the config
  in `~/.claude/`. Always visible regardless of which sub-agent runs.
- **Share across all Pip sub-agents only:** put it in
  `pip_boy_workspace/.claude/`. Visible to every sub-agent whose own
  directory lacks `.claude/`.
- **Isolate a single sub-agent:** create `sub-agent/.claude/` and write
  the full config you want. The workspace layer is no longer visible
  to that sub-agent, but `~/.claude/` still is.

### What Pip does not do

- Does not merge or copy `.claude/` configurations between tiers.
- Does not publish sub-agent skills into `~/.claude/skills/`.
- Does not spawn dispatcher/worker subprocesses — one Pip service
  process manages every channel and every sub-agent, routing each turn
  to the correct `cwd`.
- Does not support nested workspaces (no sub-agent inside a sub-agent).

## Routing and session pooling

Inbound messages resolve to an `agent_id` through the same 5-tier
`bindings.json` as before (channel / peer / guild / DM scope / default).
The registry maps `agent_id → cwd`, and that `cwd` is what we pass to
`ClaudeAgentOptions`. Session transcripts land under
`~/.claude/projects/<url-encoded-cwd>/`, so different sub-agents have
naturally disjoint transcript stores.

The warm session pool is keyed by `(agent_id, session_key)` so sub-agent
streaming subprocesses never cross-contaminate.
