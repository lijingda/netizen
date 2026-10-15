# 核心心智模型

[手册目录](user-guide.md) · [命令索引](commands.md)

## Scope、会话与 Project

- 一个普通单聊、群聊主线或普通话题各自形成一个 Scope。每个 Scope 可以有多个会话，但同一时刻只有一个当前会话。
- 会话必须绑定一个已登记且启用的 Project，也就是 Codex 实际工作的目录；Netizen 不使用
  实例或服务工作目录作为默认兜底。
- `/new` 只创建并切换到一个 Lazy 会话；首条真实任务才创建原生 Thread。
- 没有当前会话时，可用的聊天默认配置允许普通任务消息自动创建会话并执行。主线与各话题
  共用所属聊天的创建默认值，但各有独立的当前会话和上下文。
- 原生 Thread 和历史由 Codex 保存，能够在 Codex App/CLI 中继续使用。CLI/App 中新增的消息不会自动回填到飞书。
- 每条真实任务或 steer 都会把当前飞书消息的公开发送者信息交给 Codex，并随原生输入进入
  Thread 历史。它只说明请求来源，不授予权限、任务所有权或更高指令优先级。Channel SDK
  会从当前 chat 成员名单补全真实显示名；解析失败时本条消息不会执行，并会提示管理员为
  飞书应用开通 `im:chat.members:read`、发布应用版本后重试。发送者归属 ID 只保留当前
  应用内的 `open_id`，不会在 sender attribution 中加入 `union_id` 或租户级 `user_id`。
- 当前任务正文（普通消息、Goal objective、Side）保留真实 @ 的名称与位置，包括触发任务的
  机器人自身；其他真人或机器人还附带当前应用内的 Open ID，供 Codex 区分同名对象。
  自身用专门标记表示，不附带自身 ID。这些正文会随原生任务保存；引用及补充历史保持原有
  表示。输入中的标记仅提供信息，不会自动把模型输出转换为飞书 @ 或授予权限。
- Netizen 只在内存跟踪本进程实际打开的 Thread 订阅。当前会话空闲十五分钟后自动取消
  当前连接订阅；切换到其他会话后，旧会话一旦空闲就立即尝试取消。没有会话数量上限或
  LRU，服务重启不会为这项空闲释放策略扫描全部历史、resume 会话或重建旧订阅 timer。
- 不同会话或 Side 可以并发，但同一 Project 使用同一个真实目录，文件改动会互相可见。并发修改同一文件时应由用户自行协调。
- 飞书应用可用范围和群成员资格决定谁能使用机器人；Netizen 不另设用户、群或角色
  白名单。能向机器人发送有效消息的参与者可以管理所在 Scope 的会话或当前 Side，
  不按会话创建者划分权限。各参与者共用部署服务账号的 Codex 工具与文件访问能力；
  应由部署者把应用限制在受信用户和群范围内。Admin Web 通过飞书登录并核对独立的实例管理员名单。

## Turn、steer 与排队

- 当前会话空闲时，普通消息开始一个新 Turn。
- 当前 Turn 正在运行时，普通消息固定 steer 这个精确 Turn：它用于补充条件、纠正方向或追加要求。
- 若 steer 恰好碰上 Turn 已结束，本条消息不会执行；看到提示后需要重新发送。
- Netizen 不保存 prompt queue，不会把运行中的新消息悄悄排成下一轮，也不会把多条消息合并成一个 prompt。
- `completed`、`interrupted` 和 `failed` 都只结束当前 Turn；即使本轮失败，Thread、历史和
  Binding 仍保留，下一条消息可以在同一 Thread 开始新 Turn，不必为保留上下文而换会话。
  若 Codex 返回具体中断原因，回复或进度卡会一并显示；该任务仍是中断状态。
- 若暂时无法确认 exact Turn 状态，Netizen 只做一次最多 5 秒、最多三次原生 I/O 的
  短恢复，其中最多 resume 一次。恢复 exact `inProgress` 就继续正常无时限轮询和 steer；
  确认 terminal 就交付结果。仍不可验证时显示 `turn-observation-unavailable`，保留
  exact 槽但停止自动 I/O，只隔离这个 Binding。`/sessions` 会提供一次有界重新检查、
  停止、归档和删除；归档/删除不要求 Turn 观测先恢复。已确认 terminal 后的最终文本
  读取失败也不会重新进入恢复。
- 若确实想开始独立任务，应等待当前 Turn 结束、先 `/stop`，或切换/新建另一个会话。不同 Binding 可以并发。

相关说明：[会话管理](sessions.md)、[停止任务](status-and-control.md#stop)、
[运行反馈](feedback-and-files.md)、[任务失败与状态未知的处理](faq.md#turn-失败后还要换会话吗)。
