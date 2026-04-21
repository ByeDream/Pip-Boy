# claude-agent-sdk contract notes (Phase 0.5)

> Observed from `claude-agent-sdk==0.1.63` on Python 3.14.2 (Windows).
> Plan referenced `0.1.56`; `0.1.63` is the resolved wheel for `>=0.1.56`.
> Reproduce with `python scripts/sdk_smoke.py` (optionally `--skip-live`).

## 1. `ClaudeAgentOptions` — relevant fields

All fields we actually touch in `agent_runner.py`:

| field | type | default | notes |
|---|---|---|---|
| `system_prompt` | `str \| SystemPromptPreset \| SystemPromptFile \| None` | `None` | plain string works (we pass persona + appended context as one string) |
| `mcp_servers` | `dict[str, McpSdkServerConfig \| …] \| str \| Path` | `{}` | dict form for in-process SDK MCP is fine |
| `permission_mode` | `Literal['default','acceptEdits','plan','bypassPermissions','dontAsk','auto'] \| None` | `None` | `'bypassPermissions'` valid |
| `resume` | `str \| None` | `None` | pass previous SDK session_id to continue |
| `session_id` | `str \| None` | `None` | can pre-seed |
| `continue_conversation` | `bool` | `False` | alt to `resume` |
| `fork_session` | `bool` | `False` | |
| `cwd` | `str \| Path \| None` | `None` | |
| `env` | `dict[str, str]` | `{}` | merged into CLI subprocess env |
| `extra_args` | `dict[str, str \| None]` | `{}` | passed to `claude` CLI |
| `hooks` | `dict[HookEvent, list[HookMatcher]] \| None` | `None` | keys are event literals (see §3) |
| `setting_sources` | `list[Literal['user','project','local']] \| None` | `None` | **`'project'` is valid** (plan mentioned this, confirmed) |
| `agents` | `dict[str, AgentDefinition] \| None` | `None` | not used by Pip host |
| `skills` | `list[str] \| Literal['all'] \| None` | `None` | **Pip does not set this**; CC picks up `.claude/skills/` automatically |
| `model` / `fallback_model` | `str \| None` | `None` | we only set `model` |
| `can_use_tool` | callback | `None` | not used |
| `max_turns`, `max_budget_usd`, `max_thinking_tokens`, `thinking`, `effort`, `task_budget`, `plugins`, `sandbox`, `betas`, `enable_file_checkpointing`, `output_format`, `user`, `include_partial_messages`, `disallowed_tools`, `allowed_tools`, `add_dirs`, `tools`, `cli_path`, `settings`, `debug_stderr`, `stderr`, `permission_prompt_tool_name`, `max_buffer_size` | various | | not used, but available |

**Removed-from-plan concern resolved**: `permission_mode='bypassPermissions'` accepted as-is.

## 2. `HookMatcher`

```python
@dataclass
class HookMatcher:
    matcher: str | None = None
    hooks: list[HookCallback] = []
    timeout: float | None = None
```

Our callsite `HookMatcher(hooks=[_pre_compact_hook])` is correct. `matcher` is only meaningful for `PreToolUse` / `PostToolUse` (name/regex filter); omit for `PreCompact` / `Stop`.

## 3. Hook event registry keys

`ClaudeAgentOptions.hooks` dict key must be one of:

```
'PreToolUse' | 'PostToolUse' | 'PostToolUseFailure' | 'UserPromptSubmit' |
'Stop' | 'SubagentStop' | 'PreCompact' | 'Notification' |
'SubagentStart' | 'PermissionRequest'
```

Pip uses `'PreCompact'` and `'Stop'` only.

## 4. Hook input TypedDicts (the important one)

### `PreCompactHookInput`

```
session_id         : str
transcript_path    : str           # absolute path to JSONL for this session
cwd                : str
permission_mode    : str
hook_event_name    : Literal['PreCompact']
trigger            : Literal['manual', 'auto']
custom_instructions: str | None
```

**Critical**: `transcript_path` is provided directly. Phase 4.5 should read it straight from `input_data['transcript_path']`; we do NOT need to reconstruct it from cwd. This collapses most of the "how does CC encode cwd into the directory name?" worry — we only need that reverse lookup for the *ad-hoc* `reflect` MCP tool path (see §6 below).

### `StopHookInput`

```
session_id       : str
transcript_path  : str
cwd              : str
permission_mode  : str
hook_event_name  : Literal['Stop']
stop_hook_active : bool
```

`transcript_path` is present in `Stop` too. If we later decide to do per-turn incremental reflect (see plan §Phase 4.5 item 2), `Stop` is viable.

### `PreToolUseHookInput` / `PostToolUseHookInput`

Both carry `agent_id` and `agent_type` in addition to `session_id`, `transcript_path`, `cwd`, `permission_mode`, `tool_name`, `tool_input`, `tool_use_id` (+ `tool_response` on post). Not used by Pip but handy if profiling ever comes back at the CC layer.

## 5. Hook callback signature

From `HookMatcher.hooks` type:

```python
Callable[
    [HookInput, str | None, HookContext],
    Awaitable[AsyncHookJSONOutput | SyncHookJSONOutput],
]
```

So `async def hook(input_data, tool_use_id, context)` — exactly what `src/pip_agent/hooks.py` uses. Return an empty dict `{}` to be a no-op.

## 6. JSONL layout (`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`)

Not observable on this machine (no prior CC runs; `~/.claude/projects/` does not exist). Known facts:

1. Inside hooks we get `transcript_path` **for free**, so `PreCompact`-driven reflect is path-safe without guessing the encoding.
2. For the ad-hoc `reflect` MCP tool (where we only know `(cwd, session_id)`), `memory/transcript_source.py` must resolve the path. Phase 4.5 implementation strategy (resilient to encoding uncertainty):
   - **Primary**: use the current `SystemMessage(init).data['session_id']` captured in `agent_host` — when the reflect tool fires, host passes `transcript_path` into `McpContext` directly (skip the encoding problem).
   - **Fallback**: scan `~/.claude/projects/*/<session_id>.jsonl` (glob). The filename is the session_id; the directory encoding does not matter because we don't care which cwd-encoded folder it lives in — there will only be one match.
   - **Secondary fallback**: if no match, scan the newest `.jsonl` across all project dirs and see if its first `session_id` field equals ours.

The smoke-test output note `"(not present) ~/.claude/projects"` is expected — will be created on first real run.

**JSONL line schema** is undetermined from static inspection; the SDK's private types package `claude_agent_sdk.types` suggests each line is a `SessionMessage` or similar. Phase 4.5 plan: on first real run, append output of `scripts/sdk_smoke.py` (without `--skip-live`) to this doc under §7. In the meantime, write `transcript_source.py` defensively:

```python
def iter_transcript(path: Path, start_offset: int = 0) -> Iterator[tuple[int, dict]]:
    with path.open('r', encoding='utf-8') as fh:
        fh.seek(start_offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield fh.tell(), json.loads(line)
            except json.JSONDecodeError:
                continue
```

Known candidate fields to probe at runtime: `type` (`user`/`assistant`/`system`/`tool_use`/…), `message.role`, `message.content` (list of blocks), `session_id`, `parent_uuid`, `timestamp`. `transcript_source.py` should expose a `to_role_content(line: dict) -> tuple[str, str] | None` that normalizes to the `{role, content}` shape `reflect.py::_format_transcript` wants and drops lines it can't interpret.

## 7. Live-query probe (TODO after first real run)

To be appended when the user first runs `python scripts/sdk_smoke.py` in an environment with `claude` CLI + auth:

- `SystemMessage(subtype='init').data` keys
- One full JSONL line example per observed role
- `PreCompact.input_data` dump with manual `/compact`

Until then, phases that depend on this (4.5) MUST use the defensive fallback strategy described in §6.

## 8. Pip-side code-call audit

Cross-checked `agent_runner.py` and `hooks.py` against the above — all calls conform. No changes needed. `SEARCH_API_KEY` passthrough in `_build_env` is unused by CC-managed tools and will be removed in Phase 1.

## 9. SDK spawns its bundled CLI, not a system `claude`

Easy mistake to make (and I made it once during heartbeat debugging): the
transport that the SDK's `query()` uses is `SubprocessCLITransport`, which
spawns a real `claude` **executable** — but that executable is shipped
**inside the Python wheel**, not resolved from `PATH`.

Evidence (Python 3.14, Windows, `claude-agent-sdk==0.1.63`):

```
>>> from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
>>> SubprocessCLITransport(prompt="x", options=ClaudeAgentOptions())._find_cli()
'…\\site-packages\\claude_agent_sdk\\_bundled\\claude.exe'
```

And at runtime the SDK logs:

```
INFO claude_agent_sdk._internal.transport.subprocess_cli Using bundled Claude Code CLI
```

Implications for Pip-Boy as a public package:

- We do **not** require users to `npm i -g @anthropic-ai/claude-code` (or
  Tencent's `@tencent/claude-code-internal`). `pip install pip-boy` is
  sufficient — the SDK wheel carries everything it needs.
- `where.exe claude` / `which claude` failing on a user's machine is
  **not** an installation problem for us. Do not chase it.
- `ClaudeAgentOptions.cli_path` is an escape hatch, not a requirement. We
  should never set it in normal code paths; reserve it for power users
  pointing at a forked CLI.
- Debug checklist when "agent seems to do nothing": check logging config
  first (see `__main__._configure_logging` + `tests/test_main_logging.py`)
  **before** suspecting the CLI.

## 10. CC native cron is dead in our architecture — we kill it

Claude Code ships three built-in tools — `CronCreate`, `CronList`,
`CronDelete` — plus a `/loop` bundled skill that fronts them. They look
useful until you realise how they actually tick:

> "The scheduler checks every second for due tasks … A scheduled prompt
> fires between messages while you're using the CLI."
> ([scheduled-tasks docs](https://code.claude.com/docs/en/scheduled-tasks))

That second clause is load-bearing. The scheduler is a thread inside the
`claude.exe` process. Our transport (`SubprocessCLITransport`) spawns a
fresh `claude.exe` for every `run_query` and lets it exit on `end_turn`.
So in Pip-Boy's subprocess-per-turn world:

- `CronCreate` returns success → the model is happy.
- The `claude.exe` subprocess exits seconds later → the scheduler thread
  dies with it.
- The persisted session JSONL may carry the task on `--resume`, but the
  next subprocess also exits in seconds, so it still never fires.
- The user sees nothing happen. Ever.

Worse, the model has no way to know this: `CronList` cheerfully reports
"task scheduled". **A shipping API that silently lies to the agent is
worse than no API at all.**

Therefore: **we disable CC native cron across the board** by injecting
`CLAUDE_CODE_DISABLE_CRON=1` into the subprocess env — see
`agent_runner._build_env`. The regression test lives at
`tests/test_anthropic_client.py::TestBuildEnv::test_cron_kill_switch_is_always_set`.

Pip-Boy's own scheduler (`host_scheduler.HostScheduler`) is a separate
story: it lives in the long-running host process, persists jobs to
`.pip/agents/<id>/cron.json`, survives restarts, fires into CLI / WeChat
/ WeCom channels, and carries no 50-task / 3-day-expiry caps. So the
model sees exactly one cron provider: `mcp__pip__cron_*`.

If a future Pip-Boy mode ever keeps a long-lived `claude.exe` subprocess
alive across turns (e.g. a "supervised agent" mode with bidirectional
streaming), this env flag becomes **revisitable** — not wrong, just
worth reconsidering. Until then, leave it on.

## 11. Memory pipeline redesign (DESIGN — not yet implemented)

Status: **design doc, not code**. Captured here so we don't lose the
thread; wire it up after the current regression round.

### 11.1 Why the current split is wrong

Today we run two memory systems in parallel:

1. **SDK session resume** (`resume=<session_id>`) — every user turn,
   the full JSONL transcript is re-read from disk, re-tokenized, and
   re-shipped to the model. Growth is unbounded. The cold-cache penalty
   on a 60 MB JSONL is measured in minutes.
2. **Pip-Boy memory pipeline** (`memory/reflect.py`,
   `memory/consolidate.py`) — observations → memories → axioms,
   injected by the host into `system_prompt_append` every turn.

(1) is what CC gives us by default. (2) is what Pip-Boy was built for.
Run both and the model sees the same information **twice**, at
different granularities, at the cost of the more expensive one (the
raw JSONL replay) keeping grow.

`_is_ephemeral_sender` (in `agent_host.py`) already stops cron and
heartbeat turns from polluting the user's JSONL. Note the deliberate
two-layer split there: `session_for_turn=None` keeps the SDK from
resuming, but `ctx_session_id` still carries the user's session id
into MCP tools — otherwise a "2 am cron calls reflect" flow would
silently skip (reflect needs the user's JSONL path). The next step
is to make the pipeline
**authoritative** for long-term memory and let the JSONL serve only
its legitimate purpose: crash-recovery of an in-flight conversation.

### 11.2 The unified flow

```
user turn N ──┐
user turn N+1 ┼── JSONL (SDK session, grows until CC auto-compacts)
user turn N+2 ┘
              │
              ├─ (a) PreCompact hook → reflect()    ← CC's own "context full" signal
              ├─ (b) /exit           → reflect()    ← catch residual on exit
              │
              ▼
        observations.jsonl ──┐
                             │ Dream (idle-night cron, sufficient obs)
                             │   — independent of reflect, runs on
                             │   already-persisted observations
                             ▼
                        memories.json ──┐
                                        │ distill
                                        ▼
                                   axioms.md
                                        │
                                        ▼
                           system_prompt_append every turn
```

Heartbeat is **not** part of the memory pipeline. It is pure
keepalive / proactive behaviour; see the HEARTBEAT_OK sentinel
contract in `agent_host.py`.

### 11.3 Decisions (locked in this session)

| # | Decision | Rationale |
|---|---|---|
| Q1 | Reflect output **≤ 5 observations** per call | Hard cap; forces the model to pick the high-signal ones instead of dumping the transcript. |
| Q2 | Dream = existing L2/L3 code (`consolidate` + `distill_axioms`); auto-trigger = idle-hour + enough observations | Algorithm is done and tested (old Pip). Only the **trigger** needs to come back — it was deleted during the lean rewrite in favour of "agent schedules itself via `cron_create`", which turned out to waste cold-starts. |
| Q3/Q4 | **Keep axioms in our own `axioms.md`**, inject via `system_prompt_append`. Do NOT write to CC's native `~/.claude/projects/<cwd>/memory/` | We want the option to run multiple Pip-Boy agents against the same cwd (e.g. `pip-boy` + `dev-assistant`). CC's memory folder is keyed by cwd, not agent, so using it would force memory-sharing across agents. Until multi-agent is a real product need, stay with the current per-agent `.pip/agents/<id>/axioms.md`. |
| Q5 | Reflect is atomic via **session rotation**: mint a new `session_id`, archive the old JSONL, observations extracted from the archive | Avoids "truncate the live JSONL" races. On crash mid-reflect, the archive is still intact and the next start just picks up the new (empty) session. |
| Q6 | **Reflect triggers are exactly two: PreCompact hook + /exit.** Heartbeat does NOT trigger reflect. | PreCompact is CC's free "content threshold reached" signal — firing it right before CC compacts gives us exactly the boundary we want, using CC's own accumulation heuristic instead of a synthetic turn-count / byte threshold we'd have to invent. `/exit` covers short sessions that never hit the compact threshold. Heartbeat-driven reflect would either duplicate PreCompact (same signal, worse timing) or require its own threshold logic that we'd have to tune forever. Keep heartbeat off the memory path entirely. |
| Q7 | `reflect_from_jsonl` pre-LLM short-circuit: if `start_offset` has no new bytes, return `[]` without any LLM call | Today the short-circuit lives inside `load_formatted` returning empty (reflect.py:134). Lift it to the function entrance as an explicit `if new_offset == start_offset: return` so a future refactor of `load_formatted` can't silently re-introduce a "zero-new-bytes still burns a cold-start" regression. Cheap belt-and-suspenders — the existing PreCompact + /exit triggers should never hit this in steady state, but the guarantee belongs in code, not in timing assumptions. |
| Q8 | Preserve `reflect_from_jsonl`'s **failure-does-not-advance-cursor** contract | On LLM exception, `return start_offset, []` keeps the byte cursor pinned so the next trigger re-reads the same delta. This is an implicit at-least-once for reflect and has to stay — mark with a unit test that a raised LLM error keeps `state[_OFFSET_KEY]` unchanged. |

### 11.4 What has to change

Implementation status (Apr 2026):

| Area | Change | Status |
|---|---|---|
| `memory/reflect.py` | Prompt rewrite — cap at 5, explicit Q7 entry short-circuit, Q8 unit tests. Hard Python cap at `_MAX_OBSERVATIONS_PER_PASS=5`. | ✅ done |
| `memory/reflect.py` | Extract `reflect_and_persist` helper (state-key, advance-only cursor, observation persistence) so PreCompact, /exit, and the reflect MCP tool all share one implementation. Public constant `OFFSET_STATE_KEY`. | ✅ done |
| `agent_host.py` | `flush_and_rotate()` — on /exit: reflect every live session, then clear `_sessions` unconditionally. Reflect runs on `asyncio.to_thread`; failure on one session does not block rotation of the others. | ✅ done |
| `host_scheduler.py` | Revive idle-hour Dream trigger — 5 gates (window, in-flight, same-window re-entry via state, min observations, idle minutes), one-shot daemon-thread worker so the 5 s tick keeps flowing across the 30–90 s consolidate+distill pass. Guard is always released in `finally`. | ✅ done |
| `config.py` | `dream_hour_start` / `dream_hour_end` / `dream_min_observations` / `dream_inactive_minutes` settings, documented in `env.example` with the full trigger contract. | ✅ done |
| `hooks.py` | No change — PreCompact already wired. Refactored to call the new `reflect_and_persist` shared helper. | ✅ done |

### 11.5 Explicitly out of scope (for now)

- Using CC's `~/.claude/projects/<cwd>/memory/` for axiom auto-injection
  (blocked on multi-agent isolation; revisit when L2 per-agent
  `CLAUDE_CONFIG_DIR` lands).
- Any form of mid-turn "compact" — reflect rotates, it doesn't summarize
  in place.
- Auto-reflecting at a token threshold mid-conversation — PreCompact
  already handles the accumulation boundary; a second threshold would
  just race PreCompact.
- **Heartbeat-triggered reflect.** Earlier design iterations put reflect
  behind a heartbeat nudge. Dropped once it became obvious PreCompact
  covers the same "enough accumulated, time to extract" signal using
  CC's own heuristic, and does so exactly at the moment CC is about to
  discard the raw content. Do not bring heartbeat back into the memory
  pipeline without a concrete failure of the PreCompact + /exit pair.
- Making PreCompact fire on cron / heartbeat turns. Post-F
  (`_is_ephemeral_sender`), those turns run with `session_id=None` and
  a fresh near-empty context, so CC will never decide to auto-compact
  them. This is correct — reflect should only pull from user
  transcripts; cron / heartbeat output is deliberately throwaway.
  Changing this would require revisiting F's tradeoffs first.

## 12. Phase 6 / 7 / 8 — host-surface polish for v0.4.0

The three phases collectively restore the "rich host surface" that the
lean rewrite cut to the bone, without dragging any of the legacy
subsystems (lanes, resilience runner, worktrees, teammates) back in.

### 12.1 Phase 6 — slash dispatch (`host_commands.py`)

Replaces the 900-line legacy `commands.py` with a 12-command flat
surface that matches what the rewritten architecture actually has
machinery for:

```
/help /status /memory /axioms /recall /cron
/bind /unbind /name /reset /admin /exit
```

Dropped outright: `/scheduler`, `/lanes`, `/heartbeat`, `/trigger`,
`/cron-trigger`, `/profiles`, `/cooldowns`, `/stats`,
`/simulate-failure`, `/fallback`, `/update`, `/clean`, `/model`.
Each either surfaced a removed subsystem (`/lanes` → lanes gone) or
duplicated a path the CC layer now owns (`/model` → CC config).

Integration point is `AgentHost.process_inbound`: dispatch runs
*after* agent resolution (so `/status` can report the right binding)
but *before* prompt enrichment and the CC subprocess spawn. Unknown
slashes fall through to the model so the "LLM interprets `/foo` as
free text" escape valve stays open. ACL lives in the dispatcher,
not in individual handlers — CLI is always owner, `/admin` is
owner-only, everything else is owner-or-admin; missing memory store
is fail-open (pre-boot has nothing to protect).

Contract highlights that regression tests must hold:

- Handler exceptions become `[error] …` responses, not stack traces
  in the transcript (`tests/test_host_commands.py::TestErrorIsolation`).
- Leading `@mention` is stripped before slash detection so WeCom
  `@bot /help` routes correctly (`TestDispatchRecognition`).
- `/reset` builds its own `MemoryStore` instance for the routed
  agent — reusing `ctx.memory_store` would wrong-target when the
  caller did `/bind` + `/reset` in the same turn.

### 12.2 Phase 7 — multimodal inbound (`_format_prompt` → str | list[dict])

`_format_prompt` now returns either a plain string (hot path, pure
text) or an Anthropic content-block list when the inbound carries
attachments. Callers — specifically `run_query` — accept both.

Why keep two shapes instead of always producing blocks:

1. The SDK's string path is a single stdin line; the block path is
   a streaming-mode `AsyncIterable` envelope. String mode is the
   simpler code path on both sides — if there's nothing to gain
   from blocks, we don't pay the complexity.
2. Heartbeat / cron inbounds never carry attachments but *do* carry
   sentinel tags (`<heartbeat>`, `<cron_task>`) that the LLM uses
   to route. A block-list with one text block would work but
   churns existing transcripts and tests for no benefit.

`run_query` grew a `_stream_single_user_message` helper that wraps a
block list in exactly the SDK's `user` envelope shape:

```python
{
    "type": "user",
    "session_id": "",          # resumption is via options.resume, not this
    "message": {"role": "user", "content": blocks},
    "parent_tool_use_id": None,
}
```

Matching the envelope exactly (including the empty `session_id`) is
important: the shape is copy-pasted from
`claude_agent_sdk._internal.client` — drifting from it silently
breaks hook inputs that key off those fields.

Per-attachment rendering rules (from legacy `agent.py` and preserved
exactly):

- `image` with bytes → base64 image block, `media_type` defaults to
  `image/jpeg` when the channel couldn't sniff one.
- `image` without bytes → text placeholder (`[Image]` or the
  channel-supplied caption). Keep the signal, lose the pixels.
- `file` with extracted text → `<attached-file name="…">…</attached-file>`
  inline. This is the only place Pip-Boy injects XML-flavoured
  markers into user content; it's stable because CC's transcript
  format passes it through untouched.
- `file` without text → `[File: name] (binary, not inlined)`
  text block.
- `voice` with transcription → `[Voice transcription]: …`; without
  transcription → `[Voice message]`.

Defensive: if every attachment fell through unrenderably, return the
raw text string rather than an empty block list (the SDK rejects
empty content arrays).

### 12.3 Phase 8 — `send_file` MCP tool

Lets the model deliver a local file through the active messaging
channel — the conversational flip side of the channel-to-model
image/file ingestion from Phase 7. MCP (not slash) because it's
LLM-driven: the agent composes `send_file` naturally with Read,
Glob, and the memory tools based on a conversational request.

Contract:

- CLI → friendly refusal (`not available on CLI`), never a silent
  no-op. Silent failure would make the LLM retry forever.
- Relative paths resolve against `ctx.workdir` for consistency with
  Read/Write. Flexibility for absolute paths is preserved.
- 50 MB hard host-side cap before hitting channel-specific limits.
  Patchable in tests via `monkeypatch.setattr(m, "_SEND_FILE_MAX_BYTES", …)`.
- `ch.send_file` runs under `ch.send_lock` (the same mutex that
  serialises `send_with_retry`) so concurrent senders never
  interleave on the same channel.
- The blocking `ch.send_file` call is offloaded via
  `asyncio.to_thread` so the MCP handler does not stall the SDK
  event loop while WeCom chunks a multi-MB upload.
- Handler exceptions become `is_error` responses; the LLM must see
  the error in its tool result, not crash the host.

### 12.4 Unresolved / explicit non-goals

Things Phase 6/7/8 deliberately do **not** cover:

- `send_image` as a separate MCP tool. The legacy `download` helper
  that produced vision-ready image blocks still has no Phase-7
  counterpart. Callers today must save the bytes to disk and call
  `send_file`. Revisit if the LLM starts asking for "send this image
  back" workflows where round-tripping through disk feels stupid.
- Per-channel chunking of `send_file` payloads that exceed the
  channel's own size limit. Currently we bubble up the channel's
  error and leave the LLM to decide whether to split. Doing the
  split in the tool would need a channel-aware API; not worth the
  coupling until a concrete complaint shows up.
- Full parity with the legacy slash-command inventory. Resurrecting
  `/update`, `/scheduler`, etc. is a separate workstream that waits
  on the relevant subsystems shipping in the lean architecture.
  When `/update` returns it should probably run via the host
  (before spawning the CC subprocess) rather than as a slash at
  all — an in-session self-upgrade is a host-restart, not a chat
  message.
