# Dual-Backend Implementation Progress Report

> **Status**: Phase 1 complete, Phase 2+ pending  
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

### What is NOT done (deferred to later phases)

- `hooks.py` and `plugins.py` are not moved/wrapped — they are purely CC-specific and will be backend-switched in Phase 4 (hooks/reflect) and Phase 5 (plugins) respectively.
- `mcp_tools.py` stays untouched — shared tool schemas don't change; the server *startup mode* (in-process vs STDIO) changes in Phase 2.
- `agent_host.py` does not yet route through `AgentBackend` — it still directly imports `run_query` and `StreamingSession`. Wiring the backend factory into the host dispatch is Phase 6 (capability gating).
- No configuration change — `settings.backend` field not yet added to `config.py`.

---

## Phase 2-7: Pending

See [plan document](../.cursor/plans/dual-backend_evaluation_6fc33440.plan.md) for full breakdown.

| Phase | Summary | Status |
|---|---|---|
| **1** | AgentBackend abstraction + Claude Code backend | **COMPLETE** |
| 2 | Codex SDK integration + event translation | Pending |
| 3 | Codex persistent streaming session | Pending |
| 4 | SDK resume + reflect trigger adaptation | Pending |
| 5 | `/plugin` bridge for codex /plugins | Pending |
| 6 | Capability gating + slash/TUI conditionals | Pending |
| 7 | Test matrix (parametrize across backends) | Pending |

---

## Memos for next phases

### Phase 2 implementation notes

1. **Event translator is the core** — `codex_cli/event_translator.py` maps SDK JSON-RPC notifications to the 5 `StreamEvent` types. All spike data is in contract §3.2 and Appendix A.

2. **MCP bridge** — Pip-Boy's 11 tools need a STDIO MCP server process for Codex. The tool schemas in `mcp_tools.py` are reusable; only the server startup changes. `config.toml` registration with `approval_policy=never` is mandatory.

3. **`codex-python` SDK pattern** — `Codex()` → `start_thread(ThreadStartOptions(...))` → `thread.run("prompt")` → iterate events. First connection ~11s, subsequent turns ~3s.

4. **Import structure** — `codex-python` v1.122.0 exposes: `from codex import Codex, CodexOptions, ThreadStartOptions` and `from codex.protocol import types as proto`.

### Test patching memo

Tests heavily patch `agent_runner.query` (the `claude_agent_sdk.query` function) via `patch.object(agent_runner, "query", fake)`. This pattern works because:
- `agent_runner.py` imports `query` from `claude_agent_sdk` at module level
- `_run_one_attempt` calls `query(...)` through that module-level binding
- `patch.object` replaces the binding on the module namespace

For the Codex backend, tests will need to patch the Codex SDK client differently. Phase 7 should introduce a `@pytest.fixture` parametrized by backend name.

### `BackendError` hierarchy adoption

The unified error hierarchy (`BackendError` → `StaleSessionError`, `ModelInvalidError`, etc.) is defined but not yet fully adopted. Current code still catches `RuntimeError` and does string-matching. Migration path:
1. Phase 2: Codex backend raises the typed errors natively
2. Phase 6: `agent_host.py` switches to catching `BackendError` subtypes
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
    └── __init__.py       # CodexBackend (stub)
```
