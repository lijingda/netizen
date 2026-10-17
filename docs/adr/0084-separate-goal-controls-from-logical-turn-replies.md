---
status: accepted
date: 2026-10-17
amends: 0014, 0047, 0069, 0079
related: 0063, 0071
---

# Goal 管理与逻辑 Turn 回复分离

旧设计把 Goal 控制与任务答案放在同一张持续更新的卡片上，为旧按钮保留回答快照和
运行所有权，增加内存积累与控制重绘覆盖答案的风险。原生 Goal 的暂停/清除并不停止
当前 Turn；Python SDK 又把自动续跑的多个物理 Turn 合并成一个逻辑流。因此将管理与
回复分开，同时保留 SDK 已提供的逻辑执行单位。

## 管理与执行

`/goal` 只发送静态管理快照，显示 objective、状态、tokens 和可用预算。刷新与控制按钮
按 exact Binding/native Thread 查询当前 Goal，再执行与命令相同的路径，不自动刷新卡片，
不包含任务答案或文件。回调携带 Thread、原生 createdAt、objective、budget 的稳定标记，
操作前与原生状态核对；不为历史卡保存 owner、运行投影或快照表。
管理卡的标记跨进程重启保持稳定。2026-10-18 明确不兼容升级前用于测试的混合
Goal/答案卡；不为这些旧卡增加专用指纹、只读回放或拒绝后的源卡保留机制。
原生 createdAt 秒级精度下，同秒完全相同 Goal 的重建无法严格区分，这一有限边界接受，
不新增 Goal 唯一 ID 或本地 generation 持久化；原生 clear 无 expected-generation CAS，
仍不能原子防止外部客户端在最后一次读取后替换 Goal。

`/goal pause` 仅把 Goal 暂停，不中断当前 Turn。`/goal clear` 可以清除执行中的 Goal，
当前 Turn 继续并正常交付结果。`/stop` 保留停止当前执行的含义：暂停后续 Goal 自动
续跑，再中断 exact 当前物理 Turn 并执行既有 terminal cleanup。原生 mutation 结果
未知仍保留已有局部/全局隔离，展示失败不改变执行结果。固定原生 Goal set 的
`InvalidRequestError/-32600` 且完整消息为 `cannot update goal for thread <exact_id>: no goal exists`
是写入前明确拒绝，暂停/执行中恢复据此返回未执行，不关闭实例准入；其他错误不据此泛化。

暂停后当前逻辑流仍安全运行时，resume 复用现有 operation，不重复注册通知 route，也不
等待一个必然不存在的新 started 事件。旧逻辑流结束后的 resume 才注册新的 operation
和回复生命周期。观测缺失、外部 active 与 rollover 竞争继续明确处理，不增加 watchdog、
第二消费者或自动接管。成功 complete 仍自动 clear，不保留已完成 Goal。

## 回复与终态

Goal 的一个 SDK logical stream 按普通任务复用 Activity、阶段性答案、终态、Files、
富文本与结束提及机制；关闭过程卡时不强制创建 Goal 组合回复卡。不为每个物理 Turn
单独发卡。管理卡不作为回复容器，控制不会回放旧答案或覆盖其后生成的内容。

唯一 Goal Tap 和 current/final physical Turn 身份继续保留：logical ID 是第一物理 Turn，
不能作为最终历史或 Files 身份。自动 rollover 清空本轮 Activity，阶段性答案在同一逻辑
执行中累计；Result/Files 只取已核验的最终物理 Turn。原有四项终态证据仍适用；本进程
已确认的显式 clear 允许 Goal absent，不能因此丢弃仍在执行的 Turn 及最终回复。
非 unknown slot 继续只保留到有界终态交付结束。终态后不保留 Goal 回答 projection/session；
普通自包含文件 manifest、活动期间的必要状态与原生历史归属不变。

## 验证

行为测试覆盖 pause/clear 不 interrupt、stop 的暂停加中断、同流 resume、流结束后
resume、当前管理卡标记核验、逻辑多轮阶段性答案、最终物理 Files 身份和展示失败不改原生
结果。真实 bundled App Server 探针核验控制与物理执行关系及自动续跑；静态卡/富文本
实际展示需单独飞书验收。SDK 自动 interrupted continuation 的既有观测缺口不由本决定
修复，不把 synthetic 或本地 mock provider 结果宣称为完整真实模型/飞书验收。
