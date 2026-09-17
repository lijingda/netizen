---
status: accepted
date: 2026-08-30
amends: 0008, 0010, 0017, 0036, 0037, 0038
related: 0031, 0047
amended_by: 0052
---

# 有界处理 Turn 观测故障，并委托 App Server 移除 Thread

## 背景

Netizen 已把原生 `completed`、`interrupted` 和 `failed` 都视为 Ordinary Turn 终态，
但旧实现会在终态观测失败后建立长时间恢复循环，并把执行、停止和观测阶段
组合成多个 Runtime 状态。同时，运行中归档/删除在 Netizen 内再次编排 interrupt、
background-terminal cleanup、exact Turn terminal 等待和 native idle 证明。这使观测故障既
阻止同一 Thread 继续对话，又可能夺走用户归档或删除它的出口。

本 ADR 决策时锁定的 Codex/App Server 0.147.0 已原生拥有这个边界：

- [`thread/archive`](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/app-server/src/request_processors/thread_processor.rs#L855-L874)
  和 [`thread/delete`](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/app-server/src/request_processors/thread_delete.rs#L44-L123)
  都在移除前调用 App Server 的 Thread shutdown 编排。
- [`prepare_thread_for_removal`](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/app-server/src/request_processors/thread_processor.rs#L1389-L1455)
  从 ThreadManager 移除已加载 Thread，并对 `shutdown_and_wait()` 做有界等待；提交失败或
  超时不会把 archive/delete 变成无限等待。
- `thread/delete` 从 state DB 读取 root 的 spawn subtree，由 App Server 按子孙优先顺序
  级联删除 spawned descendants。

因此，Netizen 继续复制一套移除前安静化状态机没有产品或安全收益。

## 决定

### Turn 终态只释放本轮

exact Turn 的 `completed`、`interrupted` 和 `failed` 都是 **Confirmed Turn Terminal**。
Runtime 观测到任一终态就释放 Ordinary Turn 槽；`failed` 只影响本轮结果，不结束
或损坏承载它的 Native Codex Thread。后续消息仍在同一 Thread 上开始新 Turn。

Thread 的运行状态不替代 exact Turn 的持久化终态。轻量读取返回 `systemError` 或
`notLoaded` 时，仍通过公开 full read 核对 exact Thread 与唯一的 exact Turn；已确认的
`completed/failed/interrupted` 正常收尾，即使 Thread 仍保留错误标记或已卸载。未知
Thread/Turn shape、身份不匹配或重复 exact Turn 不被当成终态；`inProgress` 仍要求同一
full view 中 Thread 为 `active`。仅在未取得终态且 Thread 未加载或连接不可用时尝试 resume，
不能把 `systemError` 当作“重读必然能消失”的连接错误。

失败交付保留原生终态、可读错误与错误码；缺少说明时明确指出错误类型或无详情。
错误展示仅投影公开说明、错误码及显式异常 cause，做凭据过滤和长度限制，不展示 raw
RPC data、工具输出或 traceback。观测不可用也保留最后一次原因的有界内存摘要；日志
记录 exact IDs 与该摘要，便于区分后端拒绝、连接问题和视图不一致。

### 观测故障只有一次短恢复

稳态轮询保留原有语义：合法的长 Turn 可以无执行时长上限地等待终态。只有当
Netizen 拿不到 exact Turn 的权威视图时，才进行一次最多 5 秒、最多三次原生 I/O
的尝试；其中最多重新 `thread_resume` 一次。已知可收敛的 transport/RPC、
`notLoaded`、暂时缺少 exact Turn 或视图不一致可进入该尝试；identity/contract/
programming 错误直接失败，不冒充为值得重试的 I/O。

`InternalRpcError` 先在原连接重读 exact Thread，不直接触发 resume：收到原生 RPC
错误本身不证明订阅已丢失。后续读到 `notLoaded` 或发生 transport 故障时，才使用
同一预算内的至多一次 resume。新 Thread 的 session metadata rollout 尚为空时，
metadata read 可暂时返回 Internal；立即 resume 不能保证恢复权威视图。

`0.154.0` 的 full read 内部先从 rollout 判断分页模式，再查询 SQLite 分页投影；两者
可短暂不一致。仅在 `include_turns=True` 时，把 `MethodNotFoundError`、code `-32601`
和精确消息 `list_turns is not supported yet` / `list_items is not supported yet` 的组合
归为视图暂不可用，使用同一有界 read 预算。永久缺少分页能力仍会在预算耗尽后停止；
metadata read、其他方法名或其他 code 不进入此分类。此修正不扩大恢复预算，也不降低
exact Turn 运行/终态的证明要求。

尝试若恢复 exact `active/inProgress`，Runtime 继续普通轮询；没有停止意图时恢复 steer，
已有 stop intent 时仍保持 `stopping`，不能因观测恢复撤销停止或清理要求。若确认终态，
走唯一的终态交付路径。仍不可验证时，公开状态只投影为
`turn-observation-unavailable`：保留 exact Binding/Thread/Turn 槽，阻止该 Binding 重复
start/steer，并结束所有周期性恢复 I/O。其他 Binding 和进程 admission 保持可用。

用户的“重新检查”和新普通消息都只触发同样有界的一次尝试。新消息先检查旧 Turn，
确认终态并满足已有清理要求后释放槽，再捕获新的 admission；旧结果由原 consumer 唯一
尽力交付，不要求飞书回执先于新任务启动。恢复运行且没有停止意图时仍 steer 同一 exact
Turn。检查期间不持 Binding 锁，不保存待发 prompt、不重放旧输入。
并发请求共用已有检查，取消一个消息的等待不取消共享 consumer；结束后仍按现有
admission revision、Scope/Binding 和 lifecycle 校验，不能把消息转给变化后的目标。
复用同一 admission revision：重检开始时冻结版本，只有本次 consumer 在 Binding 锁内
确认“转换前版本仍匹配”，才允许把它推进到恢复运行或释放终态槽后的版本。停止、切换或
其他控制操作使原版本失效；复查成功也不能重新授权旧消息。这样不新增控制 epoch、等待队列
或第二套状态机。用户在停止完成后重新发送的消息仍走普通新 Turn 流程。
仍不可验证时新消息明确未执行，并返回最近原因，不再向旧消息重复发送不可用回执。
未知 start/steer 等副作用的既有隔离边界保持不变，不能只凭错误文字解除占用。
没有指数退避、长预算、背景 wake
flag 或无限 resume/read 循环。一旦 `terminal_observed=True`，final response materialization
使用现有有界重读和无文本兜底，不再进入观测恢复。
普通 Turn 终态后的唯一公开 stream 消费只补充用量和 diff，最多等待一秒；遗漏
completion 通知或统计流中断不再阻止已确认终态的交付与槽释放。取消通过 SDK 的公开
异步 stream 关闭订阅；synthetic 门禁验证阻塞 worker 被唤醒，不增加另一个通知消费者。

进入 `turn-observation-unavailable` 时，同一 Turn 的 Reaction session、Reaction Pulse
和 Progress Card 轮询全部停止；已记录的 `Typing` 与当时可见的 `THINKING` 会尽力清理，
但不会添加任何伪造的终态表情。已有进度卡一次更新为“Turn 观测不可用”后退出 presenter。
恢复观察不重建旧进度卡或 Reaction Pulse；提示明确旧卡不再更新，新 Turn 复用普通展示流程。
之后若手动重检最终收敛，终态结果仍通过普通完成路径交付，不因旧 presenter 已停而丢失。

Ordinary Turn 的公开状态集合因此只是 `running`、`stopping` 和
`turn-observation-unavailable`。`/stop` 仍是独立 Turn 控制，保留其已有的 interrupt 和
background-terminal cleanup 幂等事实；它不再是 Thread 归档/删除的前置步骤。

### 所有持久 Thread 都保留生命周期控制

只要 Binding 指向 materialized、persisted、non-ephemeral Thread，无论本地投影为 idle、
running、stopping、`turn-observation-unavailable`、Goal、Compaction 或其他原生活动，
用户都可以发起 archive 或 delete。archived Thread 始终可以 delete。Ephemeral Side 不在这个
产品边界内。

Netizen 在 Binding lock 内只做三件事：确认 exact Binding/native Thread identity、占用该
Binding 的 lifecycle intent、阻止同一 Binding 开始新 Turn。之后释放 Binding/Scope lock，
直接调用 `thread/archive` 或 ADR 0037 的固定 `thread/delete` Adapter。Netizen 不先调用
interrupt、Goal pause、terminal cleanup、Turn recovery/read，不等待 exact terminal，也不证明 native idle。
App Server 拥有 shutdown、超时、目录移动与 descendant cascade 的原子边界。

原生成功后，Runtime 取消并丢弃该 Binding 的本地 Turn/Goal/Compaction 观察者；archive
保留 Binding 并清空其 active pointer，delete 再删除 Binding。不会自动选中另一个 Binding。
Runtime 同时发送一个只用于展示清理的内部 activity-discarded 事件，停止对应的 Reaction/
Progress/Goal presenter；它不是 Turn 终态，不生成 completed/failed/interrupted 结果。exact
Binding 的 lifecycle intent 保留到这次展示清理交接结束，避免迟到的旧事件清掉归档后重新
激活的新活动；展示清理失败仍不改变已经确认的原生 mutation 和本地 Binding 结果。

若 mutation 返回非取消异常，只执行一次、只读的 native catalog 对账：archive 在 exact ID
只出现于 archived catalog 时提交本地成功；delete 在 rollout scan/state DB 的
active/archived 四视图全部 absent 时提交本地成功。明确仍 present 则保留 Binding、释放
lifecycle intent 并允许用户重新确认；对账本身失败才保留 Binding-local
`lifecycle-unknown`。调用被取消时不在已取消任务内启动新的目录 I/O，直接保留同样的
Binding-local unknown，交给正常重启后的新操作重新对账。任何路径都不自动重发 mutation，
也不因一个 Binding 的 lifecycle unknown 关闭全局 admission。

App Server 整体不可达、原生存储损坏或 ephemeral root 拒绝是能力/基础设施故障，不被伪造成
更多 Thread 运行状态，也不启动无限兜底。

### 删除旧 V2 而不保留兼容层

删除观测阶段双轴、长时间预算/退避、wake/notice/deadline flags、自动恢复通知细分、
运行中 archive/delete 的本地安静化等待、Runtime activity/physical Turn 生命周期前置条件、
以及要求这些细节的测试。卡片升级后不尝试兼容解释旧 V2 action envelope。

## 后果

- 观测故障最多保留一个静止的 Binding-local exact Turn 槽，不会泄漏成长期 I/O，也不会
  影响其他 Binding。
- 用户始终有明确退出通道：能恢复观测就继续同一 Thread，不能恢复也仍可归档或
  删除该 Thread。
- Runtime 不再与 App Server 竞争 Thread removal 编排所有权；生产代码和状态组合净减少。
- archive/delete 调用本身仍可能因 App Server 或存储故障失败。用户会看到明确错误，而不是
  一个伪造的“正在恢复”状态。

## 验证

- Runtime 测试覆盖三种终态释放槽、单次短恢复、长任务恢复 `inProgress` 后无时限
  轮询、失败后无周期 I/O、手动有界重检和 terminal materialization 与恢复解耦；Channel
  测试覆盖不可用时停止 Reaction/Progress presenter 且后续终态仍交付。
- Runtime/Management/Card/Channel 测试覆盖 idle、running、stopping、
  `turn-observation-unavailable`、Goal、Compaction 上的直接 archive/delete，并证明本地
  不 interrupt、pause、cleanup、等待 terminal 或读取 idle。
- 锁定 SDK 的 live lifecycle probe 直接删除 running disposable Thread，不先做本地安静化。
  spawned descendant cascade 由当前 0.154.0 源码复核、ADR 0037 已记录的真实
  root→child→grandchild 实测和 root-only Adapter contract 约束；routine probe 不让模型临时
  生成一棵非确定性 agent tree。
