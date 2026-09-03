---
status: accepted
date: 2026-08-09
amended_by: 0055
---

# 支持飞书逐条引用消息上下文

> [ADR 0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) 将精确依赖升级到
> 1.4.0，并重新确认首层 `ReplyRef` 与 CardKit 2.0 quote flattener 两个缺口仍存在；
> 运行时版本门禁已相应收窄到精确 1.4.0。下文的 1.2.0 叙述保留为原决定的历史证据。

> 后续 [ADR 0015](0015-support-native-image-inputs-for-message-and-quote.md)
> 取代本文“不下载资源”的决定：普通 `image` 与 `post` 图片会在有界准备后作为
> Codex 原生视觉输入；引用关系、其他消息类型和 exact-Turn 语义仍以本文为准。
> [ADR 0029](0029-project-current-message-provenance-into-prompts.md) 再把 envelope
> 升为 v3：被引用消息类型矩阵不变，最后的当前消息改为带发送者归属的结构化对象。
> 2026-09-01 的 wire 收敛把 quote envelope 升为 v4：内部仍保留本文的 rich
> projection，但模型可见的 `quoted_message` 改用与 ADR 0039 supplemental message
> 共享的 compact Historical Message；已有 v2/v3 原生历史不迁移。

## 背景

飞书把“普通引用回复”和“话题回复”编码在同一组消息关系字段中，但两者
不是同一个产品语义。[飞书消息管理概述](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/intro)
规定：

- 普通首层回复的 `root_id == parent_id == 被引用消息 ID`；
- 普通嵌套回复的 `root_id` 仍是根消息，`parent_id` 是直接被回复的上一层；
- 话题中的每条消息都按“回复话题根”表示，`root_id` 和 `parent_id` 都指向
  根消息，并以非空 `thread_id` 标识话题。

[回复消息 API](https://open.feishu.cn/document/server-docs/im-v1/message/reply?lang=zh-CN)
也将 `reply_in_thread` 定义为“以话题形式回复”，且对已在话题中的消息默认继续
话题回复。因此用户的理解正确：真实话题根/话题回复是 Scope 结构，不能伪装成
一条被用户选中的逐条引用。

固定的 `lark-channel-sdk==1.2.0` 存在一个公开归一化缺口：它只在
`parent_id != root_id` 时构造 `ReplyRef`，所以会丢掉恰好满足
`parent_id == root_id` 的普通首层引用。截至本 ADR 日期，
[`channel-sdk-python`](https://github.com/larksuite/channel-sdk-python) 的发布版和
`main` 都没有修复，因此暂无可直接升级的官方版本。

## 决定

### 关系选择

1. 非空 `conversation.thread_id` 永远表示话题结构，不解析引用、不额外查消息。
2. 非话题消息优先使用 Channel SDK 公开 `ReplyRef.message_id`，并与公开
   `InboundMessage.raw.parent_id` 交叉校验；冲突时 fail closed。
3. 只对精确的 `lark-channel-sdk==1.2.0`、缺少 `ReplyRef`、且
   `parent_id == root_id` 的组合，从公开 `raw` 恢复首层引用 ID。其他无法
   解释的组合不猜测。
4. SDK 版本变化即拒绝这条 fallback；不读 `_pipeline`/`_client`、不修改
   `site-packages`、不复制 SDK 协议模型。

这是第二个受版本门禁的 Channel 边界 workaround，与现有的话题根 mention
修复一样必须由契约测试固定。它不是新的消息协议层。

### 内容读取与投影

只在已有 active Binding 的普通 prompt 上读取引用；命令、无 Binding 消息和
话题消息不触发网络请求。命中后：

1. 以 10 秒单次请求预算调用 Channel SDK 公开
   `FeishuChannel.fetch_inbound_message(exact_parent_id)`；
2. 校验返回消息 ID 与对话 ID 精确一致、不是话题、且未删除；
3. 应用卡片 `interactive` 若类型化结果只剩 `[interactive]` 占位符，再以独立的
   10 秒单次请求预算调用 SDK 公开 `fetch_quoted_context(exact_parent_id)` 取可见
   卡片文本。两次独立网络请求不共享一个会造成假超时的总预算。

锁定的 SDK 1.2.0 还有一个已在真实 CardKit 2.0 消息上复现的公开归一化缺口：
`QuotedContext.text` 为空，因为 SDK 的 quote flattener 不遍历顶层 `header`/`body`；
同一个公共 `QuotedContext.raw` 字段仍提供精确消息项。只在 `text` 为空、卡片明确为
`schema == "2.0"` 且 SDK 精确为 1.2.0 时，Channel 边界从这个公共字段投影可见
`plain_text`/`markdown`/`lark_md` 节点。适配器最多接受 256,000 字符原始卡片、
4,096 节点、32 层、512 个文本片段和 64,000 字符中间文本；不投影 `value`、
`confirm`、`options`、`behaviors` 或 `events`。它不复制整段 raw JSON，也不读取
SDK 私有对象或修改 `site-packages`。版本变化或边界异常时 fail closed。

[接收消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/message_content)
列出飞书查询可返回的多类消息；本版本使用如下确定性矩阵：

| 引用类型 | 处理 |
| --- | --- |
| `text`, `post` | SDK 归一化文本；图片/文件节点的 exact key 与元数据保留在内部 rich projection，模型可见 wire 只保留必要附件关联 |
| `interactive` | SDK 可见文本；占位符走上述公开 fallback，1.2.0 空文本 CardKit 2.0 再走有界、版本门禁的公共 `raw` 可见文本投影 |
| `calendar`, `general_calendar`, `share_calendar_event`, `location`, `video_chat`, `todo`, `vote`, `hongbao` | SDK 归一化的结构化可见文本 |
| `merge_forward` | SDK 现有 3 层/50 条边界内的展开文本，显式保留 truncated 标记 |
| `image`, `file`, `folder`, `audio`, `media`, `sticker` | exact 资源 key、类型、文件名、时长、封面 key 等保留在内部 rich projection；compact wire 只保留可用于推理的类型、名称、时长或本地图片引用 |
| `share_chat`, `share_user` | 说明引用了群/个人名片；exact chat/user ID 只留在内部投影，不额外读取对象详情 |
| `system`, `unknown` 或新类型 | 显式拒绝本次提交 |

投影只包含一层被引用消息，不递归追溯它自己的引用。输入为版本化 JSON
envelope：被引用内容明确标记为背景，当前请求放在最后。v4 的模型可见
`quoted_message` 使用 compact Historical Message：固定包含 prompt-local `ref`、
`message_type`、`sender.display_name/open_id`、UTC ISO 8601 `created_at` 和 `text`；只有确实存在时
才加入 `reply_to`、精简为 `key/name` 的 `mentions`、`attachments` 和值为 true 的
`truncated`。单独 quote 的 ref 为 `h1`；其 reply target 不在同一 envelope 时不输出
`reply_to`。Channel SDK 的 public `Mention.name` 是 optional；真实归一化输入缺少 key/name
任一映射时不输出不可解释的 mention 对象，并显式标记 Historical Message truncated。

exact message/chat/reply/shared-object ID、资源 key、content fidelity/read 状态和详细截断
原因仍保留在进程内 rich projection，用于校验、去重、图片下载与 fail-closed 决策，
但不再复制到模型可见历史消息。`open_id` 暂时保留用于同名发送者归属；用户可见正文中
本来就出现的 ID-like 文本不做改写。引用文本最多 16,000 字符，mention 和资源描述
各最多 64 项；raw 事件和 raw 卡片 JSON 不进入 prompt。`current_message` 继续按
ADR 0029 保持独立结构，只有它的 `request_text` 表示本次要执行的请求。

本版本不下载资源。[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2?lang=zh-CN)
最大可返回 100 MB 二进制流，而当前 Channel SDK 高层方法在下载前不提供可验证的
MIME/长度门禁。在 SDK 提供有界公开 API 前，不为“原生图片输入”旁路读 raw
OpenAPI 或无界下载。

### 精确 Turn 竞态

引用查询增加了一段不可持有 Binding 锁的网络等待。为保持“running 输入只
steer 它到达时看到的 exact Turn”，运行时在查询前捕获
`(binding_id, admission_revision, thread_id, turn_id)` 条件，查询期间不持锁，
提交时再在同一 Binding 锁内校验并一次性消费。

revision 在每次 start/steer 线性化前、每次 `/stop` 尝试（包括 idle）、
`running -> stopping` 以及 active Turn 释放时递增。
这使 idle -> Turn A -> idle 的 ABA、Turn A -> Turn B、其他 steer、`/stop` 和两条
并发引用都会使旧条件显式失效。旧消息不排队，不从 steer 转 start，也不从
start 转去 steer 另一个 Turn。

admission 同时固定消息到达时选中的 Binding。查询期间另一个参与者执行 `/resume`
只改变该 Scope 后续消息的 active Binding，不撤销已捕获 Binding 的 admission；只要
原 Binding 自己的 Turn/revision 未变化，本条引用仍提交给原 Binding。

### 失败与权限

超时、无权、撤回/删除、精确目标不一致、无可见卡片文本、未知类型和
admission 失效都只回复一条可操作错误，不调用 Codex start/steer。不自动重试，因为
重试时 Binding/Turn 可能已变。

[获取指定消息](https://open.feishu.cn/document/server-docs/im-v1/message/get?lang=zh-CN)
要求机器人在目标群内。应用身份读取群消息还需 `im:message.group_msg`；只有
`group_at_msg` 不足以在用户 @ 机器人时回查另一条被引用消息。这一新权限必须
由飞书应用版本发布后才能用于群聊。

飞书应用可用范围、当前会话成员关系以及“被引用消息必须与当前消息属于同一
chat”的精确校验共同构成可见性边界。按产品权限决定，凡能在该 Scope 使用机器人的
参与者，都可以把上述 SDK 公共类型化内容作为引用上下文交给 Codex；exact ID 仍在
Channel 内部参与校验和资源读取，模型可见 wire 只保留 compact 字段与 app-scoped
sender `open_id`。这不放宽 bot 的 OpenAPI 权限，也不表示仅凭内部资源 key 已读取资源
正文；用户可见正文中本来存在的 ID-like 文本不另行改写。

## 验证

- SDK 契约测试固定 1.2.0 的普通首层缺口、嵌套 `ReplyRef`、话题非引用和
  Card 1.0/2.0 可见文本差异。
- Channel 测试覆盖首层引用、应用卡片 fallback、无 Binding/命令/话题零查询，
  以及查询失败零提交。
- 内容测试覆盖全部已分类消息类型、内部 exact ID 保留而 compact wire 不暴露、raw payload 不复制、
  CardKit 2.0 只投影可见文本、隐藏交互值不泄漏、版本门禁、单层投影和有界截断。
- Runtime 测试覆盖空闲/运行正常兑付、双 token、普通 steer、stop、completion、
  idle ABA、Turn A -> B、跨 Binding 和关闭 admission。
- 发布前除 `make check` 外，手工验收 P2P/普通群首层与嵌套引用、Card 1.0/2.0、
  话题跟进、撤回和缺权限。

## 后果与移除触发器

本实现不持久化引用内容，不增加 prompt 队列或第二个历史模型，不改变群聊/话题
每条重新 @ 机器人的准入规则。代价是每条逐条引用执行一次归一化读取；该读取内部
可能触发卡片再取或 3 层/50 条以内的合并转发展开。若卡片结果仍只有占位符，
再执行一次可见文本 fallback；每次 SDK 网络请求分别受 10 秒预算约束。读取期间任何
exact-Turn 状态变化都要求用户重发。v4 compact Historical Message、sender
`display_name/open_id` 和用户可见正文会作为 native Codex 输入进入其原生 Thread 历史；
exact message/chat/reply/resource ID 只在 Channel 内部使用，不进入历史 wire。这是上述
飞书准入边界下有意接受的可见性，而不是脱敏遗漏。

上游修复仍是长期路径，当前精确版本门禁和独立移除触发器以
[ADR 0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) 为准：

1. 同时更新 `pyproject.toml` 和 `requirements.lock` 的 exact pin；
2. 当官方发布版在“非话题且首层 `parent_id == root_id`”上正确产生 `ReplyRef`，并通过
   本 ADR 的 typed fetch 契约后，独立删除 raw relation fallback；
3. 当公开 `QuotedContext.text` 能覆盖真实 CardKit 2.0 `header`/`body` 后，独立删除
   公共 `QuotedContext.raw` 卡片适配器；
4. 保留话题 `thread_id` 分流和 exact-Turn admission，并重跑话题根 mention 契约和全部
   发布探针。

在官方修复发布前，不将未合并 fork、未发布 commit 或本地 wheel 作为生产依赖。
