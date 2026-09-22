# Codex SDK 0.155.1：Netizen 薄封装与产品妥协审计

> 本文保留实施前的研究快照，以下“当前”与验证记录均指调研阶段。后续已完成 SDK
> 0.155.1 升级及 Goal exact-Turn steer 接入；现行行为以 [ADR 0069](../adr/0069-steer-the-current-physical-goal-turn.md)
> 为准，最终单元、synthetic 与 live 验证记录见[部署文档](../deployment.md#代码门禁与按需实时兼容性验证)。

调研日期：2026-09-22。当前仓库固定 `openai-codex==0.154.0`；查询 PyPI 得到最新稳定版为 **0.155.1**，Python wheel 上传时间为 **2026-09-20 00:41:04 UTC（北京时间 08:41）**。CLI 0.155.1 的公告日期是 9 月 18 日，两者不是同一个发布时间。本次只审计，不升级依赖、不修改运行代码或 ADR。[PyPI 发布元数据](https://pypi.org/pypi/openai-codex/json)、[官方 changelog](https://learn.chatgpt.com/docs/changelog)

## 结论与证据边界

**0.155.1 没有新增 Python facade；本次升级不能直接删除任何现有 SDK gap adapter。** 本次下载的官方 0.155.1 wheel 中，`openai_codex/` 的 22 个非 `__pycache__` 文件与现有 0.154.0 包逐文件相同；进一步下载 **PyPI 官方 0.154.0 wheel** 直接对比，结果仍无差异，因此不依赖本地虚拟环境恰好未修改的假设。0.155.1 wheel 的 METADATA 第 13 行把 bundled runtime 依赖固定为 `openai-codex-cli-bin==0.155.1`。这说明本次实质变化在随附 runtime，不能把 Codex CLI/App Server 发布的新能力自动当作 Python 高层 API。[新版 Python 包](https://pypi.org/project/openai-codex/0.155.1/)、[旧版 Python 包](https://pypi.org/project/openai-codex/0.154.0/)

复核临时材料：`/tmp/netizen-sdk-review-20260922-Y14xsK/new/openai_codex/`；包 metadata：同目录的 `openai_codex-0.155.1.dist-info/METADATA`。旧包：项目 `.venv/lib/python3.11/site-packages/openai_codex/`。临时材料不作为交付依赖；包内容结论来自本次下载和比对，不等于新版 runtime 的 live 兼容性已验证。

Python 源码 fingerprint 仍为 `9db021b08bbcc75f18206d64ecf8a7d5ba63b380d91181718a3c9153ed4a053f`，但版本校验仍会失败：cleanup 校验发生在服务启动，Activity observer 也有独立版本校验。因此改 dependency pin 后不能直接部署；要先运行升级门禁，再更新通过验证的 exact version。相同 Python 源码并不能替代新版 App Server 行为验收。[cleanup 版本与指纹](../../netizen/terminal_cleanup.py#L23)、[cleanup 校验](../../netizen/terminal_cleanup.py#L105)、[Activity 校验](../../netizen/turn_plan_observer.py#L218)、[初始化接线](../../netizen/main.py#L212)

复核下载 SHA-256：0.154.0 wheel 为 `b5f354e1280621d0f5e28313ecf6974d2a98bcf0b088dbe791dc6df5044d2214`；0.155.1 wheel 为 `cc7eee57ab2803a59b5125f25190839af37689f09812f0fca6565d722303cf5d`。两个值均与各自 PyPI metadata 一致。以临时 `PYTHONPATH` 导入新包，只做静态 inventory：版本为 0.155.1，`facade_migration_requirements()` 返回空 tuple；`AsyncCodex`、`AsyncThread`、`AsyncTurnHandle` 的公开方法没有增加。发布包事实优先于根据 Git tag 提交标题推断 Python API 新增与否。

## Runtime 更新：实际收益与适用范围

以下是新旧 runtime 源码和官方提交的静态核验，不等于已在 Netizen 目标环境完成 live 验证。

| 变更 | 对 Netizen 的意义 | 不能据此删除的逻辑 |
| --- | --- | --- |
| Goal 连续三轮空自动 continuation 后写 `blocked` | 原生防止一种明确的无进展空转；需无最终正文且无可观测 activity，不能泛化为识别所有无效工作 | Goal 状态展示、四项完成证据、blocked 不自动 clear |
| `update_goal` 允许用户明确要求的 `paused` | 为将来支持自然语言暂停提供底层能力 | 当前 Goal active 时普通消息被拒绝，升级不会自动让“暂停一下”到达模型；命令暂停仍需 exact Turn interrupt/cleanup |
| pre-turn compaction 失败时保留已接收输入 | 减少失败后用户输入未写入原生历史的问题 | ACK 不等于结果已完整落盘；终态读取和后台命名的输入可见性等待仍有必要 |
| auto-review 的完整 action/授权证据、重试和错误分类改进 | Netizen 默认 `auto_review` 可继承更可靠的原生审批行为 | 不等于飞书获得 Ask/Custom 交互审批能力 |
| resume 保留运行时 workspace roots，fork 保留 runtime/multi-agent 信息与工具截断预算 | 有利于同 ID 恢复、Side 和命名 fork 的一致性 | Binding 配置意图、权限继承验证、Side boundary 和 exact identity |
| stdio 退出有界、Unix SIGTERM 更规范，MCP reader/proxy teardown 改进 | 减少退出期间的残留与等待风险 | Python SDK `close()` 仍是 terminate 后最多等 2 秒再 kill；不保证 foreground 退出，不替代 Netizen shutdown/cleanup 编排 |

依据：[Goal 空续跑 #44320](https://github.com/openai/codex/commit/0735c519789d)、[用户请求暂停 #44290](https://github.com/openai/codex/commit/fa7af3883df4)、[压缩失败保存输入 #44487](https://github.com/openai/codex/commit/ee93abb69029)、[自动审批 release notes](https://learn.chatgpt.com/docs/changelog)、[resume roots #43848](https://github.com/openai/codex/commit/6515a72db7a8)、[fork 版本 #43540](https://github.com/openai/codex/commit/cc737efd65b9)、[截断预算 #44248](https://github.com/openai/codex/commit/aa88a0333c75)、[stdio shutdown #44523](https://github.com/openai/codex/commit/713caa89f389)、[SDK close 实现](https://github.com/openai/codex/blob/rust-v0.155.1/sdk/python/src/openai_codex/client.py#L283)。

还有三类容易混淆的更新：

- **managed daemon 重启恢复**：源码明确要求 Unix socket transport 且 `managed_daemon=true` 才消费恢复快照。当前 SDK 启动的是 `app-server --listen stdio://`，因此不能以这一发布说明取消 Netizen 的 external-active Goal 隔离。改用另一种运行方式是独立架构变更；即使能恢复 runtime，也还需重建精确 Goal route、消息归属和终态 handoff。[启动条件](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server/src/lib.rs#L757)、[恢复启用条件](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server/src/lib.rs#L989)、[SDK stdio 启动](https://github.com/openai/codex/blob/rust-v0.155.1/sdk/python/src/openai_codex/client.py#L243)
- **thread attachments**：App Server 增加 thread-scoped attachment add/list/remove/updated；Python generated types 存在，但没有新的高层方法。结构是 `thread_id` 与任意 `payload/identity_key`，没有强类型 exact Turn 文件生成证明；不能直接替代 Netizen Files 的本轮 provenance、diff 与交付规则，也不应为本次升级另添私有 RPC。[协议定义](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server-protocol/src/protocol/v2/thread_attachment.rs)、[端点实现](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server/src/request_processors/thread_attachments.rs)
- **TUI 更新**：实验语音、状态栏 reasoning summary、Touch ID、agents overview 的 hide/archive/delete 等主要属于原生客户端体验；0.155.1 补丁恢复 TUI 默认关闭 reasoning summaries。这些不会自动成为飞书功能。[官方 changelog](https://learn.chatgpt.com/docs/changelog)

## 可以独立排期的产品优化

优先级建议是：先完成 runtime 兼容升级；再按独立需求评估以下工作，避免把架构变化混入换版本。

1. **Goal 停止原因更清楚**：覆盖新 runtime 的“空自动续跑达到阈值后 blocked”行为，确认它投影为停止/阻塞而非成功；没有结构化原因时只展示已有状态，不能凭 `blocked` 推断就是空转。此项直接受新版 runtime 行为影响。
2. **减少普通 Turn 轮询成本**：公开 stream 驱动完成、失败时再 read 的方案值得单独评估，但已有 0.154.0 就有所需基础。它会改变目前 history 权威和 single-consumer 设计，不能按纯删代码处理。更理想的上游退出条件是公开只读事件观察与 exact Turn read。
3. **外部通知的原生低权限输入**：`ExternalMessage` 把机器人/工具/应用通知保留为 tool-level authority，可以作为未来事件或机器人结果接入的原生选项。两个发布包已经都支持它，最低 runtime 为 0.151.0，并非本次新增。它必须作为整个 `turn()` 输入，不能混入用户输入列表；`handle.steer()` 也不接收该类型。因而不能直接替换同一次提交中的 catch-up/quoted 背景加 current request，也不能用“先发 external 再发 user”制造两轮或竞态，更不能把真人请求整体降为 tool 权限。[官方 SDK API reference](https://github.com/openai/codex/blob/rust-v0.155.1/sdk/python/docs/api-reference.md#externalmessage)、[输入类型实现](https://github.com/openai/codex/blob/rust-v0.155.1/sdk/python/src/openai_codex/_inputs.py#L48)、[现有上下文契约 ADR 0039](../adr/0039-add-binding-scoped-mention-catch-up-context.md)
4. **原生触发来源与单次 Speed**：公开 `source=` 可标记用户/定时任务/命名等启动来源；`turn_service_tier=` 可为一次新 Turn 设置 tier 而不覆盖 Thread 默认值。这也早已存在于 0.154.0 wheel；前者是可观测性补充，不能替代现有来源包装或 Scheduler，后者仅在产品明确需要单次设置时有用，不应擅自覆盖 Binding 的长期配置。[Turn options](https://github.com/openai/codex/blob/rust-v0.155.1/sdk/python/docs/api-reference.md#turn-options)

## 薄封装清单：本次均保留，退出条件逐项独立

| 当前能力与源码 | 临时 SDK 依赖 | 真正可删除的条件 | 切换公开 SDK 后仍保留的业务契约 |
| --- | --- | --- | --- |
| Skills discovery：`AppServerSkillCatalog`，[sdk_gap_adapter.py:193](../../netizen/sdk_gap_adapter.py#L193) | 私有 `_client` ownership edge + 固定 `skills/list`；实际输入已用公开 `SkillInput` | 同一 client 的公开 skills catalog 支持 cwd、force reload、errors，并通过现有 discovery/typed Turn/steer 探针 | live name/path revalidation；disabled、同名歧义、过期选择 fail closed；一个引用只注入一次 |
| Goal：`AppServerGoalControl` 与 `_AppServerGoalHandle`，[sdk_gap_adapter.py:456](../../netizen/sdk_gap_adapter.py#L456)、[handle:798](../../netizen/sdk_gap_adapter.py#L798) | 私有 `_goal` 类型、低层 register/start/pause/clear、SDK 自己的多物理 Turn stream；Netizen 没有复制 continuation 算法 | 公开 API 同时覆盖 start/resume/read/clear/pause、route-before-mutation、物理 Turn rollover、取消与通知 ownership；只有 get/set 不足以删整个 Goal shim | exact logical/physical identity、未知 mutation 不重试、四项终态证明、final Turn Result/Files、终态 handoff 后释放槽 |
| Native Delete：`AppServerThreadDeleteControl`，[sdk_gap_adapter.py:280](../../netizen/sdk_gap_adapter.py#L280) | 私有 ownership edge + 固定 `thread/delete`，generated types | `AsyncCodex.thread_delete` 或 `AsyncThread.delete` 等价公开方法，通过响应丢失/取消和 disposable live 测试 | native-first、App Server descendant cascade、一次四视图对账、Binding-local unknown、不重发未知 mutation |
| Side boundary：`AppServerSideBoundaryControl`，[sdk_gap_adapter.py:329](../../netizen/sdk_gap_adapter.py#L329) | 固定 `thread/inject_items` | 公开 inject API 保留同一个 ephemeral Thread 的 exact boundary，并通过多轮 live 探针 | Side route/墓碑、多轮 Session、parent/side 并发、ephemeral 身份、同真实 cwd |
| 订阅释放：`AppServerThreadSubscriptionControl`，[sdk_gap_adapter.py:387](../../netizen/sdk_gap_adapter.py#L387) | 固定 `thread/unsubscribe` 与三种 response status | 公开 unsubscribe 支持 `notLoaded` / `notSubscribed` / `unsubscribed` 并通过 same-ID resume | 普通 Binding 空闲策略、后台 terminal 阻止释放、瞬态 generation/ABA 检查、unknown 不降级；无订阅不等于立即卸载 |
| Background terminal clean/list：`PinnedExperimentalTerminalCleanup`，[terminal_cleanup.py:60](../../netizen/terminal_cleanup.py#L60) | 私有 ownership edge；固定 clean/list RPC；version/fingerprint/experimental gate | 同语义公开高层 clean 和 list；分别通过 exact Thread synthetic、marker/resume、release 探针 | interrupt 与 cleanup 区别；成功 clean 不证明 foreground 已退出；自动释放绝不隐式 cleanup |
| Activity retained-event 观察：`PinnedTurnActivityObserver`，[turn_plan_observer.py:86](../../netizen/turn_plan_observer.py#L86) | 私有 router、RLock、exact Turn event store、绝对 cursor、版本/指纹 | 公开 API 能提供同等事件观察与 ownership/retention/cancellation 语义，或明确修改消费架构并重验 ordinary/Side/Goal 三条链 | exact Turn 身份、安全白名单、脱敏、有界投影、展示不影响执行、无第二消费者 |

对应决策：[ADR 0014](../adr/0014-use-removable-sdk-gap-adapters.md)、[0021](../adr/0021-support-multi-turn-ephemeral-side-topics.md)、[0028](../adr/0028-release-idle-persistent-thread-subscriptions.md)、[0037](../adr/0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md)、[0009](../adr/0009-use-version-gated-experimental-terminal-cleanup.md)、[0052](../adr/0052-project-safe-turn-activity-with-one-consumer.md)。这些是各项退出条件的原始依据；公开 facade 一旦出现，现有 sentinel 要求显式迁移，而非运行时失败后 fallback。[migration sentinel](../../netizen/sdk_gap_adapter.py#L905)

`turn_activity.py` 主要是 Netizen 需要保留的事件安全投影，真正窥视 SDK 私有数据的是 `turn_plan_observer.py`。因此不能把整个 Activity 模块作为可删 shim；即便以后用公开 stream，也仍需投影和通道层展示规则。[projection 数据类型](../../netizen/turn_activity.py#L105)、[ADR 0052](../adr/0052-project-safe-turn-activity-with-one-consumer.md)

## 产品和实现妥协：哪些可考虑，哪些本版本没有解锁

### 普通 Turn 完成与历史读取

当前默认每 0.5 秒轮询原生 Thread，普通持久 Turn 仍由公开 history 中的 exact Turn 确认终态；公开 stream 只在终态证明后限时排空 usage/diff。这里的**原始 completion race 已在现有 0.154.0 修复**，不是 0.155.1 带来的新收益。代码明确记录保留 history 权威是为了 steer/interrupt 和 transport 中断后的 observation。可以另立优化，将公开 stream 作为主要完成路径、仅在故障时 read，但这是需要新契约与行为验证的架构变更，不能因更新版本号就删除 polling/recovery。[终态读取与注释](../../netizen/codex_runtime.py#L6462)、[stream drain](../../netizen/codex_runtime.py#L5785)、[ADR 0049](../adr/0049-bound-turn-observation-and-delegate-thread-removal.md)

可继续关注更细粒度的公开 `turn read/items` API：当前读取 exact Turn 仍需要 `thread.read(include_turns=True)`，长历史的读取成本和列表/SQLite 分页投影短暂不一致有专门兜底。0.155.1 没有增加 Python 方法，不能宣称这一成本已经消失。如果 runtime 修复 `list_turns/list_items is not supported yet` 的竞态，也应以对应回归证明再删除精确错误分类，而不是删除全部 observation 恢复。[full view 分类](../../netizen/codex_runtime.py#L6497)、[分页错误兼容](../../netizen/codex_runtime.py#L6650)

保留的契约：仅 exact `completed/failed/interrupted` 释放本轮；观测不可用只做一次有界恢复，之后静止在 Binding-local 槽；不启动新 Turn、不重放用户输入；已确认终态后正文 materialization 最多有限重读，不反复恢复任务。[重检入口](../../netizen/codex_runtime.py#L4555)、[terminal materialization](../../netizen/codex_runtime.py#L6541)

### Goal：零 Turn、重启接管、steer 与结构化输入

- 新 Goal 先创建/恢复 exact Thread，再要求公开 read 证明 idle、非 ephemeral、已有持久化 path；零 Turn 新 Thread 若尚未持久化就拒绝，不以 dummy Turn 垫场。runtime 若修复这一行为可改善首次使用，但 Python facade 相同不能证明新版本已解决，必须重跑 `goal` live phase。[创建与校验](../../netizen/codex_runtime.py#L4019)、[部署 goal gate](../deployment.md#代码门禁与按需实时兼容性验证)
- 重启后发现 active persisted Goal，只标记 external-active 并拒绝普通消息；安全 route 恢复缺口没有被 0.155.1 的 Python API 解锁。恢复 paused Goal 仍必须先 register route、再 set active。未来公开 attach/reconnect 若只读取状态、不能保证事件完整性，也不足以自动接管。[guard](../../netizen/codex_runtime.py#L4080)、[resume ordering](../../netizen/sdk_gap_adapter.py#L587)
- active Goal 期间普通 Prompt/steer、compact/config 拒绝是当前产品选择，理由是不能安全跨自动 rollover 定位 exact physical Turn。新版本没有公开 Goal handle/steer 能力；不应自动放开。[设计 Goal 运行约束](../design.md#运行与锁)、[ADR 0014](../adr/0014-use-removable-sdk-gap-adapters.md)
- `GoalControl.start(thread_id, objective)` 没有 token budget 或 structured Skill/App input。future API 的预算提交、typed input 和 resume parity 可减少限制，但本版本没有新增。协议中存在字段不等于现有 start helper 或 Goal 首轮具备对应语义。[GoalControl port](../../netizen/sdk_gap_adapter.py#L165)、[start 实现](../../netizen/sdk_gap_adapter.py#L571)
- 四项完成证据、complete-only 自动 clear、exact final Turn Result/Files 以及终态 handoff 期间占槽是业务正确性，不能随 facade 更换删除。尤其 thread-scoped clear 仍无 Goal generation CAS；公共 get/set/clear 即使出现，也不自动解决外部并发 Goal 更新。[终态证明](../../netizen/codex_runtime.py#L6381)、[finalization](../../netizen/codex_runtime.py#L6188)、[ADR 0047](../adr/0047-compose-typed-reply-cards-and-finalize-complete-goals.md)

### Side、订阅与后台命名

Side 使用公开 ephemeral fork 与唯一 `handle.run()`，不套普通持久 Thread 的 history recovery。现有 0.154.0 已保留快速通知；Side 专属极快完成 gate 仍未设立，这是当前明确验收范围，不能把新版发布等同于风险已额外验证。新 SDK 也不会让 ephemeral Side 在重启后可恢复，route 墓碑仍必须保留。[ADR 0021](../adr/0021-support-multi-turn-ephemeral-side-topics.md)、[当前兼容结论](../deployment.md#已验证的兼容性结论)

`include_turns=False` 已用于 resume/fork，只省略返回历史，保留模型上下文；这项优化已落地，不能再次列作 0.155.1 新收益。[resume 实现](../../netizen/codex_runtime.py#L3994)、[既有能力测试](../../tests/test_codex_sdk_capabilities.py#L64)

普通 Thread 15 分钟 warm window、切走后的释放以及 terminal inspector 是产品资源策略；即使以后公开 unsubscribe，计时与 unknown 状态不能整体删除。后台命名则已用 private ephemeral fork，首轮 ACK 不证明上下文落盘，现有有界等待输入可见后再 fork 仍需新版 runtime 专项探针验证。[ADR 0028](../adr/0028-release-idle-persistent-thread-subscriptions.md)、[ADR 0067](../adr/0067-name-threads-with-private-ephemeral-forks.md)

### Delete、Archive 与 Compact

Archive/Delete 的 active Thread 关闭和 descendant cascade **已经委托 App Server**。Netizen 已删除“先 interrupt/pause/cleanup/等待 idle 再删除”的旧妥协；不能列作待升级后新做的优化。未来公开 delete facade 只消除请求 shim，不消除目录对账和失联后 unknown；本次 facade 未变。[ADR 0049](../adr/0049-bound-turn-observation-and-delegate-thread-removal.md)、[delete runtime](../../netizen/codex_runtime.py#L2318)

`compact()` 返回 acknowledgement 而无 Turn ID，因此当前记录 baseline、从新增历史寻找唯一 compaction Turn，并限制 10 分钟完成等待。若未来公开 API 返回 exact compaction handle，可以删除 baseline 归因和部分轮询；0.155.1 facade 未变，仍不能删除。[compaction 归因注释](../../netizen/codex_runtime.py#L5700)、[compact gate](../deployment.md#代码门禁与按需实时兼容性验证)

### Plan、审批、Apps、配置与模型目录

这些仍为显式 gap 或产品非目标，不是升级包本身会实现的功能：

- Plan collaboration control：`AsyncThread.turn` 无 `collaboration_mode`，不存在公开 Plan 控制。当前 Activity checklist 只是模型 `update_plan` 的展示，不能当 Plan 模式；0.154.0 已默认关闭 `update_plan`，production 继承用户配置，live fixture 单独开启。[capability test](../../tests/test_codex_sdk_capabilities.py#L191)、[plan 验证说明](../deployment.md#代码门禁与按需实时兼容性验证)
- 新 Thread 沿用公开 SDK 默认 `auto_review`；Ask/Custom 不完整继承，飞书审批卡仍是明确非目标。即使 runtime 增强审批，也需要 Channel 原生 request/response 生命周期与权限设计，不能直接打开。[thread start 注释](../../netizen/codex_runtime.py#L3013)、[目标与边界](../design.md#目标与边界)
- Apps discovery、安装/授权管理、高层 config/MCP 管理仍无新 facade；`MentionInput` 早已公开不等于存在可信 apps catalog，也不等于 `$app` 产品入口已启用。[capability test](../../tests/test_codex_sdk_capabilities.py#L214)、[设计能力边界](../design.md#运行与锁)
- idle effective model/effort/tier read 不可得，目前只显示 Binding 客户端意图；不能把模型目录 default 当作 native Thread 实际配置。`models()` 无 cursor，非空 next_cursor 被明确拒绝；后续若支持分页，才可移除这一妥协。0.155.1 没有改变它们。[模型目录校验](../../netizen/model_settings.py#L66)、[设计配置说明](../design.md#运行与锁)

## 实际升级门禁与建议顺序

1. 将升级视为 bundled runtime 升级，保留所有现有 adapters。明确更新 exact dependency pin、cleanup/Activity 的版本 gate；源码 fingerprint 不变应记录证据，不能绕过版本与行为验证。[门禁实现](../../netizen/terminal_cleanup.py#L105)、[Activity gate](../../netizen/turn_plan_observer.py#L218)
2. 跑 facade inventory、capability shape 与真实 SDK + fake stdio synthetic；当前无 migration-required 是预期，不应删除 sentinel。`make check` 包含全部单元测试、compileall、pip check 与 `check_sdk.py`，其中快速完成 20 次、read recovery 20 次、usage/diff 40 次。[Makefile](../../Makefile#L8)、[check_sdk.py](../../scripts/check_sdk.py#L11)、[capability tests](../../tests/test_codex_sdk_capabilities.py#L30)
3. 依据部署文档，升级 pinned SDK/App Server 触发完整 live 集合：models、turn-settings、smoke、usage、steer、plan、polling、compact、concurrency、interrupt、skills、lifecycle、side、release、config、goal、sandbox；另按已规定的 SDK 变更触发运行 naming probe，以及当前调度/MCP/项目删除合同所要求的相关探针。只创建 disposable fixture，不能把一次只读研究擅自变成真实任务与删除操作。[完整触发规则](../deployment.md#代码门禁与按需实时兼容性验证)、[调度兼容性](../deployment.md#定时任务兼容性与验收)
4. Delete 保留四视图和 active/archived/running fixture；Goal 保留零 Turn persistence、resume rollover、external-active 隔离；Side 保留 parent/side overlap 与多轮；cleanup 保留 marker 分类与 same-ID resume。源码相同只缩小 Python 私有布局风险，不覆盖这些 runtime 语义。[部署验证细则](../deployment.md#代码门禁与按需实时兼容性验证)
5. 如果要同时清理设计债务，另拆一项“ordinary stream 主路径 / history fallback”的独立设计与验证工作；它有潜在收益，但不是本次版本新增能力，也不应混入安全小版本升级并声称天然等价。[ADR 0049](../adr/0049-bound-turn-observation-and-delegate-thread-removal.md)、[ADR 0052](../adr/0052-project-safe-turn-activity-with-one-consumer.md)

## 本次验证记录

- 完成：PyPI 当前版本和上传时间查询；新旧官方 wheel 下载 SHA-256 校验；22 文件逐一对比；Python package fingerprint 对比；新版公开方法和 migration sentinel 静态检查；相关 runtime 源码及 Netizen/ADR 独立复核。研究文档本地链接目标均存在，`git diff --check` 无错误。
- 未通过：尝试用临时 `PYTHONPATH` 运行现有 `test_sdk_gap_adapter.py`，首项 unsubscribe 合成测试未完成，已中止。再对单项 `test_all_unsubscribe_terminal_statuses_are_success` 作 20 秒有界运行仍超时；用原有 0.154.0 环境作 15 秒对照同样超时。这是本次环境下未解决的验证问题，不能将其判定为 0.155.1 回归，也不能声称 synthetic gate 已通过。
- 未执行：依赖升级、`make check` 全量门禁、任何 live probe、服务部署或重启；没有建立新版 runtime 的可部署兼容结论。

本次仅新增研究笔记，保留原有研究文件；未修改运行代码、依赖、产品契约或 ADR。建议以独立 runtime 升级工作解决上述验证缺口，再决定上线；薄封装退出另按各自公开 API 条件推进。
