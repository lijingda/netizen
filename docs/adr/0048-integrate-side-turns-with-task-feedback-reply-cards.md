---
status: accepted
date: 2026-08-28
amends: 0021, 0046, 0047
related: 0020, 0024, 0027
---

# 让 Side Turn 复用普通 Turn 的任务反馈与回复卡

Side Topic 已有独立根卡、两小时 idle expiry、显式 close 和永久路由墓碑；这些是临时
Thread 容器的生命周期。容器内的每轮执行却仍使用早期专用投递：无条件表情、终态文本，
也不提供进度卡或结构化文件。这使同一个 native Turn 在 Side 中呈现为另一套产品，并让
后续 Reply Card 模块继续复制分支。

## 决定

### Side 是容器，Side Turn 是普通任务

Side 根卡和 creating/open/closing/closed/expired/failed 生命周期完全保持不变。Side
Session 仍是进程内 ephemeral Thread 容器，不成为 Binding、Scope 或新的执行模型；服务
重启后仍不可恢复。

容器内每个 Side Turn 复用 ordinary Turn 的 Task Reaction 和 Reply Card 投递契约：

- Task Reaction 关闭时零 reaction 和零 steer 文字 fallback；开启时使用相同的
  `Typing`/`THINKING`、`OnIt` 与终态表情生命周期；
- Progress Card 开启时，Side Turn 接受后在 completion origin 发送一张
  Activity 卡，过程只按 exact Side Turn 活动 revision 更新，终态在同一张卡折叠并加入
  Result 与可选 Files；
- Progress Card 关闭且没有文件时仍发送富文本/静态终态；有文件时只发送一张
  Result + Files 完成卡。

Side Turn 只组合 Activity、Result、Files，不增加 Goal Module。现有唯一
`ReplyCardPresenter` 管理 ordinary、Goal 与 Side 的进程内 session，typed module、渲染器、
分页 callback 和失败回退继续是封闭集合；这不是第三个 presenter、动态插件系统或持久
卡片状态。

### 创建时冻结 Parent 配置

创建 Side 时，在 Parent Binding 的 exact admission 中同时捕获：

- 已解析的 Model、Effort、Speed；
- Task Reaction、Progress Card 及 feedback revision。

创建提交前后都复核 Parent 的 settings/feedback revision；准备期间发生变化时零 fork 地
拒绝。Side Session 保存这两组不可变快照，之后每个新 Side Turn 和 running steer 都沿用；
Parent 后续 `/config`、active pointer 变化或 Binding 生命周期变化不传播到已创建 Side。
Side 内不提供 `/config`，也不增加 Side-scoped 数据库配置。

这项冻结与 Side fork 的语义一致：Side 继承创建瞬间的 Parent 历史和执行体验，之后独立。
它不表示复制 Codex 已生效配置，也不要求把快照写进 `side_topics` 或 Channel SQLite。

### 活动与文件边界

Runtime 为 exact active Side Turn 暴露与 ordinary Turn 同形、带 `side_id` 和 revision 的
只读 Activity Snapshot。只有 Progress Card 开启时才调用 ADR 0020 的 plan observer；
关闭时不轮询。Side 继续用公开 `AsyncTurnHandle.run()` 获取终态，不增加持久 history
recovery。活动仍只包含运行/停止状态和原生 checklist，不显示 reasoning、raw tool/command
output、arguments、elapsed time、百分比或 ETA，并经过既有有界敏感模式过滤。

Side 没有可靠的持久 aggregate diff/history recovery，因此 Files 只来自 exact completed
Side Turn 的 completed `fileChange` 与 `imageGeneration` structured items；不扫描 Project、
不解析最终文本、不读取更早 Side Turn，也不猜测 shell/MCP 输出。路径以创建时的 canonical
Project cwd 解析，沿用既有文件类型、数量、容量和脱敏展示约束。

Side 的 Result + Files 或 Activity + Result + Files 继续使用自包含 v4 callback。payload
中的 Parent Binding ID 只作为创建时捕获的 provenance 与确定性 identity；分页和发送不
重读 Parent Binding、配置、Project 或 Turn，因此 Parent 后续切换、修改或删除不会改变
已发送卡片。Channel Database 不保存 Side activity、文件 manifest、message ID 或 card
session。

### Goal 与失败语义

Side 内仍明确拒绝 `/goal`、Goal 卡按钮和任何间接 Goal lifecycle。Goal 是可跨多个物理
Turn、由 native persisted state 驱动的生命周期；Side 是不可恢复的 ephemeral 容器，把两者
叠加会制造重启、自动 clear、暂停和控制身份无法证明的状态。需要 Goal 时必须回到普通
Binding。

所有 reaction/card 操作仍为尽力展示：初始或更新失败不影响 native execution，终态回退到
同一 Side Turn 在 Progress Card 关闭时的文本/文件路径。Side 根卡只由 Side lifecycle
更新，Turn presenter 不修改它。

## 验证

门禁必须覆盖：

- Side 创建前后的 settings/feedback revision 竞态，以及 Parent 后续修改不传播；
- 两项默认关闭、四种开关组合、running steer、终态 reaction 清理；
- 无文件富文本、结构化文件完成卡、Activity 单卡更新与终态折叠、v4 分页/发送；
- Progress Card 关闭时零 plan observation，展示失败不改变 native outcome；
- Side `/goal` 在 capability 可用时仍零 mutation 拒绝；
- Side 根卡 identity、close/expiry、墓碑和重启语义零变化。

飞书 live qualification 还要在 P2P 与群 Side 各验证默认关闭、两项开启、running steer、
结构化文件 callback 与根卡 close；确认所有 Turn 回复留在 Side topic、只更新同一进度卡，
且 Parent 改配置后既有 Side 仍使用创建时快照。

## 后果

Side 的执行体验与普通 Turn 一致，但 recovery 能力仍不同：普通持久 Turn 由公开 history
恢复，Side 仍接受 `handle.run()` 的已知极快 completion race。扩展新 Reply Card module 时
只需决定 ordinary/Goal/Side 哪些场景提供该 typed projection；不得复制 card shell、引入
Side 专用插件注册表，或借展示模块改变 native lifecycle。
