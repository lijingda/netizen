---
status: accepted
date: 2026-09-01
amends: 0020, 0047, 0048, 0049
related: 0021, 0046, 0051
---

# 用单一消费链投影安全的 Turn Activity

Progress Card 已能显示运行状态、steer 次数和原生 checklist，但真实长任务中 checklist
可能很晚才出现，用户仍会长时间只看到“Codex 尚未生成”。App Server 的 Turn 通知还包含
commentary、命令、工具、文件修改、搜索、图片、子任务、审查与上下文压缩等生命周期，适合
补充为有界过程信息；其中也混有 reasoning、参数、输出、路径、搜索词和 token usage，不能
直接成为通用事件渲染器。

普通持久 Turn、ephemeral Side Turn 与 Goal logical stream 的通知所有权并不相同：普通
Turn 可以在终态前只读窥视 exact queue，并由公开 `thread.read()` 确认终态；Side 的公开
`thread.read(include_turns=True)` 不能读取 exact ephemeral Turn，且现有
`handle.run()` 会消费同一队列；Goal 则由 SDK 的 logical stream 唯一消费并在内部 rollover
物理 Turn。展示层必须复用一份 Activity 投影，同时不能产生第二消费者或取得生命周期所有权。

## 决定

### Activity 仍是一个封闭模块

继续使用 ADR 0047 的 `ReplyCardActivityModule`，不增加插件注册表或新的卡片 shell。模块
只携带以下 process-local、bounded、sanitized projection：

- 运行或停止状态与已确认 steer 次数；
- 最新原生 checklist full replacement 及 freshness；
- 最近三条 completed commentary；
- 最近八个 identity-free 操作及状态；
- 子任务和文件修改的安全数量聚合；
- 只在可见内容变化时递增的 revision。

运行中展开 Activity，终态在同一张 Reply Card 中折叠，并与 Result、Files 及可选 Goal
Module 一起完整重绘。Presenter 每秒只比较 revision，不读取 SDK 队列。Activity、cursor、
message identity 与 Presenter session 都不写入 SQLite 或 Codex history。

### 只接受安全事件白名单

ADR 0020 的 pinned observer 扩展为 `PinnedTurnActivityObserver`，继续固定 SDK 版本、整包
源码指纹、对象持有关系、generated notification shape、router lock 与 exact Queue shape。
它在 router lock -> Queue mutex 顺序下复制 cursor 后的对象引用，不调用 RPC、不注册路由、
不消费或修改队列，只返回已脱敏的内部事件。opaque item identity 仅留在 Runtime 内用于把
started/completed 合并为同一操作；交给 Channel 的 snapshot 与 manifest 都移除该 identity。

白名单及展示语义如下：

- `turn/plan/updated`：完整替换 checklist；
- completed commentary `agentMessage`：保守脱敏并限长；
- command、MCP、dynamic tool：只显示“执行命令”或“调用工具”及状态；
- `fileChange`：只显示状态和 change 数量；
- web search、image view/generation、review mode、context compaction：只显示固定类别；
- collab/sub-agent：只显示运行、完成、失败等聚合数量。

明确忽略 reasoning、user/final message、`agentMessage/delta`、command input/output、工具名、
server、参数、结果、搜索词、URL、路径、diff、token usage、sleep 时长、hook prompt 和未知
事件。Activity 不显示 raw output、elapsed time、百分比或 ETA；`final_answer` 只进入 Result
Module。白名单 method 的 generated payload 形状变化会 fail closed，未知 method 则忽略。

### 普通 Turn 保持公开终态权威

只有 Binding 在 Turn admission 时冻结的 Progress Card 选择为开，Runtime 的既有 Turn
poll loop 才调用 observer 并更新 Activity。公开 `thread.read()` 仍是 exact terminal 的唯一
权威；observer 中看到 `turn/completed` 也不改变 Runtime 终态。公开 read 确认终态后，才按
既有规则唯一一次 drain handle stream。Progress Card 关闭时不增加 Activity observation 或
轮询，原终态富文本/Result + Files 路径保持不变。

### Side 先观察，终态后由同一 handle drain

Side 仍是 ADR 0021 的 ephemeral 容器，根卡、两小时 expiry、close、cleanup、unsubscribe
与 tombstone 全部不变。Progress Card 关闭时继续立即调用 `handle.run()`。

Progress Card 开启时，Runtime 暂不调用 `handle.run()`，而是每个既有 poll interval 只读
观察 exact Side Turn queue。观察到 exact `turn/completed` 只表示“可以开始 drain”，随后立即
调用同一个 `handle.run()`；只有其返回值确认 terminal、Result 与 Files。observer 不可用、
cursor 回退、allowlisted shape 变化或 queue 达到固定 4096 high water 时，停止 Activity
观察并立即回退原 `handle.run()` 路径。observer 永远不是第二消费者或 Side 终态权威。

### Goal Tap 位于唯一 logical stream 内

Goal 不再通过普通 queue observer 读取物理 Turn。Goal adapter 在现有
`next_goal_notification -> logical stream` 唯一消费链内加入窄 Activity Tap：原始通知先投影
为安全 Activity update，再原样交给 SDK logical stream。Tap 或 sink 失败只关闭 Activity，
不能打断 `wait_terminal()`。

一个 logical Goal 始终维护同一张卡片、Goal Module 与 Presenter session。收到 exact
`turn/started(new_physical_turn_id)` 时原子更新当前物理 Turn identity，清空旧 commentary、
operations、checklist 与 cursor，并递增 Activity revision；随后只接受新物理 Turn，迟到旧
事件直接忽略。Goal Module 跨 Turn 继承；Activity 每个物理 Turn 重置；中间物理 Turn 不生成
Result/Files；logical terminal 只使用 exact final physical Turn 的最终文本与 structured files。
不拼接早期输出、不回退上一轮结果、不扫描工作区或解析正文猜测文件。四证据 complete、
completed-only auto-clear、pause/blocked/limited/unknown 语义不变。

### Activity 不拥有生命周期

生命周期可以 best-effort 把 Activity 状态更新为 stopping，Activity 从不参与或阻塞生命周期：

- ordinary `/stop` 继续 interrupt exact Turn 并沿用既有 cleanup/terminal 规则；
- archive/delete 在 lifecycle intent 建立时停止该 Binding 的新 Activity reads，直接委托原生
  Thread lifecycle，不模拟 `/stop`、不等待 Presenter、observer 或 terminal；明确 mutation
  失败并释放 intent 时可以恢复观察，unknown 时保持停止；
- Side `/stop` interrupt 当前 handle，完成后仍由同一个 `handle.run()` 唯一消费，Side 保持
  open；
- `/side close` 继续先关闭新输入，再处理当前 Turn、terminal cleanup、unsubscribe、tombstone
  与根卡；Presenter 更新不在这条关键路径。

卡片发送、更新、折叠、删除或分页失败一律只记录展示故障，不能改变 stop、archive、delete、
Side close、Goal finalization 或 native outcome。

## 验证与兼容门禁

synthetic tests/probe 必须覆盖白名单/拒绝列表、脱敏与上限、exact ID、full plan replacement、
非消费对象身份、cursor 回退与 shape/fingerprint fail closed。Runtime 门禁必须覆盖 ordinary
持续刷新及 terminal authority、Progress Card 关闭零观察、Side completion-before-drain、
observer/high-water fallback、stop/steer/close、Goal Tap 唯一消费与 physical Turn reset/late
event、final-only Result/Files，以及 lifecycle intent 不等待展示。

SDK 升级必须运行 synthetic probe 和目标环境 live `plan`/Activity、Side、Goal、lifecycle
phase。若官方 SDK 提供公开、可多路复用且不改变消费时机的 Activity callback/snapshot，则用
同一组行为门禁迁移并删除 pinned queue reach-through；不得把现有 adapter 扩展为通用私有
RPC/notification gateway。

## 后果

长任务能在 checklist 之外看到安全、低噪声的真实进展，普通、Side 与 Goal 复用同一模块和
渲染器。代价是 ADR 0020 的严格私有只读例外覆盖更多固定 generated notification 类型，且
Side 开启 Progress Card 时会暂存通知至 exact completion；固定 high water 保证观察失败或
异常增长时立即回到既有唯一消费路径。
