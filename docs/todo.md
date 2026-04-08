# Pip-Boy Agent -- Backlog

## Skills

- [ ] **babysit**: PR 看护 skill -- 循环检查 PR 状态/评论/CI，自动修复直到可合并（需要先有 GitHub API 工具）
- [ ] **create-subagent**: 自定义子代理配置 -- 允许用户定义专用子代理（code-reviewer, debugger 等），各自有独立 system prompt

## Tools

- [ ] **GitHub API 工具**: PR 操作、issue 管理、CI 状态查询（babysit skill 的前置依赖）
- [ ] **grep/ripgrep 工具**: 专用代码搜索工具，比 bash + grep 更结构化

## Agent Team

- [ ] **Non-interactive long-running lead**: 将 Pip 改为非交互式长运行 agent，不再依赖用户 nudge 来推进 teammate 完成后的流程。

## Infrastructure

- [ ] **Persistent memory**: 跨会话记忆存储（项目上下文、用户偏好）
- [ ] **Configurable persona**: 可配置人格/语气
- [ ] **Skill hot-reload**: 运行时检测 skill 文件变更，无需重启
