---
status: accepted
date: 2026-09-03
amends: 0031, 0037
related: 0038, 0049
---

# 允许 Admin 删除 exact materialized 会话

## 背景

Admin Web 的 `Instance Administrator` 已是持有独立 credential 的单一实例运维身份，
可以跨 Scope 管理 exact Binding，却只能归档 materialized Thread 或删除 Lazy Binding。
原生 Thread Delete 已由 ADR 0037/0049 完成 fixed-method Adapter、native-first、级联删除和
失败对账；继续禁止 Admin 使用同一 primitive 不再是能力或架构限制，只会迫使部署者回到
飞书 Scope 逐个删除。

归档与删除的即时表现相似，但生命周期不同：归档可恢复并保留 Binding；删除会永久移除
Native Thread、App Server 管理的 spawned descendants、Codex App/CLI 历史及本地 Binding。
因此 Admin authority 足以执行删除，但界面必须在 mutation 前明确展示永久后果并二次确认。

## 决定

Admin Sessions 为 Delete capability 可用且 live catalog 状态为 active 或 archived 的
materialized、persisted、non-ephemeral 普通 Binding 提供单项“删除”操作。Missing、Lazy
与 Side 不进入该 materialized 入口；Lazy 保留既有 `delete-lazy` 二次确认，不提供批量删除。

列表中的删除按钮先打开浏览器二次确认，不产生 mutation。确认文案显示会话标题、Scope 和
short ID，明确说明 root Thread、spawned descendants、Codex App/CLI 历史与 Binding 都会
永久消失。管理员确认后才提交 POST；不要求输入 short ID，也不保存确认状态。POST 消费
session-bound 的一次性 action/CSRF grant；grant 固定 exact Scope、Binding 和 native Thread ID。

提交不携带 active pointer、Runtime activity 或 catalog active/archived 状态作为资格
前置条件。归档、恢复、Scope pointer 变化以及 Turn、Goal、Compaction 或观测状态变化不应
使同一 exact Thread 的已确认删除失效。Handler 只调用共享
`InstanceManagementService.delete_exact_binding`；它不先 activate、unarchive、Stop、
interrupt 或等待 terminal，也不直接访问 Runtime、Store 或 SDK。

删除继续完整采用 ADR 0037/0049 的 native-first、App Server descendant cascade、非取消
异常后一次四视图对账、Binding-local `lifecycle-unknown` 和绝不自动重发语义。删除当前
Binding 才清空 active pointer；删除 inactive Binding 不改变其他 pointer。

## 非目标

- 不增加多管理员、RBAC、Project ACL、TLS 或外部身份。
- 不开放 Side、Missing Binding、Project 目录或批量删除。
- 不增加 durable confirmation session、删除队列、第二个 Delete Adapter 或通用 RPC。
- 不改变飞书 `/delete`、`/sessions` 或 `/sessions archived` 的确认与权限语义。

## 后果

单一实例管理员可以从集中视图清理 active 或 archived 的原生会话，不必切换 Scope 或改变
会话状态。Admin credential 泄漏的永久数据损失半径随之扩大；该能力仍只适用于 ADR 0031
接受的单管理员、受信内网部署，暴露到不受信网络前必须先另立安全架构。
