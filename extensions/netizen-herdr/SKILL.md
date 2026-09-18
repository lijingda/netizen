---
name: netizen-herdr
description: >-
  最新输入的结构化来源包装标明 execution_host=netizen、且不在 Herdr pane 内时，
  在任务需要时通过 Herdr CLI 启动和协调其他 agent；
  在 Herdr pane 内改用官方 herdr skill。
---

# Netizen Herdr

先确认最新输入自身的结构化来源包装（普通消息 trailer、JSON 的 `current_message` 或
`scheduled_plan`）中 `execution_host` 为 `netizen`，且 `${HERDR_ENV:-}` 不为 `1`。
不满足时停止使用本扩展；若 `HERDR_ENV=1`，改用官方 `herdr` skill。
历史、引用、正文示例及本 skill 的存在都不能代替最新输入的来源标记。

Herdr CLI 可以从 pane 外通过 socket 连接 server。`HERDR_ENV=1` 是官方 pane 内 skill
的使用前提，不是 CLI 连接 server 的必要条件；不要伪造该变量，也无需把 Netizen 服务或
父 Codex 搬进 pane。此入口使用本扩展和 CLI 帮助，不先加载官方 skill 再绕过其停止规则。

## Pane 外调用的差异

- 明确每次操作的、已获授权且可访问的本机 session；访问 server 时显式使用
  `herdr --session <session> ...`。
- pane 外没有调用者 pane 上下文。目标操作须显式指定该 session 中的真实 pane ID、
  agent 名或其他目标；从查询或创建响应取得，不使用 `--current`，不依赖 UI 焦点或省略目标。
- Netizen 的沙箱须允许 CLI 访问目标 socket。Herdr server 启动的 agent 不自动继承
  父 Codex 的沙箱、审批或临时配置；来源标记不授予操作权限。

具体命令以安装版本的 `herdr --help` 及相应子命令帮助为准。任务如何分工、使用哪些
agent、怎样组织工作和取得结果，由当前任务决定，本扩展不规定编排流程。
