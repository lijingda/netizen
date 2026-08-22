---
status: superseded
superseded_by: 0007
---

# 使用 Codex App Server 支持原生活动 Turn steering

当前 Pilot 使用锁定版本 `@openai/codex` 内置的 Codex App Server，通过 stdio JSONL 完成 `thread/start|resume`、`turn/start`、`turn/steer` 和 `turn/interrupt`。选择 App Server 的直接原因是同一 Conversation 忙时必须把新输入原生追加到精确的活动 Turn；TypeScript SDK 的 `runStreamed()` 不提供这个双向控制面。业务层继续只依赖 `CodexGateway`。

单实例 Pilot 为每个活动外层 Turn 启动一个 App Server 进程，并在内存中保存 `conversationId -> nativeThreadId/nativeTurnId/client`。Steer 必须同时携带 `threadId + expectedTurnId + clientUserMessageId`；`/stop` 使用同一原生标识发送 interrupt。服务重启时活动 Turn 明确中断，不尝试恢复旧 stdio 连接或重放输入。

当前不提供飞书审批 UI：命令/文件请求一律 decline，权限请求只返回空的 Turn scope，用户输入/MCP elicitation/dynamic tool 和未知客户端方法全部 fail closed。固定 permission profile、禁网、禁 apps/plugins/MCP 与最小环境保持不变；以后若增加审批卡片，再单独扩展正向 decision allowlist。
