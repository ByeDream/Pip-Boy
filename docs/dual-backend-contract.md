# Pip-Boy 双后端架构契约文档

> **状态**: Phase 0 验证完成（SDK 路线），冻结为执行基准  
> **分支**: `feat/dual-backend`  
> **版本**: v0.3.0  
> **最后更新**: 2026-05-03

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
| **`codex-python` SDK（主要路线）** | `pip install codex-python`（v1.122.0），包含 bundled codex.exe（197MB）。`Codex()` → `start_thread()` → `thread.run()` 流式事件 |
| SDK 事件协议 | JSON-RPC 通知：`item/started`、`item/completed`、`item/agentMessage/delta`、`turn/completed` 等 |
| `codex exec --json`（备用路线） | JSONL 流式事件，one-shot 模式 |
| `~/.codex/config.toml` | Codex 全局配置，SDK 会读取（model_provider、MCP server 注册） |
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

#### Codex SDK 事件 → 五事件映射表（主要路线）

SDK 使用 JSON-RPC 通知协议，事件类型比 `codex exec --json` 更丰富。

| SDK 事件类型 | Pip-Boy StreamEvent | 映射说明 |
|---|---|---|
| `ItemAgentMessageDeltaNotification` | **`text_delta`** | **真正的增量推送**，每个 delta 是一小段文本片段，直接用于 TUI 打字机效果 |
| `ItemStartedNotification` + CommandExecution | `tool_use` | 命令开始执行 |
| `ItemCommandExecutionOutputDeltaNotification` | （附属于 tool_use） | 命令输出的增量流，可用于实时显示 |
| `ItemStartedNotification` + FileChange | `tool_use` | 文件操作开始 |
| `ItemStartedNotification` + McpToolCall | `tool_use` | MCP 工具调用开始，含 server + tool + arguments |
| `ItemCompletedNotification` + CommandExecution | `tool_result` | 含 aggregated_output + exit_code |
| `ItemCompletedNotification` + FileChange | `tool_result` | 含 changes 列表（path + kind，无 diff） |
| `ItemCompletedNotification` + McpToolCall | `tool_result` | 含 result 内容 |
| `TurnPlanUpdatedNotification` | `tool_use` + `tool_result` | 映射为 TodoWrite 等价事件（含 plan steps） |
| `TurnCompletedNotification` | `finalize` | 附带 turn 状态和 usage 信息 |
| `ThreadTokenUsageUpdatedNotification` | （内部状态） | 实时 token 使用统计 |

> **[SDK 已验证]** SDK 提供**真正的 `agentMessage/delta` 增量推送**，
> 不同于 `codex exec --json` 的一次性全文。TUI 打字机效果**无需模拟**。
>
> **[SDK 已验证]** SDK 还提供 `CommandExecutionOutputDelta` 命令输出增量流，
> 可实现实时命令输出显示（Claude Code 也有类似的 partial messages）。
>
> **[已验证]** `thinking_delta` 在 Codex backend 下不可用。gpt-5.5 产生 reasoning tokens
> 但不输出 reasoning item/delta。TUI thinking 面板在 Codex backend 下为空。

#### codex exec --json 映射表（备用路线）

如需回退到 `codex exec` 模式（如 SDK 不稳定时），事件映射如下：

| Codex JSONL 事件 | Pip-Boy StreamEvent | 注意 |
|---|---|---|
| `item.completed` + `agent_message` | `text_delta` | **无增量**，需模拟 delta 切分 |
| `item.started` + `command_execution` | `tool_use` | |
| `item.completed` + `command_execution` | `tool_result` | |
| `item.started/completed` + `file_change` | `tool_use` / `tool_result` | 仅 path + kind |
| `item.started/completed` + `mcp_tool_call` | `tool_use` / `tool_result` | |
| `item.started/updated/completed` + `todo_list` | TodoWrite 映射 | |
| `turn.completed` / `turn.failed` | `finalize` | |

### 3.3 MCP 工具桥接契约

Pip-Boy 的 11 个自有 MCP 工具 **schema 不变**，仅宿主（server 启动方式）因 backend 而异：

| Backend | MCP Server 模式 | 说明 |
|---|---|---|
| Claude Code | in-process MCP（`create_sdk_mcp_server`） | SDK 在同一进程内注册，零延迟 |
| Codex SDK | STDIO MCP server（独立子进程） | 通过 `~/.codex/config.toml` `[mcp_servers.pip]` 注册 |

**不变量**:
- 11 个工具的 name、description、input schema、output schema 完全相同
- 工具实现代码（`mcp_tools.py` 中的 handler 函数）完全复用，不因 backend 分叉

> **[SDK 已验证]** STDIO MCP server 通过 `config.toml` 注册**在 SDK 模式下完全可行**。
> SDK 的 app-server 进程读取 `~/.codex/config.toml` 的 `[mcp_servers]` 配置，
> 正常初始化 MCP server、工具发现、参数传递和结果返回。
> **关键**：MCP 调用需设置 `approval_policy=AskForApproval(root="never")`，否则会被拒绝。
> SDK 事件流中 `McpToolCallThreadItem` 包含 `server`、`tool`、`arguments`、`result` 完整字段。
>
> **[SDK 已验证]** `dynamic_tools`（Python 可调用函数直接注册为工具）需要 `experimentalApi`
> capability，当前不可用。Pip-Boy 11 个工具继续走 STDIO MCP server 路线。

### 3.4 Session 管理契约

| 维度 | Claude Code | Codex CLI | 契约 |
|---|---|---|---|
| Session 标识 | `session_id` (string) | `thread_id` (string) | Host 统一用 `session_id` 字段，backend 内部做映射 |
| Session Resume | `options.resume=session_id` | `client.resume_thread(thread_id)` | 通过 `AgentBackend.run_query(session_id=...)` 统一入口 |
| Session 持久化路径 | `~/.claude/projects/...` | `~/.codex/sessions/...` | Backend 各自管理，Host 不直接读写 |
| Transcript 格式 | Claude JSONL | Codex transcript | L1 reflect 输入解析需按 backend 适配 |
| Multi-turn 复用 | `ClaudeSDKClient.connect()` | **`Codex()` → `start_thread()` → `thread.run()`** | SDK 常驻 app-server 进程，同一 thread 多轮复用，后续 turn ~3s（与 CC 对等） |

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

| Claude Code 错误形态 | Codex SDK 错误形态 | 统一错误类型 |
|---|---|---|
| SDK 抛出异常 + 字符串匹配 | `ThreadRunError`（含 turn status） | 见上表 |
| `stale_session` 字符串 | `ThreadRunError` + turn status 检查 | `StaleSessionError` |
| `is_model_invalid_error` 匹配 | `codex._exceptions.BadRequestError` | `ModelInvalidError` |
| SDK auth 异常 | `codex._exceptions.AuthenticationError` (401) | `AuthenticationError` |
| — | `codex._exceptions.RateLimitError` (429) | `BackendTimeoutError`（可重试） |
| — | `codex.errors.CodexExecError`（binary 启动失败） | `BackendUnavailableError` |

### 3.6 TUI 渲染契约

- TUI 层（`tui/app.py`、`tui/tool_format.py`、`tui/modals.py`）**只消费 5 个 StreamEvent**，
  不直接感知 backend 类型。
- 工具名映射：Codex 的工具名如与 Claude Code 不同（如 Codex 的 `command_execution` vs
  Claude Code 的 `Bash`），在 SDK event translator 层完成映射，TUI 层看到的始终是
  Pip-Boy 标准工具名。
- 交互模态（TodoWrite / AskUserQuestion / ExitPlanMode）：
  - Claude Code backend：现有 TUI 模态正常工作
  - Codex CLI backend：**[SPIKE 已验证]** `codex exec` 非交互模式下**不阻塞**，
    `--sandbox` 控制权限自动审批。Codex 有原生 `todo_list` 事件（started/updated/completed），
    可映射为 TodoWrite。AskUserQuestion/ExitPlanMode 在 Codex backend 下
    通过 `backend.supports(Capability.INTERACTIVE_MODALS)` gate 为 False，降级处理。

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

Codex CLI 与 Claude Code 的内置工具名不同。SDK event translator 必须将 Codex 工具名
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

> **[SPIKE 已验证]** Codex 的 `file_change` 事件**仅包含路径和 kind**（add/update/delete），
> **不包含 diff 内容**。`file_change` 有 `item.started`（in_progress）和 `item.completed` 两个阶段。
> TUI 的文件变更卡片需要适配为仅显示路径和操作类型，不展示 diff。

---

## 5. Phase 0 Spike 验证结果（已完成）

> Phase 0 于 2026-05-02 完成。所有 7 项验证均有明确结论。

### 5.1 Multi-turn 复用 — **通过（SDK 路线）**

| 维度 | 结果 |
|---|---|
| **结论** | `codex exec` 是 one-shot，但 **`codex-python` SDK 支持持久连接多轮复用** |
| **SDK 方案** | `Codex()` 启动常驻 app-server → `start_thread()` → 同一 thread 上多次 `run()` / `run_text()` |
| **性能实测** | 首次连接 ~11s；后续 turn **~3s**（与 Claude Code ~2-5s 对等） |
| **上下文保持** | 同一 thread 自动保持上下文（实测 Turn 3 能正确引用 Turn 1 内容）|
| **cross-session resume** | `client.close()` 后新建 `Codex()` → `resume_thread(thread_id)` 成功，上下文保持，~5.2s |
| **对 Phase 3 的影响** | `supports(PERSISTENT_STREAMING)` = **True**，架构与 Claude Code 的 `StreamingSession` 对称 |

### 5.2 事件 schema 完备性 — **通过（SDK 路线 5/5 可靠映射）**

| 维度 | 结果 |
|---|---|
| **SDK 事件类型** | `TurnStarted`, `TurnCompleted`, `ItemStarted`, `ItemCompleted`, `ItemAgentMessageDelta`, `ItemCommandExecutionOutputDelta`, `TurnPlanUpdated`, `ThreadTokenUsageUpdated` |
| **text_delta** | **`ItemAgentMessageDeltaNotification` 提供真正增量推送**，每个 delta 是一小段文本。TUI 打字机效果无需模拟 |
| **thinking_delta** | 不可用。gpt-5.5 产生 reasoning tokens 但不输出 reasoning delta |
| **tool_use** | `ItemStartedNotification` 触发，含 CommandExecution/FileChange/McpToolCall 全类型 |
| **tool_result** | `ItemCompletedNotification` 触发，CommandExecution 含 aggregated_output + exit_code |
| **finalize** | `TurnCompletedNotification` 含 turn 状态 |
| **额外能力** | `ItemCommandExecutionOutputDelta` 提供命令输出实时流；`ThreadTokenUsageUpdated` 提供实时 token 统计 |
| **file_change** | 仅包含 path + kind（add/update/delete），无 diff 内容 |
| **对比 codex exec** | SDK 事件远比 `codex exec --json` 丰富：有真增量、有命令输出流、有实时 usage |

### 5.3 MCP Server 注册与调用 — **通过（SDK + config.toml）**

| 维度 | 结果 |
|---|---|
| **结论** | SDK 的 app-server 读取 `config.toml` 的 `[mcp_servers]`，MCP 工具正常可用 |
| **关键条件** | 必须设置 `approval_policy=AskForApproval(root="never")`，否则 MCP 调用会被 "user rejected" |
| **SDK 事件** | `McpToolCallThreadItem` 含 `server`, `tool`, `arguments`, `result`, `error` 完整字段 |
| **dynamic_tools** | SDK 的 `DynamicToolSpec` 注册需要 `experimentalApi` capability，当前不可用。继续走 config.toml STDIO MCP 路线 |
| **对 mcp_bridge.py 的影响** | Pip-Boy 11 个工具注册方式确定：config.toml `[mcp_servers.pip]` 指向 Pip-Boy 的 STDIO MCP server 进程 |

### 5.4 codex exec 交互行为 — **通过**

| 维度 | 结果 |
|---|---|
| **结论** | `codex exec` 非交互模式下**不阻塞**，`--sandbox` 控制权限自动审批 |
| **sandbox 模式** | `read-only`（默认）/ `workspace-write` / `danger-full-access` 三级 |
| **TodoWrite** | Codex 有原生 `todo_list` 事件，可映射为 TodoWrite |
| **AskUserQuestion / ExitPlanMode** | Codex exec 模式下不存在交互 prompt，`INTERACTIVE_MODALS` = False |

### 5.5 Hooks — **SDK 模式下不触发，但 SDK 事件流可替代**

| 维度 | 结果 |
|---|---|
| **codex exec 模式** | hooks 稳定触发（PreToolUse），payload 含 session_id, turn_id, transcript_path |
| **SDK 模式** | **hooks 不触发**。SDK 的 app-server 模式不执行 config.toml 中的 hooks |
| **替代方案** | SDK 事件流比 hooks **更丰富**：`ItemStarted/Completed` 覆盖 PreToolUse 的工具拦截功能；`ThreadTokenUsageUpdated` 提供实时 token 统计，可替代 transcript 大小检测 |
| **L1 reflect 触发** | turn-count 阈值 + `ThreadTokenUsageUpdated` 监测 token 累积量。无需 hooks |
| **transcript_path** | SDK 模式下不直接提供 transcript_path，但 thread_id 可推导 session 目录位置 |

### 5.6 Session Resume — **通过**

| 维度 | 结果 |
|---|---|
| **结论** | `codex exec resume <thread_id>` **完全可用**，上下文保持正确 |
| **验证** | 第一个 session 创建文件并运行 → resume 后能正确引用文件名和输出内容 |
| **thread_id 一致性** | resume 后 `thread.started` 的 `thread_id` 与原始 session **一致** |

### 5.7 Codex Python SDK 评估 — **采用为主要路线**

| 维度 | 结果 |
|---|---|
| **结论** | `codex-python` v1.122.0 可通过 `pip install codex-python` 安装，**包含 bundled binary**（197MB），无需 local checkout |
| **持久连接** | `Codex()` → `start_thread()` → `thread.run()` 模式与 `ClaudeSDKClient.connect()` **架构对称** |
| **性能** | 首连 ~11s；后续 turn **~3s**（vs codex exec 每 turn ~7-12s） |
| **事件流** | 比 `codex exec --json` 更丰富：真增量 text delta、命令输出流、实时 token usage |
| **决策** | **SDK 为主要路线**；`codex exec` 作为 fallback（SDK 不稳定时） |
| **已知限制** | hooks 不触发；dynamic_tools 需 experimentalApi；thinking_delta 不可用 |

---

## 6. 能力矩阵（双后端对比）

| 能力 | Claude Code | Codex CLI | 差异处理 |
|---|---|---|---|
| 文件编辑 / Shell / Glob / Grep | 内置 | 内置 | 工具名映射（§4） |
| MCP 工具（11 个） | in-process MCP | STDIO MCP server | 启动方式不同，schema 不变 |
| Plugin marketplace | `claude plugin` | `codex /plugins` | 分别包装，不互通 |
| `/T` slash 透传 | CC slash 体系 | Codex slash 体系 | 保留，转发到对应子进程 |
| Session resume | `options.resume` | `SDK resume_thread(id)` | 统一入口，~5.2s |
| 持久 streaming | `ClaudeSDKClient.connect()` | **`Codex()` SDK 常驻进程** | 架构对称，后续 turn ~3s |
| PreCompact hook | `HookMatcher(PreCompact)` | **SDK 模式下 hooks 不触发** | L1 reflect 走 turn-count 阈值 + SDK 事件流 token usage 监测 |
| 三层 settings walk-up | `setting_sources` | 无等价物 | `supports()` gate，Codex 下不可用 |
| TodoWrite 等交互模态 | CC 内置工具 | Codex 有原生 `todo_list` 事件 | 可映射；AskUser/ExitPlan 降级 |
| Web search | Pip 自有 MCP / CC 内置 | Codex 内置 | 已解耦 |
| Image generation | — | Codex 内置 `$imagegen` | Codex-only 新能力，可选支持 |

---

## 7. 变更日志

| 日期 | 版本 | 变更内容 |
|---|---|---|
| 2026-05-02 | v0.1.0 | 初始草案：核心原则、接口契约、Spike 验证项 |
| 2026-05-02 | v0.2.0 | Phase 0 初版（codex exec 路线）：7 项验证写入 §5 |
| 2026-05-03 | v0.3.0 | **架构路线变更**：从 codex exec 子进程切换到 `codex-python` SDK 持久连接路线。重测所有 spike：multi-turn 通过（SDK ~3s/turn）、事件 schema 5/5 映射（真增量 delta）、MCP 通过（需 approval_policy=never）、hooks 不触发（SDK 事件流替代）。更新 §3.2/§3.3/§3.4/§5/§6 全部相关章节 |

---

## 附录 A: Codex SDK 事件类型速查（主要路线，实测样本）

> 来源: `codex-python` v1.122.0 SDK 流式事件（JSON-RPC 通知）
> 以下类型来自 Phase 0 SDK 路线重测实际采集（2026-05-03）

### SDK 事件类型完整列表（实测可见）

```
TurnStartedNotificationModel          # Turn 开始
TurnCompletedNotificationModel        # Turn 完成
TurnPlanUpdatedNotificationModel      # Turn 计划更新（TodoWrite 映射）
ItemStartedNotificationModel          # Item 开始（各种类型）
ItemCompletedNotificationModel        # Item 完成（各种类型）
ItemAgentMessageDeltaNotification     # ★ 文本增量推送（真 delta）
ItemCommandExecutionOutputDeltaNotification  # 命令输出增量流
ThreadTokenUsageUpdatedNotification   # 实时 token 使用统计
```

### SDK ItemCompleted.item 子类型

```
AgentMessageThreadItem       → text, phase (commentary/final_answer)
CommandExecutionThreadItem   → command, exit_code, aggregated_output, status
FileChangeThreadItem         → changes[] (path, kind), status
McpToolCallThreadItem        → server, tool, arguments, result, error, status
UserMessageThreadItem        → content[]
```

### 关键 Python API 模式

```python
from codex import Codex, CodexOptions, ThreadStartOptions
from codex.protocol import types as proto

client = Codex(CodexOptions(api_key="...", base_url="..."))
thread = client.start_thread(ThreadStartOptions(
    sandbox=proto.SandboxMode(root="danger-full-access"),
    approval_policy=proto.AskForApproval(root="never"),  # MCP 需要！
    cwd="...",
))

# 流式消费
stream = thread.run("prompt")
for event in stream:
    match type(event).__name__:
        case "ItemAgentMessageDeltaNotification":
            # event.params.delta → 增量文本片段
            pass
        case "ItemStartedNotificationModel":
            # event.params.item.root → 具体 item 子类型
            pass
        case "ItemCompletedNotificationModel":
            # event.params.item.root → 含结果的 item
            pass
        case "TurnCompletedNotificationModel":
            # turn 结束
            pass

# 跨会话恢复
thread2 = client.resume_thread(thread.id)
```

---

## 附录 B: Codex exec --json 事件 schema 速查（备用路线，实测样本）

> 来源: https://developers.openai.com/codex/noninteractive  
> 以下样本来自 Phase 0 初版 spike 实际采集（2026-05-02）

### 生命周期事件

```jsonl
{"type":"thread.started","thread_id":"019de95b-3c85-71f3-875e-55247b02b007"}
{"type":"turn.started"}
{"type":"turn.completed","usage":{"input_tokens":25918,"cached_input_tokens":15104,"output_tokens":151,"reasoning_output_tokens":0}}
```

### Item 事件

```jsonl
// agent_message — 仅 item.completed，无增量推送（SDK 路线有真增量）
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"..."}}

// command_execution
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"...","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution","aggregated_output":"...","exit_code":0,"status":"completed"}}

// file_change — 仅 path + kind，无 diff
{"type":"item.started","item":{"id":"item_4","type":"file_change","changes":[{"path":"...","kind":"add"}],"status":"in_progress"}}

// mcp_tool_call
{"type":"item.started","item":{"id":"item_1","type":"mcp_tool_call","server":"pip_spike","tool":"spike_echo","arguments":{"message":"..."}}}
{"type":"item.completed","item":{"id":"item_1","type":"mcp_tool_call","result":{"content":[{"type":"text","text":"ECHO: ..."}]},"status":"completed"}}
```
