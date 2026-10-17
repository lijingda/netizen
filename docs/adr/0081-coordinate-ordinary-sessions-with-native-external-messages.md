---
status: accepted
date: 2026-10-15
amends: 0023, 0030, 0061, 0067
related: 0069, 0070, 0073, 0078, 0079, 0080
amended_by: 0083
---

# 普通会话协作使用原生工具形态与 ExternalMessage

> [ADR 0083](0083-page-native-session-history-through-a-narrow-read-adapter.md) 改用按需目录分页和固定只读历史分页适配，发送不再预扫归档目录。

用户需要让一个普通会话创建、发现、读取和联系其他普通会话，并在飞书话题中直接查看和
干预。采用 Codex CLI `rust-v0.162.1` 已使用的六个独立工具形态，由模型决定协作步骤；
Netizen 负责实例目录、飞书位置、访问范围和输入交接，Codex 继续拥有执行与历史。
本决定随用户确认设计并授权实施接受，功能验证与部署状态单独记录。

## 工具与身份

首版提供 `create_thread`、`list_threads`、`list_archived_threads`、`read_thread`、
`send_message_to_thread`、`wait_threads`。参数及结果尽量沿用原生 camelCase 契约，
发现增加可选搜索和分页，Netizen 位置、Binding 和接受状态使用明确的扩展字段。
分叉、改名、归档/恢复的模型工具留待后续；现有人工控制继续可用。

`threadId` 始终表示完整原生 Thread ID，经已有精确查询映射到本实例普通 Binding。
不导入外部 CLI/App 会话，不把 Side 或子 agent 纳入普通会话目录。尚无原生 Thread 的
Binding 只能展示本地元信息，不能伪造 Thread ID 或因发现/读取而物化。
现有 `/resume`、卡片和 Admin 管理继续使用 Binding 身份；ID 改动限于说明、展示与复制。

## 创建、输入与复用

创建在来源 chat 中发布新的普通 sibling topic，继承来源 Project/cwd 和模型，支持原生
可选 `model`。新 Binding 关闭表情闪烁和结束 @，复制来源过程卡设置。普通用户之后可
在该话题继续对话或使用现有控制。只设置新 Scope 的 current，不切换来源。

话题发布与身份确认、普通 Binding 创建、在途创建所有权、订阅和回复消费复用已有机制；
即时创建不是 Scheduled Plan，不保存调度 Run，也不借 Side 或 fork 表达空白独立上下文。
新话题已被其他输入建立 Binding 时失败，不覆盖已有 current。

可选 `title` 交给 ADR 0067 的既有后台命名任务，使用同一任务占位、名称写锁和失效
规则；提供标题时直接尝试写入一次，不创建生成标题的 fork。未提供标题时沿用自动
生成，输入可见性证据扩展为 exact Turn 内与本次 ExternalMessage 的 namespace、name、
output 完全匹配。命名不走人工 rename 的 lifecycle，不影响已接受任务或后续输入；
本次失败后不连续重试，后续新 Turn 仍无名且旧任务已收尾时沿用现有补名机会。

新工具首条任务和后续来信由可信工具入口构造 SDK `ExternalMessage`，保持工具来源。
2026-10-16 补充：出站 ExternalMessage 使用 CLI `rust-v0.163.0` 的 `codex_tui`
兼容 namespace 与固定 `<codex_delegation>` envelope，携带真实来源 Thread ID，并按
CLI 顺序转义正文的 `&`、`<`、`>`。该 namespace 是原生委派协议标签，不宣称消息来自
真人或另一个 CLI 进程；MCP server 名、Netizen 权限与飞书来源展示保持原有身份。
原生识别 `create_thread`／`send_message_to_thread` 后，在 preview 为空时从该工具输入
生成 preview；后续输入不覆盖非空 preview。配合公开 `thread_list(use_state_db_only=True)`
使仅收到协作来信的新普通会话可正常发现。保持工具级不可信角色，不制造 userMessage，
不额外运行模型、写原生库或回填旧空预览。升级 SDK 时需复验格式、角色与目录可见性；
若原生提供公开通用委派／预览入口，再以该公开契约替代特定 namespace 适配。

向 exact Thread 提交，由原生在接收时决定启动新 Turn 或加入正在执行的普通 Turn。
这构成对现有精确 Turn 输入规则的窄扩展：普通飞书、问题卡与定时输入仍按各自原有
start/steer 契约处理。不能通过正文、`is_bot` 或模型自填来源获得新入口。

加入同一 Turn 时沿用原消费者和结果归属；原生返回新 Turn 时按其实际身份交接。
显示一条注明来源会话的机器人消息，再内部提交一次输入，不依赖飞书回调启动。
复用 Channel 自身消息过滤及既有 TTL 去重，不增加协作消息表或全局身份就绪门槛。
已接受的 Thread/Turn 回执不等待命名；首轮展示有界尽力执行，展示超时或调用取消
不能将已知接受改写为投递未知。仍沿用普通展示 finally 释放结果消费者。
失败提示同样有界尽力展示；展示异常、超时或取消均保留已确定的原错误回执及接受状态。

目标不是所在 Scope 的 current 时，发现/读取如实报告状态，发送明确拒绝；工具不得
自动激活、恢复归档、另建替代会话或改投当前会话。等待只观察既有目标。
原生明确拒绝和副作用未知必须区分，后者沿用既有失败隔离边界，不自动重放。

当前固定 Python SDK 对本连接管理的 active Goal 拒绝普通 `turn(ExternalMessage)`。
保留现有 Goal 准入限制，不为该组合新增 RPC adapter 或修改 SDK。Goal 只读预检失败
或状态未确认时，来信尚未提交，返回明确未接受；不能据此推断投递结果未知。

## 范围与进程边界

Admin 提供实例统一的两组范围：发现、读取与发送（包括等待）。每组独立选择全部、
仅指定聊天、排除指定聊天或关闭，默认全部，按目标 chat ID 动态匹配。
只有发现权限时不返回正文预览；关闭发现但允许读发时可凭精确 Thread ID 操作。
创建也检查目标聊天的读发范围。每次工具入口捕获现行规则并检查一次，发现过滤及摘要
可见性使用同一份规则；处理中修改权限不撤销本次调用，下一次调用使用新规则。目标
身份、当前会话与生命周期检查保留；规则变化使旧发现游标在下一次调用失效。
该规则仅约束这组协作工具，不是用户 RBAC、Project ACL 或所有原生工具的信息流隔离。

复用现有实例 YAML 的安全写入保存范围，共享同一进程内现行配置。除普通会话登记外，
不增加协作过程持久化、历史副本、消息队列、协调器或新配置层。
工具使用同进程临时 MCP 入口，复用 Scheduler 的传输与生命周期机制；通过公开进程级
`config_overrides` 增加独立 server entry，保持用户 MCP、指令与配置文件不变。
create/send 的原生审批语义继续交给 Codex；没有新增私有 RPC 或 SDK patch。

历史仍只读原生 Thread。默认 read_thread 对本功能的工具来源和委派 envelope 提供
`codexDelegation.sourceThreadId/input` 摘要；其他工具输出仍由 `includeOutputs` 控制。
协作摘要保留工具角色，沿用既有输出预算，不复制历史到 Channel 存储。

实施需完成 `make check`、真实 bundled App Server 的 ExternalMessage 接入验证及受影响
的飞书展示验收。真实 App Server 搭配本地 mock provider 的通过不等于真实模型自主调用
或飞书客户端已验收；证据与剩余边界写入部署文档。
