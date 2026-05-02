# Dual-Backend Implementation Progress Report

> **Status**: Phase 1-2 complete + Phase 3 prep, Phase 4+ pending  
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

Infrastructure for the remaining phases, without touching `agent_host.py`'s
live dispatch path (high-risk change deferred to developer review).

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

## Phase 4+: Pending

See [plan document](../.cursor/plans/dual-backend_evaluation_6fc33440.plan.md) for full breakdown.

| Phase | Summary | Status |
|---|---|---|
| **1** | AgentBackend abstraction + Claude Code backend | **COMPLETE** |
| **2** | Codex SDK integration + event translation + MCP bridge | **COMPLETE** |
| **3 prep** | Plugin adapter + error detection + integration tests | **COMPLETE** |
| 4 | Host integration — route through `get_backend()` in `agent_host.py` | Pending |
| 5 | Capability gating + hooks/reflect adaptation | Pending |
| 6 | `/plugin` bridge wiring into host_commands | Pending |
| 7 | Test matrix (parametrize across backends) | Pending |

---

## Memos for next phases

### Phase 4: Host integration (HIGH PRIORITY, NEEDS REVIEW)

The core wiring that makes the dual-backend actually switchable. This is the
highest-risk change because it modifies the live `agent_host.py` dispatch path.

#### Change plan

1. **Store backend at host init**: Add `self._backend = get_backend()` in `AgentHost.__init__`

2. **Two-path dispatch in `_run_turn_streaming`** (line ~1106):
   ```python
   if self._backend.name == "codex_cli":
       session = await self._backend.open_streaming_session(...)
       result = await session.run_turn(prompt, ...)
   else:
       # existing Claude Code path unchanged
       session = self._get_or_create_streaming_session(...)
       result = await session.run_turn(prompt, ...)
   ```

3. **Two-path dispatch in one-shot `run_query`** (line ~2109):
   ```python
   if self._backend.name == "codex_cli":
       result = await self._backend.run_query(prompt, ...)
   else:
       result = await run_query(prompt, ...)  # existing path
   ```

4. **Session pool type-widening**: Change `_streaming_sessions: dict[str, StreamingSession]` to `dict[str, StreamingSessionProtocol]`. The sweep/eviction logic uses `session.last_used_ns` and `session.close()` — both are on `StreamingSessionProtocol`, so no further changes needed.

5. **Config.toml auto-setup**: When `settings.backend == "codex_cli"`, call `ensure_codex_config(workdir)` during host startup.

6. **Import dependencies** to add:
   ```python
   from pip_agent.backends import get_backend
   from pip_agent.backends.base import StreamingSessionProtocol
   ```

#### Risk mitigation
- Keep the Claude Code path EXACTLY as-is (no refactoring)
- Only add `if self._backend.name == "codex_cli":` branches
- Run full 1165 test suite after each change

### Phase 5: Capability gating

1. **`host_commands.py`**: Before executing backend-specific slash commands, check `backend.supports(cap)`. Display clear message when feature unavailable.

2. **TUI thinking panel**: When `!backend.supports(Capability.PRE_COMPACT_HOOK)`, hide the thinking panel (Codex doesn't emit thinking_delta).

3. **Plugin commands**: Route `/plugin` operations through the active backend's plugin adapter.

4. **`/T` slash passthrough**: Pass through to the active backend's CLI (claude or codex).

### Phase 6: Reflect trigger adaptation

1. **Turn-count threshold**: When `!backend.supports(PRE_COMPACT_HOOK)`, trigger reflect based on turn count (e.g., every 10 turns) + `ThreadTokenUsageUpdated` event monitoring.

2. **Transcript format**: Codex transcript parsing for L1 reflect input (different from Claude JSONL).

### Phase 7: Test matrix

1. Add `@pytest.fixture(params=["claude_code", "codex_cli"])` fixture
2. Parametrize relevant tests across both backends
3. Add Codex-specific edge case tests (stale thread, SDK errors)

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

## Commit log (feat/dual-backend)

| Commit | Summary |
|---|---|
| `7b3299c` | Phase 1: AgentBackend protocol + backends/ package hierarchy |
| `a13b512` | Phase 2: Codex runner + streaming + event translator |
| `9e0602a` | Phase 2: STDIO MCP bridge + config.toml generator |
| `e3ba2fd` | docs: Phase 2 progress report |
| `c9af76d` | Phase 3 prep: plugin adapter + error detection + integration tests |
