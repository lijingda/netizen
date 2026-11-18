---
status: accepted
date: 2026-11-18
amends: 0008, 0061
related: 0021, 0023, 0028, 0067, 0068, 0070, 0075, 0076, 0078, 0081
---

# 定时新话题会话由 Agent 自评任务是否达成

## 背景

定时任务的执行记录按 exact 首轮原生终态展示（[定时任务](../design.md#定时任务)）。“已完成”
只说明原生 Turn 正常结束。巡检类计划“检查通过”“发现问题”“没有权限、没查成”都显示为
已完成，要知道实际结果只能逐个打开话题。

[ADR 0061](0061-schedule-ordinary-threads-in-feishu-topics.md) 规定自动归档的成功是流程
成功，不从模型回复推断业务目标。本决定保留这条规则，另增一个由执行 Agent 通过工具
**显式声明**的维度，与流程状态分开展示。

## 决定

### 语义与范围

- Agent 自评是执行 Agent 对“创建本话题的那次定时任务是否达成”的**最近一次声明**。
  首轮可以作出，用户续聊协助完成后也可以更新。续聊转到其他事情时，Agent 不应调用。
  取值只有 `succeeded`、`failed`；没有记录即“未自评”，本决定之前的 Run 也一样。不存原因文本。
- 只适用于 `new_topic` Run：一次触发对应一个新 Binding，归属唯一。`binding` 目标不支持，
  因为多个 Run 可以 steer 同一 Turn，目标本身又是用户的普通会话（[ADR 0070](0070-schedule-inputs-in-existing-bindings.md)）。
- 自评只用于展示，不改变执行、调度、交接 barrier、Result/Files、结果静默、结束提及、
  重试或下次触发。`auto_archive` 仍按 ADR 0061 的流程成功判断；是否以自评门控归档另行决定。

### 专用工具与 Thread 级挂载

- 新增一个独立的 loopback MCP runner，复用 `EmbeddedMcpRunner` 的传输、bearer 鉴别和
  生命周期，只提供 `report_run_outcome(outcome)`。参数只接受这两个字面枚举，拒绝原因、
  Run/Thread/Turn 等额外字段。instructions 与 description 说明以下几点：
  - 只针对创建本话题的那次定时任务；
  - 按计划指令中的完成条件判断，例如“检查并汇报异常”在发现并汇报异常后可以是达成；
  - 判断变化时再调用；
  - 子 agent 不调用；
  - 调用不结束 Turn。
- 只在属于 `new_topic` Run 的 Binding 的 Thread **加载时**挂载，包括该 Binding 的每一次
  原生 `thread_start`（定时首轮，以及首轮未建 Thread 时续聊或 `/goal` 的首次创建）和每一次
  原生 `thread_resume`。挂载通过公开 SDK 的
  `config` 参数传入**单个点号键** `mcp_servers.<本进程随机 namespace>`。值由唯一的构造
  函数生成，只含 `url`、`bearer_token_env_var`、`enabled`、`enabled_tools`、
  `default_tools_approval_mode="approve"`，不设 `required`。禁止传整张 `mcp_servers` 表。
  bearer 值与现有两个 runner 一样，在 AsyncCodex 启动时以随机名称的环境变量注入，
  不出现在 entry 中。
- 这是对 [ADR 0008](0008-use-python-sdk-for-native-turn-control.md) 决定 2
  “Thread API 不传 config”的窄修订：只允许上述一个键，只用于上述 Binding。其他 start、
  resume、fork、Side 和命名 fork 都不传 `config`。它也是 ADR 0061 进程级 MCP 例外之外的
  第二种接入形态。不写 `config.toml`，不替换用户 MCP，不传 developer/base instructions。
- 已加载 Thread 会忽略 `config`，因此所有原生 start 和 resume 都经过同一个挂载判断：
  普通提交和 `/goal` 的 start、公共的 `_resume_thread_for_operation`、观测恢复中的
  resume，以及归档恢复中的 resume。每次
  按 exact native Thread → Binding → `schedule_runs_binding` 唯一索引现查，不缓存。
  Run 已被裁剪或 Binding 已删除时不挂载。
- runner 绑定失败时，本次服务生命期不挂载并记录有界诊断，不阻塞启动。定时首轮和
  续聊照常执行，结果显示为未自评。

### 调用身份与写入

- 只依据原生 per-call 元数据判定调用者，参数不能提供身份。以下条件须全部满足：
  - `_meta.threadId` 与 turn metadata 一致且非空；
  - 没有 `subagent_kind`；
  - 该 Thread 是本实例某个未删除的 `new_topic` Run 的 Binding；
  - `turn_id` 是本进程 Runtime 正在为该 Binding 承接的当前物理 Turn，普通 Turn 和 Goal
    物理 Turn 均可，复用 [`suppress_reply`](../design.md#当前物理-turn-的结果静默) 的核对。

  Side、fork、命名 fork、外部客户端和子 agent 的调用一律明确拒绝。拒绝结果告诉 Agent
  不要重试，也不影响当前 Turn。
- 每次被接受的调用在一个同步事务中执行条件 UPDATE，条件为 `run_id`、`binding_id`、
  `target_kind='new_topic'` 且 Binding 未删除；影响一行才返回成功，回执回显计划名称和
  触发时间。最后一次被接受的写入生效。追加输入、续聊或新 Turn 都不清除已有自评。
  不新增调用流水、幂等键、历史或后台补偿。

### 展示与数据

- `ScheduleService` 只生成一份公开投影，供 Admin 列表/详情/执行记录、`/cron` 摘要与最近
  记录、MCP `cron_manage` 的 list/view/runs 共用。原流程 `status` 不变；已建立 Binding 的
  `new_topic` Run 附加 `succeeded`/`failed`/`unreported` 一维，其中 `unreported` 只是显示值，
  不入库。各入口不自行读取原始列。界面写作“Agent 自评：目标达成 / 目标未达成 / 未自评”。
  “失败 · Agent 自评：目标达成”是成立的组合，比如首轮失败、用户续聊后完成。`binding`
  目标及尚未建立 Binding 的 Run 不显示这一维。Binding 删除后仅显示已有自评，未记录时不显示这一维。
- schema v22 为 `schedule_runs` 增加可空的 `self_reported_outcome TEXT`。CHECK 要求该列
  为 NULL，或者是文本、取值二选一、`target_kind='new_topic'` 且 `binding_id` 非空。
  新增冻结的 v21 → v22 迁移（[ADR 0075](0075-migrate-channel-databases-during-installation.md)、
  [ADR 0076](0076-separate-cli-installations-from-instance-data.md)），旧行保持 NULL，不回填。
  这是 ADR 0061 数据契约的窄扩展，只保存模型来源的这一个枚举声明，不保存原因、正文或
  历史，随 Run 沿用既有的裁剪与保留规则。

## 证据

2026-11-18 用 openai-codex 及 bundled App Server 0.169.0，以及 PR #293 待升级的 0.170.0
做了验证。环境是临时 CODEX_HOME 加 loopback mock Responses provider；进程级入口用生产的
Session/Schedule runner，`config.toml` 另放一个用户 MCP 入口，不使用模型账号或飞书。
两份独立编写的探针分别完成，其中一份出自未看过另一份的盲审，按模型实际收到的工具集
逐请求比对，两个版本结论一致：

| 场景 | 结果 |
| --- | --- |
| `thread_start` 传点号键 | 只有该 Thread 看到工具；进程级与用户入口都保留 |
| 同进程普通 Thread（挂载前后、编辑 `config.toml` 后、原生 MCP reload 后） | 看不到 |
| 传嵌套整表 `{"mcp_servers": {...}}` | 该 Thread 静默缺少两个 Netizen 进程级入口，Turn 仍正常完成 |
| 已加载 Thread 的后续 Turn、加载中的 resume | 保留工具；对已加载 Thread 传 `config` 既不新增也不替换挂载 |
| 卸载后或进程重启后 resume | 不传 `config` 时工具消失；传入时按本进程 namespace 重新挂载 |
| 持久 fork、ephemeral fork | 不继承；fork 冷 Thread 不会把源 Thread 加载起来 |
| 原生子 agent（无历史和全历史 spawn） | 继承并能调用；元数据带 `subagent_kind=thread_spawn` 及子执行者自己的 Thread/Turn ID |
| 非 required 端点拒绝连接 | Thread 启动约 10–20 ms，Turn 正常完成，工具缺失 |
| CODEX_HOME | `config.toml` 不变；未见 bearer 值、环境变量名或健康端点 URL；rollout 与原生历史按普通历史保留调用参数、结果和 MCP 归因，诊断日志可能保留不可达端点的 URL |

协议模型含 `DynamicToolSpec`，但公开 SDK 的 `ThreadStartParams` 没有对应字段，不经私有
RPC 无法使用。上述矩阵收录为 [Thread 级挂载探针](../../scripts/probe_scheduled_outcome_mount.py)，
使用生产 runner 与挂载构造函数，作为该接入的兼容性门禁。

## 取舍

- **进程级工具加服务端拒绝**：所有会话都能看到一个与己无关的工具，误调用机会更多。
  维护者选择只在定时话题会话挂载。
- **只允许首轮上报**：需要在首轮交接期间证明 exact Turn 身份，还要定义追加输入时撤销的
  语义。现有输入通知是尽力而为，清除失败会被吞掉，只能改用首轮内存暂存、终态一次落库，
  并接受崩溃丢失。这也违背用户的预期：经用户提示后完成的任务同样是达成。不采用。
- **从最终回复或原生历史推断**：违反 ADR 0061 和数据契约。不采用。
- **保存原因文本，或增加“部分达成”等取值**：原因属于模型回复内容；多值会让展示和日后的
  归档门控变得含糊。不采用。

## 后果与边界

- 子 agent 和话题中与任务无关的后续 Turn 都看得到这个工具，写入只由服务端身份收窄。
  Agent 仍可能在无关续聊中误调用；以本话题为目标的原会话计划，其输入也会写到原 Run 上。
  缓解手段是工具说明和回执回显，剩余风险可以接受，因为自评只用于展示并标明出处。
- 同一服务账号、拿得到环境变量的 shell 可以直接访问 loopback 端点并伪造元数据，这与现有
  会话 MCP 相同（同账号不构成隔离边界）。因此自评只用于展示，不作为任何执行或权限依据。
- 若某条加载路径漏传挂载，工具会缺失到下次卸载为止，不影响执行。实现规则是：此类
  Binding 的每次原生 start 和 resume 都必须经过同一个挂载判断，并用覆盖各操作的行为测试守护。
- 已提交的写入可跨崩溃保留。归档恢复后再次 resume 会重新挂载；Run 被裁剪后，旧话题
  不再挂载，写入也会被拒绝。

## 移除与重新评估

- 若公开 SDK 提供按 Thread 或 Turn 注册、且 per-call 身份等价的工具能力，先用同一请求级
  矩阵验证，再移除这个 runner、entry 及其环境变量。
- 若升级破坏点号键合并、冷恢复挂载、隔离性或元数据身份，就停用挂载（只显示未自评），
  保持执行路径可用；不得复制用户配置、强制冷 resume、读取 rollout 或私接 RPC 来修补。

## 接受与验证

本决定的交付和验收包括：

- v22 迁移、挂载函数、身份与写入、统一投影及三个入口展示；
- `make check` 与固定 SDK 的 [Thread 级挂载探针](../../scripts/probe_scheduled_outcome_mount.py)；
- AGENTS.md 的原生配置例外与数据白名单，以及工程设计中的[定时任务](../design.md#定时任务)、
  [Channel 数据库](../design.md#channel-数据库与结构校验)和[服务环境](../design.md#服务环境与受管-skill)；
- 部署文档的 [SDK 升级审查](../deployment.md#codex-sdk-升级审查)与
  [定时任务验收](../deployment.md#定时任务兼容性与验收)，以及用户指南的定时任务说明。

真实模型的调用时机与取值、飞书客户端展示另行验收，不由 mock 证据代替。
