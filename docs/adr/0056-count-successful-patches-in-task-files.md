---
status: accepted
date: 2026-09-05
amends: 0024, 0027, 0048, 0053
related: 0047, 0052
---

# 按成功 patch 累计本轮文件行数

Files 用来展示本轮修改规模。借鉴 Codex App 完成卡的成功 `fileChange` 回退路径，
Netizen 改为累计 patch 的新增/删除行数，不重建任务开始时的文件，也不承诺父子任务的
最终净 diff。同一行修改后改回原文仍计 `+2 -2`；新增 337 行再修改 `+32 -27` 得到
`+369 -27`，而非最终净差异 `+342 -0`。原生 aggregate diff 只补充文件发现，绝不与
patch 数字叠加；工作区 Git diff 会混入并发任务修改，也不作为统计事实源。

Ordinary、Side 和 Goal 的 exact 最终成功 physical Turn 共用一条 completion 展示路径。
只纳入该 Turn 及其本轮新 spawn 的递归子任务；不聚合 Goal 更早 physical Turn。成功的
v1 `spawnAgent` 和 v2 `started` 建立候选关系，公开 `AsyncThread.read()` 返回的原生
parent 再确认归属，并排除祖先继承的 Turn IDs，按 `(thread, turn, item)` 去重。
这是 Netizen 对子任务展示的补充，不声称复制了 App 的父子任务净 diff 合成。

子任务读取总预算为 3 秒、最多 32 个子任务，只做 completion 时的只读快照，不等待
子任务终态、不 resume，也不改变执行生命周期。旧子任务的交互只有 Thread 引用而没有
本轮 child Turn 归属时，不猜测历史；读失败、运行中、身份异常或超限同样使总计未知，
已经独立验证的逐文件数字仍可展示。这不是整个任务树的原子快照。
读取前后都确认根 Thread 的最新 Turn 仍为本轮；新一轮开始、收尾校验失败或总超时则
丢弃子任务快照，只保留已经冻结的根 Turn 记录，不为展示延长原生执行槽的占用。

SDK 0.154.0 的 `sendMessage`、`followupTask`、v2 `interacted`/`completed` 仅建立
Thread 引用，不从事件 ID 猜 child Turn。完成遍历后，本轮新子任务和前后核验仍为本轮
的 root 都可解析为已知引用；不重复导入 root patch。`listAgents`、`interruptAgent` 和
`closeAgent` 不建立工作归属边。工作调用状态为 `interrupted` 时无法证明已做的工作，
总计仍为未知；纯列表或控制操作不因此导入旧历史或制造空 receiver 错误。
新版 v2 mailbox `wait` 也没有工作目标：仅当 completed、sender 为当前 parent，且
receivers 与 agents states 都为空时忽略其归属边。有 receivers 的 wait 仍保留引用语义，
其他空目标或矛盾身份继续使总计未知。

`0.154.0` 中继承上下文的 child，其公开分页 Turn 列表可以只含自己的 Turn；该列表
不能证明模型上下文是否完整。来源仍按原生关系核验，公开历史若包含祖先 Turn，继续
按祖先 Turn IDs 排除。对应 live probe 验证来源与祖先 patch 排除，不声称证明了完整
模型上下文的继承；已有 synthetic 排除覆盖保留。

成功 add/delete 的完整正文按 LF 计行，update 只累计完整可验证的 hunks，识别固定 SDK
的 `Moved to:` 后缀以归入目标路径。路径按所属 Thread cwd 解析为规范 absolute path，
Project 不构成过滤边界。缺失或畸形 patch 只使对应文件数字及总计未知，不使其他文件
数字失效；binary、图片不伪造数字。已删除文件的有效文本修改仍进入总计，即使不能发送
该文件。归属明确且证据完整的新建子任务正常计入总计。
逐文件数字按 patch 的目标路径累计，不追踪重命名前后的文件身份：先新增旧路径再纯
rename 时，总计包含新增，目标路径自身可以是 `+0 -0`。

代价是数字可能高于最终净变化，且旧子任务交互或未完成子任务不能给出完整总计。作为
交换，不需要内容重建、Git 对象链或全局修改追踪；不新增 observer、私有 SDK reach-through、
数据库记录、Project 锁或跨任务执行限制。ADR 0053 的显示结构、自包含 `a/d` manifest
和容量边界保持不变。

`make check` 覆盖累计及撤销、add/delete、rename、缺失 patch、跨目录、递归子任务归属、
继承历史排除、去重和有界读取降级。改变固定 SDK 的 child/fileChange 合同后，按
`docs/deployment.md` 在可用的已登录环境验证真实子任务；不能用 synthetic 结果声称
已经通过 live 验证。
