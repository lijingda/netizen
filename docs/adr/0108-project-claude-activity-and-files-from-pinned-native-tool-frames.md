---
status: proposed
date: 2026-11-25
amends: 0105
related: 0025, 0027, 0052, 0086, 0107
---

# 从固定版本的原生工具消息投影 Claude Activity 与 Files

## 背景

[ADR 0105](0105-extend-backends-through-capabilities-and-shared-presentation.md) 的 D① 要让所有者在飞书看到 Claude 正在做什么，完成后可以取回本轮写入的文件。
固定的 SDK 0.2.186 和随附 CLI 2.1.329 在同一条有序消息流上报告工具调用及结果。调用与结果有共同的工具 ID，但工具申请写入的位置可能被原生规则改写。实测顶层 Write 的结果报告了实际写入的位置，子 Agent 的 Write 则可以成功且不报告位置，原申请路径上还可能留着另一份文件。匿名执行开始时也可能缺少足以确定飞书投递位置的身份。

## 决定

### 消息来源与物理轮

在已有 `ordered_messages` → `_observe` 消费链上读取公开的 `AssistantMessage.ToolUseBlock` 和 `UserMessage.ToolResultBlock`。展示只消费观察到的消息，不另开读流、注册 hook、查询转录或原生任务文件、追加工具调用或设置工具目录环境变量。Activity 只保存在内存中。

一个 init 的 `uuid` 标识一个物理轮。其顶层调用与结果均须满足 `parent_tool_use_id is None`，并在该物理轮内按 `tool_use_id` 配对。启动子 Agent 的顶层调用显示为一行子任务，子层操作、正文及产物不进入展示。子层清单工具使该客户端的清单转为未知。晚到的子消息不能归给下一轮，也不能改写已冻结的结果。

人工输入仍以原输入 ID 作为展示键 `turn_id`，快照的 `physical_turn_id` 取 init 的 UUID。各物理轮分别持有活动窗口、该轮清单展示状态、过程消息窗口及文件证据。操作、清单、阶段性答案或状态行发生可见变化时递增独立的展示版本，纳入快照及展示协调的签名。准入复核和停止按钮继续读取 Binding 的控制版本，单纯刷新展示不会使已准备的输入或按钮失效。

### 活动

两个后端共用同一套累积逻辑维护卡片的有界进展与操作窗口、顺序、分段、按段聚合的子任务、清单和可见变化判断。选择过程消息时，同一份安全事件在卡片聚合前交给本轮的 `ProcessActivityWindow`。两个窗口沿用执行时冻结的设置。Claude 工具消息没有原生发生时间，卡片操作取接收该消息时的本地毫秒时间，过程消息沿用首次观察秒数。顶层稳定文本只按既有条件交付为阶段性答案。

| 顶层原生工具 | 活动类型与可显示的内容 |
| --- | --- |
| Bash、Read、Glob、Grep | COMMAND。分别为「执行命令 · command」「读取文件 · file_path」「搜索文件 · pattern」「搜索内容 · pattern · path」。 |
| Write、Edit、NotebookEdit | FILE_CHANGE。分别为「写入文件 · file_path」「编辑文件 · file_path」「编辑笔记本 · notebook_path」。这里显示申请操作的对象。 |
| WebFetch、WebSearch | WEB_SEARCH。「打开网页 · url」或「搜索网页 · query」。 |
| Agent 及其 Task 别名 | SUBAGENT。计数为 1，无文字。 |
| TodoWrite、TaskCreate、TaskUpdate、TaskGet、TaskList | 仅观察清单，不占操作行。 |
| Skill、ToolSearch、TaskStop、CronCreate、CronDelete、CronList、ScheduleWakeup、EnterPlanMode、ExitPlanMode、AskUserQuestion、EnterWorktree、ExitWorktree、ListAgents、SendMessage、ReportFindings、Monitor、Workflow | TOOL。只显示本表中固定的工具名。 |
| 其他工具，包括 MCP 和自定义名称 | TOOL。统一显示「调用工具」。 |

表中的字段经 Codex 共用的操作摘要过滤，并受 `operation_max_chars` 限制。缺失或类型不符的明细降为固定标签。其他输入字段、工具结果与输出、错误与拒绝原因、思考和子层文本均不进入活动。可见的清单标签另按下节过滤。

开始为 IN_PROGRESS，匹配结果中 `is_error` 为 true 时为 FAILED，false 或空时为 COMPLETED。后台 Bash 与异步 Agent 的立即回执只完成启动操作。仍未配对的操作一直保持运行，物理 Result 到达才按该轮的完成、中断或失败分类一并收尾。观察暂缺不能产生 END。

### 清单

一个客户端在寿命内保留一份有序清单，每行只需原生 ID、标签和状态。新建和续接连接都从空集合开始，只记录本连接观察到的步骤。只有配对的顶层成功结果才按以下规则处理。

| 工具 | 对清单的影响 |
| --- | --- |
| TodoWrite | 用成功结果中的 newTodos 整体替换，取 content 作标签，保留原顺序和显式空列表。 |
| TaskList | 用返回的完整 tasks 按原生顺序整体替换。两种整体替换都能重新建立已知基线。 |
| TaskCreate | 清单已知时，以结果中的 ID 和 subject 追加 pending 步骤。重复 ID 使清单未知。 |
| TaskUpdate | success 为 true 且清单已知时，按结果 taskId 与 statusChange.to 更新状态，deleted 删除该行。updatedFields 含 subject 时结果没有实际标签，清单转为未知。仅报告 status 字段而没有 statusChange 时保留原状态。目标 ID 不在集合时保留已有步骤。success 为 false 时保留原清单。 |
| TaskGet | 保留原清单。 |

清单与 Files 只取成功结果报告的实际值。原生规则可在工具运行前同时改写步骤文字和状态。工具明确失败时保留原清单。已识别的清单结果若不符合固定形状，或该客户端在任一物理轮内或两轮之间观察到子层清单工具，则转为未知。未知时局部修改不能建立基线。接受一个缺口：本连接没有观察过的旧任务暂不显示，直至原生 TaskList 报告完整列表。已知步骤只在证实删除或整表替换时移出，过程窗口可以据此结束该步骤。

每个物理轮单独记录是否已应用至少一次成功的顶层清单结果（`plan_generated`）。仅当本轮已生成且清单未知时，`plan_available` 为假。本轮已生成且清单已知才显示步骤，用共用的 `sanitize_activity_text` 过滤标签，将 pending、in_progress、completed 映射为对应步骤状态。只有同时开启清单展示时才将这份步骤交给过程窗口。两个后端在卡片上用「任务清单：尚未生成」表示本轮尚未生成清单。

### 文件与投递

只有物理 Result 确认完成才交付该轮的 Files。成功的顶层 Write、Edit 取匹配结果的 `filePath`，NotebookEdit 取经验证的成功结果（空字符串 `error` 和有效 `notebook_path`）中的实际路径。缺少实际路径时舍弃该项，即使申请路径上的文件确实存在。子 Agent、Bash、MCP 和自定义工具的产物均不收录。中断或失败轮没有 Files。

路径经 `PhysicalAnswer.file_evidence` 以 `FileEvidence.outputs` 的 `OutputPath` 交给共用解析器。按本轮 cwd 解析相对路径，规范化去重，检查当前普通文件。工作目录外的已证实文件同样可送，按钮点击时重新解析并发送当时的内容。共用的卡片和过程消息两条交付路径都支持只有文件的成功轮，沿用逐项回执、经核实的位置和结束提及。[ADR 0025](0025-use-turn-provenance-not-project-containment-for-files.md) 和 [ADR 0027](0027-use-turn-diff-and-self-contained-file-cards.md)

匿名唤醒和会话 cron 的 init 尚不能证明发起人和飞书锚点。其 Activity 冻结在本轮终态快照，文件取本轮的写入证据。沿既有 Result 归属投递到原输入或已核实的会话位置，运行时不开启展示器，结束时也不补建过程消息。[ADR 0107](0107-present-process-in-feishu-process-messages.md)

### 降级

读流校验新增字段。UserMessage 的 content 可为字符串或块列表，父调用 ID 可为字符串或空，ToolResultBlock 的调用 ID 必须为非空字符串，is_error 必须为布尔值或空。畸形身份事实沿用已有客户端降级。错误回执允许携带字符串。无法关联的结果、未知工具或无法证实的展示字段仅使相应明细省略、清单未知或文件证据缺失，执行与其余展示继续。

## 实施与验收

兼容门禁包括 SDK 和 CLI 精确版本、脱敏的已解码夹具，以及随附 CLI 经回环 provider 执行的 Activity 与 Files 场景。升级其中任一版本均须重新采集夹具并重跑原生场景。待公开 SDK 提供类型化操作、清单及实际写入路径并通过同组门禁后，逐项移除对工具字典形状的解码。

合并前验证两种回复的安全字段边界、进度与清单的 START/END、续接后新观察的步骤及子层造成的未知、文件改写与仅文件回复、晚到子消息和独立控制版本。Codex 的冻结记录只允许上述空清单文案变化。运行会话变异检查以及 `make check`、`make check-native`、`make check-native-claude` 和 `make check-browser`。真实飞书显示及所有者 auto 场景的验收按部署门禁单独记录。

## 备选方案

| 方案 | 取舍理由 |
| --- | --- |
| 按子 Agent 申请的路径提供文件，或注册 PostToolUse hook 取得实际路径 | 原生规则可能改写目的地，申请路径上的旧文件会被误送。引入 hook 是新增原生输入，需要独立的 ADR 和门禁。 |
| 展示子 Agent 内部的操作 | 子层消息可能跨过父轮终态，难以在冻结时确认归属。Codex 也只展示子任务数量。 |
| 设置 `CLAUDE_CODE_ENABLE_TASKS=false` 强制使用 TodoWrite | 这会为取得完整清单而改变原生工具能力和所有者配置。 |
| 读取 CLI 任务文件，或主动调用 TaskList 补齐清单 | 前者越过公开 SDK 边界，后者改变本轮原生执行。 |
| Bash 只显示固定标签或模型填写的 description | 固定标签缺少 Codex 已有的命令预览。description 是模型自由文本，不能提供比过滤后的命令更强的安全保证。 |
| 按 Project 或 cwd 的目录范围筛选 Files | ADR 0025 已决定以本轮写入证据为准，目录过滤会漏掉合法的跨目录写入。 |
| 在匿名唤醒或 cron 的 Result 到达时补建过程消息 | ADR 0107 只允许从运行中的观察创建过程消息。 |

## 后果

Claude 的卡片和过程消息两种回复都能提供操作、清单和本轮文件。Codex 卡片的未生成清单提示也采用中性文案。子 Agent 和命令的产物不进入 Files，本连接未观察过的旧任务要等 TaskList 才显示，后台唤醒和会话 cron 没有实时过程消息，Claude 操作时间表示收到消息的时刻。任何 SDK 或 CLI 升级都需要重新采集夹具并重跑原生场景。
