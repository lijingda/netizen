---
status: accepted
date: 2026-09-07
amends: 0031, 0054
related: 0034, 0037, 0046, 0049
---

# 按精确会话清单删除 Project

Admin 已能跨 Scope 删除单个普通会话，但清理整个 Project 仍需逐个查找，并容易遗漏归档
会话。实例管理员现在可以确认删除一个 Project 及其完整关联 Sessions；该操作复用既有
单会话生命周期边界，不取得磁盘目录所有权，也不引入通用批量 mutation 或持久任务队列。

## 确认与执行

Projects 页提供“删除 Project 及关联 Sessions”。确认窗口显示 Project alias、关联普通
会话数量、Side 数量与永久后果，说明原生 Thread、spawned descendants、Codex App/CLI
历史和 Binding 会被删除，磁盘代码目录保留。清单来自 Project 的全部 Binding 和关联
Side route，包含 Lazy、active、archived 与 missing，不受 Sessions 页筛选影响。当前 Runtime
仍拥有但 Parent Binding 已被单独删除的 Side 也按其 Project identity 纳入清单；不把
“无法 join 到 Binding”视为 Side 已关闭，不为此增加持久 Project/Side 关系。

一次性、session-bound 的 action/CSRF grant 固定 Project alias/revision 和关联清单
fingerprint。清单包含 exact Scope/Binding/native ID，以及 Side 的 app/chat/topic/root/
parent identity；配置、current pointer、native active/archived 和 Side 终态转换不属于
identity。新增会话、Lazy materialization 或 Side topic/root identity 改变后，必须刷新并
重新确认。单次清单最多 1000 个 Binding 和 1000 条 Side route，超限明确拒绝，不截断。

提交在短 Store transaction 中重读并比较 revision/fingerprint，停用 Project、递增
revision，并在同一 Store 的进程内保留删除 intent。intent 阻止这个 Project 的新 Binding、
新 Side 和重新启用；它不是 Project execution lock，不串行化其他 Binding 的 Turn，也不
阻止其他 Project 的管理操作。事务后不持有 Project/Scope/Binding lock 等待原生 I/O。

Application service 先关闭关联 Side，再逐个删除普通会话。Side 复用 exact close，保留
永久 route 墓碑；与已经进入创建流程的 Side 通过 Parent Binding lock 交接，避免在 fork
尚未登记时误判“没有 Side”。普通 materialized 会话继续使用 ADR 0037/0049 的
native-first exact delete：在 exact Binding 上预约生命周期 intent 后释放锁，由 App
Server shutdown 并级联 descendants，不先 interrupt、cleanup 或等待 idle。Lazy 只在
exact native ID 仍为空且既有安全条件成立时删除本地 Binding。
Missing 投影也进入同一共享 delete/reconciliation primitive，只有原生成功或既有四视图
证明 exact Thread 全部 absent 才可删除 Binding；不能按清单中的 Missing 标签直接清理本地行。

Native fork 交接完成不代表飞书 root/seed 发布完成。清单中仍为 `creating` 的 Side 不得被
级联操作提前关闭或标成终态；本次删除保留停用 Project，报告 `side_creation_in_progress`，
让已进入 Channel 的发布或补偿完成，再重新预览和确认。否则晚到的 root/topic 将失去
永久路由证据。这是创建副作用的精确边界，不是普通持久 Thread 删除的 idle 前置条件。

每个对象最多等待 30 秒，整次请求最多 120 秒，不增加后台续跑器。已发出的原生 mutation
沿用既有取消与四视图对账语义，不能因 HTTP 超时或断线把副作用视为撤销。无法证明原生
删除、Side close 失败、部分删除失败、结果未知或取消都保留 Project 和剩余项，
且 Project 保持停用。已确认删除的会话不会回滚；结果必须区分已完成和剩余项，未知仅保留
在 exact Binding/Side 边界，不关闭全实例 admission。重启或再次打开页面不会自动续删，
后续尝试必须重新取得当前清单并明确确认。

只有所有关联 Binding 都已消失，且原清单全部 Side 均已终态、Runtime Side
交接也已完成，才把 Project 标为已删除。检查原 Parent IDs，不依赖仍能从 Binding join
出来的 route；并发单会话删除不能使存活 Side 从完成检查中消失。current pointer 继续
由单会话 primitive 精确清空，不自动选择其他会话。

## Project 墓碑与迁移

直接移除 Registry 行会让下次 YAML bootstrap 把 Project 重新导入，因此 schema v8 在
现有 `projects` 表增加 `deleted` 标记，保留 alias/cwd/revision 等原有 metadata。已删除
Project 从 resolve、list、聚合和选项中隐藏；bootstrap 识别墓碑，不重新验证旧 cwd、不
恢复或重新启用。只有显式登记可以复用 alias，并继续递增 revision，避免旧页面对同名
新 Project 生效。删除与复用均不删除、复制或清空原 cwd。

服务仍只接受当前 schema。安装器在已卸载服务目标、取得 lifetime lock 和数据库快照的
同一 activation/rollback transaction 内执行 v7→v8；v6 先完成原 v6→v7 的关闭 Task
Feedback 默认值迁移，再进入 v8。保留全部 Scope/Binding/Project/Dedup/Side 行，旧 Project
默认 `deleted=0`；失败恢复原数据库与 release，不重建空库。删除 intent、确认清单、
fingerprint、执行进度和结果均不持久化到 Channel SQLite。

## 边界与验证

这是 Admin 的 Project 级删除出口；飞书 `/settings` 仍只登记、创建、启停 Project，
Sessions 筛选多选也不成为任意批量 archive/stop/delete。目录删除、Side 墓碑删除、
新 SDK Adapter、自动重发和定时重试仍不开放。

自动化验证覆盖 exact 清单陈旧、并发确认、创建/启用栅栏、Side 创建交接、active/archived/
Lazy 删除、失败/取消后剩余项、墓碑隐藏、YAML 重启、alias 复用 revision、cwd 保留，以及
v6/v7 升级与原字节回滚。相关 Admin 边界变更还需按部署文档执行跨主机浏览器与 disposable
native lifecycle 验收；合成测试不能替代真实 SDK/平台结论。
