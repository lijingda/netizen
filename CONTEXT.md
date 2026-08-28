# Netizen 领域词汇

**Main Qualification / main 代码资格**：exact `main` commit 的 required CI 已成功完成
统一本地代码门禁。它是正式发布复用的代码可靠性结论，不包含账号、租户、模型提供方或
目标主机的实时兼容性结论。

**Release Integrity / 发布完整性**：证明正式制品来自具有 Main Qualification 的 exact
tag/commit，且构建后的 manifest、摘要和不可变 Release 资产保持一致。它不重新执行代码
测试或 Live Compatibility Probe。

**Published Release / 已发布 Release**：由具有 Main Qualification 的 exact commit 构建、
通过 Release Integrity 验证并经官方 Release 渠道提供的不可变安装候选。安装到另一台
受支持主机时只重复该主机自己的 Host Validation。

**Source Install / 源码安装**：把当前工作区（包括本地未提交修改）作为未获发布者认证的
候选进行安装。它与 Published Release 安装共享激活事务，但必须自行通过完整本地门禁。

**Host Validation / 主机验证**：每次安装都对目标账号、解释器、依赖、配置、原生 SDK、
飞书权限，以及按原服务意图需要启动时的 ready 条件执行的验证。它不替代 Published
Release 的 Main Qualification / Release Integrity，也不替代 Source Install 的完整本地
门禁。

**Live Compatibility Probe / 实时兼容性探针**：需要真实 Codex/飞书账号、模型提供方或
目标服务环境的开发验证。它在相关 SDK、Adapter、原生生命周期或环境变更时按需运行，
用于形成或更新兼容性结论，不是每次 merge 或 Published Release 的通用门禁。

**Binding Scope / 普通 Scope**：独立承载 active Binding 指针的飞书位置：P2P、群聊
主线或一个普通真实话题。被 Side Topic Route 占用的话题不再解释为 Binding Scope。

**Feishu App Binding / 飞书应用绑定**：当前 Netizen 实例使用的 exact 飞书应用身份。
更换 App ID 会进入新的 Binding Scope 命名空间；旧 Channel 记录和原生 Codex 历史继续
保留，但不迁移到新应用的 Scope。

**Thread Binding / 会话**：Scope 内的 Channel 记录，保存本地 ID、Project alias、
可空 native Codex Thread ID、可选的 Binding Turn Settings、Binding Task Feedback、
Mention Context Mode、对应 revision、creator 和时间。它不复制 Codex 历史或已生效的
原生 Thread 配置。

**Active Binding**：Scope 中普通消息默认进入的 Binding。`/new`、`/resume` 只切换
这条指针，不停止其他 Binding 的 Turn。

**Parent Binding**：创建 Side 时捕获的 exact active、materialized Binding。后续 Scope
active pointer 改变不重定向已创建的 Side。

**Side Topic / Side 话题**：通过 `/side` 在同一 chat 中新建的 sibling 飞书话题，承载
一个多轮、ephemeral native Thread。它不是 Binding Scope，也不拥有 active Binding。

**Side Session**：只存在于当前服务进程内的 Side Runtime 状态：ephemeral Thread
handle、当前 Turn、创建时 Turn Settings 快照、admission revision 和 idle timer。服务
重启后不能恢复。

**Side Topic Route / Side 墓碑**：Channel Database 中不含 native Thread ID 的最小
路由记录。`creating/open` 只在当前进程有效；`closed/expired/failed` 永久阻止旧 Side
话题落入普通 Binding 路由。

**Archived Binding / 已归档会话**：仍保留在 Channel Database、但其 native Thread
位于 Codex archived catalog 且不作为 Scope active pointer 的 Binding。归档状态不写入
Binding；`/sessions archived` 每次从 Codex 读取，恢复后原 Binding Turn Settings 与
Binding Task Feedback 不变。

**Native Codex Thread**：由 SDK-pinned App Server/CLI 管理的连续上下文。普通 Binding
Thread 持久化到标准 `.codex`；Side Thread 是不持久化到历史 catalog 的 ephemeral
例外。lazy Binding 在首条真实 prompt 开始前没有 native ID。

**Ordinary Thread Subscription / 普通会话订阅**：Netizen 当前 App Server 连接因
start/resume 持有的、面向一个普通持久 Thread 的事件订阅。取消它不会删除 Binding 或
原生历史，也不等同于 App Server 已卸载 Thread。

**Idle Subscription Release / 空闲订阅释放**：普通会话在没有原生活动时让出上述连接
订阅的生命周期动作。它不是 Stop、Archive 或 Delete；下一条消息仍恢复同一个原生 Thread。

**Turn**：`AsyncThread.turn()` 创建、由 `AsyncTurnHandle` 控制的一轮原生执行。
Netizen 只在内存保留当前 handle，不持久化 Turn。

**Turn File / 本轮文件**：一个已完成的普通持久 Turn 通过公开 latest aggregate Turn
diff，或 completed `fileChange` / `imageGeneration` item 指向的当前文件。Project 只是
相对路径解析基准，不是文件授权边界；新 v4 卡片在 callback payload 中自带 absolute path
与完整 manifest，切换 active Binding 或服务重启不改变旧卡片的操作来源。
_Avoid_：“产物快照”“Turn 完成时版本”，因为 Netizen 不保存内容、摘要或修改检测，
点击时发送的是该路径当前仍可访问的普通文件。

**Steer**：运行中 Binding 的下一条普通消息，固定映射为同一 handle 的
`steer()`，不是 queue 或新 Turn。

**Current Prompt Message / 当前 Prompt 消息**：产生本次模型请求的那条飞书入站消息。
它的发送者只表明请求来源，不赋予权限、所有权或指令优先级；它可以不同于被引用消息和
完成投递锚点。发送者归属只使用当前飞书应用内的 Open ID，不建立跨应用或租户级身份关联。

**Mention Context Mode / @ 上下文模式**：普通 Binding 对一次显式 @ 请求选择的上下文
范围；`current-only` 只使用当前消息，`catch-up` 还补充同一 Scope 中上一条 Context
Boundary 之后的 eligible participant messages。两种模式都不让未 @ 消息自行触发任务。

**Context Boundary / 上下文边界**：`catch-up` Binding 上最近一次被原生 Runtime 成功
接受的 exact Current Prompt Message。它不是机器人回复完成时间，也不保存边界间消息正文。

**Supplemental Context Message / 补充上下文消息**：`catch-up` 为当前显式 @ 请求读取的
历史参与者消息；它只作 inert background，不是 Current Prompt、Control Intent 或 Skill
invocation。

**Completion Origin / 完成投递锚点**：本次任务的运行态展示与最终回复所依附的飞书消息。
它通常是当前 Prompt 消息；Side 首轮则是新话题中的问题 seed，且不反向定义模型请求来源。

**Control Intent**：不交给模型的客户端操作，如 new、sessions、resume、status、
stop。slash command 和卡片动作必须进入同一路由。

**Per-message Quote / 逐条引用关系**：非话题消息明确选择一条此前的飞书消息作为当前
Prompt 的单层上下文；它不创建或改变 Scope。
_Avoid_: 引用回复（容易与话题回复混淆）

**Quoted Message / 被引用消息**：逐条引用关系中被用户选中的那一条精确历史消息，
不是当前 Prompt，也不是它自己的上游引用链。
_Avoid_: 父消息（在话题关系中含义不同）

**Topic Relation / 话题关系**：带 `thread_id` 的消息与真实飞书话题的结构关系；
其中的 root/parent 都表示话题根，不表示逐条引用。路由层先判断它是否属于 Side Topic，
只有未命中 Side route 时才把它解释为普通 Topic Binding Scope。
_Avoid_: 话题根引用、引用消息回复

**Settings Surface / 设置界面**：显示在触发它的 Feishu Scope 中、由该 Scope
参与者共同操作的客户端界面。它以无持久化状态的分区导航承载不同配置界面；当前
只实现 Projects 分区。它管理 Project Registry，但只代理 Codex-owned Setting，
不复制其值。`/config` 是会话级原生配置入口，不属于这个实例级界面；它也不等于实例
管理员使用的 Admin Control Plane。

**Admin Control Plane / 管理控制面**：实例级浏览器管理界面，用于集中查看和管理
Project、普通 Binding 与 Side Topic。它与飞书入口共享同一个 application service、Scope
coordinator、Runtime、Store 和 Codex client；不是 Prompt Channel，也不拥有第二份历史或
配置事实。

**Instance Administrator / 实例管理员**：Admin Control Plane 的单一运维身份，凭独立
credential 跨 Scope 管理当前 Netizen 实例。它不同于 Channel
Participant、Binding creator 或 Project owner，后几者都不自动获得实例管理员权限。

**Codex-owned Setting / Codex 原生设置**：由标准 Codex 配置层拥有、同时影响
CLI/App/Netizen 的设置；Netizen 只通过原生 Codex 接口读取或修改。

**Turn Model Settings / Turn 模型配置**：Model、Reasoning Effort 与 Speed
（Service Tier）的成组选择。`/new` 卡片或 `/config` 可以选择继承 Codex，或保存
Binding-scoped intent。Netizen 后续每次启动新 Turn 前都按 live 模型目录解析显式选择为
真实 wire value 并应用；继承时完全省略 override，running Turn 的 steer 不应用。

**Binding Turn Settings / 会话 Turn 配置**：Binding 上三个全有或全空的目录 ID
（Model、Effort、Service Tier）及 revision。它是用户要求 Netizen 在该会话后续新
Turn 上重复应用的客户端意图，不是 Codex 已生效配置、默认值快照或可反查的原生状态。
模型目录失效或读取失败时保留；running Turn 的 steer 不解析也不应用。

**Binding Task Feedback / 会话任务反馈**：Binding 上对 Task Reaction 与 Progress Card
的两个独立选择及 revision。两项默认关闭，只决定后续普通 Turn 的飞书展示，不是
Codex 原生设置或 Turn 状态事实。

**Native Compaction / 原生压缩**：`/compact` 对已有历史的 idle Binding 调用公开
Codex compaction。start 空响应不是完成；Netizen 临时保留 `compacting` 槽位，直到
公开 Thread history 出现唯一的新 terminal `contextCompaction` Turn。多个 candidate
或终态超时会 fail closed；生命周期内不支持外部并发写同一 Thread。状态不写入
Channel Database。

**SDK Gap Adapter / SDK 能力缺口适配器**：ADR 0014 定义的临时、可逐项删除边界。
它只复用同一个 `AsyncCodex` 已初始化的 App Server，为 ADR 0014 的固定 Goal/Skills
method、ADR 0021 的固定 Side boundary、ADR 0028 的固定 Thread unsubscribe method 和
ADR 0037 的固定 Thread Delete method 提供窄语义口；不暴露通用 RPC，不复制协议或状态，
也不按 SDK 版本号做运行时许可。SDK 升级由能力 shape、真实 SDK client synthetic harness
和目标环境 live probe 放行；高层 facade 支持一项就切回并删除一项 shim。

**Thread Lifecycle Operation / 会话生命周期操作**：一个 exact Binding 上短暂占用的
rename/archive/unarchive/delete；飞书入口围绕当前会话，Admin Control Plane 按实例级
authority 选择它已开放操作的 exact target。原生 mutation 开始后结果未知就保留
`lifecycle-unknown` 槽并关闭 admission；Delete 只有 ADR 0037 的一次只读对账例外。
名称、归档与原生存在性始终由 Codex 拥有，不写入 Channel Database。

**Binding Delete / 本地会话删除**：永久删除 Channel Database 中的 exact Binding。
Lazy Binding 可直接删除；materialized Binding 只能在原生 Delete 正常返回或 Native
Thread Delete Reconciliation 明确 absent 后提交。它本身不修改 Codex 历史。

**Native Thread Delete / 原生会话删除**：永久删除已有历史的 Codex Thread、App Server
管理的 spawned descendants 及其 Codex App/CLI 历史，并在确认后删除 Binding。它是
ADR 0037 批准的独立不可逆 native capability，只能通过固定薄 Adapter 调用，不能用本地
Binding 删除模拟。

**Native Thread Delete Reconciliation / 原生删除对账**：Delete 非取消异常后，对 exact
native ID 做一次有界四视图读取：rollout scan 与 state DB 各自的 active/archived catalog。
任一视图存在为 **present**，保留 Binding 并允许用户重新确认；四个完整视图均不存在为
**absent**，可以提交 Binding Delete；视图冲突、失败、超时或取消为 **unknown**，保留
Binding/lifecycle 槽并关闭 admission。它是只读判定，不会自动再发一次 delete。

**Goal Operation / Goal 操作**：一个原生持久化 Goal 在 Runtime 中的单一逻辑槽位。
它可跨多个物理 Turn 自动 continuation；只有逻辑通知流、persisted Goal、exact 最终
物理 Turn 与公开 Thread idle 共同确认后才释放。Goal 不写入 Channel Database。

**Externally Active Goal / 外部活跃 Goal**：服务重启或外部 Codex 客户端留下的 active
persisted Goal，但当前进程没有可安全重建的通知 route。Netizen 只读识别并阻止同一
Binding 的新 mutation；当前不猜测重挂或替用户暂停。

**Native Capability Gate / 原生能力门控**：统一命令注册表中对原生能力可用性的声明。
能力必须来自公开高层 API，或来自 ADR 0014 / ADR 0021 / ADR 0028 / ADR 0037 已验证的
窄 Adapter；否则保持 unavailable，不能通过 prompt、本地状态或任意私有 RPC 模拟。
当前 Goal、Skills、Side 与 Native Thread Delete 已通过独立口接入，Plan control 与 Apps
仍是显式 gap。

**Project**：Channel alias 对应的一个 canonical 真实 cwd。多个 native Thread 可
同时共享和修改它；不是 clone、worktree 或快照。

**Project Registry**：整个 Netizen 实例共享的 Project 目录。它保存 Project 的
alias、canonical cwd 和是否可用于新会话；停用不影响已有会话，也不删除目录。

**none**：映射到服务级 `defaultCwd` 的保留 Project alias。

**Ordinary Active Turn**：以 Binding ID 为键的内存记录：handle、owner、origin、状态、
task、receipt Event，以及只读 plan cursor/checklist、成功 steer count 与 freshness。
native 终态到达即释放，均不持久化。

**Turn Activity Projection / Turn 活动投影**：一个 exact Ordinary Active Turn 的有界、
瞬态展示视图，当前只包含状态与原生 checklist。它不是推理过程、工具日志、Turn 历史或
终态事实源。

**Side Turn**：以 Side ID 为键、在同一 ephemeral Side Thread 上串行开始或 steer 的
当前 Turn。它使用普通 `AsyncTurnHandle.run()` 完成路径，不使用持久 Thread history
recovery，且不写入 Channel Database。

**Task Reaction / 任务表情**：Binding 可选开启、由 Channel 以 exact Turn ID 管理的
纯内存展示。原任务消息使用 `Typing`/`THINKING`，成功 steer 使用 `OnIt`，终态使用
`DONE`/`ERROR`/`CrossMark`；它不是 Turn 状态事实源，也不进入 Channel Database。

**Progress Card / 进度卡**：Binding 可选开启、依附 Completion Origin 的 ordinary Turn
展示卡；运行中展开 Turn Activity Projection，终态在同一卡片折叠过程并呈现结果。它不
保存执行历史，也不是 Turn 状态事实源；所有可见 plan step 和随分页携带的过程 manifest
都经过同一套有界的常见敏感模式过滤。

**Standard CODEX_HOME**：服务 effective user 的原生 Codex 状态根；显式
`CODEX_HOME` 优先，否则为该账号的 `$HOME/.codex`。Netizen 不修改其内部
JSONL/SQLite 格式。

**Channel Database**：`channel.sqlite3`，只保存 Scope、Binding、Project Registry、
schema version、Channel SDK dedup TTL key、Binding 上可选的 Binding Turn Settings
目录 ID、Binding Task Feedback、Mention Context Mode 与 exact Context Boundary
metadata，以及不含 native ID/content 的 Side Topic Route/墓碑；不保存补充消息正文、
解析后的 wire value、Codex 已生效配置、Turn Activity Projection、Progress Card session、
普通 Thread 订阅状态或空闲 timer。

**Channel Participant / Channel 参与者**：飞书应用权限允许其消息到达 Netizen 的
发送者。Netizen 不再按 user、chat 或角色做二次准入；同一普通 Scope 的参与者共享
Binding 控制能力，同一 Side Topic 的参与者共享 stop/close 能力，不限创建者；这不会
授予 Admin Control Plane 的实例级 authority。
