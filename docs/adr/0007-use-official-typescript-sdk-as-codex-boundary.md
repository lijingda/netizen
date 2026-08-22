---
status: superseded
superseded_by: 0008
date: 2026-08-01
---

# 使用官方 TypeScript SDK 作为唯一 Codex 后端边界

## 背景

先前实现依次尝试了自建 TypeScript App Server client 和 Python App Server SDK。
两种路线都让 Netizen 承担了过多 Codex 语义：Conversation/Turn 状态、steer、
interrupt、协议事件、per-Conversation `CODEX_HOME`、Workspace 和持久队列。这与
“飞书只是另一个 Codex Channel、后续升级 SDK 即可跟随原生能力”的产品原则冲突。

官方 TypeScript SDK 能启动、继续和恢复本地 Codex Thread，并由 SDK 自己管理
锁定版本的 `codex exec`、JSONL 和 AbortSignal。它当前不提供 active-Turn steer、
list/fork/archive 或审批交互。

## 决定

1. 单个常驻 Node.js 服务拥有一个共享 `Codex` SDK 对象。
2. 生产精确使用 `@openai/codex-sdk@0.146.0` 及其 pinned CLI，不设置
   `codexPathOverride`。
3. Netizen 不启动 App Server、不解析 CLI JSONL、不复制协议模型、不 patch SDK。
4. Feishu Scope 只保存 active Thread Binding；Binding 的 native ID 在第一条真实
   prompt 后 lazy 写入。
5. 所有 native Thread 使用服务身份标准 `~/.codex`，Project 只是共享 cwd。
6. 同一 Binding 只允许一个活动 SDK 调用；忙时新 prompt 明确拒绝，不排队或
   prompt 拼接。不同 Binding 不设固定并发上限。
7. `/stop` 使用公开 AbortSignal；Netizen 不增加产品级 Turn timeout。原生终态事件
   释放执行槽，SDK cleanup 和飞书投递不会继续占用该槽。
8. Channel SQLite 只保存 Scope/Binding/消息幂等，不保存 prompt、response、cwd
   或自建 Turn。
9. 审批映射完成前固定 `approvalPolicy: "never"`；inspect/work 只映射到官方
   read-only/workspace-write sandbox。
10. SDK 未公开能力保持显式 gap。需要 shim 时必须另行讨论并新建 ADR。
11. 具名 Project 只允许位于 `${dataDir}/projects` 的 canonical 子目录；单聊用户
    可以管理自己的 P2P Binding，群聊/话题仍由 Operator 管理。

## 后果

正面：

- Agent 过程直接复用原生 CLI 的配置、认证、Skills、MCP、历史和升级路径。
- 删除约一万行自建 Runtime、队列、状态和测试，Channel 边界显著变薄。
- 多 Thread 并发自然来自多个 SDK-owned CLI process，不需产品级调度器。
- SDK 升级成为依赖 bump + 合约测试，而不是协议追赶工程。

取舍：

- 当前没有 steer；运行中消息只能拒绝。
- AbortSignal 是否清理所有工具孙进程需要 Linux release gate。
- 不再有 durable prompt queue；回执后进程崩溃可能丢失最终回复。
- 旧 per-Conversation Codex state 不自动进入新的标准 home，生产切换默认 fresh
  Binding，历史迁移需单独批准。
- 完整 App UI 能力只能随官方 SDK 公共接口逐步增加。

## 被取代方案

本 ADR 取代 ADR 0001–0006 对当前 Pilot 的架构约束；这些文档保留为历史记录。
Python SDK/App Server 方案对应的旧 ADR 0007 已随实现删除。
