---
status: accepted
date: 2026-08-30
amends: 0008, 0010, 0017, 0036, 0037, 0038
related: 0031, 0047
---

# 有界处理 Turn 观测故障，并委托 App Server 移除 Thread

## 背景

Netizen 已把原生 `completed`、`interrupted` 和 `failed` 都视为 Ordinary Turn 终态，
但旧实现会在终态观测失败后建立长时间恢复循环，并把执行、停止和观测阶段
组合成多个 Runtime 状态。同时，运行中归档/删除在 Netizen 内再次编排 interrupt、
background-terminal cleanup、exact Turn terminal 等待和 native idle 证明。这使观测故障既
阻止同一 Thread 继续对话，又可能夺走用户归档或删除它的出口。

锁定的 Codex/App Server 0.147.0 已原生拥有这个边界：

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

### 观测故障只有一次短恢复

稳态轮询保留原有语义：合法的长 Turn 可以无执行时长上限地等待终态。只有当
Netizen 拿不到 exact Turn 的权威视图时，才进行一次最多 5 秒、最多三次原生 I/O
的尝试；其中最多重新 `thread_resume` 一次。已知可收敛的 transport/RPC、
`notLoaded`、暂时缺少 exact Turn 或视图不一致可进入该尝试；identity/contract/
programming 错误直接失败，不冒充为值得重试的 I/O。

尝试若恢复 exact `active/inProgress`，Runtime 继续普通轮询并恢复 steer；若确认终态，
走唯一的终态交付路径。仍不可验证时，公开状态只投影为
`turn-observation-unavailable`：保留 exact Binding/Thread/Turn 槽，阻止该 Binding 重复
start/steer，并结束所有周期性恢复 I/O。其他 Binding 和进程 admission 保持可用。

用户的“重新检查”只启动同样有界的一次尝试。没有指数退避、长预算、背景 wake
flag 或无限 resume/read 循环。一旦 `terminal_observed=True`，final response materialization
使用现有有界重读和无文本兜底，不再进入观测恢复。

进入 `turn-observation-unavailable` 时，同一 Turn 的 Task Reaction 脉冲和 Progress Card
轮询也停止；已有进度卡一次更新为“Turn 观测不可用”后退出 presenter。之后若手动重检
最终收敛，终态结果仍通过普通完成路径交付，不因旧卡 presenter 已停而丢失。

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
  spawned descendant cascade 继续由 0.147.0 固定源码契约、ADR 0037 已记录的真实
  root→child→grandchild 实测和 root-only Adapter contract 约束；routine probe 不让模型临时
  生成一棵非确定性 agent tree。
