---
status: accepted
date: 2026-08-03
amends: 0008
amended_by: 0010, 0014, 0026
---

# 用版本锁定的实验接口清理 Thread 后台终端

> 2026-08-07 更正：本 ADR 的 adapter 只清理 App Server 已登记的后台 terminal。
> 它不能证明或保证 foreground tool process 退出；相关产品语义、探针修正和发布门禁
> 已由 [ADR 0010](0010-correct-stop-and-background-cleanup-semantics.md) 修订。
> [ADR 0014](0014-use-removable-sdk-gap-adapters.md) 新增了独立的 Goal/Skills facade-gap
> 边界，但不改变本 adapter 的精确版本、源码指纹或 `experimentalApi` 门禁。

## 背景

ADR 0008 选用官方 Python SDK，以公开 `AsyncTurnHandle.interrupt()` 实现 `/stop`。
目标 Linux 的真实 marker 探针证明：`interrupt()` 会把 Turn 置为 `interrupted`，但
已启动的 sandbox/terminal 进程仍会继续运行。官方 App Server 把这两种操作明确拆成
`turn/interrupt` 与实验方法 `thread/backgroundTerminals/clean`；固定
`openai-codex==0.144.4` 的高层 `AsyncCodex` 尚未封装后者。

用户于 2026-08-03 明确批准一个 narrow experimental cleanup adapter。授权只覆盖
上述官方方法，不覆盖通用 JSON-RPC、任意私有方法、进程扫描/kill、协议复制或第二套
App Server 客户端。

## 决定

1. 生产仍只创建一个无参数 `AsyncCodex()`。服务初始化完成、接受任何 Turn 之前，
   构造 `PinnedExperimentalTerminalCleanup`。
2. adapter 只从该共享 `AsyncCodex` 取得其固定的低层异步客户端，然后调用低层公开
   typed `request()`；唯一方法名固定为
   `thread/backgroundTerminals/clean`，唯一参数固定为 `{"threadId": exact_id}`，
   唯一响应形状固定为空 object。adapter 不公开 `request()` 或 method 参数。
3. adapter 同时校验：发行版恰为 `0.144.4`、对象私有持有关系仍是预期类型、
   `AsyncCodex` 已初始化、底层 config 的 `experimental_api` 为 true，以及整个
   `openai_codex` Python 源码包的确定性聚合 SHA-256。任一不符即启动失败，不能
   降级成仅 interrupt。
4. `/stop` 在同一 Binding 锁中先调用 exact Turn `interrupt()`，成功后再调用 exact
   Thread cleanup。按 ADR 0010，cleanup 成功只允许飞书表示“已请求清理已登记后台
   terminal”，不得表示前台命令已经停止。
5. cleanup 失败时不把 Binding 恢复为 running，也不释放已观察到的 native 终态；
   Binding 保持 `stopping`，新 prompt 被拒绝，飞书明确提示不能假定已登记后台
   terminal 已经清理，更不能推断前台命令已经停止。
   重复 `/stop` 只重试同一 Thread cleanup，不重复 interrupt；成功后恢复正常终态
   释放和同一 Thread 的后续 resume。
   interrupt 报错同样视为结果未知并保持 stopping；重复 `/stop` 先重试 interrupt。
   若这次重试仍报错但公开 read 已确认 exact native terminal，则不声称 interrupt
   RPC 成功，直接继续 exact-Thread cleanup；这样既保留一次真实重试，也避免 terminal
   child 和 completion 永久卡住。
   若 native terminal 在 stop 抢锁前已被公开 read 确认，则 stop 不再 interrupt 或
   cleanup。
6. Channel 在等待 interrupt/cleanup 之前尝试确认“正在中断当前 Codex Turn”。确认
   attempt 有界；超时或投递失败只记录告警，不能撤销停止意图。随后不再发送第二条
   control 成功
   消息，只由原 Turn 发送一个终态；cleanup 失败终态明确要求再次 `/stop`。该顺序
   只使用当前 handler 调用栈和内存 completion，不新增队列。
7. service shutdown 对每个 active Binding 使用同一 interrupt-then-clean 路径，且
   无论 interrupt cancellation 如何都最终关闭 transport 并取消 consumer。
8. 首个 real prompt 调用 `thread_start` 后，必须先把返回的 native ID 原子写入 lazy
   Binding，再调用 `thread.turn`；冲突或写入失败会关闭新 admission，且不会发送
   prompt。cleanup 前还必须让 handle Thread ID、`AsyncThread.id`、Binding native
   ID 三者一致；若 handle 返回不一致的 ID，关闭 admission 且不对该 handle 调用
   interrupt 或 Thread cleanup。

## 验证与发布门禁

仓库必须保留以下测试：

- fake App Server 证明 initialize 请求携带
  `capabilities.experimentalApi=true`，且 adapter 只发送上述 exact method/params；
- 关闭实验 capability、版本变化或源码指纹变化均在 cleanup/Turn 之前失败；
- runtime 测试证明 interrupt -> cleanup 顺序、跨 Binding 隔离、shutdown 全清理、
  interrupt/cleanup 失败保持 stopping、重复 `/stop` 可恢复、terminal race 不误清理、
  cancellation 可解除 barrier、Binding 冲突不跨会话清理；
- 按 ADR 0010，Linux marker 探针只接受 exact `argv[0]`，记录 cleanup 后 5 秒内
  foreground 是否退出，有界等待测试 marker 自然结束，并要求同一 native Thread
  随后能继续一个新 Turn。foreground 未退出是能力分类，不再冒充 adapter 失败。

真实 Linux 验证必须观察 native `interrupted`、same-Thread resume 和无 probe
遗留；任一不满足时 candidate 不能替换现役服务。

## 后果与移除触发器

这是一个明确的兼容债务，但被隔离在单个文件中；Scope/Binding、SQLite、并发、
completion polling 和原生状态模型均不改变。普通 SDK 升级会先因版本/指纹门禁失败，
必须重新审查而不是静默继续。

低层 SDK 的同步 request waiter 与其他 native 操作一样没有客户端超时。若本地 App
Server 既不响应也不关闭 transport，交互式 `/stop` 会暂时持有该 Binding 锁；其他
Binding 不受影响。Pilot 不再包一层无法取消底层 waiter 的假超时，避免请求仍在后台
执行时重复 cleanup。service shutdown 使用分步预算：Feishu handler drain 5 秒、
active Turn interrupt/cleanup 5 秒；后者成功时再等待 completion drain 最多 15 秒，
随后清理飞书运行 reaction 最多 4 秒，最后关闭 Codex transport 最多 10 秒。只有
interrupt/cleanup 失败或超时时才跳过 completion drain；systemd
`TimeoutStopSec=40s` 覆盖最多 39 秒的分步上限。

一旦官方 Python SDK 提供与 `thread/backgroundTerminals/clean` 同语义的公开高层方法，
并通过相同 fake contract 与 Linux marker/resume 探针，就删除 adapter、源码指纹和本
ADR 的运行时例外，改回纯公开 SDK 调用。即使实验方法更名或响应变化，也不得自行扩
大本 ADR；需要新的明确决策。
