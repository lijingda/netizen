---
status: accepted
date: 2026-08-12
amends: 0008, 0014
amended_by: 0019, 0036, 0037, 0038, 0049
related: 0001, 0016
---

# 以当前 Binding 管理原生 Thread 生命周期

> **修订：** [ADR 0019](0019-keep-native-thread-delete-unavailable.md) 曾暂停 materialized
> Thread Delete；[ADR 0037](0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md)
> 已在重新实测后，用薄 Adapter 与四视图失败对账恢复该能力。
> [ADR 0036](0036-archive-exact-idle-sessions-from-the-sessions-card.md) 另行允许普通
> `/sessions` 卡片按 exact Binding 归档 idle materialized 会话；`/archive` 命令仍只作用于
> 当前会话。[ADR 0038](0038-delete-exact-idle-sessions-from-the-sessions-card.md) 同样只为
> 普通 `/sessions` 增加 exact idle 会话的两阶段删除入口；`/delete` 命令仍只作用于当前
> 会话。[ADR 0049](0049-bound-turn-observation-and-delegate-thread-removal.md) 改为对所有
> materialized persisted Thread 保留 lifecycle 控制，并为 archived catalog 增加独立
> Delete。archive/delete 直接委托 App Server 的有界 shutdown/removal，不再要求
> Netizen 先停止本地活动或证明 idle；响应不确定只做一次目录对账。

## 背景

Netizen 已能在一个 Feishu Scope 中创建、切换和展示多个 Binding，但尚不能重命名、
归档或删除它们。Codex App Server 已提供 `thread/name/set`、`thread/archive`、
`thread/unarchive` 和 `thread/delete`；当前 Python SDK 高层 facade 已公开前三项，唯独
没有公开 delete。若只修改本地 Binding，会让 Codex App/CLI 与飞书看到不同历史；若把
名称或归档状态复制进 Channel Database，又会产生第二事实源。

## 决定

### 产品入口只围绕当前会话

- `/rename [name]` 重命名当前 active Binding 对应的原生 Thread。带参数时直接执行；
  不带参数时打开卡片。名称归一化空白后必须为 1 到 120 个字符。
- `/archive` 只为当前会话显示确认卡。成功后归档原生 Thread、清空 Scope 的 active
  pointer，但保留 Binding、Project 和 Binding Turn Settings，因此恢复后配置不变。
- `/delete` 为 Lazy Binding 显示红色不可恢复确认卡并删除本地记录；materialized Binding
  按 ADR 0037 携带 exact native ID 二次确认，原生成功或 absent 对账后才删除本地记录。
- 普通 `/sessions` 不显示归档会话；`/sessions archived` 使用原生 archived catalog
  单独展示。`/unarchive <会话短 ID>` 或归档卡片中的“恢复并切换”按钮恢复原生 Thread
  并把对应 Binding 设为 active。
- 不提供 `/archive <id>`、`/delete <id>` 或跨会话 rename。用户要管理另一个普通会话，
  先 `/resume`；恢复归档会话必须显式选择目标，因此 `/unarchive` 是唯一例外。

卡片携带完整 Binding ID。rename/archive/delete 提交时必须在 Scope 锁内重新确认它仍是
当前 active Binding；旧卡片不能影响后来切换到的会话。归档列表的恢复按钮只允许选择
同一 Scope 内仍存在的 exact Binding，并在执行前重新验证它仍出现在原生 archived
catalog。

### Codex 是名称、归档和删除的事实源

Channel Database 不增加 name、preview、archived、deleted 或 lifecycle 表。展示时通过
公开分页 `thread_list(archived=False|True)` 读取原生 `name` 和 `preview`；归档状态也只
由原生 catalog 判定。Binding 继续只保存 native Thread ID 与 Channel 自己拥有的映射和
Turn-setting intent。

rename 使用公开 `AsyncThread.set_name()`，archive/unarchive 使用公开
`AsyncCodex.thread_archive()` / `thread_unarchive()`。三者不得走私有 RPC。归档与删除
要求公开 Thread read 明确证明 persisted、non-ephemeral 且 idle，并与 ordinary Turn、
Goal、compaction 和其他 lifecycle mutation 互斥；rename 可以在原生 Thread 存在时执行，
但自身仍占用一个短生命周期槽。

任何已经开始的原生 mutation 若响应、取消或后续本地映射提交结果未知，Runtime 默认保留
该 Binding 的 lifecycle-unknown 槽并关闭进程级 admission。delete 的唯一例外是 ADR 0037
规定的一次有界四视图只读对账：明确 present 时保留并允许重新确认，明确 absent 时提交
Binding Delete；unknown 仍 fail closed。成功归档或删除后不自动选择其他会话。

### delete 只使用一个可移除的窄 Adapter

当前 SDK 高层没有 `thread_delete`，因此按 ADR 0014 的边界增加单一
`ThreadDeleteControl.delete(thread_id)`：

- 复用同一个已经初始化的 `AsyncCodex` 和 App Server；固定 method 为
  `thread/delete`，直接复用安装 SDK 的 `ThreadDeleteParams/Response`；
- 不暴露通用 request、字符串 method、任意 response model、第二客户端或本地删除模拟；
- 不使用 SDK 版本 allowlist。构造时按能力独立校验 ownership edge、generated model
  fields/aliases 与空 response；synthetic harness 断言 exact method/params，并把响应丢失
  作为 unknown mutation 交给 ADR 0037 的 Runtime 对账；
- facade inventory 一旦发现 `AsyncCodex.thread_delete` 或 `AsyncThread.delete`，升级即以
  `migration-required:thread-delete` 失败，直到实现改回公开 SDK 并删除该 shim。Goal、
  Skills 和 delete 各自独立迁移，不建立 provider framework。

## Harness 与发布门禁

仓库检查覆盖命令解析、严格卡片解码、stale card、Binding 原子 deactivate/delete、
Runtime 互斥和未知副作用、SDK facade inventory，以及真实安装 SDK client 对 fake App
Server 的 delete shape/synthetic contract。

每次 SDK/App Server 升级和候选发布还必须在目标环境运行 `--phase lifecycle`。该 phase
只操作自己创建的探针 Thread，依次执行一个 seed Turn、公开 rename、公开 archive、公开
unarchive 和 Adapter delete，并通过 rollout scan/state DB 各自的 active/archived 分页
catalog 观察名称保留、同一 ID 恢复以及最终从四个视图消失。任一步失败都不得发布
materialized delete；探针不会删除任何既有 Thread，也不会在 delete 响应丢失后自动重试。

## 结果

飞书、Codex App 与 CLI 共享同一原生会话生命周期，Channel 不复制 Codex 状态。产品面
保持当前会话优先，危险操作必须确认，恢复入口仍可发现。新增私有边界只有一个可逐项
移除的 delete method；其生命周期由升级 harness 而非永久兼容代码控制。
