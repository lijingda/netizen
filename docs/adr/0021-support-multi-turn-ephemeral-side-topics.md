---
status: accepted
date: 2026-08-15
amends: 0008, 0014
related: 0009, 0010, 0016, 0020
amended_by: 0048
---

# 用 ephemeral fork 支持多轮 Side 话题

> [ADR 0029](0029-project-current-message-provenance-into-prompts.md) 明确区分 Side 首轮的
> 原 `/side` 来源消息与新话题 seed 完成锚点；seed 继续作为 reaction 与完成投递锚点。
> [ADR 0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md) 进一步让容器内的
> Side Turn 复用普通 Turn 的任务反馈与 Reply Card；根卡、路由、过期和关闭生命周期不变。

## 背景

用户需要从当前 Codex 会话旁开一个临时讨论分支，并在飞书中连续追问多轮，而不是只做
一次性 fork。公开 Python SDK 已提供
`AsyncCodex.thread_fork(thread_id, ephemeral=True)`，但当前高层 facade 尚未暴露 App
Server 的 `thread/inject_items` 和 `thread/unsubscribe`。飞书侧还必须把这个临时分支放进
一个新话题；从已有话题触发时，新话题必须是同一 chat 下的 sibling，不能嵌套或继续写入
原话题。

Side 的 native Thread 是 ephemeral，服务重启后不能恢复。与此同时，飞书旧话题仍会继续
收到消息。如果不保存一个最小路由墓碑，这些消息会被普通 Scope/Binding 路由接管并意外
创建持久会话。

## 决定

### 单一运行时与原生边界

1. 继续使用一个长驻 `AsyncCodex` 和一个 `CodexRuntime`。Side 不创建第二个 App Server、
   agent runtime、scheduler、workspace、`CODEX_HOME` 或历史模型。
2. Parent 必须是当前 active、已物化且可确认的持久 native Thread。Parent 普通 Turn 为
   `running` 时允许 fork；`stopping`、Goal、compaction、lifecycle mutation、外部未知
   active 状态和 Lazy Binding 均拒绝。Parent active pointer 后续变化不影响已经创建的
   Side。
3. 使用公开 `thread_fork(..., ephemeral=True)` 创建 Side，并读取 exact Thread 验证 ID、
   `ephemeral=True` 和可用时的 `forked_from_id`。
4. 增加可移除的 `SideThreadControl`。它只提供两个方法：向 exact Side Thread 调用固定
   `thread/inject_items` 注入固定 Side boundary，以及调用 exact
   `thread/unsubscribe`。实现复用同一个已初始化 SDK client 和安装 SDK 的 generated
   request/response model，不暴露通用 `request`，不复制协议，不做隐式重试，也不按版本
   allowlist 放行。任一响应丢失都视为状态未知。
5. `notLoaded`、`notSubscribed`、`unsubscribed` 三种 unsubscribe status 都表示本次关闭
   已达到无订阅终态。高层 facade 出现等价公开能力后，migration sentinel 必须要求删除
   对应 shim。

### 飞书 Side Topic 与持久路由

1. `/side` 和 `/side <首轮问题>` 在当前 Scope 的 active Binding 上创建 Side。创建前先按
   `(app_id, source_message_id)` 原子写入 `creating` 占位，保证 webhook 重投不会重复 fork
   或发多个话题。
2. 始终向 underlying `chat_id` fresh send 一张根卡片。若发送响应已经携带非空
   `thread_id`，它就是新话题；否则对 exact 根消息再次 send，并固定
   `reply_in_thread=True`、`reply_target_gone="fail"`。不能使用会留在当前话题的普通
   `reply()`，也不能接受 SDK 默认的 `fresh` 降级。
3. 两步发送都严格校验 success、raw code、message/chat/root/parent/thread ID 和未分片
   结果。从已有话题创建时，新 `thread_id` 必须不同于来源 topic。带首轮问题时，无论根
   响应是否直接返回 `thread_id`，都对 exact 根消息发送一条明确标注来源的问题 seed，
   并以它作为 origin 和 reaction anchor；来源位置创建成功后不发文字回复，失败仍明确
   报告。展示副本有界并中和原始 `<at>` 标签，Codex 仍接收未改写的完整问题。无首轮
   问题且必须靠 seed 建话题时，seed 只提供简短引导。root 与
   seed 分别使用稳定且互异的确定性 UUID；transport exception 或 retryable result 只允许
   再发一次相同 UUID 做有界对账，不能换 UUID 或隐式循环重试。飞书公开接口只承诺 UUID
   的一小时去重窗口，没有 lookup-by-UUID 能力，因此目标 app 必须通过 live gate 证明重放
   返回原消息的 exact identity 且不重复建话题；否则 Side 不得上线。若两次响应都丢失，
   或服务端接受 fresh root 后进程在持久化返回 identity 前崩溃，本地仍无法发现已被服务端
   接受的 orphan root/topic；启动时只能过期 source-keyed reservation，无法为未知 topic
   建墓碑。这是 V1 明确保留的外部系统 crash window，永久墓碑保证只覆盖已经持久化
   root/topic identity 的 Side。
4. `side_topics` 只保存 Side ID、app/chat/topic/root/source、Parent Binding ID、creator、
   mention policy、状态和时间。它不保存 ephemeral native Thread ID、prompt、回复、Turn、
   配置、历史或卡片 session。`closed`、`expired`、`failed` 记录永久作为路由墓碑。
5. Side Topic route 在普通 Scope/Binding 前查询；除 topic ID 外，也按 inbound root ID
   回退查询，以覆盖 promotion 响应丢失。terminal 墓碑只返回明确提示，绝不成为普通
   Binding。每次 `ServiceCore.start()` 在注册 handler 前把遗留 `creating/open` 原子转为
   `expired`；单纯打开 Store 不改变生命周期。
6. P2P 及 P2P 话题不要求 @；群主线、普通群话题和话题模式群中的 Side 每条消息仍要求
   @。判断依据是 underlying `chat_type`，不是统一为 `TOPIC` 后的 Scope kind。飞书对 P2P
   建话题和事件字段的真实支持是发布 live gate；错误 230071 必须显式失败。

### 多轮 Runtime 与控制面

1. Side Session 只存在内存，以 Side ID 为键，保存 exact Parent、ephemeral
   `AsyncThread`、创建时的 Binding Turn Settings 与 Task Feedback 解析快照、当前 handle、
   独立 admission revision、话题标识和 monotonic activity time。两类快照在 fork 前后按
   revision 复核，Parent 后续配置变化不传播。
2. idle 消息在同一 Side Thread 上开始新 Turn；running 消息 steer exact handle。引用、
   图片和 `$skill` 使用现有 Prompt preparation，但在异步准备前捕获 Side admission，提交
   时校验 revision，避免 idle/running/idle ABA、关闭或过期后的延迟投递。
3. Side completion 直接使用公开 `AsyncTurnHandle.run()`。ephemeral Thread 不套用普通
   持久 Thread 的 history polling、completion recovery 或 release gate。产品接受极快
   Turn 的 `turn/completed` 通知竞态风险；发布门禁不增加 Side 专项 completion-race
   探针，也不改变普通 Turn 现有的持久化终态恢复。
4. Side 中仅允许普通 Prompt、`//`、`/status`、`/stop`、`/help`、`/` 和
   `/side close`。`/stop` 只中断并清理当前 Side Turn，成功后同一 Side 可继续；其他
   lifecycle/config/Goal 命令和嵌套 Side 均拒绝。Side 话题参与者共享控制权，不增加
   creator-only ACL。
5. Side idle 满两小时后过期；active Turn 不计时，终态后重新开始完整窗口。idle timer
   不进入普通 `wait_idle()` task 集合。正常 shutdown 在关闭 Codex transport 前有界并发
   关闭所有 Side。
6. 统一 close 顺序为：Side 锁内停止 admission 并快照 active，锁外 interrupt 并等待
   `handle.run()` 给出 exact terminal evidence，随后对 exact Side Thread 请求 terminal
   cleanup，再 unsubscribe，最后写 terminal 墓碑、移除内存 Session 并更新根卡片。不能
   持 Side 锁等待 consumer；interrupt 成功只证明请求已接收，drain 超时仍必须停在
   non-admitting `closing`。已知 handle 上的 interrupt、cleanup 或 unsubscribe 结果未知
   时保留 Session 和非 terminal route，允许显式 close/shutdown 重试。`turn/start` 响应或
   `handle.run()` 终态未知则可能仍有前台副作用，必须关闭全服务 native admission，保留
   non-terminal route，并要求 transport 重启；此时不能以 cleanup/unsubscribe 冒充终态。

## 验证与发布门禁

仓库必须保留以下测试：

- Adapter generated-model shape、固定 boundary/payload、三种 unsubscribe status、响应
  丢失不重试和 facade migration sentinel；
- 同一 Side 至少三轮、running steer、Parent/Side 并发、admission ABA、stop 后继续、
  cleanup/unsubscribe 重试、idle expiry 和 shutdown 顺序；
- 五类飞书入口、两种 topic promotion 返回、fresh sibling、确定性 UUID、source 重投
  幂等、root fallback、terminal tombstone、P2P/群 @ 规则、初始 prompt origin 和卡片防
  篡改；
- 目标环境 live phase：materialized Parent running -> ephemeral fork -> Parent/Side Turn
  overlap -> boundary -> Side 多轮 -> cleanup/unsubscribe -> Parent 仍可用。另做五类飞书
  真实入口、多个 Side 并行以及 root/seed 相同 UUID 重放的 exact identity 验收；P2P Topic
  与 UUID 对账不能只由 fake test 宣称支持。

## 后果与移除触发器

Side 的模型上下文继承 Parent 创建时的历史，但之后独立；两个 Thread 仍共享同一真实 cwd，
因此文件修改会互相可见。飞书话题消息可以保留，但服务重启后只作为 expired 墓碑，不提供
原生恢复。

飞书 [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN)
与 [回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply?lang=zh-CN)
文档给出 UUID 去重窗口和长度约束，但没有按 UUID 查询已发送消息的接口；因此上述 same-UUID
exact identity 验收是发布门禁，不是可由本地单测替代的协议事实。

官方 Python SDK 一旦公开 inject/unsubscribe 等价高层 API 并通过 synthetic/live 行为验证，
删除 `SideThreadControl` reach-through，改用公开 facade；ephemeral fork、多轮 Session、飞书
route 和墓碑语义保留。不得把本 Adapter 扩展成通用私有 RPC、持久 Side native ID、提示词
模拟边界或第二套 Agent Runtime。
