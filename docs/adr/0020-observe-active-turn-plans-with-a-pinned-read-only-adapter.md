---
status: accepted
date: 2026-08-13
amends: 0008
related: 0009, 0010, 0014
amended_by: 0026, 0052
---

# 用精确锁定的只读 Adapter 观察运行中 Turn 的原生 Plan

## 背景

产品需要让用户主动通过 `/status` 查看当前任务 checklist，而不是由机器人定时发送
进度消息。[官方 App Server 文档](https://developers.openai.com/codex/app-server/)
说明 `turn/plan/updated` 会携带当前 Turn 的完整 plan，步骤状态为
`pending`、`inProgress` 或 `completed`。真实探针确认，固定
`openai-codex==0.144.4` 能在运行中收到该通知，但完成后的公开
`thread.read(include_turns=True)` 不保留 plan。

当前 Python SDK 也没有公开的全局异步通知订阅或只读 plan 快照。为每个 Turn 常驻调用
`AsyncTurnHandle.stream()` 会通过 `asyncio.to_thread` 长期占用一个 executor worker，
还会改变 Netizen 现有的通知消费时机。另起 App Server、复制协议或直接消费私有队列
都会破坏单一 SDK 运行时和终态恢复边界。

## 决定

增加一个临时的 `PinnedTurnPlanObserver`，只允许读取同一个 `AsyncCodex` 已经为当前
Turn 登记的通知队列：

1. Adapter 精确锁定 SDK 版本和整个 Python 源码包指纹，并校验
   `AsyncCodex -> AsyncCodexClient -> CodexClient -> MessageRouter -> Queue` 的对象
   持有关系、类型和必要字段。任一不符时该展示能力 unavailable，但服务和 Turn 继续
   工作；若同一 SDK 变化同时触发 ADR 0009 的独立 service-wide cleanup 门禁，仍按
   ADR 0009 拒绝启动，不能由本展示能力放宽。
2. Adapter 不调用 RPC，不启动第二个客户端、进程、线程、async task 或通知消费者；
   不注册/注销 Turn route，也不对队列调用 `get`、`put` 或修改操作。
3. 只有 `/status` 读取或 steer 的 freshness bookkeeping 会触发同步快照；steer 前的
   快照只建立 cursor，count/stale mutation 仍必须等 native steer 成功。快照按
   SDK router lock -> exact Queue mutex 的顺序复制从内存 cursor 之后的引用，保持队列
   长度、顺序和对象身份不变。cursor 和投影只存在于 `_ActiveTurn` 内存中。
4. 只接受 method 为 `turn/plan/updated`、payload 为固定 generated type，且
   `threadId + turnId` 与当前 exact Turn 同时一致的事件。其他 Turn 的事件跳过；当前
   Turn 的 payload 或 step 形状异常使本次展示 unavailable。每个有效事件都是完整
   plan，Runtime 整体替换旧 checklist，不做增量拼接。
5. 成功 native steer 后才增加 steer count 并把已有 plan 标为可能过期。以 steer 请求
   前已经观察到的 queue cursor 为边界；之后的首个 exact plan event 整体替换 plan 并
   清除过期标记。steer 失败不改变计数或 freshness。
6. `thread.read()` 继续是 Turn 终态的唯一权威来源。`terminal_observed` 后不再展示或
   快照 plan；现有的终态后公开 `handle.stream()` usage drain 保持原样。观察失败不能
   阻塞、终止或改变 Turn、steer、`/stop`、completion 和最终回复。
7. Channel 只在 `/status` 中渲染当前 active Turn，限制步骤数量和单步长度；不展示
   推理、工具日志、百分比或 ETA，也不把 plan、cursor、steer count 写入 SQLite。

## 验证与发布门禁

仓库必须保留 synthetic contract，证明：

- 版本、源码指纹、持有关系或 queue shape 变化均 fail closed；
- snapshot 前后队列长度、顺序和对象身份完全相同；
- exact ID 校验、完整 plan replacement、step status 映射和并发 Turn 隔离；
- 成功/失败 steer 的 count 与 stale 语义正确；
- plan 观察异常只影响 `/status` 展示，公开终态与 usage drain 仍能消费原队列。

SDK/App Server 升级必须先运行 synthetic probe 和目标环境 live probe。live probe 至少
观察一轮 plan update、一次 steer 后的 freshness 更新、最终 completion，以及同一
Turn 的公开 usage drain。未通过时关闭 checklist 展示，不能放宽类型或队列门禁。

## 后果与移除触发器

这是一个比普通 facade-gap 更严格的只读兼容债务：它接触 SDK 私有内存，但不发送请求
也不夺取通知所有权。强制进程退出时内存投影自然丢失，不需要恢复。

一旦官方 Python SDK 提供不占用每 Turn 常驻 worker 的公开 plan snapshot、callback 或
可多路复用异步通知面，并通过相同 synthetic/live 行为探针，就删除本 Adapter、源码
指纹门禁和本 ADR 的运行时例外。不得把这个入口扩展成任意通知读取、RPC gateway、
推理/工具日志或第二套进度运行时；任何扩展都需要新的明确决策。
