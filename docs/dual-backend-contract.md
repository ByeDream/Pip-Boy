# Pip-Boy 双后端架构契约文档

> **状态**: DRAFT — Phase 0 Spike 验证期间持续迭代  
> **分支**: `feat/dual-backend`  
> **版本**: v0.1.0  
> **最后更新**: 2026-05-02

---

## 0. 本文档定位

本契约文档是 Pip-Boy 双后端（Claude Code + Codex CLI）架构改造的**最高优先级约束**。
所有实现（包括计划文档中的各 Phase）**必须**先遵循本契约；如有冲突，以本契约为准。

Phase 0 Spike 验证的结果将**持续反馈**到本文档，补充或修正各项条款，直到验证阶段结束后，
本文档成为正式实施的"一次性瀑布式执行"基准。

**关联文档**:
- 计划文档: [`.cursor/plans/dual-backend_evaluation_6fc33440.plan.md`](../.cursor/plans/dual-backend_evaluation_6fc33440.plan.md)

---

## 1. 核心原则（不可违反）

### 1.1 向后兼容：不破坏 Claude Code 现有功能

- **禁止删减**任何 Claude Code 后端已有的功能、命令、MCP 工具、TUI 交互。
- **允许新增**功能（如 Codex-only 能力）和**扩展**现有功能（如增加 backend 感知逻辑）。
- 衡量标准：改造完成后，`backend=claude_code` 配置下的所有现有测试（当前 946 个）**全绿**，
  用户体验与改造前**完全一致**。
- 任何 refactor（如将模块搬到 `backends/claude_code/` 下）必须是**纯搬迁**，
  行为不变，不借机"顺手改进"引入不可控变更。

### 1.2 一致体验：相同功能使用相同入口

- Claude Code 和 Codex CLI **语义等价的功能**在 Pip-Boy Host 侧必须使用**相同的入口点**：
  - 相同的 slash 命令（如 `/plugin`、`/compact`、`/T`）
  - 相同的 MCP 工具 schema（11 个 Pip-Boy 自有工具的输入/输出不因 backend 而异）
  - 相同的 TUI 渲染逻辑（文本流、工具卡片、进度指示）
  - 相同的 channel adapter 接口（WeChat/WeCom/TUI 不感知 backend 差异）
- 差异点通过 `backend.supports(capability)` **显式探测**，不做 silent 降级。
  用户在使用不支持的功能时应得到**清晰的提示**，而非静默失败。

### 1.3 自定义实现：脱离 Claude Code 的等价物

- 以下强依赖 Claude Code 的功能，**如有必要**，可做脱离 Claude Code 的自定义实现，
  以满足双后端使用：
  - **多对话 session 的观察提取**（L1 reflect 依赖 PreCompact hook + transcript 解析）
  - **System prompt 注入**（Claude Code 的 `preset:"claude_code"` + append 模式）
  - **Session 持久化与 transcript 格式**（Claude Code JSONL vs Codex session 格式）
  - **上下文压缩触发**（PreCompact hook vs turn-count 阈值）
- 自定义实现必须**语义等价**：最终效果（如 reflect 产出、memory 更新、session 恢复）
  与 Claude Code 版本一致，允许实现路径不同。

---

## 2. 官方参考文档

### 2.1 Codex CLI 官方文档

| 文档 | URL | 用途 |
|---|---|---|
| CLI 主页 | https://developers.openai.com/codex/cli | 概览、安装、基本用法 |
| CLI 命令参考 | https://developers.openai.com/codex/cli/reference | 所有命令行选项和子命令 |
| 功能特性 | https://developers.openai.com/codex/cli/features | 交互模式、session resume、MCP、slash 命令等 |
| 非交互模式 | https://developers.openai.com/codex/noninteractive | `codex exec` 详解、JSONL 输出、CI 集成 |
| MCP 集成 | https://developers.openai.com/codex/mcp | MCP server 配置（STDIO/HTTP）、config.toml 格式 |
| SDK（TS + Python） | https://developers.openai.com/codex/sdk | 编程控制 Codex agent（TypeScript/Python） |
| GitHub 仓库 | https://github.com/openai/codex | 源码、issue、release |

### 2.2 关键技术参考

| 参考 | 说明 |
|---|---|
| `codex exec --json` 事件 schema | JSONL 流式事件，详见 §3.2 |
| Codex Python SDK (experimental) | `codex_app_server.Codex` / `AsyncCodex`，JSON-RPC 控制 app-server |
| Codex TypeScript SDK | `@openai/codex-sdk`，`startThread()` / `run()` / `resumeThread()` |
| `~/.codex/config.toml` | Codex 全局配置，包括 MCP server 注册 |
| `.codex/config.toml` | 项目级 Codex 配置（需 trusted project） |

---

## 3. 接口契约

### 3.1 AgentBackend Protocol

所有 backend 实现必须遵循此 Protocol。这是 Host 层与 Backend 层的唯一交互面。

```python
from typing import Protocol
from enum import Enum, auto
from pathlib import Path

class Capability(Enum):
    """Backend 能力枚举，用于显式探测"""
    PRE_COMPACT_HOOK = auto()           # 支持上下文压缩前 hook
    SETTING_SOURCES_THREE_TIER = auto() # 支持 user/project/local 三层配置
    PERSISTENT_STREAMING = auto()       # 支持持久 streaming session（多 turn 复用同一进程）
    PLUGIN_MARKETPLACE = auto()         # 支持 plugin marketplace
    SLASH_PASSTHROUGH = auto()          # 支持 /T slash 命令透传
    INTERACTIVE_MODALS = auto()         # 支持 TodoWrite/AskUserQuestion/ExitPlanMode 等交互模态
    SESSION_RESUME = auto()             # 支持 session resume

class AgentBackend(Protocol):
    """Agent 后端接口"""

    @property
    def name(self) -> str:
        """后端标识符: 'claude_code' | 'codex_cli'"""
        ...

    async def run_query(
        self,
        prompt: str | list[dict],
        *,
        mcp_ctx: "McpContext",
        model_chain: list[str],
        session_id: str | None,
        system_prompt_append: str,
        cwd: str | Path,
        on_stream_event: "StreamEventCallback | None",
    ) -> "QueryResult":
        """执行单次 query，返回结果"""
        ...

    async def open_streaming_session(
        self,
        *,
        session_key: str,
        model_chain: list[str],
        system_prompt_append: str,
        cwd: str | Path,
        mcp_ctx: "McpContext",
    ) -> "StreamingSessionProtocol":
        """打开持久 streaming session（如 backend 不支持，抛 NotImplementedError）"""
        ...

    def supports(self, capability: Capability) -> bool:
        """查询 backend 是否支持指定能力"""
        ...

    async def health_check(self) -> tuple[bool, str]:
        """检查 backend 可用性，返回 (ok, message)"""
        ...
```

### 3.2 流式事件契约（StreamEventCallback）

这是 Pip-Boy 架构中**最关键的抽象**。TUI / WeChat / WeCom 所有 renderer 只消费这 5 个语义事件。
**两个 backend 必须翻译成完全相同的 5 个事件**，renderer 层零改动。

```python
StreamEvent = Literal[
    "text_delta",       # 模型文本输出增量
    "thinking_delta",   # 模型思考过程增量（reasoning）
    "tool_use",         # 工具调用开始（包含 tool name + input）
    "tool_result",      # 工具调用结果（包含 output）
    "finalize",         # turn 完成
]
```

#### Codex JSONL → 五事件映射表

| Codex JSONL 事件 | Pip-Boy StreamEvent | 映射说明 |
|---|---|---|
| `item.completed` + `type=agent_message` | `text_delta` | `item.text` 作为最终文本（注意：Codex 不做增量推送，需在 translator 中模拟 delta 或一次性发送） |
| `item.completed` + `type=reasoning` | `thinking_delta` | `item.text` 作为思考内容（需启用 `model_reasoning_summary=detailed`） |
| `item.started` + `type=command_execution` | `tool_use` | `item.command` 映射为工具名 `shell`/`bash`，command 内容作为 input |
| `item.started` + `type=file_change` | `tool_use` | 映射为 `write`/`edit` 工具 |
| `item.started` + `type=mcp_tool_call` | `tool_use` | `item.server` + `item.tool` 作为工具标识 |
| `item.completed` + `type=command_execution` | `tool_result` | `item.aggregated_output` + `item.exit_code` |
| `item.completed` + `type=file_change` | `tool_result` | `item.changes` 列表 |
| `item.completed` + `type=mcp_tool_call` | `tool_result` | `item.result` 内容 |
| `item.completed` + `type=web_search` | `tool_use` + `tool_result` | 映射为 web_search 工具使用 + 结果 |
| `item.started/updated/completed` + `type=todo_list` | `tool_use` + `tool_result` | 映射为 TodoWrite 等价事件 |
| `turn.completed` | `finalize` | 附带 `usage` 信息 |
| `turn.failed` | `finalize` (with error) | `error.message` 传递给 Host |
| `thread.started` | （内部状态） | 记录 `thread_id` 供 session resume 使用 |

> **[SPIKE 待验证]** Codex `agent_message` 是否在 `item.started` 时有增量流推送，
> 还是仅在 `item.completed` 时一次性返回全文。如果是后者，TUI 的打字机效果需要
> translator 做模拟增量切分。

### 3.3 MCP 工具桥接契约

Pip-Boy 的 11 个自有 MCP 工具 **schema 不变**，仅宿主（server 启动方式）因 backend 而异：

| Backend | MCP Server 模式 | 说明 |
|---|---|---|
| Claude Code | in-process MCP（`create_sdk_mcp_server`） | SDK 在同一进程内注册，零延迟 |
| Codex CLI | STDIO MCP server（独立子进程） | Pip-Boy 启动一个 STDIO MCP server 进程，通过 `~/.codex/config.toml` 或 `codex mcp add` 注册给 Codex |

**不变量**:
- 11 个工具的 name、description、input schema、output schema 完全相同
- 工具实现代码（`mcp_tools.py` 中的 handler 函数）完全复用，不因 backend 分叉

> **[SPIKE 待验证]** Pip-Boy 作为 STDIO MCP server 注册给 Codex 子进程的可行性。
> 需验证 `codex exec --json` 模式下 MCP server 是否正常初始化和调用。
> 参考: https://developers.openai.com/codex/mcp

### 3.4 Session 管理契约

| 维度 | Claude Code | Codex CLI | 契约 |
|---|---|---|---|
| Session 标识 | `session_id` (string) | `thread_id` (string) | Host 统一用 `session_id` 字段，backend 内部做映射 |
| Session Resume | `options.resume=session_id` | `codex exec resume <thread_id>` | 通过 `AgentBackend.run_query(session_id=...)` 统一入口 |
| Session 持久化路径 | `~/.claude/projects/...` | `~/.codex/sessions/...` | Backend 各自管理，Host 不直接读写 |
| Transcript 格式 | Claude JSONL | Codex transcript | L1 reflect 输入解析需按 backend 适配 |
| Multi-turn 复用 | `ClaudeSDKClient.connect()` | 待验证 | 见 §5 Spike 验证项 |

### 3.5 错误处理契约

所有 backend 的错误必须翻译成统一的 Host 层错误类型：

```python
class BackendError(Exception):
    """Backend 统一错误基类"""
    pass

class StaleSessionError(BackendError):
    """Session 已过期或不可恢复"""
    pass

class ModelInvalidError(BackendError):
    """请求的模型不可用"""
    pass

class AuthenticationError(BackendError):
    """认证失败（API key 无效、过期等）"""
    pass

class BackendUnavailableError(BackendError):
    """Backend 二进制不存在或无法启动"""
    pass

class BackendTimeoutError(BackendError):
    """Backend 响应超时"""
    pass
```

| Claude Code 错误形态 | Codex CLI 错误形态 | 统一错误类型 |
|---|---|---|
| SDK 抛出异常 + 字符串匹配 | exit code + stderr 文本 | 见上表 |
| `stale_session` 字符串 | `turn.failed` + 特定 error message | `StaleSessionError` |
| `is_model_invalid_error` 匹配 | exit code + model error stderr | `ModelInvalidError` |
| SDK auth 异常 | `codex login` 状态 / CODEX_API_KEY 缺失 | `AuthenticationError` |

### 3.6 TUI 渲染契约

- TUI 层（`tui/app.py`、`tui/tool_format.py`、`tui/modals.py`）**只消费 5 个 StreamEvent**，
  不直接感知 backend 类型。
- 工具名映射：Codex 的工具名如与 Claude Code 不同（如 Codex 的 `command_execution` vs
  Claude Code 的 `Bash`），在 JSONL translator 层完成映射，TUI 层看到的始终是
  Pip-Boy 标准工具名。
- 交互模态（TodoWrite / AskUserQuestion / ExitPlanMode）：
  - Claude Code backend：现有 TUI 模态正常工作
  - Codex CLI backend：**[SPIKE 待验证]** 是否能接管交互。如不能，
    通过 `backend.supports(Capability.INTERACTIVE_MODALS)` gate，
    TUI 在 Codex backend 下降级显示纯文本

### 3.7 Plugin 系统契约

- `/plugin` slash 命令在两个 backend 下**都保留**，但底层分别走
  `claude.exe plugin` 和 `codex /plugins`
- 两边 plugin **不互通**（Claude 装的 plugin 在 Codex backend 下不可见，反之亦然）
- `/plugin list` 和 `/help` 必须**标注当前 backend**，避免用户困惑
- `pip-boy doctor` 必须能检测并提示 backend 特定的 plugin 状态

### 3.8 配置契约

```toml
# Pip-Boy settings.toml 新增字段
[agent]
backend = "claude_code"  # "claude_code" | "codex_cli"
# 默认值 "claude_code"，确保向后兼容

[agent.codex_cli]
# Codex CLI 特定配置
binary_path = ""  # 空字符串表示从 PATH 查找
model = "gpt-5.5"
sandbox = "workspace-write"  # read-only | workspace-write | danger-full-access
api_key_env = "CODEX_API_KEY"  # 或 "OPENAI_API_KEY"
```

- 新增 `backend` 配置项，默认 `"claude_code"`，**确保未配置时行为与改造前完全一致**
- Codex 特定配置放在 `[agent.codex_cli]` 节下
- `pip-boy doctor` 扩展：检测 `codex` 是否在 PATH 上，检测 API key 是否配置

---

## 4. 工具名映射表

Codex CLI 与 Claude Code 的内置工具名不同。JSONL translator 必须将 Codex 工具名
映射为 Pip-Boy TUI 能识别的标准名，确保 `tool_format.py` 的渲染逻辑不用改动。

| Codex item.type | Codex 细节 | Pip-Boy 标准工具名 | 备注 |
|---|---|---|---|
| `command_execution` | `item.command` | `Bash` / `shell` | 映射为 Claude Code 的 Bash 工具格式 |
| `file_change` + kind=`add` | `item.changes[].path` | `Write` | 新建文件 |
| `file_change` + kind=`update` | `item.changes[].path` | `Edit` | 编辑文件 |
| `file_change` + kind=`delete` | `item.changes[].path` | `Write` (delete) | 删除文件 |
| `mcp_tool_call` | `item.server` + `item.tool` | 保持原名 | MCP 工具名本身已标准化 |
| `web_search` | `item.query` | `WebSearch` | 映射为 Pip-Boy 自有 web 工具 |
| `todo_list` | `item.items` | `TodoWrite` | 映射为 TodoWrite 工具 |
| `agent_message` | `item.text` | — | 不是工具，映射为 `text_delta` 事件 |
| `reasoning` | `item.text` | — | 不是工具，映射为 `thinking_delta` 事件 |

> **[SPIKE 待验证]** Codex 的 file_change 事件是否包含 diff 内容，
> 还是仅有路径和 kind。如果没有 diff，TUI 的文件变更卡片需要适配。

---

## 5. Phase 0 Spike 验证项（验收标准）

Phase 0 的目标是**消除所有关键不确定性**，使后续 Phase 可以瀑布式执行。
每项验证必须有**明确的通过/失败标准**和**失败时的 fallback 方案**。

### 5.1 codex exec multi-turn 复用

| 维度 | 内容 |
|---|---|
| **验证方法** | 启动 `codex exec --json` 子进程，不退出，连续写多个 prompt 到 stdin，观察是否同一 session 持续响应 |
| **通过标准** | 单个 `codex exec` 进程能处理 ≥3 个连续 prompt，每次返回 `turn.completed`，`thread_id` 保持不变 |
| **失败 fallback** | 退化为 one-shot exec：每 turn 启动新的 `codex exec` 进程 + `codex exec resume <thread_id>` 恢复上下文。性能略降（付进程启动税），功能完整 |
| **影响范围** | Phase 3（StreamingSession 实现形态）、§3.4 Session 管理契约 |

### 5.2 JSONL 事件 schema 完备性

| 维度 | 内容 |
|---|---|
| **验证方法** | 跑一段含 file edit / shell command / MCP tool call / web search 的综合 prompt，捕获完整 JSONL 流，逐事件对照 §3.2 映射表 |
| **通过标准** | 五个 StreamEvent（text_delta / thinking_delta / tool_use / tool_result / finalize）均能从 JSONL 事件中可靠映射 |
| **失败 fallback** | 缺失的事件类型在 translator 中**合成**（如用 `item.started` 推导 `tool_use`） |
| **特别关注** | (a) `agent_message` 是否有增量推送 (b) `reasoning` 是否默认启用 (c) `file_change` 是否包含 diff 内容 |
| **影响范围** | Phase 2（JSONL translator 实现）、§3.2 事件映射表、§4 工具名映射表 |

### 5.3 MCP Server 注册与调用

| 维度 | 内容 |
|---|---|
| **验证方法** | 起一个 minimal STDIO MCP server（暴露 1-2 个工具如 `memory_search`），通过 `~/.codex/config.toml` 注册，在 `codex exec --json` 中触发 MCP 工具调用 |
| **通过标准** | (a) Codex 成功初始化 MCP server (b) JSONL 流中出现 `mcp_tool_call` 事件 (c) 工具调用参数和结果正确传递 |
| **失败 fallback** | 方案 A: 走 `codex mcp add` 命令动态注册；方案 B: 11 个工具改成 Codex plugin 形式 |
| **影响范围** | §3.3 MCP 桥接契约、Phase 2（mcp_bridge.py） |

### 5.4 Codex TUI 交互接管

| 维度 | 内容 |
|---|---|
| **验证方法** | 跑触发 plan review / file approval 的 prompt，观察 `codex exec --json` 模式下交互是否吐到 JSONL（而非强制走 Codex 自家 TUI） |
| **通过标准** | `codex exec` 非交互模式下，所有审批和交互通过 `--sandbox` 配置自动处理，不会阻塞等待用户输入 |
| **失败 fallback** | Codex backend 下 Pip-Boy TUI 不展示交互模态，审批走 `--sandbox workspace-write` 自动批准 |
| **影响范围** | §3.6 TUI 渲染契约、Capability.INTERACTIVE_MODALS |

### 5.5 Hooks 稳定性（session_stop 等）

| 维度 | 内容 |
|---|---|
| **验证方法** | 跑两次 session，观察 Codex hooks（如有）是否可靠触发，能否拿到 transcript 路径 |
| **通过标准** | hooks 在 ≥90% 的 session 中稳定触发 |
| **失败 fallback** | L1 reflect 触发器 fallback 到 turn-count 阈值（每 N turn 跑一次） |
| **影响范围** | Phase 4（reflect 触发器）、§3.4 Session 管理契约 |

### 5.6 Session Resume 行为

| 维度 | 内容 |
|---|---|
| **验证方法** | `codex exec "第一条指令"` → 记录 thread_id → `codex exec resume <thread_id> "第二条指令"` → 验证上下文保持 |
| **通过标准** | resume 后的 Codex 能引用第一条指令中的上下文，thread_id 保持一致 |
| **失败 fallback** | 如 resume 不可靠，Pip-Boy 自行维护上下文摘要，每次作为 system prompt 注入 |
| **影响范围** | §3.4 Session 管理契约、Phase 4 |

### 5.7 Codex Python SDK 可用性评估

| 维度 | 内容 |
|---|---|
| **验证方法** | 安装 Codex Python SDK（experimental），测试 `AsyncCodex` 的 `thread_start()` / `run()` 是否稳定工作 |
| **通过标准** | SDK 能稳定启动、执行 query、resume thread，且不需要 local checkout of codex repo |
| **失败结论** | 继续走 `codex exec` 子进程路线（当前计划的路线 b），放弃 SDK 路线 |
| **影响范围** | 整体架构路线选择（子进程 vs SDK） |

---

## 6. 能力矩阵（双后端对比）

| 能力 | Claude Code | Codex CLI | 差异处理 |
|---|---|---|---|
| 文件编辑 / Shell / Glob / Grep | 内置 | 内置 | 工具名映射（§4） |
| MCP 工具（11 个） | in-process MCP | STDIO MCP server | 启动方式不同，schema 不变 |
| Plugin marketplace | `claude plugin` | `codex /plugins` | 分别包装，不互通 |
| `/T` slash 透传 | CC slash 体系 | Codex slash 体系 | 保留，转发到对应子进程 |
| Session resume | `options.resume` | `codex exec resume` | 统一入口 |
| 持久 streaming | `ClaudeSDKClient.connect()` | [SPIKE 待验证] | 视验证结果 |
| PreCompact hook | `HookMatcher(PreCompact)` | [SPIKE 待验证] | 有则用，无则 turn-count fallback |
| 三层 settings walk-up | `setting_sources` | 无等价物 | `supports()` gate，Codex 下不可用 |
| TodoWrite 等交互模态 | CC 内置工具 | [SPIKE 待验证] | 视验证结果 |
| Web search | Pip 自有 MCP / CC 内置 | Codex 内置 | 已解耦 |
| Image generation | — | Codex 内置 `$imagegen` | Codex-only 新能力，可选支持 |

---

## 7. 变更日志

| 日期 | 版本 | 变更内容 |
|---|---|---|
| 2026-05-02 | v0.1.0 | 初始草案：核心原则、接口契约、Spike 验证项 |

---

## 附录 A: Codex exec --json 事件 schema 速查

> 来源: https://developers.openai.com/codex/noninteractive  
> 详细速查: 见 Phase 0 spike 实际采集的 JSONL 样本

### 生命周期事件

```jsonl
{"type":"thread.started","thread_id":"<uuid>"}
{"type":"turn.started"}
{"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":N,"output_tokens":N}}
{"type":"turn.failed","error":{"message":"..."}}
{"type":"error","message":"..."}
```

### Item 事件

```jsonl
// agent_message (仅 item.completed)
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"..."}}

// reasoning (仅 item.completed, 需启用)
{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"..."}}

// command_execution (item.started + item.completed)
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","aggregated_output":"...","exit_code":0,"status":"completed"}}

// file_change (仅 item.completed)
{"type":"item.completed","item":{"id":"item_4","type":"file_change","changes":[{"path":"...","kind":"add|update|delete"}],"status":"completed"}}

// mcp_tool_call (item.started + item.completed)
{"type":"item.started","item":{"id":"item_5","type":"mcp_tool_call","server":"pip","tool":"memory_search","arguments":{...},"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_5","type":"mcp_tool_call","server":"pip","tool":"memory_search","result":{...},"status":"completed"}}

// web_search (仅 item.completed)
{"type":"item.completed","item":{"id":"item_7","type":"web_search","query":"..."}}

// todo_list (item.started / item.updated / item.completed)
{"type":"item.started","item":{"id":"item_8","type":"todo_list","items":[{"text":"...","completed":false}]}}
```
