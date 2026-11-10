---
status: accepted
date: 2026-11-10
amends: 0078
related: 0037, 0049, 0084
---

# 删除明确拒绝时保留原执行观测

Codex 0.167 的 `thread/delete` 可在 shutdown 前因 attached Thread 拒绝，也可在
shutdown 后因持久分支历史引用拒绝。明确 RPC 错误只能证明本次删除未成功，不能证明
原执行已停止。ADR 0078 的统一丢弃活动策略会让仍在运行的任务失去终态交付和停止入口。

明确 `ThreadDeleteRejected` 时保留 Binding，只释放本次 exact lifecycle intent，
向用户返回经过既有安全过滤的原生原因；飞书与 Admin 使用同一错误解释器。保留现有
Turn、Goal、Compaction 的观察者、handle 和订阅，不发送 discard，不主动停止、恢复、
重启或重建任务，也不按错误文案猜测 shutdown 是否发生。释放 intent 只恢复当前仍存活
owner 的 Activity 观测，不能把删除在途期间已完成的执行重新放回运行态。

执行状态继续由原观察链确认：原生仍运行就继续观察，确认终态就按原有规则收尾；若
无法确认则沿用该执行类型的观测未知保护，不因删除被拒就宣布空闲或伪造终态。用户后续
对话、停止、重试删除仍走原准入与精确身份校验。与主动重建监听相比，这避免新增恢复
状态机，也保留普通 Turn 有界恢复和 Goal 终态证明的职责边界。

删除成功仍清理活动并提交 Binding 删除；超时、断连、取消、不可验证响应及本地提交
失败仍按已有 Binding-local lifecycle-unknown 处理。删除失败不新增 read/list 对账或
自动重试；原执行观察链已有的读取与有界恢复不属于删除对账。

验证覆盖运行中拒绝、原生停止后拒绝、完成竞态、Goal/Compaction、错误原因安全展示，
并通过真实 bundled App Server 与隔离 mock provider 复核原生拒绝顺序；真实飞书客户端
和部署验收单独记录。
