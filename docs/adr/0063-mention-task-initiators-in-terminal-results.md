---
status: accepted
date: 2026-09-14
amends: 0046, 0047, 0048, 0061
amended_by: 0068
---

# 在任务结束时提及任务发起人

2026-09-21 修订：[ADR 0068](0068-run-saved-scheduled-plans-manually.md) 撤销本文的
v10 → v11 安装期迁移，服务与安装器只接受当前结构；下文迁移描述保留为历史决策。

持续更新的进度卡可能使离开聊天的用户错过任务结束。新增默认开启、独立于进度卡的
Completion Mention / 结束提及。2026-09-15 修订：用户观察到提前读过运行卡后，
终态更新中首次加入的 @ 不产生新通知；公开更新接口也没有重新通知参数。因此原卡
更新不再内嵌 @，确认终态更新后另发一条短文本真实 @。2026-09-16 补充：Goal 即使
关闭 Progress Card 仍复用 Goal 卡，所以旧 Goal 卡更新也使用独立提醒，不受进度开关
影响。新发送的最终富文本、文件卡或 Goal 卡仍在回复内 @，不增加提醒消息。
若过程卡不可用而另发最终回复，也复用上述回复内 @；不为提醒额外创建结果卡。
新消息进入正常提及通知路径，但客户端效果仍须实测，不能由 API 成功推断。

2026-09-16 修订：普通聊天把卡片转成话题会额外触发话题通知，因此独立提醒继续以
本次实际结果卡为回复锚点，使用公开 Channel `send` 与 `reply_target_gone="fail"`，
但私聊和群主线改为 `reply_in_thread=False` 的普通引用回复；已有话题保持
`reply_in_thread=True`，在原话题内发送新提醒。不能改选用户消息作为锚点。
校验回执的聊天、消息和根/父消息身份：普通引用回复不得含 thread，parent 必须为
本次结果卡；已有话题须确认同一 thread 及非空 root/parent。普通回复树可能保留更早的
root，不能要求 root 等于卡片 ID。卡片删除、发送失败或返回未知时不换位置、不转回复
用户消息、不补发第二次 @。提醒不建立 Binding 或 Side route；Files 的话题发送规则不变。

`BindingTaskFeedback.completion_mention_enabled` 与既有 feedback revision 一起保存，
`/new`、`/config` 和 Scheduled Plan 的共享 SessionSettings 都显式携带该布尔值。
新建和 v10 升级的 Binding、计划设置默认开启；既有表情/进度卡选择不变。旧配置卡缺少
新字段时明确过期，不能提交时隐式重开；三项反馈仍在同一次 revision/CAS 中更新。
普通/Goal admission 捕获快照，Side 创建时冻结 Parent 的选择，运行中配置不漂移。

提及对象只取本轮 Runtime admission 捕获的 `owner_id`，不从模型正文、最终消息、
配置修改人、后续 steer 或 Completion Origin 的 sender 猜测；Side 首轮 origin 可以是
机器人 seed。只允许一个有效用户 open_id，不允许 `all` 或自动计划身份。
Scheduled 自动首轮没有人类请求发起人，因此不提及；该话题后续人类请求正常使用开关。

普通与 Side 可确认的 completed、failed，以及普通持久 Turn 已交接的执行错误使用一次结束提及。
Side `handle.run()` 错误没有持久历史终态证据，仍按状态未知处理，不提及。当前固定版本
`openai-codex==0.154.0` 的 `AsyncTurnHandle.run()` 在 native failed 后也抛无类型区分的
`RuntimeError`，因此真实 Side 失败目前进入既有 unknown 路径，不能承诺失败提醒。
不得为提及而把未知执行冒充已知失败；待公共 SDK 能区分失败终态与观测错误后另行验证。
interrupted、用户主动停止或关闭 Side、compaction、生命周期控制以及状态未知通知不提及。
Side 的 `stop_requested` 单独捕获 STOPPING 意图，覆盖主动关闭与自然完成的竞态；
`background_cleanup_requested` 仍只代表 cleanup RPC 成功，不混用这两个事实。
Goal 只在逻辑终态交接时处理，不能随物理 Turn rollover 提及；complete、blocked、
usageLimited、budgetLimited 还需已有 completed/failed 最终物理 Turn 且无观测错误。
paused/active/未知状态不提及。完成工作但自动 clear 未确认时，可以随已有的明确说明
提及，仍不得伪造自动收尾成功。

新最终回复中的提及仍是 Result 的一次投递元数据；过程卡或 Goal 卡正常更新时则是独立短消息。
正文保持原样，不增加第五种 Reply Card 模块。v4/v5 文件 manifest 只保留结果
正文，不携带提及身份；进程内保留的 Goal 控制投影也清除该字段。翻页或后续控制重绘
因此不会重新加入或发送 @；新最终卡片中的 @ 行可能随重绘消失。不新增通知队列、持久消息身份、
用户身份历史或跨重启补发。原有有界终态回退仍适用，展示异常不改变原生执行。
SDK 失败结果不能证明零投递。过程卡或 Goal 卡不可用、更新未确认时，保留原有最终正文、
文件卡或 Goal 新卡回退，复用普通新回复内的 @，不再另发独立提醒。原卡更新未带 @，所以
更新回执丢失也不消耗提及机会；新卡/正文已经尝试过 @ 后，后续回退不再次提及。
Goal Presenter 显式返回本次更新的卡片 ID，不能从旧 Origin 猜测；超大正文另发时
直接在该正文内提及，不再回复控制卡提醒。独立提醒只进行一次应用层发送调用，
携带由本轮和卡片身份生成的稳定 UUID；未知或失败不进入结果回退，不新增跨重启
补发状态。长富文本可能首段已提及、后续分段才被审核拒绝，因此固定审核失败回执
也不再次提及。开关和终态资格在所有路径上相同，关闭提醒不改变原有回退展示。

另有固定 `lark-channel-sdk==1.4.0` 的公开出站路径限制：`OutboundPost.mentions` 在
平台返回 FORMAT_ERROR 时会由 SDK 自动降级为普通文本，真实 at 节点变成字面量
`@open_id`，并可能返回 success；应用无法从多段发送的最终回执识别中间降级。
本地公开 `OutboundSender` 故障注入已复现。SDK 没有禁用降级的公开选项，本次保留
富文本形态，不改私有 SDK 或为其新增适配器；该情况下不保证提及，应待上游修复并
完成兼容性验证后解除此限制。

初次引入结束提及时 schema 升为 v11；本次通知路径修订不新增迁移。
服务严格只接受当前 schema；安装器在 manager target 已卸载、持有
lifetime lock 且数据库快照完成后，执行唯一 v10 → v11 原子迁移，同时补齐当前
Scheduled Plan 的 SessionSettings JSON。损坏或其他旧版本拒绝，不重建空库；候选失败
仍按原安装事务恢复数据库/release/Skill。本次窄迁移取代 ADR 0061 的不保留旧版迁移
约定，不恢复更早的迁移链。

本地门禁覆盖默认值、持久化/CAS/旧卡、迁移原子性与安装失败回滚、两种终态回复、
发起人身份、Side/Goal、停止/失败/未知、原卡更新及分页零新增提及。飞书验收分别检查
私聊、群主线、话题的独立终态 @ 通知（包括提前读过卡片）和后续重绘不重复通知；未实测前只承诺生成真实
提及，不宣称客户端通知已经通过。

官方依据：[卡片 Markdown 的 @ 语法](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/content-components/rich-text)
与[更新卡片接口](https://open.feishu.cn/document/server-docs/im-v1/message-card/patch)、
[回复消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply)。
