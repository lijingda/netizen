---
status: accepted
date: 2026-09-23
amends: 0039, 0061, 0063, 0068
related: 0029, 0046, 0051, 0060, 0069
---

# 定时向原会话提交普通输入

用户需要“十分钟后在这里继续检查”的原会话续跑。Scheduled Plan 增加执行目标：
`new_topic` 保持每次新建普通话题与持久会话；`binding` 固定创建时选择的 exact
Binding。两者共用时间规则、唯一 Scheduler、管理服务和 Runtime。原会话模式采用
普通输入的 start/steer 语义，避免新增等待空闲队列、历史快照或另一套执行配置。

## 原会话输入与反馈

原会话计划保存 exact Binding 引用，从该 Binding 取得 Project、Scope 与当前配置；
不保存独立的模型、反馈或上下文配置。自然语言使用原生调用身份解析当前普通 Binding，
管理入口也可以明确选择 exact Binding；创建后执行目标不可编辑为另一会话。
Side 和无普通 Binding 的原生子任务不能隐式成为目标。

`/cron`、Admin 和 MCP 共用管理与执行资格规则：非当前目标仍可增删改查，旧表单
保留明确选定的 Binding，不随会话切换转投。只有自动触发或立即运行时要求目标是其
Scope 的 current Binding；管理界面消费公共资格投影，实际执行仍在认领与提交时复核。

每次触发先在原 Scope 发送真实机器人锚点消息，验证 exact chat/topic/message
身份后，沿共同的消息准备、准入、start/steer 和展示流程消费计划指令。锚点发送失败或
结果未知时本次不调用原生执行。定时输入保持显式 Scheduled Plan 系统来源；锚点的
机器人作者不是请求发起人，不伪造飞书入站事件，也不依靠机器人消息回流触发消费。
计划指令作为工作输入，不进入 slash control 解析。

新 Turn 使用原 Binding 当前配置，反馈与完成回复锚定触发消息；steer 保留原 Turn
或 Goal 的模型、过程卡、完成来源、发起人和结束提及，只给追加消息普通 steer 回执。
系统输入新建的 Turn 不产生结束 @，不从计划创建者、锚点机器人或最近参与者猜测对象。
表情、过程卡、Files、结果、准备及执行异常复用普通消息机制；反馈失败不改写执行。

`catch-up` 沿用原 Binding 的范围、过滤、资源限制、exact admission 与 cursor CAS。
本次真实触发消息作为 upper anchor，成功接受输入后才推进 Context Boundary；历史
仍是 inert supplemental context。普通真人消息的 @ 准入保持不变。读取期间发生
切换、配置变更、Turn/Goal rollover 等竞态仍明确拒绝，不改投其他 Binding 或 Turn。
stopping、compacting、unknown、外部活跃等情形继续遵守普通 Runtime 的准入规则。

## 一次触发的交接与不确定性

原会话 Scheduled Run 描述一次输入交接，记录 exact Binding、已确认接收的物理
Turn 和 `started/steered` disposition。成功代表“已启动新一轮”或“已追加当前任务”，
不代表业务任务完成；多个 Run 可以指向同一 Turn，最终结果仍只由普通消费者交付。
Goal 的物理 Turn 结束或 rollover 不代表 Goal 逻辑完成，保留既有四证明契约。

原会话计划的 barrier 只保护在途交接。明确接收、明确拒绝或结果未知都结束本次交接
占用；未知记录保留，但不阻塞下一到期点，也不重发本次输入。服务重启不重放在途输入。
不能以目标 Turn 后来完成推断某次 steer 已被接受。新话题模式仍使用原先等待 initial
Turn 终态的 barrier，不因这项扩展改变。

Runtime 自身因未知副作用关闭 admission 的规则不变：此时后续定时输入与用户输入
一样被拒绝。计划层不额外阻塞下一次，不等于绕过原生故障边界或自动恢复 Runtime。

## 生命周期与持久化

计划 `enabled` 只保存用户启停意愿。原 Binding 不再是 Scope current、已归档或
不可用时，原会话计划自动暂停；切回或恢复后重新满足执行条件，不改写手动暂停意愿。
暂停期间错过的到期点不补跑，包括尚在普通迟到宽限内的旧到期点；一次性时间已过则
结束。手动“立即运行”可用于手动暂停的计划，但不能绕过目标会话或 Runtime 准入。
Binding 删除确认提交时一并删除关联计划定义；原生删除未确认时不提前删除计划。
Project/App 边界及已接受原生任务的生命周期保持既有规则。

当前 schema 升版，Plan/Run 增加执行目标及最小输入交接证据；放宽仅适用于新话题模式
的 Binding/topic 唯一约束。不保存第二份 native Thread 映射、发送者投影、输入历史、
响应或任务终态。继续只接受当前完整 schema，不增加旧库自动迁移或自动重建。

## 实施与验证

实施顺序与验证清单见[实施计划](../session-scheduling-plan.md)。代码门禁为 `make check`；
受影响的真实 MCP、普通输入交接、飞书主线/话题及 catch-up、生命周期和安装 schema
验收按部署文档分别记录。接受本决定不代表功能已经实现、安装或完成真实兼容性验证。
