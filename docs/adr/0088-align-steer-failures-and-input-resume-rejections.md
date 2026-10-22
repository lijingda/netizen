---
status: accepted
date: 2026-10-22
amends: 0069
related: 0021, 0078, 0081
---

# 统一追加输入失败与普通消息的恢复拒绝

同一次 exact-Turn steer 的一般 RPC 错误，原先在普通消息中只反馈失败，在 Side 和
Goal 中却关闭整个实例。统一沿用普通消息的处理：保留已有执行和观察链，报告本次追加
未确认，不自动重试、不登记成功回执、不推进上下文边界；单次追加失败不再单独改变
Side/Goal 生命周期或关闭其他会话准入。

这不保证追加未生效，用户应先查看状态再决定下一步。只有成功结果才增加普通/Side
steer 计数和计划过期标记；Goal 继续要求匹配捕获的 physical Turn receipt。原生
InvalidRequest 仍按现有规则报告本条输入被拒绝。取消等待仍沿用全局关闭，Side 进入
closing、Goal 保留 unknown；Goal receipt 畸形或返回错误 physical Turn 也仍保留
unknown 并关闭准入。共享的只是一次调用的错误解释，输入准入、身份、换轮和生命周期
归属不变，不增加消费者、重试或后台对账。

普通消息（含 Skill 准备）和 Agent 跨会话入口复用 ADR 0078 的 exact resume 拒绝
分类。原生明确拒绝归档、不存在、关闭中或占用中的目标时，保留 Binding，只拒绝本次
操作；不能把已验证的拒绝再包装成全局恢复结果未知。其余 resume 错误和错误返回身份
继续沿用原来的未知结果保护，不自动新建替代 Thread 或清理绑定。

本决定仅修订 ADR 0069 中“一般 steer 响应丢失就关闭准入”的部分；Goal
start/resume/pause/clear、Side fork、compaction、archive/delete 等操作的过程策略
不变。行为验证区分成功、明确拒绝、一般 RPC 错误、取消、Goal 换轮和错误 receipt，
并检查失败时的零成功记账、Binding 保留及其他会话准入；原生兼容性仍按部署手册触发。
