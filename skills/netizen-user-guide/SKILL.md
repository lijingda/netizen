---
name: netizen-user-guide
description: >-
  解答 Netizen 飞书 Channel 的使用问题，包括命令、会话、执行反馈、消息与文件，
  以及与 Codex App/CLI 的差异。用户未说“Netizen”但在询问当前飞书机器人或会话
  如何使用时也适用。仅用于使用咨询；普通编码及工程实现、架构、部署、调试不适用。
---

# Netizen 用户指南

Netizen 通过官方 Codex SDK 将飞书接入原生 Codex。默认按当前 Netizen 飞书场景解答；
用户明确指定其他宿主时，按其指定场景解释或比较。

## 查阅

回答前阅读 [用户手册](references/user-guide.md) 的相关章节；入门、完整能力说明或横向
比较需阅读全文。常用入口：

- [会话与 Project](references/user-guide.md#会话与-project-管理)、
  [停止](references/user-guide.md#stop)、[Side](references/user-guide.md#side-临时话题)、
  [Goal](references/user-guide.md#goal)：说明对应的并发、失效或历史删除后果。
- [运行反馈](references/user-guide.md#飞书中的运行反馈)、
  [本轮文件](references/user-guide.md#查看和发送本轮文件)：按手册解释开关、展示范围和统计口径。

## 回答与边界

- 使用用户的语言，先回答问题，再给必要的命令和注意事项。区分 Channel control、
  原生 Codex 能力和 App/CLI 宿主命令；不要把命令示例说成已经执行。
- 手册解释稳定语义；当前能力、会话状态和 Model/Effort/Speed 以运行时 `/help`、
  `/status`、卡片或明确错误为准。冲突时说明版本差异，不猜测隐藏状态。
- 本 Skill 只提供使用咨询：说明应发送的飞书命令或应打开的卡片，不代为修改会话、
  Project、Codex 配置或文件。可解释 Admin Web 入口，但不读取、展示或代填 credential。
  工程任务应回到仓库文档和源码。
