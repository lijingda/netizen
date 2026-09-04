---
status: accepted
date: 2026-08-24
amends: 0008, 0012, 0016
related: 0039
---

# 把普通会话创建收口到 `/new` 卡片并展示全部 Project

## 背景

当前普通 Binding 有两条创建路径：零参数 `/new` 打开 Project、Model、Effort、Speed
表单，`/new <project>` 则绕过表单快速创建并继承 Codex。后者曾为模型目录不可用、
Project 超过卡片中人为截取的前 12 项提供兜底，但新会话需要选择的 Binding-scoped 选项
继续增加，两条路径已经产生不同的配置能力和帮助语义。

Project 本来就在一个 CardKit `select_static` 中；12 项是 Netizen 自己的展示截断，不是
产品需要的分页边界。本次删除这个应用级限制，不增加 Project 分页或搜索流程。

## 决定

1. 只保留 exact `/new` control。任何参数，包括 `/new alias` 和引号形式，
   都零 mutation 地回复“快捷创建已下线，请发送 `/new` 并在卡片中选择”。`/help`、空
   Scope 引导、README、Skill 用户手册和部署验收统一只展示 `/new`。
2. `/new` 仍只创建并切换 lazy Binding，不创建 native Thread 或 Turn。群聊和群话题仍
   必须在 `/new` 消息中 @ 机器人；卡片回调继续从 exact source card 恢复 Scope，不从
   chat ID 猜 topic。
3. Project 使用现有单个静态下拉框，按 Registry 稳定顺序渲染全部 enabled Projects，
   不提供默认或系统 Project。`/new` 可以基于同 Scope 现有 Binding 元数据预选当前或
   最近使用且仍 enabled 的 Project，但不另存 recent 状态；没有可用偏好时保持未选。
   删除 `MAX_PROJECT_ROWS`、截断 slice 和 `/new alias` 提示；不实现 Project 分页。
   option 继续编码 alias + revision，提交时原子校验 enabled/revision/cwd。
4. 在 ADR 0039 的 schema v6 增量中，群聊主线和普通群话题的表单增加 Mention Context
   Mode，默认 `current-only`，可选 `catch-up`。P2P 不显示该字段并固定 `current-only`；
   Side 内仍不支持 `/new`。选项与创建成功卡必须明确提示 catch-up 会把同 Scope 中未 @
   的成员消息送入 Codex 原生历史；先行交付的 schema v5 卡片只创建 `current-only`。
5. Model 选择增加稳定的 `inherit Codex` sentinel。选择它时创建
   `turn_settings=None`，Netizen 后续 Turn 完全省略 Model/Effort/Service Tier override；
   选择实际 Model 时才要求并 live resolve Effort/Speed。模型目录可用时表单显示 inherit
   与全部有效模型；目录不可用时仍显示可提交的 Project + Context Mode + inherit 表单，
   不再让新建会话整体不可用。
6. CardKit 表单保持一个创建动作。Model/Effort/Speed 字段允许两种严格、版本化 shape：
   catalog 不可用时的 minimal inherit shape 不携带 Effort/Speed；catalog 可用时的 inherit
   shape 可以携带卡片已渲染的 Effort/Speed 默认值，但 decoder 只做有界字符串校验并明确
   丢弃，绝不 resolve 或持久化；explicit shape 必须三项完整并通过 live catalog resolve。
   未知字段、与已渲染版本不符的 shape、stale Project、Scope 不一致或 catalog 变化均零
   mutation 地失败并重绘/回复可操作错误。
7. `/config` 使用同一 Model inherit/explicit 语义，并允许 idle Binding 在 ADR 0039 的
   两种 Mention Context Mode 间切换。模型目录不可用时仍可清除显式设置回到 inherit；
   启用 catch-up 必须先取得 exact Feishu card anchor，失败时模型与模式都不部分保存。
8. 创建成功卡片展示 Project、会话短 ID、Model 来源以及 Mention Context Mode；
   `/status` 也显示 mode。卡片 update 失败继续用同一 Scope 的等价文本 fallback，不新增
   card session。

“展示全部 Project”只删除 Netizen 的人为 12 项限制，不承诺绕过飞书平台自身的 Card JSON
或 option 上限。若真实平台拒绝某个超大 Registry 生成的卡片，必须明确报告平台错误；本期
按产品决定不引入分页、截断或重新开放命令旁路。

## 与现有架构的关系

ADR 0016 的“三项全空或全有”持久约束保持不变，但“零参数 `/new` 一定显式保存三项、
模型目录失败只能走命令旁路”的决定被本 ADR 取代。`inherit Codex` 只是 Card Intent，落库
后仍表示三个 NULL；它不是新的 native wire enum，也不冒充 Codex 当前 effective setting。

Card decode 后仍进入同一个 `InstanceManagementService.create_current_binding()`，在 Scope
Coordinator 下创建 Binding、切换 active pointer 并通知唯一 Runtime。不得因卡片选项增加
第二套创建 service、pending form state 或 Project lock。

Admin Web 的 exact Lazy creation 保持可用并默认 `current-only`；它不是 `/new` 的命令旁路。
Admin JSON 继续调用同一个 management boundary，不能发送首条 Prompt。若 Admin 显式省略
Turn Settings，语义仍是 inherit Codex。

本 ADR 的 `/new` 收口、全量 Project 下拉和 Model inherit 三项可以先在 schema v5 上以
`current-only` 独立实现；Mention Context Mode 字段、成功摘要和 `/config` mode mutation
必须与 ADR 0039 的 schema v6/context revision 同一增量交付，不能用卡片临时状态模拟或在
v5 丢弃选择。

## 验证与实施顺序

实现先完成卡片与解析的纯客户端变化，再接 ADR 0039 的 history/runtime 能力：

1. 删除 `/new` arguments parser path、旧 usage/help/error 文案和所有 `/new alias` fallback；
2. 删除 Project slice/常量，新增超过 12 项的 card serialization + decode 测试，确认每个
   option 保留 exact revision；不添加分页控件；
3. 增加 inherit/explicit form shape、catalog unavailable 仍可 inherit 创建、P2P/group/topic
   mode 字段矩阵；
4. 接入 Context Mode/anchor transaction 后更新成功卡、`/config`、`/status`、README、
   user-guide Skill、design、deployment 和手工验收。

聚焦测试至少覆盖：

- `/new` 唯一合法，所有带参数形式零创建并返回迁移提示；`//new alias` 仍是字面 Prompt；
- 0、1、12、13 和大量 enabled Projects 全部出现在同一下拉框，disabled 项不出现，
  无可用偏好时不预选，current/recent 只从现有 Binding 元数据推导，且无分页元素；
  另在查明飞书当前 option/Card payload 硬限制后，以接近并
  超过该限制的契约或 live case 验证平台拒绝会产生明确错误，且不会静默截断、分页或回退
  到命令旁路；
- catalog success 下 inherit/explicit、catalog failure 下 inherit、伪造混合字段与 live
  catalog 变化；
- group/topic 默认 current-only 和可选 catch-up，P2P 无该字段，stale card/active switch
  零 mutation；
- 成功 update 与 update-failure fallback 均准确显示创建结果和配置来源；
- repository hygiene 不再出现面向用户的 `/new <...>`、`/new [...]` 或 `/new alias` 文案。

除 `make check` 外，真实 CardKit 2.0 验收必须覆盖超过 12 个 Project 的同一下拉框、模型目录
暂不可用时 inherit 创建，以及群主线/普通话题/P2P 的字段差异。

## 后果与非目标

所有普通用户都通过一个可发现、可扩展的创建界面得到一致 Binding 配置；代价是熟悉 alias
的用户不能再用一条命令快速创建，且超大 Project Registry 若触及平台硬限制时没有分页
兜底。这个取舍是有意的，不能通过隐藏命令或静默截断恢复旧行为。

本 ADR 不实现无需 @ 自动响应、不增加 Project 分页/搜索、不改变 Project Registry 或
canonical cwd 语义，也不让 `/new` 立即创建原生 Thread。
