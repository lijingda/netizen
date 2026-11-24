---
status: proposed
date: 2026-11-24
amends: 0052, 0053, 0056, 0079, 0084, 0105
related: 0047, 0048, 0086
---

# 以飞书展示 Turn 统一各后端的回复展示

## 背景

同一条输入可能对应多个原生物理 Turn，两个后端对此采用了两种展示模型。Codex Goal 的自动续跑按 [ADR 0084](0084-separate-goal-controls-from-logical-turn-replies.md) 共用一张任务回复卡：阶段性答案跨物理 Turn 累计，Activity 每轮重置，中间 Turn 不展示 Result/Files。Claude 的后台唤醒按 [ADR 0105](0105-extend-backends-through-capabilities-and-shared-presentation.md) 阶段 C 的实现，每个有正文的物理 Turn 单独回复，状态行在新旧卡之间交接。

两套模型让展示层理解了 Goal、逻辑 Turn、唤醒与后端。回复呈现器允许逻辑会话接受任意物理 ID，Goal 有单独的快照读取与留存规则，Claude 有专用的物理回答、状态行和逻辑终态路径。阶段 C 的复核发现大多集中在这些按物理 Turn 分卡、跨卡交接状态的机制上。今后接入飞书 COT 消息、其他 Agent 或 Claude 的自动续跑时，每种展示调整都要在多处分别实现。

维护者要求统一展示概念。每个物理 Turn 都映射为一个飞书展示单位，各 Agent 只负责映射，展示样式只针对这个单位设计。长 Goal 产生较多卡片是可以接受的代价，以后可以在展示层合并或提供配置。

以下事实来自对 `main@71feb18` 的只读调查，记录在本地研究目录 `docs/research/multi-agent-positioning/phase5/` 的 `PRESENTATION-TURN-FACTS.md` 中：

- **Codex Goal 的唯一 Tap 能看到每个物理 Turn 的事实。** 包括原始 `turn/started`、各 Turn 已完成的 `agentMessage` 及其 phase、终态 status、成功 `fileChange`、生成图片和匹配的 diff。SDK 的逻辑游标会丢弃后续 start，并改写条目的 Turn ID，因此这些事实只能在 Tap 处、换轮重置之前冻结。
- **中间 Turn 的子任务文件无法完整证明。** 若 P1 的子任务在 P2 开始前尚未完成根核对，现有收集器会丢弃这部分证据，以免把 P2 派给同一子任务的工作算进 P1。P1 自身已证实的直接文件仍可使用。
- **Claude 的匿名唤醒和 cron Turn 到 Result 才知道归属。** 无正文、无片段的匿名成功 Result 不产生任何可见内容。
- **固定版 SDK 在自动续跑被中断时可能结束逻辑迭代器。** 之后的物理 Turn 不再到达 Tap，现有的 unknown 通知继续作为恢复手段。

## 决定

1. **飞书展示 Turn 是唯一的回复展示单位。** 一个展示 Turn 对应一个原生物理 Turn。
   - **展示形态：** 开启过程卡时是一张回复卡，关闭时是对应的一组富文本消息，以后可以是一条 COT 消息。
   - **展示内容：** 只展示该物理 Turn 自己的 Activity、阶段性答案、结果，以及成功终态下已证实的本轮文件。阶段性答案只追加到所属展示 Turn 的消息里。
   - **身份：** 精确所有者（Binding 或 Side）、原生会话、物理 Turn 身份。
   - **空展示：** 没有任何可见内容的物理 Turn 不发消息，不强制生成空卡。
2. **逻辑运行只承载生命周期信号，不承载正文。** 一个逻辑运行是一条人工输入、一次定时输入或一次 `/goal` 启动，加上由它派生的物理 Turn。它负责：
   - 原消息上的生命周期表情；
   - 最新展示 Turn 上的状态行；
   - 停止后对提醒的抑制；
   - 结束时最多一次符合条件的结束提及，引用最后一个已确认送达内容的展示 Turn。

   卡片标题反映该物理 Turn 自己的终态。只有逻辑运行结束时，最新展示 Turn 才显示整个任务已完成。单轮的普通任务因此保持现有字节不变。
3. **展示层只消费类型化事件。** 最小事件集如下，展示层不按后端、Goal、唤醒或 cron 分支：
   - `TurnOpened`：展示 Turn 身份、可选的逻辑运行、已核实的投递目标、冻结的任务反馈；
   - `ActivityChanged` 与 `PartialAnswer`；
   - `TurnResult`：终态、正文、安全错误、本轮文件和剩余片段；
   - `RunState` 与 `RunEnded`。

   每条原生事件流仍只有一个消费者（[ADR 0052](0052-project-safe-turn-activity-with-one-consumer.md)）。飞书 I/O 不进入原生消费链。
4. **各后端只做映射。**
   - **Codex 普通 Turn 与 Side：** 一一映射，线上输出保持字节不变。
   - **Codex Goal：** 每个已观测的物理 Turn 映射为一个展示 Turn。
     - **冻结时机：** Tap 在换轮重置前冻结本轮的最终答案、终态和直接文件证据。一个已完成的物理 Turn 先作为候选保留，直到下一次 start 证明它是中间 Turn，或四项终态证据证明它是最终 Turn，再一次性写入结果。自动续跑中，中间 Turn 的结果几乎立即可见，展示层也无需对已发布内容再做对账。
     - **保持不变的契约：** 四项证据约束逻辑收尾、本进程已确认的显式 clear、只对 completed 自动收尾、有界 handoff，以及独立的管理卡。
     - **文件：** 中间 Turn 只显示已观测的直接文件；子任务统计仍只属于最终 Turn，沿用既有归属证明。
   - **Claude：** 首答、每次唤醒和每个 cron Turn 各映射为一个展示 Turn。
     - **匿名 Turn：** 在 Result 确认归属后才打开。
     - **会话级 Turn：** 无人工归属的 Turn 发往已核实的会话位置，没有提醒对象。
     - **不变的部分：** 无正文回执行为保持 ADR 0105 的约定。
5. **降级以展示 Turn 为界。** 投递拒绝、结果未知、渲染失败或超限，只影响该展示 Turn：
   - 按本轮投递账本转为文本；
   - 在原位置尝试一次"未能确认送达"提示；
   - 不重发已尝试的内容。

   展示失败不改变原生执行，也不影响同一逻辑运行的其他展示 Turn。
6. **兼容范围。** 已发出卡片的 Files 分页与过程清单继续按既有格式解码。新发出的卡片按物理 Turn 标识。不承诺服务重启后恢复进行中的展示。

## 实施与验收

分两个 PR 交付：

- **重构展示层，不改变行为。** 展示层改为只消费上述事件。Goal 暂时把所有物理 Turn 映射到同一个展示 Turn，复现现有单卡行为。Codex 差分夹具逐字节不变，Claude 行为不变。Claude 的专用物理回答、跨卡状态交接和逻辑终态路径并入通用路径。
- **切换 Goal 的映射。** 只修改 Goal 映射，有意更新相关的 Goal 测试与差分夹具。用真实 bundled App Server 跑一次至少两个物理 Turn 的 Goal，核对两张卡各自的答案、文件与终态，并只在最后一张卡上提醒一次；飞书展示另行验收。

每个 PR 的规格先写明通用降级路径和阻断标准。以下情况阻断合并：

- 内容送到错误的展示 Turn、会话或人；
- 提醒发错对象，或者重复、缺失；
- Netizen 逻辑造成内容丢失；
- 缺失本应发出的降级提示；
- 悬挂、泄漏或异常逃逸；
- Codex 普通 Turn 与 Side 的行为变化。

失败路径里的观感问题，以及已被通用规则覆盖的注入异常变体，不阻断合并。

## 备选方案

| 方案 | 结论 |
| --- | --- |
| 一个逻辑运行共用一张卡，Claude 向 Goal 现有模型对齐 | 不采用。展示层仍需理解逻辑运行和换轮。数小时后才到的唤醒结果会静默更新旧卡。COT 和其他 Agent 也没有统一单位。 |
| 各后端各自实现展示 | 不采用。重复实现，样式会分化，正是本决定要消除的状态。 |
| Goal 物理 Turn 完成后立即发布结果，再由四项证据对账 | 不采用。需要对已发布的卡片和回执补写或去重。下一次 start 在自动续跑中几乎紧随其后，保留候选的延迟可以忽略。 |
| 为每个物理 Turn 强制生成可见消息 | 不采用。无内容的匿名唤醒会产生空卡，并改变 ADR 0105 的无正文回执行为。 |

## 后果

- 一个长 Goal 会产生与物理 Turn 数相同的任务卡，另加一张管理卡。若体验不佳，可在展示层把同一逻辑运行的展示 Turn 合并为一条消息，或做成可选配置，不需要修改任何 Agent 映射。
- 每张新卡单独计入飞书的发送频率，并有各自 14 天的更新窗口。快速换轮会更早接近同一聊天每秒 5 条的回复上限。
- AGENTS.md 的 Goal 条款改为："保留 Goal 的四项证据逻辑收尾与有界最终 handoff，包括当前 Turn 完成期间已确认的显式 clear；每个展示的阶段性答案、Result 与 Files 绑定其精确物理 Turn。"design.md 的 Goal、阶段性答案和本轮文件章节随实施 PR 同步修改。
- 本决定修订：
  - [ADR 0052](0052-project-safe-turn-activity-with-one-consumer.md)：单卡、中间 Turn 无 Result/Files；
  - [ADR 0053](0053-show-exact-turn-line-statistics-in-files.md)：只统计最终 diff；
  - [ADR 0056](0056-count-successful-patches-in-task-files.md)：“不聚合 Goal 更早 physical Turn”改为每个展示 Turn 只统计本轮，子任务统计仍只在最终 Turn 上取得；
  - [ADR 0079](0079-deliver-partial-answers-with-existing-reply-presentation.md)：片段跨换轮累计；
  - [ADR 0084](0084-separate-goal-controls-from-logical-turn-replies.md)：不为每个物理 Turn 单独发卡；
  - [ADR 0105](0105-extend-backends-through-capabilities-and-shared-presentation.md)：Claude 专用的物理回答与逻辑终态展示路径。
