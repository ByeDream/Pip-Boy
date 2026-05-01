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
| Sub-agent | `<pip_boy_workspace>/workspace/<id>/.pip/` | An individual sub-agent | Independent `persona.md`, `memory`, `observations/`. Contacts live in the shared workspace-level `addressbook/` (uuid-keyed, lazy-loaded) and are not duplicated per sub-agent. The `workspace/` container is hard-coded (`routing.SUBAGENTS_SUBDIR`) — `/subagent` commands take a bare id and the prefix is added for them. |

Scalar settings from higher tiers override lower tiers; array-valued
settings (like permission allow-lists) are unioned with dedup.

**Persona, memory, observations, and axioms never merge.** Each agent —
including `pip-boy` itself — owns its own. The `addressbook/` is the
one deliberate exception: it lives at the workspace root and is read /
written by every agent so a contact learned anywhere is known
everywhere. Each contact is stored as `<user_id>.md` where `<user_id>`
is an opaque 8-hex handle; profiles are loaded lazily via the
`lookup_user` tool rather than injected into the system prompt, so
prompt cost stays flat as the book grows.

## User recognition

Every inbound user message is wrapped in a `<user_query>` envelope the
agent sees at the top of each turn:

```xml
<user_query from="<channel>:<sender_id>" user_id="<8-hex or unverified>">
...message body...
</user_query>
```

- `from` is the raw channel + sender identity (e.g. `cli:cli-user`,
  `wecom:abc123`). Useful for channel-dependent decisions (markdown on
  CLI vs plain text on WeChat) but never by itself sufficient for
  identity — the same person can reach you from multiple channels.
- `user_id` is the stable, opaque addressbook handle (e.g. `9c8b2a3e`)
  — or the literal string `unverified` if this sender isn't in the
  addressbook yet. The agent is expected to treat it as a meaningless
  key and look up the profile on demand via `lookup_user(user_id)`.
- `group="true"` is present on group-chat messages.

The flow:

```
first contact              follow-up turns
---------                  ---------------
user_id="unverified"       user_id="<8-hex>"
        │                          │
        ▼                          ▼
 agent asks name            agent knows them already
        │                   (optional: lookup_user on
        ▼                    first mention of a turn,
 remember_user(              then cache for the rest)
   name=...,                        │
   call_me=...)                     ▼
        │                    remember_user(notes=...)
        ▼                    updates only their own
 store mints new             record; different user_id
 8-hex user_id,              is refused
 binds sender to it
```

`remember_user` is strictly self-directed: verified callers update
their own record and nothing else; unverified callers create a fresh
record and receive their assigned `user_id` in the response. To record
facts about a *third* person (e.g. "Alice mentioned her colleague
Bob"), use `memory_write` — it lives in the per-agent observations
stream rather than the shared contact book.

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
    addressbook/<user_id>.md  # shared contacts (every agent reads / writes via lookup_user / remember_user)
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

## Sub-agent lifecycle and routing

Two separate concerns with two separate verb surfaces:

- **`/subagent`** — sibling lifecycle (create, archive, delete, reset,
  list). Pip-boy only; sub-agents don't manage siblings. Subcommand
  style is git-like; no `--flag` options for configuration.
- **`/bind` / `/unbind`** — symmetric routing pair for *this chat*.
  Works from any agent, including directly from one sub-agent to
  another. It's user navigation, not sibling management, so it's
  not gated to pip-boy.

  - `/bind <id>` — route this chat to sub-agent `<id>`.
  - `/unbind` — clear the binding; routing falls back to pip-boy.

  `/bind pip-boy` is rejected with a redirect to `/unbind` so
  "on pip-boy" has exactly one canonical representation (no binding
  row), not two (absent row vs explicit row pointing at root).

| Command | Effect | ACL |
| --- | --- | --- |
| `/bind <id>` | Route this chat to sub-agent `<id>`. Works from any agent. `/bind pip-boy` is rejected with a redirect to `/unbind`. | open |
| `/unbind` | Clear this chat's binding so routing falls back to pip-boy. No-op when already on pip-boy. | open |
| `/subagent` | **pip-boy only.** List known sub-agents (alias for `/subagent list`). | CLI-only |
| `/subagent list` | **pip-boy only.** List known sub-agents. | CLI-only |
| `/subagent create <id>` | **pip-boy only.** Scaffold `<workspace>/workspace/<id>/.pip/` with a cloned persona + HEARTBEAT and register it. The `workspace/` container is hard-coded — the command takes a bare id. | CLI-only |
| `/subagent archive <id>` | **pip-boy only.** Move `workspace/<id>/.pip/` → `<workspace>/.pip/archived/<id>-<ts>/.pip/`, drop bindings. Project files in `workspace/<id>/` untouched. | CLI-only |
| `/subagent delete <id> --yes` | **pip-boy only.** `rmtree(workspace/<id>/.pip/)` and drop bindings. Project files in `workspace/<id>/` untouched. | CLI-only |
| `/subagent reset <id>` | **pip-boy only.** Rebuild sub-agent `<id>`'s `.pip/` from a minimal backup (see below). Refused on the root agent. | CLI-only |

"CLI-only" commands are refused outright on remote channels (WeCom,
WeChat, …) and are not advertised in the remote `/help` listing, so a
random peer on those channels can't even discover they exist. "Open"
commands run for anyone regardless of channel.

There are no CLI flags for `model` / `dm_scope` / `description`. Edit
the relevant file directly:

- `<workspace>/workspace/<id>/.pip/persona.md` — YAML frontmatter for
  `model` and `dm_scope`.
- `<workspace>/.pip/agents_registry.json` — description and registry
  metadata.
- `<workspace>/.pip/bindings.json` — channel → agent routing with
  optional per-binding `overrides` (`scope`, `model`).

Destructive commands (`create`, `archive`, `delete`, `reset`) are
**scoped to the agent identity surface** (`.pip/`). Project files,
`.git/`, build artefacts, etc. inside the sub-agent's directory are
never touched. "Delete the agent" means "end its identity", not "nuke
the project".

### `/subagent reset <id>` — backup, delete, rebuild

`reset` is implemented as a clean four-step dance, not as surgical
field-by-field clearing:

1. **Stash** the identity files (`persona.md`, `HEARTBEAT.md`) to a
   sibling temp directory.
2. **Delete** the agent's entire `.pip/` directory.
3. **Rebuild** an empty `.pip/` and restore the stash into it.
4. **Drop** the temp stash.

As a side-step, entries keyed `agent:<id>:…` in the workspace-shared
`sdk_sessions.json` are also removed, so the next turn opens a fresh
SDK session.

What survives: identity (`persona.md` + `HEARTBEAT.md`).
What's wiped: observations, memories.json, axioms.md, state.json,
incoming/, cron.json, sdk_sessions entries for this agent,
scaffold manifest, etc. These are all lazily re-created by the host
on the next turn. The shared `addressbook/` lives at the workspace
root and is never touched by a sub-agent reset.

**Root agent is refused.** `/subagent reset pip-boy` returns an
explanatory error instead of running. The root's `.pip/` holds
workspace-shared state (`addressbook/`, `bindings.json`,
`agents_registry.json`, `credentials/`, `archived/`) that other
agents depend on, and its `MemoryStore` / `StreamingSession` are
in active use by the very command handler that would perform the
reset — any in-process self-surgery has a window in which the
cached store points at wiped paths, sessions hold handles against
CC's project dir, and concurrent writes can resurrect the files
we just deleted. If you really need to reset the root, stop the
host (`/exit`) and rebuild the root `.pip/` offline.

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
