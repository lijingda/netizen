---
status: accepted
date: 2026-09-23
amends: 0020, 0039, 0052
related: 0029, 0046, 0069, 0070
---

# 将原生结构化问题的回答作为原会话普通输入

Codex 的结构化问题已经通过 SDK 的 `agentMessage.questions` 到达 Netizen，官方客户端
使用普通用户输入提交回答。我们为普通 Binding 和其中运行的 Goal 增加自包含问题卡片，
保留原会话归属，并复用现有输入准入、准备、start/steer 和反馈；不另建等待回答的执行
状态或问题生命周期。Side Topic 没有普通 Scope 的 current Binding，因此不纳入这条路径。

## 接收问题与展示

只识别 exact Thread/Turn 下的 `item/completed`，且 item 必须是带非空 `questions` 的
typed `agentMessage`。`delivery` 和 `phase` 不作为展示条件；`final_answer` 也不能证明
Turn 结束。题目和选项是独立的用户交互投影，保留完整内容，不套用 Activity 的 160 字符
commentary 裁剪，也不读取 reasoning 或工具参数/输出。

普通 Turn 复用已有非消费 observer 的同一个 cursor，在公开终态确认后、唯一 handle
stream drain 前再读取一次 retained events。Goal 复用 logical stream 的唯一 Tap；
每次物理 Turn rollover 后拒绝迟到旧 Turn 事件。生产 Channel 注册问题展示 handler 后，
这两条观察路径不受 Progress Card 开关影响。Side 原有观察和消费方式不变。
不新增 SDK subscriber、第二消费者、私有 RPC 入口或后台轮询循环。

Runtime 只在当前执行的内存中以 exact 物理 Turn + item ID 去重，问题发送是有界、
best-effort 的普通后台任务，由服务统一清理，不等待初始任务回执。生命周期 intent
沿用现有观察暂停；已经发起的发送可能在归档/删除后才到达，不另外维护每题或每个
执行的发送生命周期。晚到卡片仍在点击时校验原会话和普通输入准入。卡片失败或超时
不改变原生终态、Goal 收尾或输入准入。

每题一张卡片，提供建议选项和“自行填写”选项，再由用户点击提交。选中建议时只提交
该选项；只有选中“自行填写”时才读取非空文本，未选中的文本框不影响答案。
适配失败重绘时保留选项索引或自由文本来源。卡片自包含原 Binding、
原生 item ID、题号、完整题目/选项与回调 nonce。沿用卡片容量边界，不能截断后假称完整题目；
超限或投递失败仅作为展示失败。卡片独立于封闭的 Goal/Activity/Result/Files Reply Card
模块，不新增通用卡片注册表或持久 card session。

## 回答归属与普通输入

提交时从公开消息接口核验原卡片的 exact App/chat/topic，再核验卡片 Binding 属于该
Scope 且仍是 current Binding。这里的 current 指选中的会话，与是否正在执行无关。
若已切换，提示先切回原会话，不按点击时的新 Binding 重定向。

通过上述校验后才捕获普通 submission admission。随后核验实际点击者的 Open ID 与
姓名，并在原位置发送真实回答回执作为新的反馈/历史 upper anchor；机器人锚点的作者
不是回答者，旧问题卡的 ID 也不能充当本次回答的历史上界。锚点发送失败或身份无法确认
时不提交原生输入。专用 typed `CardAnswerProjection` 明确区分回答者、原问题卡与反馈
锚点，然后携带原 Binding 和已捕获 admission 进入共享 `_consume_prompt()`。

该入口沿用 Binding 的当前配置、上下文模式与普通输入消费策略：捕获时运行中则 steer
exact 当前 Turn，空闲则在同一 Thread 开始后续 Turn；准备期间发生切换、配置变化、Turn
结束或 Goal 换轮，仍按现有 race 拒绝，不排队、不改投、不自动转成新 Turn。归档、删除、
停止、压缩、观测不可用及其他原生状态继续由既有 Runtime 准入决定。原提问 Turn 已结束
或问题较旧本身不是拒绝条件；已经展示的卡片不绑定原 Turn 的答复期限。

`catch-up` 使用新回执作为 upper anchor，沿用有界历史读取、真实提交者归属及 cursor CAS；
回答成功接受后才推进 Context Boundary。引用/补充历史仍为 inert context。新启动任务的
回复与完成提醒归属回答者及回答回执；steer 保留原任务的结果锚点、发起人和反馈设置。
卡片回答不经过 slash control 解析；只有用户实际提交的答案可提供显式 Skill 引用，
模型生成的题目或 item ID 不能激活 Skill。

## 0.156.1 回答格式与交接结果

按固定版本 `rust-v0.156.1` 官方客户端格式生成 `questionItemId`：
`JSON.stringify(["request_user_input_async", messageItemId, questionIndex])`。
回答使用顶层 `<send_user_message_question_reply>` 包裹 JSON 数组，元素包含 `answer`、
`question`、`questionItemId`；题目 UTF-8 字节上限及过长 identity 的引用文本回退均跟随
该版本。模型生成字段中的 `$` 使用 JSON Unicode escape，解码后的内容不变；纯文本回退中的题目也将 `$` 表示为字面 `\u0024`，避免原生全文扫描误激活 Skill，卡片原题目和用户答案不变。
即使附带 catch-up，回答片段也不放进引用或补充历史中。这是普通用户输入格式，不是
专用 answer RPC，也不为 SDK 增加 question ID 字段。

依据：[原生提问实现](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core/src/tools/handlers/request_user_input_async.rs)、
[CLI 问题状态与身份](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/tui/src/bottom_pane/async_questions/state.rs)、
[回答片段](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/context-fragments/src/answered_question.rs)。

沿用 Channel SDK 的回调去重。只有进入共享 `_consume_prompt()` 前的卡片适配失败才
重绘新 nonce，允许用户修正后显式重试；nonce 只影响传输去重，不参与业务字段校验。
交接后直接复用 `_report_input_error()`，保留普通消息的错误原因、未知结果和恢复提示，
不维护卡片专用的 attempted/accepted/unknown 分类，也不根据执行结果重绘原卡。
同一用户、原卡和表单的重复投递由 SDK 有界去重；改选或修改回答是新的显式输入，
不是服务自动重试。需要重发时按聊天反馈在原会话直接发送回答。不承诺卡片只能回答一次，
也不把它标记为永久已回答。不形成待回答/已回答/过期状态表，不把题目、答案、卡片会话
或原生执行历史写入 Channel SQLite。

`item/tool/requestUserInput` 路径保持 SDK 当前处理，不由 Netizen 接管；本决定不以它将被
废弃为前提，也不宣称 SDK 的默认响应等同于有效的人类回答。

## 验证边界与移除条件

合成门禁覆盖 typed 事件、phase/delivery 无关性、exact identity、重复事件、关闭进度卡、
terminal drain、Goal rollover、发送超时与服务清理，以及互斥选项、重绘后改选、SDK 回调
去重、卡片 Scope/Binding 校验、点击时 admission、真实回答者/新锚点、catch-up、late answer、
竞争拒绝及共享错误反馈。仓库门禁为
`make check`；实际飞书卡片展示、表单回调和真实 start/steer/catch-up 的 live 验证仍待完成，
不能用合成通过代替已验证的真实兼容性结论。

沿用 ADR 0020/0052 的 exact SDK 版本、源码指纹和非消费门禁；若官方提供公开且不改变
通知消费时机的可多路复用 callback/snapshot，则将问题与 Activity 一并迁移并移除该
retained-event reach-through。不能借此扩展为任意通知或 RPC gateway。
