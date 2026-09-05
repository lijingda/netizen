---
status: accepted
date: 2026-09-05
amends: 0053
related: 0014, 0027, 0047, 0052
---

# 合成 causal root task 的净文件行数

## 背景

ADR 0053 只读取父 Agent 当前 physical Turn 的 latest aggregate
`turn/diff/updated`。子 Agent 在独立 Thread/Turn 中先创建文件、父 Agent 再修改时，这份
snapshot 的左侧已经是子 Agent 的中间版本。例如子 Agent 新建 337 行、父 Agent 再产生
`+32 -27`，最终新增文件为 342 行；父 Turn snapshot 只能给出 `+32 -27`，不能表达整个
任务的 `+342 -0`。

Codex App Server 把 diff 定义为单个 Turn 内的 latest aggregate snapshot，并把子 Agent
表示为独立 Thread。公开 Python SDK 的 `TurnResult`/持久 Turn 不包含 diff，也不会把子
Turn diff 合入父 Turn；子 Turn 的 transient notification 在未注册队列收到
`turn/completed` 后还会被 SDK 丢弃。Codex CLI 同样只消费其目标 physical Turn；Codex App
默认 Changes 面板读取 checkout 状态，属于 workspace scope，不是 task attribution。
因此没有可复用的公开 root-task aggregate API，也不能把父子 `+/-` 数字直接相加。

## 决策

Netizen 不 fork Codex Core。服务在同一个 `AsyncCodex` 初始化后、任何 Turn 可以启动前，
安装一个 exact-version/fingerprint-gated `PinnedTaskDiffObserver`。它只在 SDK
`MessageRouter.route_notification()` 调用原 router 前同步复制以下语义事件：

- Thread/Turn started 与 Turn completed；
- `collabAgentToolCall` started/completed；
- completed `fileChange` 的 path；
- 每一次 `turn/diff/updated` snapshot。

wrapper 不消费、注册、删除或改写 SDK 队列；投影失败仍把完全相同的 notification 对象调用
原 router 恰好一次。单事件在遍历 changes/receivers 前先受数量和字符串上限约束；transport
failure、ring buffer 溢出或 pinned contract 不匹配都使 capture 不可用。生产门禁失败时注入
显式 unavailable observer，不能退回父 Turn 数字。

每次 Ordinary Turn 在 native `thread.turn()` 前建立 capture，完成并确认 terminal 后收口。
每次 Goal start/resume 同样在原生 mutation 前建立 capture；一个 Goal run 纳入 capture 中
直到 exact final physical Turn 为止的所有父 Thread physical Turns。两者都用
`collabAgentToolCall.senderThreadId/receiverThreadIds`、`Thread.parentThreadId` 和完整 Turn
lifecycle 建立因果树。复用 child Thread 的新 Turn 只有再次被本任务的 collab call 唯一触发
时才纳入；在 pinned 版本中 `spawnAgent` 与 `sendInput` 触发 Turn，`resumeAgent` 只恢复
Thread、不会独自认领后续 Turn。多解、漏 claim 或未完成 child 一律不可用。

`TaskDiffComposer` 不相加 snapshot。每个成功 `fileChange` 必须严格跟随一份非空 aggregate
snapshot，且每一份 snapshot 都参与 OID 证明：同一 `(Turn,path)` 第一次产生
`aggregate.old → current.new` checkpoint，之后产生 `previous-current → current.new`
checkpoint。每个 path 的全部 checkpoint 必须形成覆盖完整、恰有一个 source/sink、无分叉或
循环的简单链；因此同 Turn 连续 Add→Update 可用，而 `root A→B / child B→A / root A→D`
会暴露分叉，不能被 root 最终的 `A→D` snapshot 隐藏。跨 Thread notification 到达顺序不作为
commit 顺序。

完成时只读取 touched path 的最终 regular UTF-8 文件或确认 absence，并要求其 Git blob OID
等于 checkpoint 链尾。中间 snapshot 只保留 OID；每个 Turn 的最终 snapshot 另形成内容 edge，
其 source/sink 必须与 checkpoint 链一致。随后从 final anchor 沿最终 edges 反向应用 hunk并
校验两端 OID，得到 baseline 后运行有界 Myers，按 Codex `similar` 的 CR/LF/CRLF 语义重算
`+N -M`。实现不缓存历史文件版本。

capture 内非因果 Turn 触及同一路径、证据缺失或格式不支持时，整个 task summary 不可用。
事件、diff、path、文件和计算均有 task 级固定上限；重型 composer 使用非阻塞单并发闸门，
Runtime 只在短 metadata deadline 内等待。繁忙或超时只省略统计。

Ordinary Turn 只有发现至少一个 causal descendant Turn 时，verified task summary 才覆盖
ADR 0053 的 physical-Turn summary；Goal start/resume run 在发生 physical-Turn rollover 时也
合成多个 root Turn。只有一个 root physical Turn 且没有 descendant 时继续复用原 exact
physical Turn parser。任一证据缺失或格式不支持时使用空 summary，Files 仍可从 final Turn
的 structured items 展示文件，
但不显示无法证明的 diff path 或数字。summary 和原 diff 一样只活到 completion/card manifest，
不持久化原始 diff 或文件内容。Side 不安装 capture，语义不变。

## 明确边界

事实边界就是 pinned Codex/App Server 暴露的 completed `fileChange` 与
`turn/diff/updated`；Netizen 不补充识别其他写入来源。只支持单一 local environment；遇到
rename、binary、空/缺失 snapshot、snapshot path 消失、非 UTF-8、symlink/special file、
事件丢失、繁忙、超时或资源越界时省略统计。Adapter 不承诺重启 replay 或每个任务都有数字。

这里的 fail-closed 只覆盖可观察到的证据缺口。若 App Server 在没有 gap marker 的情况下把
一整条 collab/lifecycle/diff 分支都丢失，客户端无法区分“没有 child”与“child 的全部证据均
不可见”；薄 Adapter 不能对此给出绝对证明。当前 pinned 单连接 reader 与有界同步投影降低了
该风险，但没有消除协议层的不确定性。

断线恢复、跨环境或强制可用需要 Codex Core/App Server 提供持久 root-task tracker；Netizen
不维护 Codex fork，也不以 checkout-wide `git diff` 冒充 task attribution。

## 验证

单元测试覆盖同一 child Turn Add→Update 后的 337→342 主案例、被 final snapshot 隐藏的跨
Turn re-entry、notification 重排、Goal 多 physical Turn、证据缺失、final anchor、安全文件
读取、task 级资源上限、composer 并发闸门、终态 deadline 和 CR/LF-only 行语义。
`scripts/probe_sdk_task_diff.py` 使用真实 pinned Python SDK 连接 fake App
Server，证明 pre-router tap 保留随后被 SDK 丢弃的 child notification、原 root stream 仍可
正常排空，并以两次 child patch 与一次 parent patch 得到 `+342 -0`；它同时进入 `make check`
与安装期 host qualification。
