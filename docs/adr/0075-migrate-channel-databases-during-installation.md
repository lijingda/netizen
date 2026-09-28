---
status: accepted
date: 2026-09-28
amends: 0034, 0057, 0059, 0068
related: 0074
---

# 在安装事务中维护 Channel 数据库的前向迁移

实例已经保存持续使用的会话、计划和默认配置，每次改表都要求人工转换已不适合作为
长期升级流程。从 schema v14 建立前向兼容基线：每次结构或持久化数据语义变化提供
显式、版本化的迁移，由 Published Release、Source Install 和 Admin Upgrade 共用的
安装器执行。此决定替代 ADR 0068 的“不维护历史数据库迁移”政策；不补建 v13 及更早
试验版的迁移，也不改变 ADR 0074 对旧服务名、安装布局及全局 Skills 的边界。

使用标准库 SQLite 和小型线性迁移注册表，保留 `schema_version` 作为数据库版本依据。
安装器按显式源／目标版本关系选择完整路径，不从应用版本号或动态结构 diff 猜测操作。
已经发布的迁移及其版本校验保持稳定，不依赖未来会变化的当前建表函数。新库直接创建
当前完整结构；Runtime 只接受当前完整 schema，不在服务启动时迁移，也不维持旧字段
兼容分支。当前 schema 仍为 v14，首次真实结构变化时才增加第一条正式迁移。

候选下载、依赖准备和只读迁移预检先于停服。停止本实例服务并确认 manager target 已
卸载后，安装器持有 lifetime lock 保存原始数据库恢复快照，再在一个 SQLite 事务中
执行完整路径；数据变更、版本推进及最终结构／完整性校验一起成功才提交。迁移不执行
外部副作用，不自行提交事务。安装锁继续覆盖完整操作，原本停止的服务升级后保持停止。

升级中断可能发生在数据库已提交、release 已切换或候选开始接收输入之后，单靠异常
回滚与 active/enabled 意图不能判定数据库是否可覆盖。安装事务须持久关联原始数据库
快照、旧服务定义、原 `current`／`previous`、源／目标 schema 和阶段。私有
`state/activation-recovery-<id>/manifest.json` 保存这些证据，activation intent v2 仅增加
该恢复记录 ID，保留原 release 与启停意图字段。重试沿用该恢复依据，不能把已迁移的
数据库重新当成升级前快照。恢复数据库的全部写入过程必须同时
满足 manager target 已卸载并持有 lifetime lock，不能检查锁后释放再写。

候选在开放任何输入边界前，必须持久写入恢复目录的 `admission` marker；写入失败则
不开放输入。该 marker 表示候选可能已经接受输入，是自动恢复升级前数据库的禁止边界，
不能以 ready 不存在推断未接收请求。安装器先确认服务退出并持有 lifetime lock 再判定：
marker 不存在时，启动失败仍可回滚旧库与旧 release；marker 存在时保留新库，重跑
exact 候选安装器才能向前完成原事务，拒绝直接改装其他版本。证据缺失或矛盾时保留
材料并明确报错。快照包含主数据库及 WAL 等文件，主文件 schema 正确不证明恢复完整。
实际还原时持锁将 `current` 暂指带 admission guard 的候选，记录 `restoring`，全部
文件复制并 fsync 后记录 `database_restored`，才恢复旧 release 与服务定义，防止
旧程序在半恢复状态接受输入。`restoring` 重试完整复制；`database_restored` 之后
不得再拷快照，避免丢弃旧服务恢复后新接收的数据。此边界也适用于同 schema 的候选
失败；完整回滚才报告 `rolled_back`，不能确认或恢复不完整仍报告 `recovery_required`。

首次引入该机制时，旧 release 尚无 admission hook。中断后若 `current` 仍指向原
release、尚未开始恢复写入，且只读校验确认数据库仍为原完整 schema，则保留该数据库，
不以快照覆盖旧服务可能在重启后写入的数据；将此选择持久记录为 `restoring_source`，
后续重试沿用。
卸载清除 intent 和程序，但保留恢复材料；没有 intent 引用的材料不自动认领或重放。

每次 schema 变化必须提交稳定的迁移与旧库夹具，覆盖关联数据保留、跨版本路径、
失败原子性、重复执行、新库等价性和关键中断阶段。安装器边界变更还须完成受影响的
Linux/macOS 隔离实机验收；`make check` 和替身测试不代替平台证据。迁移只解决
Channel 数据库前向升级，不承诺任意旧应用版本降级、在线混用不同 schema 或共享
Codex 状态的回滚。操作与验收细节见[部署手册](../deployment.md#数据库迁移与中断恢复验收)。
