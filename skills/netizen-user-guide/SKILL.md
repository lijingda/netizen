---
name: netizen-user-guide
description: >-
  Netizen 用户指南。Netizen 是通过官方 Codex SDK 将飞书接入原生 Codex 的 Channel。
  用户询问当前飞书机器人或会话如何使用，或询问 Netizen 的命令、会话、Side、Goal、
  Skills、配置、任务表情、进度卡、状态、引用、图片、本轮文件、与 Codex App/CLI 的差异
  及常见限制时使用；
  即使用户未明确说“Netizen”，只要问题指向当前飞书交互或这些 Channel 能力也应触发。
  仅用于使用咨询；普通编码任务以及 Netizen 工程实现、架构、部署、调试或代码修改不应触发。
---

# Netizen 用户指南

Netizen 是通过官方 Codex SDK 将飞书接入原生 Codex 的 Channel，不是 Codex fork 或
另一个 Agent Runtime。本 Skill 被调用时，默认用户正通过 Netizen 的飞书 Channel 与
原生 Codex 交互；用户未明确说出“Netizen”时，不要因此改按 Codex App/CLI 场景回答。
若用户明确指定其他宿主，则按其指定场景解释或比较。

使用本 Skill 解答 Netizen 的用户使用问题，不把它当作工程实现文档。

## 回答方式

1. 回答前阅读 [用户手册](references/user-guide.md) 中与问题相关的章节；用户要求入门介绍、完整能力说明或横向比较时阅读全文。
2. 先直接回答用户的问题，再给出必要的命令和注意事项。不要无关地倾倒整份命令清单。
3. 准确区分飞书 Channel control、原生 Codex 能力和 Codex App/CLI 宿主命令；不要臆造可用命令，也不要把命令文本说成已经执行。
4. 手册用于解释稳定语义。命令是否在当前实例开放、当前会话状态和动态 Model/Effort/Speed 选项，以运行时 `/help`、`/status`、卡片或明确错误为准。
5. 若运行时结果与手册冲突，优先相信运行时证据，并说明可能存在版本差异；不要猜测隐藏状态。
6. 使用用户的语言，优先采用场景化、易懂的表达。涉及 `/stop`、`/release`、并发 Project、Side 过期、原生历史删除或本轮文件时必须保留关键风险：materialized `/delete` 会永久删除原生 Thread、spawned descendants、Codex App/CLI 历史与 Binding；`/release` 只取消当前连接订阅，不删除历史，也不证明 writer 已立即释放；本轮文件按需发送当前内容，不是 Turn 完成时快照，也不会扫描未进入 native Turn diff/items 的输出。
7. 用户询问执行反馈时，明确 Task Reaction 与 Progress Card 是普通会话的两个独立选项，
   可在 `/new` 或 idle 时的 `/config` 开启，默认都关闭。Progress Card 只逐步展示状态和
   checklist，计划步骤经过常见敏感模式过滤，不显示耗时、百分比、ETA、reasoning 或 raw
   tool/command output；关闭时仍是无文件富文本/静态文本、有文件现有完成卡。展示失败
   不会改变 Codex Turn。

## 边界

- `/help` 是当前命令清单，本 Skill 是自然语言说明和场景咨询的补充。
- 本 Skill 不修改 Netizen、会话、Project、Codex 配置或任何文件。普通使用者需要操作时，
  说明应在飞书中发送的命令或打开的卡片；实例管理员询问跨 Scope 管理时，可说明 Admin
  Web 的入口和边界，但不能读取、展示或代填 credential。
- 工程架构、部署、SDK Gap Adapter、代码调试或贡献流程应查看 Netizen 仓库文档和源码，而不是以本用户手册代替。
