---
status: accepted
date: 2026-09-22
amends: 0014
related: 0039, 0047, 0052
---

# 将 Goal 中的普通消息 steer 到捕获的当前物理 Turn

Goal 的续跑和物理 Turn 切换已经由 SDK 管理；原生 `turn/steer` 接受 exact
`expectedTurnId`，不禁止正在执行 Goal 的普通物理 Turn。原先一律拒绝 Goal 期间
的普通消息是产品接入缺口，不需要用另一套续跑机制或消息队列补齐。

复用普通 Prompt 的准备、Skill 校验、上下文 cursor commit 和接收回执。准备前
捕获 Binding/revisions、exact Thread 和 SDK route 当前物理 Turn；Goal 启动、
恢复和释放沿用现有 revision 隔离，不新增 logical run admission 字段。提交时
重新校验，向捕获的 `expectedTurnId` steer 一次。准备期间 A 换为 B、换轮间隙、
暂停或终态都明确拒绝，不改投 B、不排队、不启动新的 Turn。校验后再换轮由原生
expected-ID 契约拒绝；SDK rollover 不更新 Binding revision，因此身份变化是
正常的 `SteerRace`，不能误判成进程内部损坏而关闭其他 Binding。

只接受本进程安全 route 上的 RUNNING Goal。starting、pausing、unknown、外部
active、logical stream 已结束或最终 handoff 中均不接收消息。`/config`、
`/compact` 和第二个 Goal 仍拒绝；四项完成证明、最终物理 Turn 的 Result/Files
和原 Goal 投递来源保持不变。“暂停一下”作为用户消息交给模型理解；不增加
自然语言控制解析器，确定的控制入口仍是 `/goal pause` 或 `/stop`。

在已有 Goal adapter 内新增窄 `steer(input, expected_turn_id)`，复用同一 SDK
client 的 typed `turn_steer`、生成的 receipt 类型，以及普通公开 handle 使用的
SDK 输入归一化/序列化函数。私有依赖只留在这个 adapter，并纳入 capability shape
和真实 SDK synthetic/live 门禁；不复制输入 schema，也不临时构造会注册额外
订阅的 `AsyncTurnHandle`。公开 facade 将来提供不额外占用通知所有权的 Goal
exact-turn steer 后，以相同契约替换此方法并移除这部分私有依赖。

只有匹配目标 Turn 的成功 receipt 才推进 catch-up cursor 与显示 steer 成功。
原生 InvalidRequest 明确拒绝；响应丢失、错误 receipt 或取消等待无法证明请求
未生效，保留 Goal unknown 并关闭 admission，不重试 mutation。Goal Activity
沿用唯一 logical stream，不附加调整次数或动态 checklist freshness 状态；
清单固定标注为最近一次原生上报，可能尚未反映追加消息。每条成功追加仍有接收回执。

SDK/CLI 固定到 `0.155.1`。本次不统一普通 Turn/Side/Goal 的事件消费，也不接入
ExternalMessage。候选 `AsyncTurnHandle.stream()` 仍以 `asyncio.to_thread`
阻塞等待通知，与 steer、interrupt、read 共享默认 executor；统一消费未消除
恢复/终态校验和所有权差异，暂不足以抵消线程池风险与迁移成本。

验证覆盖 typed 输入、准备及 RPC 期间 rollover、无 route/终态拒绝、取消与未知
receipt、成功后的 cursor commit 和唯一消费者；升级仍须
通过 `make check` 与部署文档要求的原生兼容性探针，不能以 shape 通过代替 live。
