---
status: accepted
date: 2026-08-20
amends: 0009, 0021
related: 0010, 0017, 0020, 0026
---

# 释放空闲普通 Thread 的连接订阅

## 背景

普通持久 Binding 每次通过 `thread/start` 或 `thread/resume` 打开原生 Thread 后，当前
App Server 连接会持续订阅该 Thread。Netizen 使用一个长驻 `AsyncCodex`，此前没有普通
Thread 的取消订阅路径，因此已经完成工作的 Thread 会持续留在 App Server 运行态并持有
writer。Binding 与原生历史本身应继续保留；需要释放的是当前连接的订阅，不是会话数据。

App Server 的 `thread/unsubscribe` 只移除当前连接订阅。最后一个订阅者离开后，Thread
仍要连续三十分钟没有订阅和活动才会被卸载，因此取消订阅不能表述为 writer 已立即释放。
当前 Python SDK 高层 facade 尚未公开该方法，但 ADR 0021 已通过同一个初始化 SDK client
为 Side Thread 固定实现了 exact method、generated response model 和三种成功 status。

## 决定

1. 从 ADR 0021 的 Side 适配器中抽出可独立移除的 `ThreadSubscriptionControl`，只暴露
   `unsubscribe(exact_thread_id)`。Side boundary injection 继续是 Side 专用能力；普通
   Binding 与 Side 关闭复用同一个 unsubscribe provider，不增加第二 App Server、通用
   RPC 或重复协议实现。公开 facade 出现 unsubscribe 时只迁移这一项，不与 Side boundary
   的迁移绑定。
2. Runtime 只为本进程实际 start/resume 的普通持久 Thread 保存瞬态订阅记录：exact
   Binding/Thread、monotonic activity、generation、状态和 idle task。`thread/unarchive`
   只恢复归档 rollout，不作为订阅证据；恢复后必须再 exact `thread_resume`，只登记 resume
   返回的 handle。记录不进入 Channel Database；服务启动不扫描 Binding、不 resume Thread，
   也不重建 timer。服务退出关闭 App Server 已是更强的全量释放。
3. 当前 active Binding 在原生操作精确回到 idle 后等待十五分钟再尝试释放；已经切出
   active pointer 的 idle Binding 立即尝试。没有 Thread 数量上限、LRU、Project gate
   或跨 Binding semaphore。新活动在 exact Binding 锁内取消旧 timer 并推进 generation，
   防止 idle/running ABA 或旧 timer 取消新订阅。
4. 自动释放前必须确认本进程没有普通 Turn、Goal、compaction 或 Thread lifecycle 槽，
   公开 Thread read 为 exact idle，且 App Server 没有为该 Thread 登记后台 terminal。
   后一项由 ADR 0009 同一 exact-version/fingerprint 边界增加的只读
   `BackgroundTerminalInspector` 调用固定
   `thread/backgroundTerminals/list(limit=1)`，只投影是否非空，不向 Runtime 暴露进程
   明细。公开 read 的 `notLoaded` 只表示该次只读投影没有加载 Thread，不等同于
   `thread/unsubscribe` 的 `notLoaded` 成功响应。检查失败、非 `idle` 或存在 terminal 时
   保留订阅并在新的完整空闲窗口重查；自动释放绝不 cleanup、terminate 或 signal 进程。
5. `/release` 只作用于当前 active Binding。Lazy Binding 视为无需释放；任何运行中或
   状态未知的原生操作都拒绝。已登记后台 terminal 存在时同样拒绝，不把 release 隐式
   解释为 `/stop` 或 terminal cleanup。确认取消订阅后 Binding、active pointer、Turn
   Settings 和原生历史全部不变；下一条消息仍 `thread_resume(exact_id)`。
6. `thread/unsubscribe` 返回的 `notLoaded`、`notSubscribed` 和 `unsubscribed` 都表示当前
   连接已经达到无订阅状态。响应丢失时不宣称成功、不删除 Binding，也不关闭全局
   admission；状态保留为 unknown，任何 read/terminal 检查延期、local busy 或调用取消都
   不能把它降为 subscribed/pending。只有成功 exact `thread_resume` 或确认的 unsubscribe
   响应才能收敛该状态。自动路径只在新的完整空闲窗口重试，不做紧密隐式循环。
7. `/status` 只展示 Netizen 当前进程的订阅状态，不能把“已取消订阅”显示成“writer 已
   释放”或“Thread 已关闭”。`/stop`、archive、delete 与 Side close 的既有语义不变。

## 验证

- capability adapter 必须覆盖固定 method/payload、三种成功 status、响应丢失、按能力
  拆分的 facade migration sentinel，以及后台 terminal list 的 exact-version/fingerprint
  与布尔投影；
- Runtime 必须覆盖十五分钟 idle、切换 Binding、活动取消、ABA、Turn/Goal/compaction/
  lifecycle 互斥、后台 terminal 延期、unsubscribe 未知、same-ID resume、多 Binding 并发、
  无数量淘汰、shutdown 和新进程不扫描；
- Channel 必须覆盖 `/release` 的 Lazy、busy、terminal、成功和未知结果，并保持 Side
  `/side close` 回归；
- 目标 Linux live phase 必须证明 inspector 在无登记 terminal 时返回空、普通持久 Thread
  unsubscribe 后能在同一连接按 exact ID resume 并继续 Turn，且进程退出后另一 App
  Server 能接管。目标 App Server 没有稳定、无副作用的“造一个登记 terminal”测试夹具，
  因此存在 terminal 与 inspector 错误时阻止释放由真实 SDK fake-server harness 和 Runtime
  测试作为发布硬门禁，不能用 live 空列表结果替代。

## 后果与移除触发器

常用普通 Thread 会保留十五分钟 warm window；不再使用的 active Thread 通常在最后活动
约四十五分钟后才可能真正卸载，切走或显式 release 的 Thread 仍受 App Server 固定三十
分钟 grace 约束。持续运行的后台 terminal 会有意阻止自动释放。

官方 Python SDK 一旦提供等价高层 unsubscribe 并通过 synthetic/live 验证，删除
`ThreadSubscriptionControl` reach-through；若公开高层 API 能安全查询后台 terminal，
同样替换 ADR 0009 的只读 inspector。空闲策略、无持久 timer 和 Binding 恢复语义保留。
