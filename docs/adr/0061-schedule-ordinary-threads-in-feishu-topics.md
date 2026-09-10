---
status: accepted
date: 2026-09-08
amends: 0008, 0023, 0029, 0030, 0031, 0034, 0060
related: 0016, 0017, 0021, 0022, 0037, 0039, 0049
---

# 在独立飞书话题中调度普通持久会话

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

## 失败边界与后果

时间到期先持久 claim，再发飞书消息和启动原生执行。第一版不补跑停机错过的触发，不自动
重试失败的业务执行，不承诺外部副作用 exactly-once；同一计划的 initial Turn 未确认结束时
不重叠启动。pause/delete 不终止已有普通任务，用户通过普通会话控制处理它。

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
