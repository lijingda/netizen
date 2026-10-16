---
status: accepted
date: 2026-10-16
amends: 0017, 0028, 0039, 0078
related: 0049, 0069, 0070
---

# 归档恢复直接使用精确原生结果

归档恢复前的分页查询增加目录扫描开销，也无法防止查询后会话被恢复、删除或占用。
`/unarchive`、归档卡片和 Admin 的恢复操作保留 exact Scope/Binding/native 身份与本地
准入校验，直接执行一次公开 unarchive，再按结果执行一次 exact resume。目录只供展示，
不再作为这条恢复链的前置或失败对账；不增加 RPC adapter、状态缓存或自动重试。

## 原生结果与失败边界

固定 SDK/App Server 0.163.0 没有独立的“已经恢复”结果。只在以下三种结果后调用
`thread_resume(exact_id, include_turns=False)`，不传设置或权限 override：

- unarchive 正常返回同一 ID；
- 公开 `InvalidRequestError`、code `-32600`，完整 message 为
  `no archived rollout found for thread id <exact_id>`；
- 同类型及 code，完整 message 为 `thread <exact_id> already has an active writer`。

后两种拒绝由固定原生实现证明发生在 rollout 移动前，但不能分别证明“已经恢复”或
“被外部占用”。missing 与已经恢复都可能得到第一条，本连接已加载会话也可能得到第二条。
只有后续 resume 返回同一 ID，才可以继续登记订阅及本地切换。类型、code、完整消息、
exact ID 与调用阶段必须同时匹配；不检查异常链、不匹配关键词、不把其他 RPC 错误视为
无副作用。后续 SDK 升级须重新核验这些原生边界。
对应源码为固定 tag 的 [LocalThreadStore unarchive](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/thread-store/src/local/unarchive_thread.rs)
与 [App Server Thread processor](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server/src/request_processors/thread_processor.rs)。

resume 的已验证明确归档、缺失、writer 或关闭拒绝结束本次操作，释放本次 lifecycle
reservation，保留 Binding/current/context，不循环 unarchive/resume 或自动清除关联。
writer 拒绝说明本连接无法取得会话，不提供 owner 身份。若本次 unarchive 已成功，反馈
必须保留“已解除归档，但未完成恢复／切换”的部分结果；不能声称整个操作未执行，也不
保证外部没有随后再次改变归档状态。

unarchive 或 resume 已发出但超时、断连、取消、ID 不匹配或其他结果无法确认时，停止
后续步骤并保留目标 Binding-local lifecycle-unknown。原生成功而本地提交无法确认时也
保留已知订阅与这一局部隔离，推进 admission revision 使旧准备输入失效；本地事务可能
已提交，不能宣称 current 已回滚。跨 Binding 身份不变量损坏仍遵守其全局 fail-closed。
普通 `/resume` 的全局 unknown 边界不变；不能直接复用它的外层异常处理。

## 运行会话、订阅与消息边界

Scope 协调、目标 Binding 锁及 lifecycle reservation 继续保护身份与本地提交。
允许本进程已持有 exact handle 的普通 `RUNNING` 和 Goal `RUNNING` rejoin，并保留原
Turn/Goal handle、观察者及输入准入，不重新启动任务。Compaction、普通 `STOPPING`／
`OBSERVATION_UNAVAILABLE`、Goal `STARTING`／`PAUSING`／`EXTERNAL_ACTIVE`／`UNKNOWN`
继续在原生调用前拒绝。原生 cold resume 可继续 persisted active Goal 的既有边界不变。

原生 resume 成功后先保留 exact 订阅，再提交本地 current/context；unarchive 返回的
handle 不作为订阅依据。目标已是 current 时保留现有 Context Boundary 和 context revision，
防止旧卡把已推进的边界回退；真正切换时仍以 exact control/card message 设置新边界。
Admin 的仅恢复操作不改变 current/context。卡片更新失败不撤销原生成功或重发恢复；
本决定不改变传输去重，也不解决旧卡内容未更新的独立展示问题。

## 验证

行为测试覆盖正常恢复、已恢复 cold/loaded/running、missing、外部 writer、两步之间关闭
或再次归档、明确拒绝与未知结果、本地提交前后失败、执行 handle 保留、旧输入失效和
current/context 不被旧卡重置；断言链路没有 list/read 预检或自动重试。使用 disposable
Thread、隔离 Codex 状态与真实 bundled App Server 核验原生错误和 rejoin，再单独验收
飞书结果展示。隔离原生调用段的性能对照只证明省去分页的收益，不代表生产端到端耗时。
