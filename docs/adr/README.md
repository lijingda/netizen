# 架构决策记录（ADR）索引

本目录按编号保存 Netizen 的决策记录。ADR 是历史记录：说明当时的问题、选项与取舍，
不随实现更新而改写。**当前契约以 [工程设计](../design.md) 为准**；要了解某个 ADR 今天
是否仍然完整有效，先看下表的“状态”与“修订”列，再顺着修订链阅读。

## 何时写 ADR

新建 ADR 必须同时满足三条（与 domain-modeling skill 的标准一致）：

1. **难以撤销**：以后改主意的代价明显；
2. **缺少上下文会令人意外**：后来的读者看到代码会问“为什么要这样做”；
3. **确实经过取舍**：存在真实的备选方案，并出于具体理由做了选择。

本项目中常见的符合条件的决策：跨模块或跨生命周期的边界（Netizen 与 Codex 的职责、
身份与并发、持久化范围）；带锁定的技术选择（数据结构与迁移、部署与发布方式、对 SDK
私有能力的依赖）；明确拒绝某个看似合理、以后很可能被再次提出的方案。

单个功能的行为、展示与文案（例如卡片布局、统计口径、默认开关）不满足这三条，直接更新
[工程设计](../design.md)与[用户手册](../../skills/netizen-user-guide/references/user-guide.md)。
ADR 可以很短，一段话说清背景、决定和理由即可。

格式沿用本目录的现有约定：front matter 写 `status` 和 `date`。`status` 只用三个值：
`proposed`（已起草、尚未接受和实施）、`accepted`、`superseded`；修订关系写在
`amends`/`amended_by`、`supersedes`/`superseded_by` 字段，不写进 `status` 的值。草案只在
自身写 `amends`；接受并实施时改为 `accepted`，再在被修订的 ADR 中补上 `amended_by`，并同时
更新 design.md 中对应的当前契约和本索引。

下表的“类型”帮助判断阅读优先级：**架构决策**是相关改动前必须了解的约束；
**功能契约**是早期以 ADR 形式记录的单个功能行为，今后同类内容写入 design.md；
**升级记录**保存依赖重新认证的证据。

## 运行时边界与 SDK 适配

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0002](0002-separate-control-and-execution-planes.md) | 飞书控制面与 Codex 执行面分离 | 架构决策 | 已被 [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) 取代 |  |
| [0003](0003-codex-gateway-sdk-and-app-server.md) | 使用 Codex App Server 支持原生活动 Turn steering | 架构决策 | 已被 [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) 取代 |  |
| [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) | 使用官方 TypeScript SDK 作为唯一 Codex 后端边界 | 架构决策 | 已被 [0008](0008-use-python-sdk-for-native-turn-control.md) 取代 |  |
| [0008](0008-use-python-sdk-for-native-turn-control.md) | 使用官方 Python SDK 获得原生 Turn 控制 | 架构决策 | 有效 | 被 [0009](0009-use-version-gated-experimental-terminal-cleanup.md)、[0010](0010-correct-stop-and-background-cleanup-semantics.md)、[0012](0012-use-native-turn-model-settings.md)、[0013](0013-map-public-native-compaction-safely.md)、[0014](0014-use-removable-sdk-gap-adapters.md)、[0026](0026-upgrade-and-requalify-python-sdk-0147.md)、[0049](0049-bound-turn-observation-and-delegate-thread-removal.md)、[0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) 修订 |
| [0009](0009-use-version-gated-experimental-terminal-cleanup.md) | 用版本锁定的实验接口清理 Thread 后台终端 | 架构决策 | 有效 | 被 [0010](0010-correct-stop-and-background-cleanup-semantics.md)、[0014](0014-use-removable-sdk-gap-adapters.md)、[0026](0026-upgrade-and-requalify-python-sdk-0147.md) 修订 |
| [0014](0014-use-removable-sdk-gap-adapters.md) | 用可逐项移除的薄 Adapter 补齐 Python SDK 高层能力缺口 | 架构决策 | 有效 | 被 [0018](0018-remove-skills-command.md)、[0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md)、[0069](0069-steer-the-current-physical-goal-turn.md)、[0083](0083-page-native-session-history-through-a-narrow-read-adapter.md)、[0084](0084-separate-goal-controls-from-logical-turn-replies.md) 修订 |
| [0020](0020-observe-active-turn-plans-with-a-pinned-read-only-adapter.md) | 用精确锁定的只读 Adapter 观察运行中 Turn 的原生 Plan | 架构决策 | 有效 | 被 [0026](0026-upgrade-and-requalify-python-sdk-0147.md)、[0052](0052-project-safe-turn-activity-with-one-consumer.md) 修订 |
| [0026](0026-upgrade-and-requalify-python-sdk-0147.md) | 升级并重新认证 Python SDK 0.147.0 | 升级记录 | 有效 | 被 [0027](0027-use-turn-diff-and-self-contained-file-cards.md) 修订 |
| [0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) | 升级并重新认证 lark-channel-sdk 1.4.0 | 升级记录 | 有效 |  |
| [0077](0077-read-account-rate-limits-on-demand.md) | 按需读取共享 Codex 账号额度 | 架构决策 | 有效 |  |
| [0083](0083-page-native-session-history-through-a-narrow-read-adapter.md) | 原生会话历史采用只读分页适配 | 架构决策 | 有效 |  |

## 会话身份、生命周期与并发

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0001](0001-scope-conversation-and-project-binding.md) | 飞书作用域映射当前对话，Project Binding 创建时即不可变 | 架构决策 | 已被 [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) 取代 |  |
| [0004](0004-shared-conversation-permission-ceiling.md) | 共享 Conversation 的执行上限不可提升 | 架构决策 | 已被 [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) 取代 |  |
| [0006](0006-share-project-working-directory-across-conversations.md) | 同一 Project 的 Conversation 共享唯一工作目录 | 架构决策 | 已被 [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) 取代 |  |
| [0010](0010-correct-stop-and-background-cleanup-semantics.md) | 更正 `/stop` 与后台终端清理语义 | 架构决策 | 有效 | 被 [0049](0049-bound-turn-observation-and-delegate-thread-removal.md) 修订 |
| [0012](0012-use-native-turn-model-settings.md) | 通过真实 Turn 配置 Model、Effort 与 Speed | 架构决策 | 有效 | 被 [0014](0014-use-removable-sdk-gap-adapters.md)、[0016](0016-store-binding-turn-settings.md)、[0018](0018-remove-skills-command.md)、[0040](0040-make-new-card-only-and-show-all-projects.md) 修订 |
| [0013](0013-map-public-native-compaction-safely.md) | 安全映射原生会话压缩 | 架构决策 | 有效 |  |
| [0016](0016-store-binding-turn-settings.md) | 在 Binding 上保存持久的 Turn 配置意图 | 架构决策 | 有效 | 被 [0039](0039-add-binding-scoped-mention-catch-up-context.md)、[0040](0040-make-new-card-only-and-show-all-projects.md) 修订 |
| [0017](0017-manage-native-thread-lifecycle.md) | 以当前 Binding 管理原生 Thread 生命周期 | 架构决策 | 有效 | 被 [0019](0019-keep-native-thread-delete-unavailable.md)、[0036](0036-archive-exact-idle-sessions-from-the-sessions-card.md)、[0037](0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md)、[0038](0038-delete-exact-idle-sessions-from-the-sessions-card.md)、[0049](0049-bound-turn-observation-and-delegate-thread-removal.md)、[0082](0082-restore-archived-sessions-from-exact-native-results.md) 修订 |
| [0019](0019-keep-native-thread-delete-unavailable.md) | 在公开 Python SDK 支持前保持原生 Thread Delete 不可用 | 架构决策 | 已被 [0037](0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md) 取代 |  |
| [0028](0028-release-idle-persistent-thread-subscriptions.md) | 释放空闲普通 Thread 的连接订阅 | 架构决策 | 有效 | 被 [0078](0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md)、[0082](0082-restore-archived-sessions-from-exact-native-results.md) 修订 |
| [0036](0036-archive-exact-idle-sessions-from-the-sessions-card.md) | 从 `/sessions` 卡片归档 exact idle 会话 | 功能契约 | 有效 | 被 [0049](0049-bound-turn-observation-and-delegate-thread-removal.md) 修订 |
| [0037](0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md) | 用薄 SDK Gap Adapter 与原生目录对账开放 Thread Delete | 架构决策 | 有效 | 被 [0038](0038-delete-exact-idle-sessions-from-the-sessions-card.md)、[0049](0049-bound-turn-observation-and-delegate-thread-removal.md)、[0054](0054-allow-admin-to-delete-exact-materialized-sessions.md)、[0078](0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md) 修订 |
| [0038](0038-delete-exact-idle-sessions-from-the-sessions-card.md) | 从 `/sessions` 卡片两阶段删除 exact idle 会话 | 功能契约 | 有效 | 被 [0049](0049-bound-turn-observation-and-delegate-thread-removal.md) 修订 |
| [0040](0040-make-new-card-only-and-show-all-projects.md) | 把普通会话创建收口到 `/new` 卡片并展示全部 Project | 功能契约 | 有效 |  |
| [0049](0049-bound-turn-observation-and-delegate-thread-removal.md) | 有界处理 Turn 观测故障，并委托 App Server 移除 Thread | 架构决策 | 有效 | 被 [0052](0052-project-safe-turn-activity-with-one-consumer.md)、[0078](0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md) 修订 |
| [0073](0073-create-sessions-from-chat-defaults.md) | 根据聊天默认配置在原消息位置自动创建普通会话 | 架构决策 | 有效 |  |
| [0078](0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md) | 持久分支使用普通会话，恢复与删除以原生返回为准 | 架构决策 | 有效 | 被 [0082](0082-restore-archived-sessions-from-exact-native-results.md) 修订 |
| [0082](0082-restore-archived-sessions-from-exact-native-results.md) | 归档恢复直接使用精确原生结果 | 架构决策 | 有效 |  |
| [0088](0088-align-steer-failures-and-input-resume-rejections.md) | 统一追加输入失败与普通消息的恢复拒绝 | 架构决策 | 有效 |  |

## Goal、Side 与会话命名

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0021](0021-support-multi-turn-ephemeral-side-topics.md) | 用 ephemeral fork 支持多轮 Side 话题 | 架构决策 | 有效 | 被 [0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md) 修订 |
| [0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md) | 让 Side Turn 复用普通 Turn 的任务反馈与回复卡 | 功能契约 | 有效 | 被 [0051](0051-keep-lifecycle-reactions-and-make-pulse-optional.md)、[0052](0052-project-safe-turn-activity-with-one-consumer.md)、[0072](0072-share-question-interaction-across-binding-and-side.md)、[0079](0079-deliver-partial-answers-with-existing-reply-presentation.md) 修订 |
| [0067](0067-name-threads-with-private-ephemeral-forks.md) | 用内部临时 fork 自动补全原生会话名称 | 架构决策 | 有效 |  |
| [0069](0069-steer-the-current-physical-goal-turn.md) | 将 Goal 中的普通消息 steer 到捕获的当前物理 Turn | 架构决策 | 有效 | 被 [0084](0084-separate-goal-controls-from-logical-turn-replies.md)、[0088](0088-align-steer-failures-and-input-resume-rejections.md) 修订 |
| [0084](0084-separate-goal-controls-from-logical-turn-replies.md) | Goal 管理与逻辑 Turn 回复分离 | 架构决策 | 有效 |  |

## 会话协作与 Worktree

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0081](0081-coordinate-ordinary-sessions-with-native-external-messages.md) | 普通会话协作使用原生工具形态与 ExternalMessage | 架构决策 | 有效 | 被 [0083](0083-page-native-session-history-through-a-narrow-read-adapter.md)、[0092](0092-restrict-execution-to-enabled-group-chats-on-demand.md) 修订 |
| [0085](0085-manage-worktrees-independently-of-sessions.md) | Worktree 作为独立受管资源，会话使用实际工作目录 | 架构决策 | 有效 |  |

## 消息输入

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0005](0005-gate-websocket-inbound-on-redelivery-proof.md) | 生产 WebSocket 入站必须先证明失败重投 | 架构决策 | 已被 [0007](0007-use-official-typescript-sdk-as-codex-boundary.md) 取代 |  |
| [0011](0011-support-feishu-quoted-message-context.md) | 支持飞书逐条引用消息上下文 | 功能契约 | 有效 | 被 [0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) 修订 |
| [0015](0015-support-native-image-inputs-for-message-and-quote.md) | 支持普通消息与逐条引用中的原生图片输入 | 功能契约 | 有效 | 被 [0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) 修订 |
| [0029](0029-project-current-message-provenance-into-prompts.md) | 把当前飞书消息来源投影到每次 Prompt | 架构决策 | 有效 |  |
| [0030](0030-require-resolved-current-sender-names.md) | 要求当前 Prompt 消息有可解析的发送者姓名 | 功能契约 | 有效 |  |
| [0039](0039-add-binding-scoped-mention-catch-up-context.md) | 为普通 Binding 增加 @ 时补充期间消息的上下文模式 | 架构决策 | 有效 | 被 [0070](0070-schedule-inputs-in-existing-bindings.md)、[0082](0082-restore-archived-sessions-from-exact-native-results.md) 修订 |
| [0064](0064-share-card-and-forward-content-projection.md) | 统一卡片与转发材料的输入投影 | 功能契约 | 有效 |  |
| [0071](0071-answer-native-questions-through-binding-input.md) | 将原生结构化问题的回答作为原会话普通输入 | 架构决策 | 有效 | 被 [0072](0072-share-question-interaction-across-binding-and-side.md) 修订 |
| [0072](0072-share-question-interaction-across-binding-and-side.md) | 共用问答交互，分别进入 Binding 与 Side 输入入口 | 功能契约 | 有效 |  |
| [0090](0090-retain-input-attachments-independently-of-native-execution.md) | 输入附件保存原件，生命周期独立于原生执行 | 架构决策 | 有效 |  |

## 回复、Activity 与本轮文件

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0024](0024-send-structured-turn-files-from-completion-cards.md) | 从普通 Turn 终态卡片按需发送结构化本轮文件 | 功能契约 | 有效 | 被 [0027](0027-use-turn-diff-and-self-contained-file-cards.md)、[0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md)、[0053](0053-show-exact-turn-line-statistics-in-files.md)、[0065](0065-preview-local-images-in-result-markdown.md) 修订 |
| [0025](0025-use-turn-provenance-not-project-containment-for-files.md) | 本轮文件使用 Turn provenance，而不是 Project containment | 架构决策 | 有效 | 被 [0027](0027-use-turn-diff-and-self-contained-file-cards.md) 修订 |
| [0027](0027-use-turn-diff-and-self-contained-file-cards.md) | 以 Turn diff 补全本轮文件，并让新卡片自带文件清单 | 功能契约 | 有效 | 被 [0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md)、[0053](0053-show-exact-turn-line-statistics-in-files.md)、[0058](0058-use-page-selection-for-file-cards.md)、[0065](0065-preview-local-images-in-result-markdown.md) 修订 |
| [0046](0046-add-opt-in-binding-task-feedback.md) | 增加默认关闭的 Binding 任务反馈与有界进度卡 | 功能契约 | 有效 | 被 [0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md)、[0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md)、[0051](0051-keep-lifecycle-reactions-and-make-pulse-optional.md) 修订 |
| [0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md) | 组合类型化回复卡并只自动收尾完成的 Goal | 架构决策 | 有效 | 被 [0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md)、[0052](0052-project-safe-turn-activity-with-one-consumer.md)、[0053](0053-show-exact-turn-line-statistics-in-files.md)、[0079](0079-deliver-partial-answers-with-existing-reply-presentation.md)、[0084](0084-separate-goal-controls-from-logical-turn-replies.md) 修订 |
| [0051](0051-keep-lifecycle-reactions-and-make-pulse-optional.md) | 始终反馈任务生命周期，只让执行中表情闪烁可选 | 功能契约 | 有效 |  |
| [0052](0052-project-safe-turn-activity-with-one-consumer.md) | 用单一消费链投影安全的 Turn Activity | 架构决策 | 有效 | 被 [0071](0071-answer-native-questions-through-binding-input.md)、[0072](0072-share-question-interaction-across-binding-and-side.md)、[0079](0079-deliver-partial-answers-with-existing-reply-presentation.md)、[0086](0086-configure-progress-card-presentation-with-bounded-activity-segments.md) 修订 |
| [0053](0053-show-exact-turn-line-statistics-in-files.md) | 在 Files 模块展示 exact Turn 行数统计 | 功能契约 | 有效 | 被 [0056](0056-count-successful-patches-in-task-files.md) 修订 |
| [0056](0056-count-successful-patches-in-task-files.md) | 按成功 patch 累计本轮文件行数 | 功能契约 | 有效 |  |
| [0058](0058-use-page-selection-for-file-cards.md) | 文件卡片统一使用页码选择与跳转 | 功能契约 | 有效 |  |
| [0063](0063-mention-task-initiators-in-terminal-results.md) | 在任务结束时提及任务发起人 | 功能契约 | 有效 | 被 [0068](0068-run-saved-scheduled-plans-manually.md)、[0070](0070-schedule-inputs-in-existing-bindings.md) 修订 |
| [0065](0065-preview-local-images-in-result-markdown.md) | 在结果正文中预览明确引用的本地图片 | 功能契约 | 有效 |  |
| [0079](0079-deliver-partial-answers-with-existing-reply-presentation.md) | 在现有回复形态中交付阶段性答案 | 架构决策 | 有效 | 被 [0084](0084-separate-goal-controls-from-logical-turn-replies.md) 修订 |
| [0086](0086-configure-progress-card-presentation-with-bounded-activity-segments.md) | 用有界活动分段配置过程卡展示 | 功能契约 | 有效 |  |
| [0087](0087-use-instance-progress-card-settings-for-missing-session-intent.md) | 未配置过程卡的会话使用实例级兜底 | 功能契约 | 有效 |  |

## 命令与 Skills

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0018](0018-remove-skills-command.md) | 删除 `/skills` 浏览命令，保留原生 Skill 调用 | 功能契约 | 有效 |  |

## 定时任务

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0061](0061-schedule-ordinary-threads-in-feishu-topics.md) | 在独立飞书话题中调度普通持久会话 | 架构决策 | 有效 | 被 [0063](0063-mention-task-initiators-in-terminal-results.md)、[0068](0068-run-saved-scheduled-plans-manually.md)、[0070](0070-schedule-inputs-in-existing-bindings.md)、[0075](0075-migrate-channel-databases-during-installation.md)、[0076](0076-separate-cli-installations-from-instance-data.md)、[0085](0085-manage-worktrees-independently-of-sessions.md)、[0092](0092-restrict-execution-to-enabled-group-chats-on-demand.md) 修订 |
| [0068](0068-run-saved-scheduled-plans-manually.md) | 通过现有调度链路手动执行已保存的计划 | 功能契约 | 有效 | 被 [0070](0070-schedule-inputs-in-existing-bindings.md)、[0075](0075-migrate-channel-databases-during-installation.md) 修订 |
| [0070](0070-schedule-inputs-in-existing-bindings.md) | 定时向原会话提交普通输入 | 架构决策 | 有效 | 被 [0092](0092-restrict-execution-to-enabled-group-chats-on-demand.md) 修订 |

## Admin Web 与 Project 管理

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0031](0031-add-in-process-admin-web-control-plane.md) | 引入进程内 Admin Web 管理控制面 | 架构决策 | 有效 | 被 [0037](0037-reconcile-native-thread-delete-with-a-thin-gap-adapter.md)、[0049](0049-bound-turn-observation-and-delegate-thread-removal.md)、[0054](0054-allow-admin-to-delete-exact-materialized-sessions.md)、[0057](0057-run-manual-admin-upgrades-through-the-shared-installer.md)、[0060](0060-delete-projects-with-exact-session-inventory.md)、[0078](0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md)、[0080](0080-authenticate-admins-with-feishu-identities.md) 修订 |
| [0054](0054-allow-admin-to-delete-exact-materialized-sessions.md) | 允许 Admin 删除 exact materialized 会话 | 功能契约 | 有效 | 被 [0060](0060-delete-projects-with-exact-session-inventory.md) 修订 |
| [0057](0057-run-manual-admin-upgrades-through-the-shared-installer.md) | 通过共享安装器执行管理页手动升级 | 功能契约 | 有效 | 被 [0059](0059-support-explicit-admin-service-restart.md) 修订 |
| [0059](0059-support-explicit-admin-service-restart.md) | 通过同一部署执行者支持管理页独立重启 | 功能契约 | 有效 |  |
| [0060](0060-delete-projects-with-exact-session-inventory.md) | 按精确会话清单删除 Project | 架构决策 | 有效 | 被 [0078](0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md) 修订 |
| [0080](0080-authenticate-admins-with-feishu-identities.md) | 用飞书身份登录并维护实例管理员 | 架构决策 | 有效 | 被 [0091](0091-load-admin-user-credentials-on-demand.md)、[0092](0092-restrict-execution-to-enabled-group-chats-on-demand.md) 修订 |
| [0091](0091-load-admin-user-credentials-on-demand.md) | 按用户独立存储并按需读取 Admin 飞书凭证 | 架构决策 | 有效 |  |
| [0089](0089-browse-current-project-files-through-admin.md) | Admin 按需查看 Project 当前目录中的文件 | 架构决策 | 有效 |  |

## 数据、凭据与服务环境

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0022](0022-load-account-shell-environment-at-service-start.md) | 每次服务启动时加载账号 Shell 环境 | 架构决策 | 有效 |  |
| [0023](0023-keep-service-environment-authoritative-for-tools.md) | 让服务启动环境成为工具子进程的基线 | 架构决策 | 有效 |  |
| [0066](0066-share-lark-app-credentials-with-optional-cli.md) | 用单一应用凭据文件与可选 Lark CLI 共享机器人身份 | 架构决策 | 有效 |  |
| [0075](0075-migrate-channel-databases-during-installation.md) | 在安装事务中维护 Channel 数据库的前向迁移 | 架构决策 | 有效 |  |

## Channel 准入

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0092](0092-restrict-execution-to-enabled-group-chats-on-demand.md) | 按需限制机器人只在已启用的群聊中执行 | 架构决策 | 有效 |  |

## 安装、部署与发布

| ADR | 标题 | 类型 | 状态 | 修订 |
| --- | --- | --- | --- | --- |
| [0032](0032-use-single-user-product-root.md) | 使用单一用户产品根 | 架构决策 | 有效 | 被 [0066](0066-share-lark-app-credentials-with-optional-cli.md) 修订 |
| [0033](0033-use-official-sdk-for-feishu-app-onboarding.md) | 使用官方 SDK 完成飞书应用初始化 | 架构决策 | 有效 | 被 [0035](0035-gate-activation-on-effective-feishu-permissions.md)、[0044](0044-decouple-existing-app-repair-from-terminal-input.md)、[0045](0045-model-chat-info-as-an-alternative-scope-requirement.md)、[0062](0062-decouple-initial-app-onboarding-from-terminal-input.md)、[0066](0066-share-lark-app-credentials-with-optional-cli.md) 修订 |
| [0034](0034-support-macos-with-a-user-launchagent.md) | 使用用户级 LaunchAgent 支持 macOS | 架构决策 | 有效 | 被 [0057](0057-run-manual-admin-upgrades-through-the-shared-installer.md) 修订 |
| [0035](0035-gate-activation-on-effective-feishu-permissions.md) | 激活前校验飞书租户权限并复用官方已有应用选择 | 功能契约 | 有效 | 被 [0044](0044-decouple-existing-app-repair-from-terminal-input.md)、[0045](0045-model-chat-info-as-an-alternative-scope-requirement.md)、[0062](0062-decouple-initial-app-onboarding-from-terminal-input.md)、[0066](0066-share-lark-app-credentials-with-optional-cli.md) 修订 |
| [0041](0041-separate-published-release-and-source-install.md) | 分离已发布 Release 安装与源码安装 | 架构决策 | 有效 | 被 [0042](0042-reuse-main-qualification-for-release.md)、[0057](0057-run-manual-admin-upgrades-through-the-shared-installer.md)、[0062](0062-decouple-initial-app-onboarding-from-terminal-input.md) 修订 |
| [0042](0042-reuse-main-qualification-for-release.md) | 正式发布复用 main 代码资格 | 架构决策 | 有效 | 被 [0043](0043-remove-redundant-release-approval.md) 修订 |
| [0043](0043-remove-redundant-release-approval.md) | 移除冗余的正式发布人工审批 | 功能契约 | 有效 |  |
| [0044](0044-decouple-existing-app-repair-from-terminal-input.md) | 将已有飞书应用补权与终端输入解耦 | 功能契约 | 有效 | 被 [0062](0062-decouple-initial-app-onboarding-from-terminal-input.md) 修订 |
| [0045](0045-model-chat-info-as-an-alternative-scope-requirement.md) | 将群信息权限建模为等价 scope 能力 | 功能契约 | 有效 |  |
| [0050](0050-agent-executed-release-chain.md) | 由脚本在维护者指令后执行发布全链 | 功能契约 | 有效 |  |
| [0062](0062-decouple-initial-app-onboarding-from-terminal-input.md) | 将首次飞书应用初始化与终端输入解耦 | 功能契约 | 有效 | 被 [0066](0066-share-lark-app-credentials-with-optional-cli.md) 修订 |
| [0074](0074-deploy-independent-instances-by-root.md) | 按根目录部署独立实例，继续共享 Codex 用户状态 | 架构决策 | 有效 |  |
| [0076](0076-separate-cli-installations-from-instance-data.md) | 用普通 Python CLI 分发程序，以实例启动负责数据准入 | 架构决策 | 有效 |  |

## 统计

共 92 个 ADR：架构决策 59 个，功能契约 31 个，升级记录 2 个；其中 8 个已被取代，无草案。
