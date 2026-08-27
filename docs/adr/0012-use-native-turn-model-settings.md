---
status: accepted
date: 2026-08-09
amends: 0008
amended_by: 0014, 0016, 0018, 0040
---

# 通过真实 Turn 配置 Model、Effort 与 Speed

> 当前交互与持久化语义已由
> [ADR 0016](0016-store-binding-turn-settings.md) 修订：`/new`、`/config` 不再要求任务，
> 并允许 Binding 保存后续新 Turn 重复应用的客户端配置意图；
> [ADR 0040](0040-make-new-card-only-and-show-all-projects.md) 又把会话创建收口为 `/new`
> 卡片，并允许模型目录不可用时选择继承 Codex。下文保留原决定的历史背景。

## 背景

飞书需要同时保留两种体验：`/new <project>` 像 Codex App/CLI 一样继承用户原生
默认值；用户又能在新建和既有会话中显式选择 Model、Reasoning Effort 与 Speed。

锁定的 `openai-codex==0.144.4` 公共高层 API 提供：

- `AsyncCodex.models()`，返回模型、各模型支持的 Effort、默认 Effort、Service
  Tiers 和默认 Tier；
- `AsyncThread.turn(..., model=..., effort=..., service_tier=...)`，可把三项 override
  与一条真实输入一起提交。

它没有公开的 idle Thread settings read/update。`thread_resume()` 也不是一个可确认
持久化设置、但不创建 Turn 的控制面。App Server 协议文档虽然列出 goal、skills 和
apps RPC，固定 Python SDK 的 `AsyncCodex`/`AsyncThread` facade 尚未公开这些方法；
`SkillInput(name, path)` 虽然公开，但没有公开 discovery 可提供可信的 name-to-path
映射。按项目边界，不能访问 `_client`、增加通用 RPC gateway、用 prompt 模拟控制，
或在 Channel SQLite 保存待应用 Codex 配置。

## 决定

1. `/new <project|none>` 和 `/new` 卡片中的快速 Project 按钮继续只创建 lazy
   Binding。首条普通输入仍调用 `thread_start(cwd=...)` 和 `thread.turn(input)`，不传
   `model`、`effort`、`service_tier`，由共享标准 `CODEX_HOME` 的原生 Codex 默认值
   决定三项配置。
2. 零参数 `/new` 增加高级区：Project、Model、Effort、Speed 和首条真实任务必须一起
   提交。`/config` 是当前 Binding 的独立会话配置卡，不放入实例级 `/settings` 的
   Projects 分区；它同样要求下一条真实任务。
   [飞书 Card JSON 2.0 input](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/input)
   的官方上限是 1,000 字符，因此两张表单按该上限校验；更长的后续输入仍使用普通
   飞书消息。
3. 卡片展示和提交分别调用一次 live `codex.models()`。展示不缓存；提交再次校验
   Model 是否仍存在、Effort/Tier 是否仍被该 Model 支持，旧卡片或不兼容组合明确
   失败。固定高层 facade 不接受 cursor；若目录响应带 `next_cursor`，必须整体 fail
   closed，不能把第一页当作完整目录。模型 ID、Effort 和加速 Tier 均不硬编码。原生
   协议用于显式回到 Standard
   服务层的 `default` request value 是跨模型 reset 语义，不伪装成模型能力；其余
   Speed 只来自模型目录。固定 `rust-v0.144.4` 的
   [协议源码](https://github.com/openai/codex/blob/rust-v0.144.4/codex-rs/protocol/src/config_types.rs)
   将该值定义为 explicit standard routing sentinel；同版本的
   [模型过滤逻辑](https://github.com/openai/codex/blob/rust-v0.144.4/codex-rs/protocol/src/openai_models.rs)
   会在发给 Responses API 前移除该 sentinel，而不是把它当作模型广告的 Tier。
4. 配置只在真实 `thread.turn()` 接受三项 override 时算生效。Netizen 不创建空白
   Turn、不保存 pending config，也不声称能读取当前 Thread 已生效的三项值。卡片
   初始值因此只表示当前模型目录默认项。
5. 配置提交若命中 running/stopping Turn 必须拒绝，不能降级为 `steer()`、排队或等
   Turn 完成后自动重放。`/config` 卡片携带完整 Binding ID；若期间 `/new` 或
   `/resume` 改变 active Binding，本次提交不执行。
6. Model/Effort/Speed 统一收口到 `/new` 高级区和 `/config`，不注册独立 `/model`、
   `/effort`、`/fast`。Fast 只显示模型目录提供的 Service Tier 名称，不增加费用提示，
   也不与独立的 Codex Spark 模型合并。
7. slash command 使用统一注册表记录 Channel/native/host ownership 和 availability。
   `/goal` 与 Skills discovery 后由
   [ADR 0014](0014-use-removable-sdk-gap-adapters.md) 通过可逐项删除的窄 Adapter 启用；
   [ADR 0018](0018-remove-skills-command.md) 随后删除 `/skills` 浏览 control，仅保留
   `$skill-name` 的 discovery/revalidation。`/plan`、`/apps` 仍 fail closed 且不进入
   `/help`。一个飞书消息仍只解析一个 control 或 prompt，不增加任意 `/`、`@` 串联
   解释器。固定 SDK 已公开且可安全观察完成的 `/compact` 另由
   [ADR 0013](0013-map-public-native-compaction-safely.md) 处理。

若后续 public Skills/Apps discovery 可用，引用语法应沿用 Codex 的 `$skill-name` /
`$app-slug`，而不是复用飞书本身的 `@成员` 语义。同一条 prompt 可以解析多个受支持
reference 并构造成多个 typed input，但它们仍属于一个原生 Turn，不等于执行多个
slash control。

## 验证

- Runtime 单测证明快速路径的 `thread.turn` kwargs 为空，而 configured Turn 只把三项
  参数交给 exact native Turn；既有 Thread 仍按 exact ID resume，不传 resume
  override。固定 SDK 生成的 `TurnStartParams` 把三项都定义为对当前及后续 Turn 的
  override，因此配置由原生 Thread 延续，不由 Channel 重发或保存。
- 模型目录单测覆盖未知未来 Effort/Tier、不同模型支持矩阵、默认项、过期卡片、
  不兼容组合及不可读取的分页；进度/终态卡片验证 Speed 显示 live catalog 名称而
  不是协议 ID；SDK contract test 固定 `models()`/Turn 参数存在、公开 facade
  migration sentinel，以及 ADR 0014 Goal/Skills Adapter 的独立 capability contract。
- Channel/Card 单测覆盖高级新建、`/config`、完整 Binding 前置条件、running 拒绝、
  live revalidation、回执屏障和 completion 回写。
- 发布前运行 `scripts/probe_python_sdk.py --phase models`，只读输出目标 Codex 的实时
  默认模型、各模型 Effort/Tier 支持和默认值；不能用仓库内静态列表替代。
- 目标环境手工验收依次提交目录广告的加速 Tier、`Standard`，再发送一条省略
  override 的普通消息，确认 exact Thread 连续可用；这补充验证 release source 中的
  `priority -> default -> inherited standard` 路径。

## 后果与移除触发器

用户可以无摩擦继承原生默认值，也可以显式配置三项；代价是当前 SDK 下不能只改配置
而不开始下一条真实任务，也不能在卡片中可靠显示既有 Thread 的当前值。这一限制会在
卡片中明确说明。

当官方 Python 高层 facade 提供可确认的 Thread settings read/update 时，可新增 ADR
把 `/config` 改成真正的 idle 编辑，同时仍由 Codex 持久化并删除“必须同时提交任务”
限制。当 facade 提供 Goal/Skills 高层方法时，按 ADR 0014 的 migration sentinel 和
parity harness 逐项切回公开实现；Plan/Apps 在另有产品决策前仍不得以私有 RPC 或
prompt shim 绕过。
