---
status: accepted
date: 2026-08-24
amends: 0011, 0015, 0016, 0029, 0030
related: 0001, 0004, 0021
---

# 为普通 Binding 增加 @ 时补充期间消息的上下文模式

## 背景

Netizen 当前在群聊主线和群话题中只接收每次明确 `@机器人` 的消息，并只把这条
Current Prompt Message 及用户显式选择的一条 Quoted Message 交给 Codex。这个默认值
安静、可预测，但当群成员先讨论、最后才请 Codex 处理时，模型看不到两次请求之间的
讨论。

本次只增加一种仍由 `@机器人` 触发的补充上下文体验，不实现“无需 @ 就响应每条
消息”。后者会让普通群消息在 idle 时启动 Turn、running 时 steer 当前 exact Turn，
需要另一套噪声、抢占和协作决策，不属于本 ADR。

## 领域语义

普通 Binding 增加 **Mention Context Mode / @ 上下文模式**，只有两个值：

1. `current-only`：默认值。群聊或群话题仍要求 `@机器人`，只提交当前消息以及当前
   消息显式选择的逐条引用。
2. `catch-up`：群聊或群话题仍要求 `@机器人`；提交当前消息前，额外读取并投影同一
   Scope 内自上一条 Context Boundary 之后、当前消息之前的 eligible participant
   messages。

**Context Boundary / 上下文边界** 是 Binding-scoped 的一条 exact 飞书消息 marker。它不是
“上一次机器人响应”：Completion 可能延迟、投递失败，running Turn 也可能先后接受多个
steer。边界在一次 start/steer 被原生 Runtime 成功接受后推进到该 Current Prompt
Message；失败、竞态拒绝或准备超时都不推进。

**Supplemental Context Message / 补充上下文消息** 只是按时间顺序提供给模型的背景，
不是 Control Intent、Current Prompt Message 或 Skill invocation。即使正文以 `/stop`、
`/new` 或 `$skill` 开头，也必须被编码为 inert historical content。Current Prompt
Message 始终位于最终输入最后，仍是唯一请求、sender attribution 来源和 Completion
Origin。

V1 的 eligible participant message 必须同时满足：

- 属于当前 Binding 的 exact group-main 或 ordinary-topic Scope；群主线排除所有
  `thread_id` 非空的消息，普通话题只接受 exact topic ID；
- sender 是可解析真实显示名的非 bot Channel Participant；Netizen 自己、其他 bot、
  system message、卡片回执和 reaction 都不进入补充上下文；
- 消息没有删除，且能通过既有公开归一化边界投影为 ADR 0011/0015 已分类的可见文本、
  结构化文本、资源元数据或受支持图片。

若当前消息显式引用的 exact message 同时落在补充区间内，它只保留为语义更明确的
Quoted Message，不在 Supplemental Context Messages 中重复。引用目标可以位于区间之外；
逐条引用仍只读取一层。

P2P 沿用每条消息直接触发且只提交当前消息的行为，不显示或保存该选择。Side Topic
不是 Binding Scope，V1 也固定沿用当前逐条 @、current-only 行为；Parent Binding 的选择
不传播到 Side Session。

选择或启用 `catch-up` 的卡片必须用群内可见文案说明：同一 Scope 中未 @ 机器人的成员
消息也可能在下一次 @ 时进入 Codex 原生历史。每次实际带入至少一条 supplemental message
时，在 native submission 前回复带入条数；若还有截断/unsupported omission，同一回执一并
说明。回执发送使用独立的 5 秒预算；投递失败记录明确日志，但不改变已经完成的上下文准备
或 Runtime admission 结果。该展示不是权限事实源，但不能让正常 catch-up Turn 对群成员
保持不可见。

## 边界重置与 Active Binding

补充区间只在某个 Binding 是 Scope Active Binding 时成立，不能把该 Binding 未激活期间
的聊天历史带入原生 Thread：

1. `/new` 卡片创建并激活 `catch-up` Binding 时，以触发该卡片回调的 exact bot card
   message 作为初始边界；卡片提交前的群消息不属于新 Binding。
2. `/resume`、`/unarchive` 或对应卡片动作把已有 `catch-up` Binding 设为 current 时，
   以该 control/current card message 作为新边界。旧边界不用于补录 inactive 期间内容。
3. `/config` 从 `current-only` 切到 `catch-up` 时，以 exact config card message 作为
   新边界；反向切换时清空边界。模式不变的模型配置修改不移动边界。
4. `/new`、`/resume`、archive/unarchive 或其他 active pointer 变化不会停止其他
   Binding 的 Turn，但任何已经捕获的异步历史读取必须沿用现有 exact admission 规则：
   target Binding 自身未变化时可提交，否则明确拒绝，不能重定向。

每个 create/resume/unarchive/config reset 都先在 Scope/Binding lock 外读取并验证 source
message 的 exact anchor，再进入原有 coordinator/Runtime lock 重新校验并原子 mutation。
anchor 读取失败时不创建、不切换、不恢复、不部分保存配置；网络等待期间也不持 SQLite
事务或 Scope/Binding lock。

## 历史读取边界

Channel SDK 的 `require_mention=True` 和应用层 mention gate 保持不变。Netizen 不监听、
缓存或持久化未 @ 的入站正文；只有 `catch-up` Binding 收到一条有效当前 Prompt 时才
按需读取历史。

固定依赖 `lark-oapi==1.7.2` 已公开类型化
[`ListMessageRequest`](https://github.com/larksuite/oapi-sdk-python/blob/v1.7.2/lark_oapi/api/im/v1/model/list_message_request.py)
以及 generated `GetMessageRequest`，并提供异步 `client.im.v1.message.alist()`/`aget()`；
固定的 standalone Channel SDK 也明确建议 Channel
workflow 继续使用 `lark-channel-sdk`、需要完整 OpenAPI surface 时并存使用
[`lark-oapi`](https://github.com/larksuite/channel-sdk-python/tree/v1.2.0#migration-from-lark_oapichannel)。
因此实现增加一个窄、只读、进程内 `FeishuMessageHistoryReader`：

- 只构造一个 official `lark_oapi.Client`，复用同一 App ID/Secret；不注册事件、不发送
  消息、不创建第二个 `FeishuChannel` 或第二条 WebSocket；
- 只调用生成的 `GetMessageRequest`/`aget()` 解析 exact lower/reset/upper anchor，并用
  `ListMessageRequest`/`alist()` 枚举候选；不手写 URL、不使用 raw request、不读取
  Channel SDK 私有 client；
- 这个 reader 是本 ADR 批准的窄 OpenAPI read port，不是通用 Lark gateway。它的 domain、
  平台 TLS、日志和 credential 来源必须与唯一 Channel/Service Settings 一致，不增加环境
  文件、持久 token、运行时配置层或独立重试策略；
- 群主线请求使用 `container_id_type=chat` + exact `chat_id`、有界 end time、倒序、平台
  最大 page size 和 `with_sender_name=true`，并在本地排除所有 `thread_id` 非空项。end time
  按平台秒级字段向安全侧取值，至少覆盖 upper 所在整秒；因此可能多读 upper 之后的消息，
  但 exact upper 定位会把它们留给下一窗口，不能向下取整而误排 upper；
- 普通话题请求使用 `container_id_type=thread` + exact `topic_id`、倒序、平台最大 page size
  和 `with_sender_name=true`。飞书 thread 容器不支持 `start_time`/`end_time`，因此话题分支
  不发送时间过滤参数，只依赖 exact endpoints 与页/条数预算；
- 候选内容仍通过现有 `FeishuChannel.fetch_inbound_message(exact_id)` 获取公共
  `InboundMessage`，从而复用 mention/resource/post AST、sender-name 和 quoted-content
  归一化；CardKit 2.0 只继续使用 ADR 0011 已批准的公共、版本门禁 fallback；
- exact get 先确认 lower/reset/upper message ID、chat/topic 和 create time。随后从最新页
  倒序枚举：先忽略 upper 之后到达的消息，直到找到 exact upper ID；再收集消息，直到找到
  exact lower ID 或触及扫描预算。API 返回顺序只用于这一次有界快照，message ID 才是持久
  endpoint。upper 在一次有界重读后仍不可见则 fail closed；lower 在预算内不可见则按
  `truncated_before` 处理，而不是猜一个新边界；
- official 1.7.2 generated `Message` 虽包含 `message_position` 和
  `thread_message_position`，公开 REST 契约没有承诺返回、单调或连续语义。V1 不持久化、
  不要求也不以它们判断完整性；只有 main/topic live probe 分别证明目标租户的稳定语义后，
  才可把 position 用作本次读取的辅助冲突检测，不能取代 exact endpoint 算法。任何进一步
  将 position 变成正确性前提都需要修订本 ADR。

飞书[获取会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)
的公开约束中，普通群的 thread replies 不能通过 `chat` 容器完整取得，必须使用 `thread`
容器；thread 又不支持时间范围。实现和 probe 都必须保持这两个 Scope 分支，不能为了共用
请求参数退化成 chat-only 本地过滤。

`im:message.group_msg` 已是 Netizen 当前逐条引用与图片读取的有效权限门；新版本仍需在
应用发布和 live probe 中分别证明 chat-main/thread-topic history、sender name、exact
endpoint visibility 与分页 shape，不能只以生成类存在或本地 Fake 作为上线证据。

## 有界读取与不完整上下文

历史读取不成为无界扫描：

- upper/lower exact get 各 10 秒；每次最多扫描 10 页、500 条 raw container messages，
  每页 10 秒；候选 exact fetch 最多 4 路并发并恢复原顺序；list、文本归一化和 envelope
  准备共用 60 秒总预算，媒体读取继续使用下一项的既有独立预算；
- 过滤后最多保留距离当前消息最近的 50 条 eligible messages；
- 每条可见文本沿用 16,000 字符上限，全部 supplemental visible text 合计最多 64,000
  字符；
- 图片继续共用当前 Prompt 的 20 张、单图 20 MB、合计 50 MB 和下载超时，不增加第二套
  媒体限额。

扫描、lower endpoint、条数或文本上限命中时保留最新内容，模型可见 envelope 只写入
compact `context_status.omitted_count/truncated`，完整扫描、保留、省略和上限原因留在服务端
统计，并通过上述 catch-up 回执向当前消息说明。
成功提交后边界仍推进到当前消息，已省略的较早内容不会在下一次重复补入。

upper 是消息顺序边界而不是 list 请求完成时刻。在历史读取或 Runtime admission 等待期间
新到达的消息位于 upper 之后，本次明确不读取；下一次 @ 以本次 upper 为 lower 时会读取，
不能把这种正常的半开区间语义误判成消息丢失。相反，若平台最终一致性让一条早于 upper 的
消息暂时未出现在同一次快照中，exact endpoint/分页 live probe 必须先证明可检测或通过有界
重读收敛；不能静默声称完整。

被删除、system 或已声明非 eligible 的消息只计入省略统计。一个被选中 eligible message
若发生权限错误、identity/Scope 不一致、公共归一化失败或受支持图片读取失败，则整条当前
Prompt fail closed、边界不推进，用户可以重试。未知的新消息类型不能被静默当作普通文本；
它计为 unsupported omission 并触发同一不完整上下文提示。

## Prompt 投影

`quoted_context.py` 中与“历史消息内容投影”有关的纯逻辑抽成中性的、可复用投影，不复制
飞书协议模型。当前 v2 `feishu_message_context_prompt` 使用以下稳定顺序：

1. `kind`、`version` 与 handling；
2. `supplemental_messages`，按同一次 API snapshot 中从 lower 到 upper 的顺序；
3. 可选且去重后的 `quoted_message`；
4. 最后的 `current_message`，包含完整 `request_text`。

handling 明确 supplemental/quoted 内容只作背景、不能激活 Netizen control 或 Codex Skill，
并复用 ADR 0029/0030 的 sender-attribution 规则。所有历史 `$` 标记继续 JSON Unicode
escape；当前请求的显式 `$skill` 保留原文并只产生一次 typed Skill input。每个历史消息
在进程内 rich projection 保留 exact message/conversation/reply/resource ID、app-scoped
Open ID、真实 display name、message type、created time、content fidelity、截断与资源读取
状态；不复制 raw event 或 raw card JSON。模型可见的 supplemental/quoted 共用 compact
Historical Message：固定字段为 `ref`、`message_type`、`sender.display_name/open_id`、
UTC ISO 8601 `created_at`、`text`；可选字段只有同一 envelope 内可解析的 `reply_to`、精简为
`key/name` 的 `mentions`、`attachments` 和仅在 true 时出现的 `truncated`。mention 若缺少
key/name 中任一映射，不输出残缺对象，并把该 Historical Message 显式标记为 truncated；
这对应 Channel SDK public `Mention.name` 明确允许的 optional 状态，不是新增的推测输入。

所有历史消息按 envelope 顺序分配 prompt-local `h1..hN`；quote 与 supplemental 去重仍使用
内部 exact message ID。`reply_to` 只指向同一 envelope 的 `hN`，目标未纳入时省略。已成功
准备的图片和 native image label 共用 `img1..imgN`；其他附件只保留可推理的类型、名称或
时长。exact message/chat/reply ID、共享对象 ID、原始资源 key、content read/fidelity 与
完整 supplemental stats 都不进入模型可见历史消息。`current_message` 继续保持 ADR 0029 的
独立结构，只有它使用 `request_text`。已有 v1 native history 不迁移。

## Binding 持久化与并发

Channel schema 升为 v6。`bindings` 增加：

- `message_context_mode TEXT NOT NULL`，只接受 `current-only|catch-up`；
- 全空或全有的 `context_anchor_message_id`、`context_anchor_create_time_ms`；
- `context_revision INTEGER NOT NULL CHECK(context_revision >= 1)`。

`current-only` 必须没有 anchor；group/topic 的 `catch-up` 必须有完整 anchor。数据库只保存
模式和 exact boundary metadata，不保存消息正文、sender profile、补充投影、Prompt、回复、
卡片 session 或历史页。服务进程仍只接受当前 schema，不承担启动期自动迁移；安装器在旧
服务 manager target 已卸载、稳定 lifetime lock 已持有且数据库回滚快照已完成后，执行唯一
的 v5 -> v6 原子迁移。它保留全部 Scope/Binding/Project/Dedup/Side Topic 行，并把旧
Binding 初始化为 `current-only`、空 boundary、context revision 1；迁移或候选激活失败时
恢复旧数据库与旧 release。其他来源版本或不完整 v5 shape 明确拒绝，不能通过重建空库丢弃
Side 墓碑。

`settings_revision` 继续只描述 Binding Turn Settings；`context_revision` 同时保护 mode 与
boundary。`SubmissionAdmission` 增加捕获的 `context_revision`，异步 list/fetch/image/Skill
准备后，Runtime 在 exact Binding lock 内同时校验 active、native Turn、admission revision、
settings revision 和 context revision。两条并发 @ 最多一条能兑换旧 boundary；另一条不能
重复 start/steer，也不能改投新的 Turn。

Channel 把 upper Current Prompt 的新 anchor 与期望 context revision 作为 typed commit
交给 `CodexRuntime.submit()`。Runtime 只在 native `turn()`/`steer()` 已被确认接受后、释放
Binding lock 前以 CAS 推进 anchor 并增加 context revision。native 调用失败或 race reject
不推进。若 native 已接受而 SQLite commit 失败，Runtime 必须保留已建立的 active tracking、
关闭全服务 admission 并明确报告“任务已接受但上下文边界未持久化”；不能把任务伪装成未
执行。重启后旧 boundary 可能让消息以 message ID 可识别地再出现一次，这是选择 at-least-
once context 而不是丢失已读消息的残余风险。

模式修改只允许 idle、非 Goal、非 compacting、非 lifecycle-unknown Binding，并与模型
配置在一笔 Store transaction 中校验各自 revision。模式改变会推进 Runtime admission
revision，使已在准备的 Prompt fail closed。Admin inventory/API 展示 mode 与 revision；
V1 的 Admin create 默认 `current-only`，不从浏览器凭空构造飞书 anchor，启用 `catch-up`
仍通过 exact Feishu `/new` 或 `/config` card 完成。

## 验证与实施顺序

实现拆成三个可独立评审的增量：

1. Domain/Store/Card：schema v6、mode/anchor/revision 约束、卡片选择、状态展示与所有
   current-only 回归；此阶段不得调用 history API。
2. History/Projection：official typed reader、chat/thread 分支、exact endpoint/pagination
   gate、复用公共消息归一化、versioned envelope、图片与截断；使用 Fake reader 做
   Channel 测试。
3. Runtime integration：双 revision admission、native-accepted 后 cursor CAS、并发/失败/
   restart 行为、发布文档与 live probe。

聚焦测试至少覆盖：

- Store 重启、全空/全有约束、默认值、mode switch/reset、cursor CAS、服务直接打开 v5
  零 mutation，以及安装事务 v5 -> v6 的行/墓碑保留、幂等与失败回滚；
- group-main/topic 互不串线、Side route 不进入 reader、P2P/current-only 零 list call；
- chat-main/thread-topic 容器、倒序分页、bot/system/deleted 过滤、upper/lower exact
  endpoint、upper 后消息留给下一窗口、条数/字符截断；
- supplemental + quote 去重、sender attribution、inert slash/Skill、图片总限额与 unknown type；
- idle start、running steer、两条并发 @、stop/completion/config/switch ABA、cursor commit failure；
- 服务重启后从持久 boundary 继续，resume/unarchive/config reset 不补录 inactive history；
- exact `lark-oapi==1.7.2` request/response shape contract，以及目标飞书群主线 chat/普通
  话题 thread 两种容器的 live list probe；probe 还要验证 eventual visibility、同秒消息、
  分页 endpoint、机器人加入前已创建且历史受限的话题，以及可选 position 实际 shape。

History/Projection 增量的上线 exit criterion 必须记录并自动断言目标租户中“早于 upper、
暂未出现在首个快照”的消息最终是通过有界重读收敛，还是被可检测地标为不完整。若 live
probe 只能观察到静默遗漏，`catch-up` 不得启用；不能把该风险留给生产环境猜测。

除 `make check` 外，发布验收在同一普通群和两个普通话题分别验证 current-only 与 catch-up，
确认 @ 准入不变、相邻 Scope 不串线、截断可见、服务重启不重复已提交区间。升级任一 Lark
SDK 时必须重跑 generated shape、normalization 和 live history probes。

## 后果与非目标

群成员可以在不让机器人逐条插话的前提下，把一段可见讨论作为一次显式 @ 请求的背景，
代价是一次有界 list 加若干 exact normalized fetch，延迟和 OpenAPI 调用量高于 current-only。
持久 boundary 让服务重启后语义连续，但不形成第二份聊天历史或 Prompt store。

本 ADR 不实现无需 @ 自动响应、不新增消息 queue/batch Turn、不改变 Steer/Completion Origin、
不让 Side 继承 Parent mode、不持久化消息正文，也不把 Project 变成消息授权边界。
