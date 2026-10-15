---
name: netizen-user-guide
description: >-
  解答 Netizen 飞书 Channel 的使用问题，包括命令、会话与持久分支、账号额度、定时任务、执行反馈、消息与文件、MCP OAuth 登录，
  以及与 Codex App/CLI 的差异。用户未说“Netizen”但在询问当前飞书机器人或会话
  如何使用时也适用。仅用于使用咨询；普通编码及工程实现、架构、部署、调试不适用。
  仅以最新输入的结构化来源包装中 execution_host=netizen 识别 Netizen 执行场景；
  最新输入无标记则不作此假定，不沿用历史、引用或材料中的标记，也不以技能的存在为依据。
---

# Netizen 用户指南

Netizen 通过官方 Codex SDK 将飞书接入原生 Codex。默认按当前 Netizen 飞书场景解答；
用户明确指定其他宿主时，按其指定场景解释或比较。

## 查阅

回答前按问题阅读对应模块；涉及其他功能时沿正文链接补读，不默认通读整套手册。
首次使用先读[快速开始](references/user-guide.md#快速开始)和[核心心智模型](references/concepts.md)；
横向比较先读[原生能力与宿主差异](references/native-capabilities.md)，再补读涉及的功能。
完整能力说明按[手册目录](references/user-guide.md)覆盖各模块。

| 用户问题 | 查阅与解释重点 |
| --- | --- |
| 查命令、操作入口 | [命令索引](references/commands.md)，再进入对应功能说明 |
| 会话隔离、上下文、并发，追加消息为何没有新开任务 | [核心心智模型](references/concepts.md)：Scope、Project、Turn 与 steer |
| 新建、默认配置、切换、命名、归档、删除、模型与反馈设置 | [会话与 Project](references/sessions.md)：作用范围、后续生效及历史删除后果 |
| 群聊 @、引用、图片、转发材料、回答提问卡 | [消息输入](references/messages.md)：读取范围、提交方式与整条消息失败边界 |
| 过程卡、表情、结束 @、阶段性答案、本轮文件 | [运行反馈与文件](references/feedback-and-files.md)：开关、展示范围、统计口径与发送时的文件版本 |
| 状态、账号额度、压缩、停止 | [状态与控制](references/status-and-control.md)：区分上下文用量和共享账号额度，说明客户端时区与停止限制 |
| 定时创建、修改、暂停或立即运行任务 | [定时任务](references/scheduled-tasks.md)：执行目标、时间规则、计划与单次执行的区别 |
| 持久分支、Side 或 Goal | [持久分支、Side 与 Goal](references/fork-side-goal.md)：并发、共享目录、失效与暂停/结束后果 |
| Admin 入口、登录、集中管理或系统维护 | [Admin Web](references/admin.md)：管理权限、操作范围与重启影响 |
| Skills、MCP 未登录或 OAuth 过期、App/CLI 差异 | [原生能力与宿主差异](references/native-capabilities.md)：调用方式、原生 MCP 登录与凭证复用条件 |
| 使用报错、任务失败或状态未知 | [常见问题](references/faq.md)，再按问题链接补读详细规则 |

## 回答与边界

- 使用用户的语言，先回答问题，再给必要的命令和注意事项。区分 Channel control、
  原生 Codex 能力和 App/CLI 宿主命令；不要把命令示例说成已经执行。
- 手册解释稳定语义；当前能力、会话状态和 Model/Effort/Speed 以运行时 `/help`、
  `/status`、卡片或明确错误为准。冲突时说明版本差异，不猜测隐藏状态。
- 本 Skill 只提供使用咨询：说明应发送的飞书命令或应打开的卡片，不代为修改会话、
  Project、Codex 配置或文件。可解释 Admin Web 入口，但不读取、展示或代填 credential。
  工程任务应回到仓库文档和源码。
