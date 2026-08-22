---
status: accepted
date: 2026-08-12
amends: 0008, 0012
---

# 在 Binding 上保存持久的 Turn 配置意图

## 背景

ADR 0012 让 `/new` 和 `/config` 使用公开 `AsyncCodex.models()` 与真实
`AsyncThread.turn(..., model=..., effort=..., service_tier=...)`，但高层 SDK 没有
idle Thread settings read/update。最初的一次性 pending 方案在首个配置 Turn 获得 handle
后清除本地选择，假设后续由原生 Thread 延续。这样 `/status` 绝大多数时间只能说明原生
值不可读取，用户无法确认自己在 Netizen 中为当前会话选择了什么。

公开 `thread/read` 仍不返回三项有效值，Netizen 不能把模型目录默认值或本地记录冒充为
Codex 原生状态。但它可以保存用户对这个 Binding 的客户端意图，并在自己启动的每条新
Turn 上重复应用；这与保存历史、模拟 Agent runtime 或复制 Codex 配置不同。

## 决定

1. `/new` 仍只创建并切换 lazy Binding，不要求任务、不创建 native Thread 或 Turn。
   `/new <project|none>` 把三项留空并继承 Codex；零参数 `/new` 卡片直接选择并保存三项，
   不再增加“继承/自定义”配置方式。模型目录不可用时不展示可提交表单，并提示重试或
   使用命令快捷路径。
2. Binding 可选保存
   `BindingTurnSettings(model_id, effort_id, service_tier_id)` 与
   `settings_revision`。三个 live catalog ID 必须全空或全有。它是用户要求 Netizen 在
   该会话后续新 Turn 上应用的持久客户端意图，不是 effective/default Codex config。
3. `/config` 只编辑当前 active Binding，不显示跨会话选择或配置模式；配置其他会话必须
   先 `/resume`。表单携带完整 Binding ID 与 revision，active Binding 切换或 revision
   stale 时零 mutation。running/stopping、Goal active 与 compaction 期间仍拒绝修改。
4. 普通消息需要启动新 Turn 且 Binding 配置非空时，Runtime 在任何 native mutation 前
   重新读取 live `codex.models()`，按所选 Model 验证 Effort/Tier，并只把目录解析出的
   wire value 交给这次 `turn()`。每条后续新 Turn 都重复同一流程，不在 handle 返回后
   清除设置。
5. 模型目录失败、分页、选项下线或组合不兼容时不 start/resume/turn，设置原样保留供
   用户重新 `/config`。`turn/start` 结果未知时也保留，并沿用全局 fail-closed。
6. running Turn 的普通消息始终只 steer exact handle；即使 Binding 有设置，也不读取
   模型目录、不向 steer 注入配置。配置 revision 继续进入 submission admission，图片、
   引用或 Skill 准备期间发生配置变化时，本条消息不被重解释。
7. `/status` 总是一项一行显示 Model、Effort、Speed 与配置来源。三项为空时显示“继承
   Codex”；非空时从 live catalog 解析显示名称。目录暂不可用时回退显示保存的精确 ID；
   目录可用但选择已下线时明确提示配置已失效并引导 `/config`。这些文案只说明 Netizen
   后续 Turn 的选择，不声称反查到了 native Thread 的有效值。
8. Schema v4 只描述当前数据结构，不包含 v1/v2/v3 自动迁移。Pilot 只有单一使用者，
   旧 Binding 没有保留要求；启动遇到非当前 schema 时明确失败，由部署者归档旧数据库后
   创建干净的当前 schema，避免长期累积迁移分支。
9. Channel Database 仍不保存解析后的 wire enum、prompt、Turn、回复、卡片 session、
   queue 或 Codex-owned state。Fast 仍只是同一模型的 Service Tier，不增加费用提示，
   不与 Codex Spark Model 合并，也不新增 `/model`、`/effort`、`/fast`。

## 验证与 SDK 升级门禁

- 数据层覆盖当前 schema 创建/重启、三项原子约束、revision/CAS，以及旧 schema
  零 mutation 拒绝。
- Runtime 覆盖同一 Binding 连续新 Turn 每次 live resolve 并显式应用、steer 不解析或
  应用、目录失效与 start response loss 保留设置、配置变化使 admission 失效。
- Card/Channel 覆盖直接选择三项的 lazy `/new`、当前 Binding `/config`、
  stale/running/catalog failure、持久成功文案，以及 `/status` 的动态值、继承来源与
  目录失败回退。
- `make check` 继续检查公开 `models()` 与 `turn()` 参数；目标环境
  `scripts/probe_python_sdk.py --phase turn-settings` 必须在同一 Thread 的连续两个 Turn
  上重复提交相同显式 override 并都完成。每次 SDK 升级仍运行完整 release probes。

## 后果与移除触发器

用户可以随时从 `/status` 看见当前 Netizen 会话意图，且 `/config` 保存后不会因第一个
Turn 完成而消失。代价是外部 CLI/App 可在两次 Netizen Turn 之间改变 native Thread；
下一次 Netizen 新 Turn 会按 Binding 设置再次覆盖。为空时 Netizen 完全省略三项，由
Codex 自己决定。

当官方高层 API 能在 idle Thread 上原子更新并读取 Model/Effort/Speed，且 live harness
证明更新、继承、重启、外部修改与并发语义后，已有 native Thread 的 `/config` 与
`/status` 应逐项切回 SDK provider。lazy Binding 在 native Thread 尚不存在时仍需要
保存用户选择；任何进一步删除或迁移必须另作决定，不能把客户端意图直接声称为原生
effective config。
