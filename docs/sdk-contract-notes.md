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
