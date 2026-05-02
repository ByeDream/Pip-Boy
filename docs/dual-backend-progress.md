# Dual-Backend Implementation Progress Report

> **Status**: Phase 1-4 complete, Phase 5-6 in progress, Phase 7 pending  
> **Branch**: `feat/dual-backend`  
> **Contract**: [`docs/dual-backend-contract.md`](./dual-backend-contract.md) v0.3.0 (frozen)  
> **Last updated**: 2026-05-03

---

## Phase 1: AgentBackend Abstraction — COMPLETE

### What was done

Introduced the `AgentBackend` protocol and `backends/` package hierarchy
without breaking any existing functionality.

#### New files created

| File | Purpose |
|---|---|
| `src/pip_agent/backends/__init__.py` | Package root; exports `AgentBackend`, `Capability`, error hierarchy, `get_backend()` factory |
| `src/pip_agent/backends/base.py` | Shared types: `QueryResult`, `StreamEventCallback`, `StreamingSessionProtocol`, `Capability` enum, `BackendError` hierarchy |
| `src/pip_agent/backends/claude_code/__init__.py` | `ClaudeCodeBackend` — wraps existing `agent_runner` / `streaming_session` behind the `AgentBackend` protocol |
| `src/pip_agent/backends/codex_cli/__init__.py` | `CodexBackend` — stub, raises `NotImplementedError` for all operations |

#### Files modified

| File | Change |
|---|---|
| `agent_runner.py` | `QueryResult` and `StreamEventCallback` now imported from `backends.base` (canonical source). All other code unchanged. Re-exports both symbols so downstream imports are unaffected. |
| `streaming_session.py` | `StaleSessionError` now imported from `backends.base`. `QueryResult` likewise. All other code unchanged. |

#### Key design decisions

1. **Keep implementation in place, abstract over it** — `agent_runner.py` and `streaming_session.py` retain their full implementations. The `backends/claude_code/` package delegates to them rather than owning a copy. This preserves all existing `mock.patch` targets (e.g. `patch.object(agent_runner, "query", ...)`) and avoids the risk of behavioral drift between an "original" and a "moved" copy.

2. **Shared types live in `backends/base.py`** — `QueryResult`, `StreamEventCallback`, and the error hierarchy are backend-agnostic. Moving them to `backends/base.py` makes the dependency direction clean: both `agent_runner` (Claude Code specific) and future `codex_cli` modules import from the same place.

3. **`StaleSessionError` now extends `BackendError(RuntimeError)`** — preserves existing `except RuntimeError` catches while enabling future `except BackendError` patterns. Full error hierarchy defined per contract §3.5.

4. **`get_backend()` factory** — reads `settings.backend` (default `"claude_code"`) and returns the appropriate `AgentBackend` instance. Ready for host-layer routing in later phases.

5. **`Capability` enum** — all 7 capabilities from contract §3.1 defined. `ClaudeCodeBackend.supports()` returns `True` for all 7; `CodexBackend.supports()` returns `True` for 4 (no `PRE_COMPACT_HOOK`, `SETTING_SOURCES_THREE_TIER`, `INTERACTIVE_MODALS`).

### Test results

- **1085 tests passed**, 0 failed, 0 skipped
- No test files modified
- Lint clean (`ruff check` passes on all new and modified files)

---

## Phase 2: Codex SDK Integration — COMPLETE

### What was done

Fully implemented the Codex backend with SDK runner, persistent streaming,
event translation, MCP bridge, and config management.

#### New files created

| File | Purpose |
|---|---|
| `src/pip_agent/backends/codex_cli/event_translator.py` | Maps SDK JSON-RPC notifications into 5 Pip-Boy semantic events (`text_delta`, `thinking_delta`, `tool_use`, `tool_result`, `finalize`) |
| `src/pip_agent/backends/codex_cli/runner.py` | One-shot `run_query()` via `Codex()` → `start_thread()` → `thread.run()` → stream → close lifecycle |
| `src/pip_agent/backends/codex_cli/streaming.py` | `CodexStreamingSession` implementing `StreamingSessionProtocol` for persistent multi-turn connections |
| `src/pip_agent/backends/codex_cli/mcp_bridge.py` | STDIO MCP server exposing Pip-Boy's 11 tools via standard MCP JSON-RPC over stdin/stdout |
| `src/pip_agent/backends/codex_cli/config_gen.py` | Generates `~/.codex/config.toml` with `[mcp_servers.pip]` STDIO entry |
| `tests/test_codex_event_translator.py` | 19 tests for event translation (all 5 event types + edge cases) |
| `tests/test_codex_backend.py` | 8 tests for backend factory, capabilities, delegation, config |
| `tests/test_codex_mcp_bridge.py` | 9 tests for config_gen and tool collection |

#### Files modified

| File | Change |
|---|---|
| `src/pip_agent/backends/codex_cli/__init__.py` | Upgraded from stub to full `CodexBackend` delegating to runner/streaming |
| `src/pip_agent/config.py` | Added `backend: str = Field(default="claude_code")` |
| `pyproject.toml` | Added `[project.optional-dependencies] codex = ["codex-python>=1.122"]` |

### Event translator mapping (contract §3.2)

| SDK Event | Pip-Boy Event | Details |
|---|---|---|
| `ItemAgentMessageDeltaNotification` | `text_delta` | Incremental text streaming |
| `ItemReasoningTextDeltaNotification` | `thinking_delta` | Model reasoning (if available) |
| `ItemReasoningSummaryTextDeltaNotification` | `thinking_delta` | Reasoning summary |
| `ItemStartedNotification` + CommandExecution | `tool_use` | name="Bash", input={command} |
| `ItemStartedNotification` + FileChange | `tool_use` | name=Write/Edit per kind |
| `ItemStartedNotification` + McpToolCall | `tool_use` | name=tool, input=arguments |
| `ItemStartedNotification` + WebSearch | `tool_use` | name="WebSearch" |
| `ItemCompletedNotification` + cmd/file/mcp | `tool_result` | is_error from exitCode/status/error |
| `TurnPlanUpdatedNotification` | `tool_use` + `tool_result` | name="TodoWrite" |
| `ThreadTokenUsageUpdatedNotification` | (internal state) | Tracked in turn state dict |
| `TurnCompletedNotification` | `finalize` | Final text + usage + elapsed |

### MCP bridge architecture

```
Codex app-server process
    ↓ STDIO (stdin/stdout JSON-RPC)
pip_agent.backends.codex_cli.mcp_bridge
    ↓ direct Python calls
pip_agent.mcp_tools._memory_tools/._cron_tools/etc.
    ↓
MemoryStore / HostScheduler / Channel (via McpContext)
```

Registration: `~/.codex/config.toml` gets `[mcp_servers.pip]` via `config_gen.ensure_codex_config()`.

### Test results

- **1121 tests passed** (1085 existing + 36 new), 0 failed
- No existing test files modified
- Lint clean on all new and modified files

### Key implementation notes

1. **Codex `run_query` is async but the SDK iteration is sync** — `thread.run()` returns a sync iterable (`CodexTurnStream`). The `for event in stream` loop runs synchronously within the async function. This is fine for the current architecture where each query runs in its own coroutine.

2. **`_blocks_to_text` flattener** — Claude Code uses `list[dict]` content blocks (Anthropic format); Codex accepts plain strings. The flattener extracts text parts and joins with newlines.

3. **Error mapping** — `StaleSessionError` is raised when known stale-session marker strings appear in exceptions. Other Codex errors propagate as generic `BackendError` subclasses.

4. **MCP bridge runs standalone** — `python -m pip_agent.backends.codex_cli.mcp_bridge` starts the STDIO server. It builds its own `McpContext` with a fresh `MemoryStore` from `PIP_WORKDIR/.pip/`. This means tool handlers in bridge mode have access to memory but NOT to the host's live scheduler or channel state.

5. **`approval_policy=never`** — both runner and streaming session set `AskForApproval(root="never")` per contract §3.3, otherwise MCP calls get "user rejected".

---

## Phase 3 prep: Plugin adapter + error detection + integration tests — COMPLETE

### What was done

Infrastructure for the remaining phases.

#### New files

| File | Purpose |
|---|---|
| `src/pip_agent/backends/codex_cli/plugins.py` | Plugin CLI adapter: marketplace add/remove/upgrade via bundled `codex.exe` |
| `tests/test_backend_integration.py` | 39 integration tests: factory, capabilities, errors, protocol conformance (parametrized across both backends), settings |
| `tests/test_codex_plugins.py` | 5 tests for CLI resolution and error types |

#### Files modified

| File | Change |
|---|---|
| `src/pip_agent/models.py` | Extended `_SDK_NEVER_MODEL` (+ `codexautherror`) and `_SDK_DEFINITELY_MODEL` (+ `modelinvaliderror`) for Codex error detection |

### Test results

- **1165 tests passed** (1085 existing + 80 new), 0 failed
- No existing test files modified

---

## Phase 4: Host Integration — COMPLETE

### What was done

Wired `get_backend()` into `AgentHost` so the dual-backend dispatch is
actually switchable via `settings.backend`. The Claude Code path is
**exactly unchanged** — only `if/else` branches were added alongside.

#### Files modified

| File | Change |
|---|---|
| `src/pip_agent/agent_host.py` | Added `self._backend = get_backend()` in `__init__`; backend-aware `_get_or_create_streaming_session`, one-shot `run_query`, and streaming fallback paths; widened `_streaming_sessions` type for protocol compatibility; `getattr`-safe `_turn_lock` checks in idle sweep |
| `src/pip_agent/host_commands.py` | `/status` shows active backend; `/help` shows backend info section |

#### Key design decisions

1. **Backend resolved once at host init** — `self._backend = get_backend()` is called once in `AgentHost.__init__`. All dispatch paths read `self._backend.name` for routing. No per-turn resolution overhead.

2. **Three dispatch points modified**:
   - `_get_or_create_streaming_session`: Codex path uses `backend.open_streaming_session()`, bypasses JSONL resume logic (Codex handles resume natively)
   - One-shot `run_query` (line ~2135): Codex path uses `backend.run_query()`
   - Streaming fallback (line ~1149): Codex path uses `backend.run_query()` when session creation fails

3. **Session pool compatibility** — `_streaming_sessions` type widened to `dict[str, StreamingSession | Any]`. Idle sweep uses `getattr(sess, "_turn_lock", None)` to safely handle both `StreamingSession` (has `_turn_lock`) and `CodexStreamingSession` (no `_turn_lock`).

4. **Claude Code path completely undisturbed** — every change is additive. The `else` branch is always the original code, character-for-character.

### Test results

- **1165 tests passed**, 0 failed — zero breakage
- Default `settings.backend = "claude_code"` means all existing tests exercise the unchanged Claude Code path

---

## Phase 5-6: Capability Gating + UI — IN PROGRESS

### What's done so far

- `/status` now displays `Backend: claude_code` (or `codex_cli`)
- `/help` now has a `## Backend` section showing the active backend
- `Capability` enum defines 7 flags; both backends declare their support sets
- `supports()` is callable from any code path that needs to gate behavior

### Remaining items

1. `/plugin` routing through active backend's plugin adapter
2. TUI thinking panel hide/show based on `supports(PRE_COMPACT_HOOK)`
3. Reflect trigger adaptation for Codex (turn-count threshold, no PreCompact)

---

## Phase 7: Test Matrix — PENDING

See [plan document](../.cursor/plans/dual-backend_evaluation_6fc33440.plan.md).

---

## File inventory (new in this branch)

```
src/pip_agent/backends/
├── __init__.py           # get_backend(), re-exports
├── base.py               # QueryResult, StreamEventCallback, Capability,
│                         # AgentBackend Protocol, StreamingSessionProtocol,
│                         # BackendError hierarchy
├── claude_code/
│   └── __init__.py       # ClaudeCodeBackend
└── codex_cli/
    ├── __init__.py       # CodexBackend (full implementation)
    ├── config_gen.py     # ~/.codex/config.toml management
    ├── event_translator.py  # SDK events → 5 Pip-Boy events
    ├── mcp_bridge.py     # STDIO MCP server (python -m ...)
    ├── plugins.py        # Codex plugin CLI adapter
    ├── runner.py         # One-shot run_query
    └── streaming.py      # CodexStreamingSession

tests/
├── test_backend_integration.py    # 39 tests (protocol conformance, factory, capabilities)
├── test_codex_backend.py          # 8 tests
├── test_codex_event_translator.py # 19 tests
├── test_codex_mcp_bridge.py       # 9 tests
└── test_codex_plugins.py          # 5 tests
```

## Modified files

| File | Phases | Change summary |
|---|---|---|
| `agent_runner.py` | 1 | Imports from `backends.base` |
| `streaming_session.py` | 1 | Imports from `backends.base` |
| `config.py` | 2 | `backend` field added |
| `pyproject.toml` | 2 | `codex-python` optional dep |
| `models.py` | 3 | Codex error detection |
| `agent_host.py` | 4 | Backend dispatch routing |
| `host_commands.py` | 5 | Backend info in /status, /help |

## Commit log (feat/dual-backend)

| Commit | Summary |
|---|---|
| `7b3299c` | Phase 1: AgentBackend protocol + backends/ package hierarchy |
| `a13b512` | Phase 2: Codex runner + streaming + event translator |
| `9e0602a` | Phase 2: STDIO MCP bridge + config.toml generator |
| `e3ba2fd` | docs: Phase 2 progress report |
| `c9af76d` | Phase 3 prep: plugin adapter + error detection + integration tests |
| `f7ee3c1` | docs: Phase 3 prep progress report |
| `2d3aa5d` | Phase 4: host integration — backend dispatch in agent_host.py |
