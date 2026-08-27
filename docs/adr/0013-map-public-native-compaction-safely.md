---
status: accepted
date: 2026-08-09
amends: 0008
---

# 安全映射原生会话压缩

> 2026-08-27 兼容性更正：固定 `openai-codex 0.147.0` 的 live probe 能确认
> `contextCompaction` 终态，但未能成功完成同一连接的后续普通 Turn。由于本 ADR 的验收
> 明确包含压缩后继续 Turn，当前 `/compact` 保持 unavailable、不进入帮助且不执行 native
> mutation。底层 controller 与探针保留；只有匹配 SDK/App Server 的完整 compact phase
> 通过后才重新开放。以下正文保留决策形成时的历史依据。

## 背景

统一命令层应把固定 SDK 已提供的原生能力映射为飞书 control，而不是把 slash
command 当作 Prompt。`openai-codex==0.144.4` 的公开 `AsyncThread.compact()` 可触发
原生上下文压缩；但官方
[App Server 文档](https://learn.chatgpt.com/docs/app-server#api-overview) 明确说明
`thread/compact/start` 立即返回空响应，实际进度随后通过 Turn/Item 事件推进。空响应
只证明请求被接受，不证明压缩已经完成。

固定 Python 高层 facade 没有为 `compact()` 返回可等待的 Turn handle，也不暴露该
调用专属的通知流。它仍提供公开 `AsyncThread.read(include_turns=True)`，可以读取同一
原生 Thread 的持久化 Turn 和 `contextCompaction` item。项目已经使用这条公开状态面
恢复普通 Turn 终态，因此无需访问 `_client` 或复制 App Server 协议。

## 决定

1. 注册零参数 `/compact`，owner 为 native Thread。它只作用于当前 exact Binding；
   lazy Binding 没有原生 Thread，或当前普通 Turn 正在 running/stopping 时明确拒绝。
2. 开始前通过 exact native ID `thread_resume()`，用
   `thread.read(include_turns=True)` 记录已有 Turn ID 集合，并确认原生 Thread idle。
   然后调用一次公开 `thread.compact()`。
3. 从请求开始到终态确认期间，内存中以 Binding ID 保留 `compacting` 槽位。普通
   Prompt、引用消息准备、`/config` 和再次 `/compact` 都明确拒绝；`/status` /
   `/sessions` 显示 `compacting`。`/stop` 只控制普通 Turn，不声称能中断压缩。
4. 完成证据必须同时满足：baseline 之后出现且仅出现一个新的
   `contextCompaction` Turn；该 Turn 状态为 terminal；Thread 状态为 `idle`。单独的
   compact 空响应或一次 `idle` read 都不算完成。若出现多个候选，公开 ACK 没有
   Turn ID 可用于归因，必须按终态未知 fail closed，不能任取第一个。
5. 同一 native Thread 在 `/compact` 生命周期内不支持 CLI 或其他 App Server 并发写入；
   serial CLI exact-ID resume 仍受支持。这个互斥条件是空 ACK 下进行时间归因的必要
   前提；检测到多个 compaction candidate 时按上一条关闭 admission。
6. 轮询先读取轻量 Thread status，仅在 idle 时读取完整 history，并以 10 分钟为终态
   上限。超过上限或持续缺少唯一候选时保留槽位、关闭 admission 并要求重启，避免
   永久挂起 command handler。
7. terminal `failed`/`interrupted` 会释放 Binding 并向触发者报告失败。compact start
   响应丢失，或在看到唯一 terminal candidate 前遇到不可分类读取错误时，副作用状态
   未知：保留槽位并关闭进程级新 admission，要求重启，不能假装回到 idle。
8. Channel SQLite 不保存压缩状态、Turn 或通知。服务重启后仍以 Codex 原生 Thread
   历史为准；不增加私有 RPC adapter、prompt shim 或第二套历史模型。

## 验证

`scripts/probe_python_sdk.py --phase compact` 使用同一个固定公开 facade 创建一条短
会话、压缩并继续下一 Turn。2026-08-09 本地真实探针观察到：compact request 在
约 1ms 返回；随后公开 read 的状态序列为 `idle -> active -> idle`；新增 Turn 为
`completed`，唯一 item 类型是 `contextCompaction`；同一 Thread 随后正常回复
`COMPACT-AFTER`。首个 `idle` 证明只等 Thread 状态会形成提前释放竞态，因而必须使用
上述 exact Turn/item 条件。

单元测试覆盖 lazy/running 拒绝、exact-ID resume、压缩期间 Prompt 与 `/stop` 边界、
receipt 先于终态、terminal failure 释放、idle 但缺少 item、候选歧义、终态超时，
以及 compact start 结果未知时全局 fail-closed。SDK capability contract 固定
`compact()` 和 `read(include_turns=...)` 都在公开高层 surface。

## 后果与移除触发器

飞书获得与原生 Codex 一致的上下文压缩能力，并明确展示异步生命周期；代价是压缩时
该 Binding 暂时不能接受输入。若未来固定 SDK 的 `compact()` 返回可等待的 typed
handle 或公开专属事件 iterator，可新增 ADR 直接等待官方 completion，并删除
baseline 差集轮询；在此之前不得把 start acknowledgement 当作完成。
