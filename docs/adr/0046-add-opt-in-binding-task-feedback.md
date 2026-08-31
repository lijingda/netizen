---
status: accepted
date: 2026-08-27
amends: 0016, 0020, 0024, 0027, 0039
amended_by: 0047, 0048, 0051
---

# 增加默认关闭的 Binding 任务反馈与有界进度卡

> 修订说明：[ADR 0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md) 保留两项
> 默认关闭和相互独立的设置，但将 Progress Card 的 Activity 投影扩展到 Goal；Goal 卡片
> 本身始终存在。随后 [ADR 0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md)
> 让 Side 在创建时冻结 Parent 的两项设置，并让 Side Turn 复用两种反馈。下文“只控制普通
> Turn”与“排除 Side/Goal”仅保留为历史背景。
> [ADR 0051](0051-keep-lifecycle-reactions-and-make-pulse-optional.md) 又将历史 Task
> Reaction 开关收窄为只控制 `THINKING` Reaction Pulse；`Typing`、成功 steer
> 的 `OnIt` 和终态表情改为始终尽力展示。下文的零 reaction 关闭语义仅保留为
> 本决定的历史背景。

Netizen 需要让飞书用户在长 Turn 中看见“已经接受、正在做什么、计划到了哪里”，同时避免
任务表情在移动端形成额外通知，也不能把展示层变成第二套 Agent Runtime。我们选择两个
Binding-scoped、默认关闭的 opt-in，并从同一个有界 Turn 活动投影驱动进度卡；现有终态投递、
Codex 原生事实源和静态 Channel 回复模式保持不变。

## 决定

### Binding Task Feedback

Channel schema 升为 v7。`bindings` 增加：

- `task_reactions_enabled INTEGER NOT NULL DEFAULT 0`；
- `progress_card_enabled INTEGER NOT NULL DEFAULT 0`；
- `feedback_revision INTEGER NOT NULL CHECK(feedback_revision >= 1)`。

两个布尔值都受 `0|1` 数据库约束。它们与 revision 合称 **Binding Task Feedback / 会话任务
反馈**，只控制该 Binding 后续普通 Turn 的飞书展示，不是 Codex 原生配置。`/new` 和
`/config` 都提供两项独立选择，默认均为关闭；`/config` 与 Model/Effort/Speed、Mention
Context Mode 在同一事务中校验各自 revision，旧卡或 active Binding 已变化时零 mutation
失败。新 Turn 在 exact admission 中捕获当时的 Task Feedback，运行期间修改配置不会改变
已经开始的 Turn；running Binding 仍不允许 `/config`。

安装器只在旧 manager target 已卸载、稳定 lifetime lock 已持有且数据库回滚快照已完成后，
执行唯一的 v6 -> v7 原子迁移。现有 Binding 的两项都初始化为关闭、feedback revision 为
1；当前仍未向外部用户正式开放，不保留旧版本默认开启表情的兼容行为。迁移或候选激活失败
时恢复旧数据库与旧 release。服务进程仍只接受当前 schema，不在启动时自动迁移。

这不是通用插件或动态 KV 配置系统。新增 Task Feedback 类型仍须显式增加 typed domain、
数据库约束、表单、Runtime capture 和展示测试，避免任意配置键成为第二套运行策略。

### Turn Activity Projection

Runtime 为 exact Ordinary Active Turn 暴露只读、带 revision 的 **Turn Activity
Projection / Turn 活动投影**。首期只包含：

- native Turn 被接受后的运行中、正在停止状态；终态卡只使用权威 outcome；
- ADR 0020 已有的 exact `thread_id + turn_id` 原生 plan/checklist 投影及 steer freshness。

Progress Card 开启时可以按有界节奏读取这项快照，并且只在 projection revision 变化时更新；
关闭时不得为进度卡启动轮询或创建/更新卡片。ADR 0020 的只读 plan observer 因此增加一个
明确调用方，但其精确 SDK 版本/源码指纹、非消费队列、exact ID、完整 plan replacement 和
fail-closed 门禁保持不变。它不能扩展为任意通知读取或第二个 stream consumer。

活动投影不包含或推断 elapsed time、百分比、ETA、模型 reasoning、raw command output、
tool arguments、环境/credential、完整 commentary 或 subagent 对话。展示不得把计划状态
冒充原生终态；`thread.read()` 继续是普通持久 Turn 的终态权威来源。原生 plan step 虽是
模型生成的展示文本，不是额外 credential 字段，但进度卡、`/status` 和文件分页 callback
仍必须经过同一套有界的常见 secret/token、邮箱、用户目录、内联代码/参数、百分比和 ETA
过滤；未经处理的原 step 不进入卡片或 callback。

### 两个独立 Presenter

Task Reaction 与 Progress Card 是同一个 Task Feedback 边界下的两个独立、尽力而为的
presenter：

1. Task Reaction 开启时保持现有生命周期：Completion Origin 上的 `Typing`/`THINKING`、
   成功 steer 消息的 `OnIt`，以及终态的 `DONE`/`ERROR`/`CrossMark`。关闭时不创建、删除
   或更新任何任务表情，`OnIt` 的文字 fallback 也不发送。
2. Progress Card 开启时，native Turn 被确认接受后在 Completion Origin 回复一张运行卡。
   顶部 `collapsible_panel` 在运行中展开，逐步显示当前状态和 checklist；同一张卡只按活动
   revision 合并更新，不用定时文案制造“仍在工作”的假进度。
3. 终态先停止更新，再把同一张卡更新为 collapsed process、最终回答和可用的本轮文件；
   completed/failed/interrupted 使用对应的明确状态。无本轮文件时也在这张卡中呈现回答，
   有文件时复用 ADR 0024/0027 的脱敏标签、自包含 v4 manifest、分页和发送 callback；分页
   callback 只携带已经过滤、裁剪且有界的过程 manifest，翻页后继续显示同一折叠过程。
4. 全局 `reply_mode="static"` 保持不变；进度卡是普通 Card 2.0 回复和显式原卡更新，不把
   所有 Netizen 回复切成 Channel streaming mode。

两项均关闭时，普通 Turn 从 accepted 到终态之间不产生任务反馈，最终结果才按原路径出现。
只开任一项时不隐式开启另一项。

### 关闭语义与失败回退

Progress Card 关闭时必须完全保留现有完成投递：没有文件使用富文本/静态文本回复，有文件
使用现有“最终回复 + 本轮文件”卡片；这条路径不得创建或更新进度卡。

展示失败不能阻断、取消、重试或改写 Codex Turn：

- 初始运行卡发送失败后，该 Turn 停用 Progress Card，终态走上述现有路径；
- 中间更新失败后停止该 Turn 的卡片更新，避免重试风暴，终态走现有路径；
- 终态卡更新失败或卡片超出平台容量时，终态同样走现有路径；
- reaction 或卡片异常只记录脱敏日志，不改变 native outcome，也不能让 receipt barrier
  卡住已接受的 Turn。

进程内只保留 exact active Turn 所需的 card identity、projection revision 和 update task；
终态或正常关闭后释放。Channel Database 不保存 Activity Projection、卡片 session、卡片
消息 ID、计划、过程文本或回复。进程崩溃、强制 kill 或主机掉电后旧运行卡可能停留在最后
一次可见状态；重启时不扫描飞书、不猜测旧 Turn，也不在 SQLite 增加 recovery 状态。

## 扩展边界

首期只交付普通 Binding Turn 的 status + plan。Side、Goal、compaction、跨重启恢复，以及
完整 commentary/tool/subagent stream 都不是本决定的一部分。未来过程项应通过受限 typed
activity item 和独立 renderer/presenter 接入，从而复用 exact-Turn lifecycle、revision
coalescing、终态折叠与失败回退；不得让一个新项绕过敏感信息裁剪或直接拼接 raw SDK event。

加入 commentary/tool/subagent 摘要前，必须先证明来源是公开且不会消费终态通知的接口，
定义允许字段、长度/频率/容量边界与敏感信息测试，并更新兼容性决定和 live probe。不得把
ADR 0020 的 plan observer 泛化为 reasoning、工具日志或任意私有队列浏览器。

## 验证

聚焦门禁覆盖：

- fresh v7、v6 -> v7 行/墓碑保留、两项默认关闭、revision/CAS、失败回滚；
- `/new`、`/config` 的默认值、四种开关组合、旧卡与 running 拒绝；
- Progress Card 关闭时“无文件富文本、有文件现有卡片”的精确回归，以及零 card
  create/update；Task Reaction 关闭时零 reaction 和零 steer fallback；
- Progress Card 的 initial/update/terminal collapse、plan replacement、file callback、
  并发 Turn 隔离、容量边界和 initial/intermediate/final failure fallback；
- plan step 中的常见 secret/token、邮箱、用户目录、内联代码/参数、百分比和 ETA 被过滤，
  原值不进入卡片正文或文件分页 callback；正文也不出现 reasoning 或 raw output。

除 `make check` 外，飞书 live qualification 需要在 P2P、群主线和普通话题分别验证四种
开关组合、移动端关闭表情时不产生额外表情通知、同一卡片更新/折叠、本轮文件 callback，
以及卡片权限/更新故障不影响 native Turn 与最终结果。
