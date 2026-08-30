---
status: accepted
date: 2026-08-24
amends: 0017, 0037
amended_by: 0049
related: 0036
---

# 从 `/sessions` 卡片两阶段删除 exact idle 会话

> **修订：** [ADR 0049](0049-bound-turn-observation-and-delegate-thread-removal.md)
> 保留本 ADR 的两阶段危险确认和 exact Binding/native identity，删除 idle、active-pointer
> 和 Runtime activity 前置条件。materialized 行直接委托 App Server Delete；
> `/sessions archived` 也使用同一原生删除 primitive。Lazy 语义不变。

## 背景

ADR 0037 恢复了 native Thread Delete，并把 `/delete` 文本命令固定为 current-only：确认卡
携带 exact native identity，Runtime 使用 native-first 删除和一次有界四视图对账处理响应
丢失或未知副作用。普通 `/sessions` 已经按 ADR 0036 为每行携带完整 Binding、Scope、active
pointer 快照与页码，并能在不切换当前会话的前提下 exact archive。若删除非当前会话仍要求
先 `/resume`，会不必要地改变后续普通消息的目标；直接复用一阶段危险按钮又不足以表达永久
删除的后果。

## 决定

`/delete` 文本命令保持 current-only，不增加 `/delete <id>`。普通 `/sessions` 为同一
Scope 中呈现为 idle 的 Lazy 行，以及 Delete capability 可用且呈现为 idle 的 materialized
未归档行显示“删除”；running、stopping、Goal、compacting、lifecycle-unknown 或 capability
不可用的 materialized 行不显示入口。`/sessions archived` 不提供删除。

删除使用两个独立 typed action：

1. `binding.delete.exact.prepare` 携带完整 Binding ID、Scope、卡片生成时的 active Binding
   ID、可空的 exact native Thread ID 与页码。它只在共享 Scope 锁内重新校验 exact
   Binding、Scope、active pointer、native identity、Delete capability 和实时 idle 状态，
   不产生 mutation；通过后打开独立的红色危险确认卡。
2. `binding.delete.exact` 从确认卡携带同一组完整前置条件。最终点击再次执行相同校验，不能
   依赖 prepare 阶段保留的进程内或数据库 card session。Lazy 仅删除本地 Binding；
   materialized 复用 ADR 0037 的 exact Runtime delete、native-first 顺序和一次四视图失败
   对账，不增加第二个 Adapter、重试或 switch-then-delete 流程。

确认卡明确区分后果：Lazy 只永久删除本地 Binding；materialized 会永久删除原生 Thread、
App Server 管理的 spawned descendants、Codex App/CLI 历史与本地 Binding。最终按钮使用
危险样式和内置二次确认。

删除 inactive Binding 时保留真实 active pointer；删除当前 Binding 时清空 pointer；两种
情况都不自动选择其他会话。成功后从 live catalog 重建同一卡片并把原页码夹取到有效范围。
若删除已确认提交而卡片重建或更新失败，只发送等价成功反馈，不能把已提交 mutation 误报为
失败。旧卡、跨 Scope、active pointer 变化、Lazy 物化、native ID 变化、目标已归档或不再
idle 均零 mutation 地失败；ADR 0037 对账无法判定时保留 Binding、标记 lifecycle unknown
并关闭 admission。

Channel Database 不保存卡片 session、删除意图或原生生命周期副本。本决定不开放批量删除、
Admin materialized delete、已归档行删除或任意目标文本命令。

## 后果

用户可以在 `/sessions` 中永久删除任意符合条件的普通 idle 会话，而不改变非目标的当前工作
上下文。危险操作比行内归档多一个明确的红色确认阶段；每次最终提交仍依据 live 状态，而非
卡片展示时的假设。原生删除的兼容门禁、四视图对账与 removal trigger 继续只有 ADR 0037
定义的一份。

## 否决的方案

- 扩展为 `/delete <短 ID>`：重新引入文本目标解析，缺少逐行身份与后果上下文。
- 先自动切换再调用 current-only `/delete`：会改变 active pointer，并把一个 mutation 拆成
  两个可见状态。
- 在会话行直接放最终永久删除：一次误点即可进入不可逆操作，缺少独立危险说明。
- 对所有状态都显示后再拒绝：会让已知 running、Goal 或 capability 不可用的目标产生误导。
- 新增本地删除队列或 card session：会制造第二事实源，并不能替代最终 live revalidation。
