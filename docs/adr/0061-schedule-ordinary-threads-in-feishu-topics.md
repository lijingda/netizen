---
status: accepted
date: 2026-09-08
amends: 0008, 0023, 0029, 0030, 0031, 0034, 0060
related: 0016, 0017, 0021, 0022, 0037, 0039, 0049
amended_by: 0063, 0068, 0070, 0075, 0076, 0085
---

# 在独立飞书话题中调度普通持久会话

2026-10-22 修订：新话题计划增加默认关闭的 `auto_archive` 选项，仅在本次首轮流程成功且
既有结果交付确认送达后，调用既有准确会话归档能力。不增加续聊或空闲判断、不改回复展示；
归档恢复复用 `/sessions archived`。schema v20 通过冻结 v19 → v20 迁移使旧计划默认关闭。

2026-10-20 修订：新话题计划增加独立工作区策略，复用
[ADR 0085](0085-manage-worktrees-independently-of-sessions.md) 的受管资源、目录交接及失败边界。
它不读取聊天默认配置；原会话目标继续继承 exact Binding 的实际目录。schema v17
通过 ADR 0075/0076 的冻结迁移保存策略和最小 Run 工作区回执，取代下文早期无迁移政策。

2026-09-23 修订：[ADR 0070](0070-schedule-inputs-in-existing-bindings.md) 增加 exact
Binding 目标，复用普通输入的 start/steer；本文的新话题创建与初轮屏障保留为 `new_topic` 模式。

2026-09-21 修订：[ADR 0068](0068-run-saved-scheduled-plans-manually.md) 允许三个管理
入口手动执行已保存的计划版本，并恢复仅支持当前数据库结构的约定；执行仍共用
本 ADR 的普通会话链路。

Netizen 需要支持自然语言、飞书卡片和 Admin 对定时计划进行增删改查与启停。每次触发
创建一个独立飞书话题及普通持久会话，后续停止、继续对话、配置、归档和删除复用既有能力。
用户已确认这一产品方向、显式群 ID 优先及不侵入用户配置的要求，授权基于证据优化
接入方式，并按完整设计实施。公开 SDK 的进程 MCP override 与单工具真实探针
通过后，本决定选择 MCP。
完整行为见[定时任务设计](../design.md#定时任务)，兼容性范围与验收要求见
[部署文档](../deployment.md#定时任务兼容性与验收)。本文接受架构边界，不代表能力已验收或上线。

## 选择与理由

- 在现有 Python 服务及 Channel background loop 内增加一份调度器，继续共用 Store、
  management service、Runtime、FeishuChannel 和 AsyncCodex。定时计划由 Netizen 拥有，
  原生执行由 Codex 拥有；不把此功能声称为 SDK 已有的原生定时能力。
- 自然语言入口使用同进程 Streamable HTTP MCP 和单一 cron_manage 工具，按 mode 提供
  管理操作；卡片与 Admin 直接调用同一服务。用公开 CodexConfig.config_overrides 接入
  Netizen 自身的独立 server entry，不写 config.toml、不覆盖用户已有 MCP。这是对
  ADR 0023 唯一 override 的窄扩展；保留 allow_login_shell=false 及用户其余原生配置。
  loopback 端点和临时凭据随服务生灭，不增加守护进程、持久配置或基础管理 Skill/脚本。
  此约束针对 Netizen 的接入行为；原生 Codex 仍可能按自身规则登记 Project trust。
- 使用 MCP initialize.instructions 与工具 description 提供经产品适配的 Codex App
  自动化使用指引。不传新的 developer/base instructions，不破坏用户指令继承和 exact
  resume；不复制 App 的 heartbeat、私有桥或存储。工具数量减少不等于已测得 token 节省。
- 工具显式传入 chat_id 时使用该值；省略时读取原生 MCP 调用 _meta.threadId，
  由服务从 exact Thread → Binding → Scope 解析默认会话和 Project。它只是默认值来源，
  不作为用户权限凭据；不新增用户、群、创建者或 Project ACL。
- 目标会话支持普通群、话题群和私聊；省略 chat_id 时均使用当前 Scope 的 chat_id。
  用户在验收中明确要求私聊也能创建。每次触发仍须取得飞书真实新话题标识，不改动来源
  会话，不用本地伪造 topic ID 或静默换群兜底；私聊话题的实时兼容性单独记录。
- 一次触发对应一个新普通 topic Scope、一个普通 Binding 和一个持久 native Thread。
  不 fork 创建计划的 Thread，不使用 Side，不修改来源 Scope 的 active pointer。
  调度首轮仅允许在新 Binding 上 start，不能复用普通 submit 的 running-to-steer 分支。
- 定时计划保存与 `/new` 共用的会话配置意图：Model/Effort/Speed、Reaction Pulse、
  Progress Card 与 Mention Context Mode。创建时默认复制 exact 来源 Binding 的选择，
  显式覆盖后形成独立配置；来源后续变化不传播。无来源按 `/new` 默认选择，原生继承
  仍保存为继承，不复制有效 Codex 配置。每次认领冻结设置并用于该次普通 Binding。
  catch-up 的边界从本次真实新话题根/seed 开始，自动首轮不读取来源历史或伪造真人 cursor；
  私聊继续 current-only。此项由维护者于 2026-09-09 明确要求并接受。
- SQLite 新增当前计划定义、最小调度交接证据和有界管理请求去重。这是对现有“无 prompt、
  无 Turn 持久化”限制的窄例外：可保存用户明确指定的计划指令和 exact initial Turn ID
  引用，不复制聊天正文、历史输入版本、响应、Activity 或原生终态历史。
- 维护者在发布前精简中明确取消历史数据库兼容：仅支持当前完整结构，新库直接创建，
  旧版本只读拒绝，不保留历次迁移或自动重置数据。当前库重装、安装快照、lifetime lock
  与失败回滚继续保留；这项选择取代早期设计中的逐版本数据库升级路径。
- 重复计划支持可选截止时间，包含该应触发时刻。截止只停止后续触发，
  已有普通执行按原生命周期继续收尾。
- 定时首轮没有实时的人类入站消息，使用明确的 Scheduled Plan 来源。Completion Origin
  指向新话题中的机器人消息；不伪造 Current Prompt Message、发送者或当前用户授权。
- Admin 可以编辑计划定义，但不直接提交任意即时 Prompt、steer 或提供独立执行面。
- Project 删除提交同时删除已确认的关联计划并阻止新调度交接；在途外部副作用继续纳入
  精确剩余清单。部分删除失败不复活计划，同名 Project 重新登记也不复活旧计划。

## 新话题工作区策略

新话题计划保存独立于 SessionSettings 的 `workspace`：`project` 使用 Project 目录；
`existing` 保存 exact 受管 Worktree ID，各次执行共享其当前文件与分支；`new` 为每次
实际执行的 Run 新建 detached 工作区，可选 `ref`，省略时取创建当时 Project HEAD。
仅检出提交内容，不复制未提交文件、不执行初始化脚本，也不保存固定命名分支模板。
创建计划省略 workspace 使用 `project`，更新省略则保留旧策略；显式提供时完整替换。
不从来源 Binding 或 `/defaults` 隐式复制工作区。原会话计划 workspace 为 null，
显式覆盖必须拒绝；不增加目录缺失时一律禁聊的规则。

options、预览、保存、busy/missed/blocked 跳过不创建工作区。指定资源在首次飞书发送前
经共享选择服务校验；已确认不可用时零 root/seed、零 Binding、零原生提交，保留计划和
enabled 意愿，记录本次未执行并推进自动到期点，不补跑。手动请求在认领前拒绝则不伪造
Run、不改定时游标；已经受理的 Run 可在首次发送前明确失败。
目录缺失、身份替换、删除预约与读取未知分别解释，不回退目录、不重建替代品。同路径
新资源或同 alias Project 重登记不能冒充原资源。用户重新选定有效策略只影响未来认领。

交接复用唯一 Scheduler、Run 屏障与单次发布准入：取得所有权后先校验，发送真实
root/seed，再按 `new` 策略创建工作区、绑定目录并提交首轮。创建前把 exact 资源 ID
关联到 Run，保留进程中断后可追溯的最小回执。绑定前再次复核 Project/Worktree 删除准入；
已经发送话题后的竞态只报告已知失败，不撤销或声称没有话题。长 Git/飞书/原生 I/O
不持有 Scope/Binding 锁或数据库事务，不增加默认会话的 per-Scope 创建 Future、
Project 执行锁或跨会话并发上限。

Admin、`/cron` 和 MCP 投影按需展示策略、不可用原因和 `can_run_now`，不持久化资源健康
状态或后台巡检。失效引用保留在编辑器中，未改变 Project/workspace 的名称、指令等编辑
允许保存；实际变更时严格校验。普通工作区查询继续用调用者 Project 范围的
`list_worktrees`，计划配置选项用 `cron_manage options`；已知 exact ID 可直接用于写入。
两者直接复用服务，不经 MCP/HTTP 互相转调或扩大工具范围。

删除 Worktree 不级联删除引用它的新话题计划；若显式级联删除 Binding，则仍删除以该
Binding 为目标的原会话计划。Project 删除继续墓碑关联计划，不套用聊天默认配置的保留规则。
暂停、修改、删除计划不回收已生成的工作区；每次新建策略的旧资源删除不阻塞未来执行。

## 新话题完成后自动归档

`auto_archive` 独立于 SessionSettings，只允许新话题计划配置，创建省略为 false，更新
省略保留原值；定时触发和已保存计划的立即运行共用。原会话计划保持 false，不接受显式
覆盖。认领时冻结在 `Claim.plan` 并传入本次内存 `ScheduledOrigin`，计划后续编辑、暂停
或删除不改变已经认领的一次；不把策略快照复制到 Run，也不读取最新计划替换已认领选择。

完成后策略仅接在既有结果送达收尾：exact initial Turn 正常 completed 且无执行错误，
结果确认 sent，才归档本次创建的准确 Binding/Thread。这里的成功是流程成功，不由模型
回复内容推断业务目标达成。执行、投递的异常路径保留原语义；归档异常不能改写已成功的
执行或送达回执，不新增重试、持久终态、归档历史或补偿队列。

维护者明确选择执行完成后直接归档，不因用户已经续聊、存在后续 Turn 或其他活动而跳过
或等空闲。这可能按原生归档语义结束后续工作；保留既有生命周期预约、锁外原生调用和
未知结果有界对账，不能转而归档该 Scope 后来切换到的其他会话。它不构成普通输入准入规则
的变更，也不新增条件归档接口。

结果卡、过程卡、Files 和富文本回复全部保持现状，不增加归档提示或恢复按钮，避免两种
回复方式产生额外差异。用户通过 `/sessions archived` 恢复；飞书话题、原生历史和工作区
保留。重启只按原规则收敛 Run，不重放执行或投递，也不从历史补归档。

## 失败边界与后果

时间到期先持久 claim，再发飞书消息和启动原生执行。第一版不补跑停机错过的触发，不自动
重试失败的业务执行，不承诺外部副作用 exactly-once；同一计划的 initial Turn 未确认结束时
不重叠启动。pause/delete 不终止已有普通任务，用户通过普通会话控制处理它。

工作区已创建而 Binding 或首轮失败时保留资源，并在执行记录中给出 exact ID/路径。
Git 创建取消、超时或结果未知沿用受管资源回执，不自动重试或删目录；它不等于 native
Turn 可能启动。重启仅收敛旧 Run，不重建工作区、不重放输入；仅在能证明原生输入未提交时
释放该交接屏障，原生未知仍按下述既有边界处理。创建预算须容纳共享服务的有界 Git
变更预算，并与停机及 Project 删除排空一致；不能用旧的 30 秒交接总限额提前截断创建。

thread_start/thread_resume/turn_start 结果未知继续沿用现有 Runtime 的全服务 native
admission 关闭规则。不能借增加调度器之机将它缩小为计划局部。恢复后只按持久 exact
引用做有界只读核查，不续发未确认的启动，不重建普通 Turn history 或持久投递队列。

代价包括数据库安装/回滚、MCP 生命周期和原生工具策略兼容、来源模型扩展、主动建话题的
崩溃窗口、调度交接与普通生命周期的协作，以及无人值守任务可能失败或需要用户处理。
现有默认 sandbox、auto_review 和同 Project 多 Binding 并行语义继续适用。

## 接受与验证

基础只读 MCP 探针已验证公开启动接入及同 cwd 并发 Thread 的原生调用身份。实施时继续
完成工具发现/缓存/恢复、默认群映射、生产传输与权限、以及普通群/话题群新建 topic 的
兼容性验收。本 ADR 随用户实施授权接受，AGENTS.md 同步记录上述窄例外；其余既有
guardrails 保持适用。
实现使用 make check 作为代码门禁，并执行部署文档列明的 SDK、Feishu、生命周期和安装
回滚相关 live 验收；交付时更新现有设计/部署文档与用户指南。
ADR 接受与功能验证/上线是不同阶段，不因本决定提前宣布兼容性通过。
