---
status: accepted
date: 2026-08-24
amends: 0017
related: 0031
---

# 从 `/sessions` 卡片归档 exact idle 会话

## 背景

ADR 0017 将飞书的 rename、archive 和 delete 产品入口收窄到当前 Binding，避免短 ID
命令和旧确认卡误操作其他会话。普通 `/sessions` 后来升级为无持久状态的分页卡片，每行
已经携带完整 Binding ID、Scope 和 active pointer 快照；Admin Web 也已通过共享
Management/Runtime 边界安全支持 exact inactive archive。继续要求先 `/resume` 再
`/archive` 会制造一次不必要的当前会话切换，并可能改变后续普通消息的目标。

## 决定

`/archive` 命令保持 current-only，不增加 `/archive <id>`，也不改变 ADR 0017 的现有
确认卡。普通 `/sessions` 为同一 Scope 中呈现为 idle 的 materialized、未归档行增加
“归档”按钮；卡片快照已知为 Lazy、running、stopping、Goal、compacting 或 lifecycle
状态的行不显示该按钮。

该按钮使用独立的 `binding.archive.exact` typed action，携带完整 Binding ID、Scope、当前
页码和卡片生成时的 active Binding ID（可为空）。飞书内置确认明确展示目标会话；提交在
共享 Scope 锁内重新校验 exact Binding、Scope、active pointer 和实时 Runtime 状态，并
由原生 Thread read 证明 persisted、non-ephemeral、idle。任一前置条件变化、外部 active、
目标已归档或副作用状态未知都 fail closed，不切换后重试，也不自动重放。

归档 inactive Binding 时保留真实 active pointer；归档当前 Binding 时清空 pointer；两种
情况都不自动选择其他会话。成功后从原生 active/archived catalog 重建同一卡片并把原页码
夹取到有效范围。若原生 mutation 已确认成功而卡片重建或更新失败，发送等价成功反馈，
不能把已提交操作误报为失败。

Channel Database 不保存归档状态、卡片 session 或动作记录；实现复用既有 exact
Management/Runtime archive 事务和公开 `AsyncCodex.thread_archive()`，不增加 SDK Gap
Adapter、批量归档或 switch-then-archive 流程。

## 后果

用户可以直接整理 `/sessions` 中的空闲历史会话，而不会为了归档非当前会话改变当前工作
上下文。`/archive` 的简单 current-only 语义、`/sessions archived` 和恢复行为保持不变。
旧卡片在 active pointer 或 Runtime 状态变化后必须重新打开；归档仍受原生 idle/lifecycle
门禁约束。

## 否决的方案

- 扩展为 `/archive <短 ID>`：重新引入文本目标解析，且不能提供逐行身份与确认上下文。
- 先自动切换再调用现有 `/archive`：会改变当前会话，并把一个 mutation 拆成两个可见状态。
- 对所有行都显示按钮后在点击时拒绝：让已知不可执行的 Lazy/running/Goal 状态产生误导。
- 在 Channel SQLite 记录 archived 标志：与 Codex 原生 catalog 形成第二事实源。
