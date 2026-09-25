# Jev 与群聊自主触发：资料核验

核验日期：2026-09-20。本文是研究笔记与待验证建议，不是已接受的架构决策；未调用推理 API、上传聊天内容或修改运行行为。

## 已核实的公开能力

- Jev 是 TypeSafe AI 于 2026-09-15 宣布的 System One 决策模型，发布时为 early access。它接收状态与预先定义的问题，返回有限选项、评分或布尔概率，不生成回复、代码或推理解释。当前只接受文本，包括字符串和 JSON 状态。[发布公告](https://typesafe.ai/blog/introducing-system-one-models-and-jev)、[能力边界](https://docs.typesafe.ai/concepts/system-one)
- 三种原语为 Choice、Score、Noul；前两者有选项概率分布及 confidence，Noul 返回肯定答案的概率。多道独立问题可共享一个状态并行求值。[原语概览](https://docs.typesafe.ai/introduction)
- 当前官方模型为 `jev-1.13.0`；直连价格为每百万输入 token **$0.042**，输出免费。`jev-latest` 和 `jev-preview` 目前都指向该版本，但别名会移动。官方建议阈值调优后固定版本。当前额度为每分钟 1,200 请求、每秒 250,000 token，官方明确额度仍可能变化。[模型与价格](https://docs.typesafe.ai/models)
- 提供官方 Python 包 `typesafe-sdk`，含 `AsyncTypeSafeClient` 和同步客户端；HTTP 接口为 `POST https://api.typesafe.ai/v1/systemone`，使用 API key。无需为 Python 工程另起 Node 服务。[Python SDK](https://docs.typesafe.ai/sdk/python)、[官方源码](https://github.com/typesafe-ai/typesafe-sdk-python)、[HTTP API](https://docs.typesafe.ai/api)
- Vercel AI Gateway 已提供 `typesafe-ai/jev` evaluation 接口，也提供 TypeSafe 兼容入口；不能当作 OpenAI 兼容聊天接口调用。[Gateway 文档](https://vercel.com/docs/ai-gateway/modalities/evaluation)
- 官方明确英语是主要训练语言、准确率最佳；包括中文在内的 CJK 可以处理，但效果不齐，需要使用自己的非英语数据评测。[语言支持](https://docs.typesafe.ai/models#language-support)

## 必须区分的宣传与证据

- 发布公告声称端到端响应为 **70–500 ms**。首页的 **193.6 倍速度、444.6 倍成本优势**来自厂商自建工作流评测；公告自己说明这是实际收益较高端的情形，且比较方法与工作流选择存在局限。没有本项目部署区域到服务端的实测，因此不能把这些数字写成 Netizen 的延迟承诺。[公告与评测说明](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- “零幻觉”在公告中的直接依据是输出匹配 schema，并不证明语义判断正确。官方能力文档明确：概率校准描述一组预测与实际结果的关系，不能保证单次判断正确。[公告](https://typesafe.ai/blog/introducing-system-one-models-and-jev)、[校准边界](https://docs.typesafe.ai/concepts/system-one)
- `confidence` 是从概率分布计算的集中程度，不等同于“本次回答正确的概率”；不能把 `confidence > 0.95` 宣称为误触发率低于 5%。阈值要在相同问题定义、模型版本及目标语言上用标注样本验证。[Confidence](https://docs.typesafe.ai/confidence)、[Vercel 阈值说明](https://vercel.com/i/jev-probabilities-and-thresholds)
- 2026-09-17 审核的已知缺陷明确列出：容易按字面理解、多跳判断较弱、无关长上下文降低准确率；state 默认不视为敌对数据，注入指令和误导性表述可以改变答案。因此分类结果不能授予执行权限。[Jev 1.13 已知限制](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

## 对 Netizen 的设计建议（待验证）

建议把 Jev 限定为“是否让 Codex 接收这条群消息”的前置判断器。确定性规则负责模式开关、消息来源、去重与已有显式触发；Jev 负责识别没有显式触发的消息是否在寻求助手介入；Codex 仍承担对话与执行。这符合厂商推荐的有限分类与代码控制流程的分工。[适用场景](https://vercel.com/i/when-to-use-jev)

1. 初版保留现有显式触发路径，建议在普通 Binding 上显式启用自主模式，分别对应群主会话或话题，不引入全群继承；它额外评估该范围的普通文本消息。缺少接收权限或 SDK 会提前过滤的情况，必须先解决接收边界；模型不能补回未收到的消息。
2. 将“明确求助”“当前任务的明确跟进”“助手可以主动补充”“普通成员间对话”“上下文不足”分开定义。初版优先接收求助和相关跟进，再评估主动补充；“有用”不能直接等同于“应该打断群聊”。不确定、超时或服务错误时保持安静，显式触发仍可用。
3. 给判断器的状态仅包含当前消息、必要的短上下文和明确助手职责。上下文引用与当前消息分开，保留原始消息归属；分类结果只是一项路由依据。不要伪造 `@`、把被动历史转换为新指令，或据此放宽原生工具权限。
4. 若有 Turn 正在运行，自动接收的消息同样会影响当前任务；必须单独定义跟进消息的接收标准并保持精确 Scope/Binding/Turn 路由。不得将无关群消息汇总为任务或添加另一套会话历史。
5. 初期离线评测，再做只计算拟触发结果、不发言的观察阶段。样本覆盖中文口语、反问、引用、多人对话、机器人输出、明确求助、任务跟进、诱导分类以及缺上下文。测每千条普通消息误触发数、应触发消息召回率、实际触发精度、p50/p95 延迟、错误率及总成本；阈值在独立样本上验证。

以上是建议；现有工程约束见 [AGENTS.md](../../AGENTS.md)。上线前仍需在对应设计文档和 ADR 中确定消息接收、持久化设置与执行语义。

## 当前代码的接入位置与约束

以下来自本地源码检查，不表示已完成接入或真实飞书事件验证。

- 当前有两层过滤：[main.py](../../netizen/main.py) 的 `build_channel()` 设置 `require_mention=True`，[channel_app.py](../../netizen/channel_app.py) 的 `handle_message()` 再次拒绝普通群聊中未 @ 的消息。仅修改其中一处不能实现自主模式。需要让唯一 Channel 接收到候选消息，再按 exact 群主线或话题及其当前 Binding 的模式过滤。
- 固定 `lark-channel-sdk==1.4.0` 的公开 `GroupOverride.require_mention` 和 `FeishuChannel.update_policy()` 提供 chat 级策略能力，topic/Binding 级判断仍属应用。其入站流程在调用应用 handler 前已可能进行卡片/转发归一化和发送人姓名解析；因此 Jev 接入能避免后续大量准备及 Codex 执行，但不是在所有网络 I/O 之前过滤。SDK 的 self-sent 防护也不能代替自主分支的其他 bot 过滤。依据为本地固定依赖的 `channel/channel.py`、`channel/normalize/pipeline.py`、`channel/safety/policy_gate.py` 及 `channel/config.py`；依赖版本见 [pyproject.toml](../../pyproject.toml)。
- 安装器已经声明 `im:message.group_msg` 和 `im.message.receive_v1`，参见 [权限模板](../../scripts/feishu_app_onboarding.py) 与 [部署权限契约](../deployment.md#前置门禁)。真实租户能否收到未 @ 的群主线、话题消息仍需验证，声明不能替代投递证据。
- [_prompt()](../../netizen/channel_app.py) 在历史、引用和图片准备前捕获 `SubmissionAdmission`，随后交给同一个 Runtime 校验并提交。Jev 的异步判断应纳入这一边界：调用前固定 Binding、Turn 和配置修订，返回后复核；不能重新选择当前会话，也不能将已经结束的 Turn 的跟进变成新任务。[Runtime admission](../../netizen/codex_runtime.py)
- 空闲时消费消息会启动 Turn；运行中消费会 steer 当前 exact Turn。因此两种状态应分别评估。自主分支宜先限普通 Binding 的真人文本，其他机器人、纯图片和缺少必要上下文的消息不自动触发；显式 @ 继续沿用既有能力。未知状态、正在停止、Goal 和生命周期操作沿用 Runtime 的现有准入结果。[运行语义](../design.md#运行与锁)
- 触发选择与 `current-only|catch-up` 是两个问题。Jev 跳过一条消息不应推进 catch-up 的 Context Boundary；后续触发仍可能把它作为历史背景交给 Codex。因此“减少唤醒”不能宣称“噪音永不进入上下文”。若还要筛选历史内容，需单独定义截断、遗漏与背景语义，不能通过推进边界丢弃消息。[ADR 0039](../adr/0039-add-binding-scoped-mention-catch-up-context.md)
- 短上下文不是现成缓存：ADR 0039 目前明确不监听、缓存或持久化未 @ 的入站正文。每条候选都执行完整 catch-up 读取会增加网络和准备耗时。若新模式需要短暂内存窗口，必须明确修订该边界，限定 exact Scope、启用时点、容量、有效期和切换时清理，且不成为另一套会话历史、不写正文到 SQLite。另一候选是按需复用现有只读历史端口获取小快照；两者的延迟和质量需要比较。
- 消息分类失败、限流或超时应使自主分支暂时不触发，并保留 @ 路径；不对每条被跳过的群消息发错误回执，不为每条不确定消息调用 Codex 兜底。仅在确定提交后进入已有任务反馈链。slash/control 仍需既有显式入口，分类器不能把普通讨论升级成管理命令。
- 启用模式时需要让群成员知道未 @ 的候选文本及必要上下文会发送给 Jev，并且入选消息会进入 Codex 原生历史；这是新增的数据流。[现有上下文可见性契约](../adr/0039-add-binding-scoped-mention-catch-up-context.md)

## 建议验证顺序

1. 准备人工标注的中文样本，比较简单规则、Jev 以及合适的廉价分类基线。先明确“应消费”的标准，再调阈值，并保留独立测试集。输入包括当前消息、回复对象、少量同 Scope 上下文、助手职责及运行状态；不需要把完整仓库或完整 Thread 历史交给分类器。
2. 分别评估空闲唤醒和运行中跟进。除精度、召回与 p95 延迟外，重点报告每千条无关消息误触发数，以及每天多出的 Codex 调用。例：一万条无关消息的 1% 误触发率意味着一百次额外介入；这只是算术示例，不是 Jev 的实测表现。阈值不能仅用模型返回的 confidence 命名为准确率。
3. 若离线结果合格，再在显式启用的测试会话中进行只判断、不提交 Codex 的观察实验；只保留必要的有界诊断数据，实验不创建持久消息库。随后小范围开放真实触发，保留快速退回 @ 模式的方式。
4. 实现阶段补行为测试并运行 `make check`；覆盖显式 @ 不依赖 Jev、默认模式不变化、重复事件、机器人消息、分类超时/限流、Scope 隔离、模式切换、Binding 切换和 Turn 完成竞态、历史仍为背景、skip 不推进边界。按部署文档对修改后的 Channel 接收边界执行相关 live 验证。

## 成本估算与待验证项

按直连标价算，每日 10,000 条候选消息、每条总输入 2,000 token（含问题及历史），每天为 2,000 万 token，分类费用约 **$0.84/天，$25.20/30 天**。这是算术估算，不包含后续 Codex、网络、网关、重试或税费；上下文增长时费用也会增长。[计价依据](https://docs.typesafe.ai/models)

对比现有“只处理 `@`”基线，这会新增分类费用，并可能新增 Codex 调用；其价值是用较低成本扩展主动介入能力。不能据此宣称总成本比现有模式更低。相较“每条消息都调用大模型”，是否节省以及节省多少仍需实测。

尚未验证：当前账号是否有访问资格、部署地区实际可达性与延迟、中文群聊质量、与简单规则及其他廉价分类方案的比较、正式 SLA、具体账号的数据保留配置。官方称不使用客户请求/响应训练模型，并为企业客户提供 ZDR；“不训练”不等于当前账号默认零保留。[数据处理说明](https://docs.typesafe.ai/legal)

实际集成还须限制请求时长与重试：官方异步客户端支持配置，默认会重试；它会隐藏日志中的密钥头，但不会隐藏请求与响应正文，因此不能打开会泄露群聊内容的 SDK 调试日志。[异步客户端](https://docs.typesafe.ai/sdk/python/api/clients/async)
