---
status: accepted
date: 2026-10-10
updated: 2026-10-17
amends: 0047, 0048, 0052
related: 0056, 0058, 0069, 0071, 0072, 0078
amended_by: 0084
---

# 在现有回复形态中交付阶段性答案

> [ADR 0084](0084-separate-goal-controls-from-logical-turn-replies.md) 将 Goal 管理改为独立静态卡，任务回复复用普通逻辑流；pause/clear 不中断当前 Turn，`/stop` 仍暂停并中断。下文保留原决定的历史背景。

Codex SDK/App Server `0.162.1` 的 `partial_answer` 是稳定答案正文，之后仍可能继续执行；
现有最终正文提取和 Activity 都不会交付它。将全部片段塞入最终 Result 会延迟答案、重复
已读内容，塞入 Activity 则会受到摘要限长与折叠影响。Netizen 因此增加独立的
**Partial Answer / 阶段性答案** 展示，复用现有观察链、唯一 Presenter 和原生终态，
不增加答案历史库、通知消费者或执行生命周期。

2026-10-17 修订：保留运行中的整卡更新，终态原卡不可用时统一按原生 item 交给现有
富文本发送路径，不再为阶段性答案另造替代答案卡。此修订接受原卡更新结果未知时可能
与后续富文本重复，但不重发已经尝试、结果未知的独立阶段性消息。

## 展示决定

Reply Card 的封闭模块及顺序扩展为 **Goal → Activity → Partial Answer → Result → Files**。
阶段性模块只在存在完整稳定片段时出现，按原生已观察顺序追加，运行中和终态都不折叠。
阶段性模块沿用 Goal 区域的浅灰底与内边距，最终 Result 保持默认底色，以便区分过程中的
稳定答案和最终回复；底色不改变片段语义、顺序或终态规则。
普通会话、持久 fork 与 Side 按相同的实际回复形态交付：已有运行卡时更新该卡；没有
运行卡时，每个完整 item 以带“阶段性答案”标识的富文本投递到原消息/话题。复用最终回复
的正文格式化与路由，但不调用带终态副作用的发送入口，也不逐 token 发消息。
初始运行卡发送失败或没有可复用的消息 ID 时，仍建立现有观察/投递 session，按无卡路径
交付后续阶段性答案；Progress Card 开关不充当原卡已经建立的证明。

Progress Card 只控制 Activity，不能关闭答案观察或交付。普通/Side 的最终回复形态不变：
无运行卡且无 Files 时是富文本，有 Files 时是 Result + Files 卡，不因后来出现 Files
重发此前的阶段性消息。持久 fork 是普通 Binding；Side 继续沿用创建时冻结的反馈配置与
ephemeral 生命周期，不增加 Goal 或 history recovery。

Goal 即使关闭 Progress Card 也有控制卡，正常展示时阶段性答案放在同一卡中。同一次连续
logical run 的自动物理 Turn rollover 保留已经观察到的片段；Activity 仍按物理 Turn
重置，Result/Files 仍只取四项终态证据确认的 exact 最终物理 Turn。暂停、失败或中断
保留已展示内容和真实状态；手动 resume 创建新 logical run，阶段性模块从新执行段开始。
服务重启、缓存丢失或外部 Goal 重挂不恢复旧阶段性答案，不扫描完整 Thread、旧飞书卡
或 Project，也不借该模块扩大子任务 Files 的归属范围。

## 身份、观察与投递

仅接受 completed `agentMessage` 中明确标记 `partial_answer` 的正文，保留 exact
Thread、physical Turn、item 身份与顺序；不展示 reasoning、原始工具参数或输出。按
item 身份合并通知、终态补读及重复事件，不做语义文本去重，模型在 final 中自行复述
的内容原样保留。Goal 跨物理 Turn 的去重至少包含 physical Turn 与 item ID。

普通/Side 复用 ADR 0020/0052 的非消费 observer；Goal 在原有唯一 logical stream tap
内提取。保留版本/整包指纹、shape、ownership 与 cursor 守卫。普通观察不可用后停止
周期观察 I/O；Side 在 observer 不可用、cursor 异常或原有 4096 条通知 high-water 时
降级到唯一 `run()`，不为阶段性推送增加消费者、绕过上限或轮询 ephemeral history。
降级可能暂停提前交付，终态从可取得的权威 typed items 补齐已知遗漏。SDK `0.162.1`
的 Side `handle.run()` 遇到 failed 会直接抛错、不返回 items；若先因 high-water 或 observer
异常降级，只能保留此前已经观察到的片段，降级后未观察且没有返回 items 的片段无法补齐。
不为此读取 ephemeral history 或增加消费者。Goal 只补读四证明锁定的最终 Turn，不补扫旧轮历史。

阶段性与最终正文分别投影，不机械拼接已交付片段。终态先停止运行中更新器，保留独立
消息的 attempted/confirmed item 记录，再尝试把全部阶段性答案与 final 更新到原卡。
同一冻结终态卡最多更新三次，重试和等待共用五秒预算。没有原卡、渲染失败或更新仍未
确认成功时，普通/Side/Goal 共用一次有界正文交付：按原生顺序，每个阶段性 item 对应
一次逻辑富文本发送，最后发送 final 或真实终态说明。SDK 继续负责重试和长文分片；
Netizen 不自行拆分 Markdown，不重写正文、不合并 item，也不增加后台补发队列。

终态只补投尚未尝试的独立阶段性消息。发送前已记 attempted 的 item 即使超时或返回
失败，也可能已有 SDK 分片送达，不重发整个 item；发送前的格式化失败仍可在终态尝试。
原卡更新结果未知时允许上述一次富文本交付，因此用户可能同时看到原卡和富文本正文；
这不是跨消息的恰好一次保证。只有 partial、没有 final 时，已确认交付
才可用简短收尾说明答案见阶段性内容；否则不能声称答案送达，也不能误报没有生成正文。
阶段性投递不触发 Completion Mention、DONE、Scheduled Run 完成回执或执行槽释放。
失败/中断时不把已有片段称为成功结果；任务是否结束仍由原生事实决定。

Presenter 的整卡更新按 exact 回复身份、generation 和 revision 串行收束；迟到的运行态
更新不能覆盖终态、新片段或关闭状态。初始发送、观察/更新失败及终态 handoff 继续有界，
不能改变或永久阻塞原生执行、槽释放和 Side 关闭。运行期间累计片段与去重/投递记录
仅在进程内，不写 Channel 数据库；一次更新失败不丢弃可供后续既有重绘使用的片段。

普通/Side 有阶段性答案时，终态正文交付共用十秒预算，阶段性消息最多使用五秒且不超过
本次交付预算的一半，为 final 留出时间；无阶段性答案的既有 final-only 发送预算不变。
Goal 沿用原生终态交付的二十秒总预算，图片准备、原卡更新、正文交付和控制卡恢复共享
剩余时间，正文交付还须通过 exact generation/logical run 守卫。超时只表示交付未确认，
不产生新执行、持久重试或阻塞后续生命周期的无限等待，也不承诺任意长度答案必达。

定时新话题的 `ScheduledOrigin` 保持更严格边界：已经尝试更新原终态卡而未确认成功后，
不再发布第二份结果；本地渲染失败等尚未发布的路径才可进入上述交付。阶段性消息不写
Scheduled Run 完成回执，final 之后才汇总；任一阶段性消息未确认时，final 成功也不能
把整次交付记为 sent。SDK 长文的各分片仍须确认 exact 聊天/话题归属。定时输入已有
Binding 的 `ScheduledBindingOrigin` 沿用普通回复规则。

## 整卡分页与容量

文件翻页、Goal 状态/暂停和正常终态都重绘完整投影。Goal 或 Partial Answer 与 Files
同卡时使用 v5 完整 Reply Card manifest，可选的阶段性模块随 callback 自包含；无这两项
的普通/Side 文件卡仍用 v4。旧 v4 和不含阶段性模块的 v5 继续严格解码，缺少新模块等同
没有阶段性答案，不读取旧飞书卡或数据库重建。手动 Goal resume 仍按新 run 重置。

阶段性答案不受 Activity 的四条/160 字摘要限制，运行卡容量和更新节奏沿用现有规则。
终态原卡无法承载正文时使用上述按 item 的富文本交付，不增加阶段性专用文本分段、
容量限额或持久化。普通/Side 的最终 Files 卡只携带 final 与 Files，不再复制阶段性答案。
Goal 正文交付之后可尽力恢复精简 Goal/Files 控制卡；Files 仍来自 exact 最终物理 Turn，
其 manifest 与分页不变。Files 所需的 Result 只放简短正文去向说明，控制卡不再重复
原生阶段性/final 正文；原终态卡正常更新时仍保留完整 Result + Files。

已知固定 Channel SDK 在 native Markdown post 遭格式拒绝后的纯文本降级可能遗漏 md
正文。本次按用户要求不修复此 SDK 行为、不新增 Markdown 转换或私有补丁，也不据发送
成功宣称 SDK 的所有降级都无损；平台内容展示和交付的限制须单独记录。

## 验证

覆盖普通/fork/Side/Goal 与 Progress 开关：初始无模块、多片顺序、重复/终态补读去重、
partial-only 收尾、final 自行复述、失败/中断、确认失败与未知投递、观察降级，以及阶段
消息零完成副作用。Goal 至少两轮证明 rollover 保留、旧轮重复/迟到事件不重复，暂停保留、
手动恢复重置；阻塞一次更新再触发终态/rollover/Side close，证明旧投影不能覆盖新状态。
完整重绘、v4/v5 旧卡、新阶段性 manifest、文件翻页、既有容量失败与恢复均需回归。
修订还需覆盖初始卡失败后继续阶段性投递、原卡重试耗尽后按 item 交付、未知独立消息
不重发、长文部分分片失败、final 时间预留、Scheduled Run 聚合回执与严格未知边界，
以及 Goal generation 变更、共享总时限和正文后的控制/Files 恢复。
合成测试、原生生成及飞书展示分别记录，接受此 ADR 不等于已经通过对应 live 门禁。
真实飞书验证只在明确授权的测试群/话题执行，不以本次设计修订视为已完成 live 验收。
