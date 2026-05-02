# Dual-Backend Implementation Progress Report

> **Status**: Phase 2 complete, Phase 3+ pending  
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

## Phase 3+: Pending

See [plan document](../.cursor/plans/dual-backend_evaluation_6fc33440.plan.md) for full breakdown.

| Phase | Summary | Status |
|---|---|---|
| **1** | AgentBackend abstraction + Claude Code backend | **COMPLETE** |
| **2** | Codex SDK integration + event translation + MCP bridge | **COMPLETE** |
| 3 | Host integration — route through `get_backend()` | Pending |
| 4 | Capability gating + hooks/reflect adaptation | Pending |
| 5 | `/plugin` bridge for codex /plugins | Pending |
| 6 | Session management + transcript format | Pending |
| 7 | Test matrix (parametrize across backends) | Pending |

---

## Memos for next phases

### Phase 3 implementation notes

1. **`agent_host.py` wiring** — Currently `AgentHost._query()` directly imports `run_query` from `agent_runner` and `StreamingSession` from `streaming_session`. It needs to switch to `get_backend()` → `backend.run_query()` / `backend.open_streaming_session()`. The backend instance should be resolved once at host init and stored as `self._backend`.

2. **Session pool compatibility** — The existing `SessionPool` manages `StreamingSession` objects. It needs to accept `StreamingSessionProtocol` instead, so `CodexStreamingSession` objects can participate. The pool's sweep logic (idle TTL) works identically since both session types expose `last_used_ns`.

3. **Config.toml auto-setup** — When `settings.backend == "codex_cli"`, the host should call `ensure_codex_config(workdir)` during startup to ensure the MCP bridge is registered.

### Phase 4 implementation notes

1. **Hooks** — `PreCompact` and `Stop` hooks are Claude Code only. When backend doesn't support `PRE_COMPACT_HOOK`, the reflect trigger needs an alternative (turn-count threshold or explicit command).

2. **Reflect** — The L1 reflect pipeline currently parses Claude Code JSONL transcripts. For Codex, a different transcript format parser is needed, or the reflect input changes to use in-memory state instead of file parsing.

### Test patching memo

Tests heavily patch `agent_runner.query` (the `claude_agent_sdk.query` function) via `patch.object(agent_runner, "query", fake)`. This pattern works because:
- `agent_runner.py` imports `query` from `claude_agent_sdk` at module level
- `_run_one_attempt` calls `query(...)` through that module-level binding
- `patch.object` replaces the binding on the module namespace

For the Codex backend, tests patch at `pip_agent.backends.codex_cli.runner.run_query` or mock the `Codex`/`Thread` objects directly. Phase 7 should introduce a `@pytest.fixture` parametrized by backend name.

### `BackendError` hierarchy adoption

The unified error hierarchy (`BackendError` → `StaleSessionError`, `ModelInvalidError`, etc.) is defined and used by the Codex backend natively. Migration path:
1. ~~Phase 2: Codex backend raises the typed errors natively~~ **DONE**
2. Phase 4: `agent_host.py` switches to catching `BackendError` subtypes
3. Phase 7: test assertions updated to match

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
    ├── runner.py         # One-shot run_query
    └── streaming.py      # CodexStreamingSession

tests/
├── test_codex_backend.py         # 8 tests
├── test_codex_event_translator.py # 19 tests
└── test_codex_mcp_bridge.py      # 9 tests
```
