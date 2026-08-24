---
status: superseded
date: 2026-08-13
amends: 0017
related: 0014
superseded_by: 0037
---

# 在公开 Python SDK 支持前保持原生 Thread Delete 不可用

> **已被取代：** [ADR 0037](0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md)
> 在 `0.147.0` 重新完成 active、archived、descendant cascade、response-loss 与四视图
> live qualification 后，以薄 Adapter 和失败对账恢复了 materialized Thread Delete。
> 本文继续保留 `0.144.4` 的失败证据与当时的安全决定。

## 背景

ADR 0017 设计了一个可移除的 `ThreadDeleteControl`，用于补齐 Python SDK 高层 facade
尚未公开的 `thread/delete`。2026-08-13 候选发布在目标 Linux 上使用仓库锁定的
`openai-codex==0.144.4` 及其自带 App Server/CLI `0.144.4` 运行完整 lifecycle gate：
公开 rename、archive、unarchive 均成功，delete 却返回内部错误，报告 Codex state
database 中不存在 `agent_jobs` 表。

只读核对显示该 state database 完整性正常，迁移记录包含创建 `agent_jobs` 的 migration
14 和删除它的 migration 42；同版本 App Server 的 delete 路径仍会访问该表。更重要的
是，RPC 报错后探针 Thread 已同时从 active 与 archived catalog 消失。这是“响应失败但
发生了部分副作用”，客户端既不能重试，也不能把报错猜成成功。

## 决定

1. `/delete` 只支持尚未创建原生 Thread 的 Lazy Binding。它显示红色二次确认，确认后
   仅删除 Channel Database 中的 Binding。
2. 当前 Binding 一旦已有 `native_thread_id`，`/delete` 在读取 metadata、显示确认卡或
   调用任何 native mutation 之前明确回复暂不可用。Binding 与 Codex 历史均保持不变。
3. 生产 `ServiceCore` 不构造或注入 `AppServerThreadDeleteControl`。ADR 0017 的窄 Adapter
   暂时只保留在 capability shape、真实 SDK client synthetic contract 和 facade
   migration sentinel 中；它不是产品 provider，也没有 live delete phase。
4. `lifecycle` 候选门禁只验证已公开的 rename、archive、unarchive，并要求恢复后保持
   exact native Thread ID。验证后用公开 archive 收尾，不会通过固定 RPC 尝试删除探针。
5. 不创建或修补 `agent_jobs` 表，不改用系统上另一份 CLI binary，不解析 CLI 输出，
   不把 materialized Binding 的本地删除包装成原生删除，也不吞掉 App Server 错误。

## 重新开放条件

Python SDK 出现公开高层 Thread Delete 后，facade migration sentinel 必须先让升级失败，
推动实现逐项切回公开 SDK。重新开放还必须同时满足：

- 删除 dormant Adapter 与固定 `thread/delete` synthetic contract；
- 在目标环境用新 SDK 自带的 App Server/CLI 运行 exact Thread live delete；
- 明确观察成功响应、active/archived catalog 均不存在、相关派生语义符合官方契约；
- 覆盖响应丢失、未知副作用、stale card、运行态互斥与 Binding 提交顺序；
- 更新本 ADR、设计文档和发布记录后再恢复 materialized 删除确认卡。

不以版本号白名单判断可用性；公开 surface 与上述升级 harness 是迁移触发器。

## 结果

用户仍可清理从未开始任务的 Lazy 会话，也不会误以为 materialized 会话已被安全删除。
生产不再触达已知会产生不确定副作用的私有路径。仓库保留的 shim 很薄，只承担检测 SDK
是否已追平 CLI/协议以及防止未来迁移被遗漏的职责；公开能力到位后应整体删除。
