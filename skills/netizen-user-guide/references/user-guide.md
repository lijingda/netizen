# Netizen 飞书用户手册

Netizen 把飞书单聊、群聊主线和话题接入原生 Codex。飞书负责消息、卡片和会话入口；任务执行、原生 Thread、Turn、历史、工具、Skills、MCP、权限与 sandbox 仍由 Codex 管理。

本手册解释稳定的使用语义，是 `/help` 的自然语言补充。当前实例实际开放的命令以运行时 `/help` 为准；当前会话和任务状态以 `/status` 为准。

## 快速开始

1. 发送 exact `/new`，在卡片的单个下拉框中选择 Project，并选择继承 Codex 或显式
   Model、Effort 和 Speed；还可按需开启 Task Reaction 和 Progress Card，两项默认关闭。
   群聊和群话题还可选择 @ 时读取的消息范围。`/new` 不接受参数。
2. 直接发送任务描述。第一次真实任务才会创建原生 Codex Thread；单独 `/new` 不会产生空白 Turn。
3. 普通 Turn 运行时继续发送普通消息，会 steer 当前精确 Turn，不会排队成下一轮；
   Goal、停止或压缩状态会拒绝普通消息。
4. 用 `/status` 查看当前会话、任务、计划和上下文窗口；用 `/sessions` 查看同一聊天或
   话题中的其他会话，并可在卡片中直接将其设为当前。
5. 用 `/help` 查看当前实例此刻开放的命令。

群聊主线和群话题中，每一条触发机器人的消息都必须重新 `@机器人`。单聊以及单聊中的
话题无需 `@`。即使选择“自动带上期间的群聊讨论”，未 @ 的消息也只可能在下一次 @ 时
作为背景进入 Codex，不会让机器人自动响应。

## 核心心智模型

### Scope、会话与 Project

- 一个普通单聊、群聊主线或普通话题各自形成一个 Scope。每个 Scope 可以有多个会话，但同一时刻只有一个当前会话。
- 会话绑定一个 Project，也就是 Codex 实际工作的目录。实例默认目录作为 `none` 选项
  出现在 `/new` 的 Project 下拉框中。
- `/new` 只创建并切换到一个 Lazy 会话；首条真实任务才创建原生 Thread。
- 原生 Thread 和历史由 Codex 保存，能够在 Codex App/CLI 中继续使用。CLI/App 中新增的消息不会自动回填到飞书。
- 每条真实任务或 steer 都会把当前飞书消息的公开发送者信息交给 Codex，并随原生输入进入
  Thread 历史。它只说明请求来源，不授予权限、任务所有权或更高指令优先级。Channel SDK
  会从当前 chat 成员名单补全真实显示名；解析失败时本条消息不会执行，并会提示管理员为
  飞书应用开通 `im:chat.members:read`、发布应用版本后重试。发送者归属 ID 只保留当前
  应用内的 `open_id`，不会在 sender attribution 中加入 `union_id` 或租户级 `user_id`。
- Netizen 只在内存跟踪本进程实际打开的 Thread 订阅。当前会话空闲十五分钟后自动取消
  当前连接订阅；切换到其他会话后，旧会话一旦空闲就立即尝试取消。没有会话数量上限或
  LRU，服务重启也不会扫描 SQLite、resume 历史会话或重建 timer。
- 不同会话或 Side 可以并发，但同一 Project 使用同一个真实目录，文件改动会互相可见。并发修改同一文件时应由用户自行协调。

### Turn、steer 与排队

- 当前会话空闲时，普通消息开始一个新 Turn。
- 当前 Turn 正在运行时，普通消息固定 steer 这个精确 Turn：它用于补充条件、纠正方向或追加要求。
- 若 steer 恰好碰上 Turn 已结束，本条消息不会执行；看到提示后需要重新发送。
- Netizen 不保存 prompt queue，不会把运行中的新消息悄悄排成下一轮，也不会把多条消息合并成一个 prompt。
- 若确实想开始独立任务，应等待当前 Turn 结束、先 `/stop`，或切换/新建另一个会话。不同 Binding 可以并发。

### 飞书中的运行反馈

- 每个普通会话有两个独立的任务反馈选项，可在 `/new` 创建时或空闲时通过 `/config`
  修改；两项默认都关闭。都关闭时，任务被接受后到终态之间不发送任务表情或进度卡，
  最终结果仍会正常回复。
- Task Reaction 开启后，普通任务在原任务消息上使用 `Typing` 和低频 `THINKING`；steer
  成功后在 steer 消息上使用 `OnIt`，原任务消息仍是运行状态锚点。完成、失败或中断时先
  使用相应终态表情，再清理运行态表情。关闭时也不会发送 `OnIt` 失败的文字确认。
- Progress Card 开启后，Turn 被接受时回复一张运行卡。顶部过程区在运行中展开，只按
  当前状态与原生 checklist 的变化更新同一张卡；不显示耗时、完成百分比、ETA、内部
  reasoning、raw command/tool output 或 tool arguments；计划步骤中的常见 secret/token、
  邮箱、用户目录和内联代码/参数会被过滤。终态会在同一卡片折叠过程并显示最终回答和
  可用的本轮文件，文件翻页后仍保留折叠过程。
- 两项可以只开一个或同时开启。reaction/card 展示失败不会改变 Codex Turn；Progress
  Card 初始、过程或终态更新失败时，最终结果会回退到原有回复方式。
- Progress Card 关闭时严格保持原有终态：无文件的普通 Turn 使用富文本/静态文本回复，
  有文件时最终文本和“本轮文件”合成现有完成卡。首期这两个会话选项不改变 Goal、Side
  或压缩自己的展示方式。

## 发送消息、引用与图片

### 普通文本和斜杠

- 直接发送普通文本即可开始 Turn 或 steer。
- 群内不同参与者依次发消息时，Codex 会分别看到每条当前消息的发送者；任务的运行反馈和
  最终回复仍锚定原任务消息，不会因为后来有人 steer 就迁移。
- 若收到“无法获取当前消息发送者姓名”，管理员需要在飞书开发者后台开通
  `im:chat.members:read` 并发布新应用版本；Netizen 不会用“未知发送者”降级提交。
- 每条消息只表示一个 control 或一个 prompt；不能在一条消息里串联多个 slash control。
- 未知的 `/command` 会明确拒绝，不会作为普通 prompt 交给模型。
- 若要把以 `/` 开头的文字作为普通 prompt，使用 `//`。例如 `//plan this work` 会向 Codex 发送字面 `/plan this work`。

### 群聊的 @ 时读取的消息范围

群聊主线和普通群话题的每个会话有两种消息范围，可在 `/new` 创建时选择，也可在会话
空闲时通过 `/config` 切换：

- “仅这条 @ 消息”（默认）：只把当前消息和用户显式选择的一条飞书引用交给 Codex。
- “自动带上期间的群聊讨论”：仍然只有 `@机器人` 才触发，但触发时会有界读取同一群聊
  主线或同一话题中，从该会话上一次已接受请求之后到当前消息之前的非机器人成员消息，
  作为 Supplemental Context 一并提交。

补充消息只是背景：其中即使写了 `/stop`、`/new` 或 `$skill-name` 也不会执行 control 或
激活 Skill。当前 @ 消息始终是唯一请求和回复锚点。P2P、P2P 话题与 Side 固定使用“仅这条
@ 消息”。切换会话、恢复会话或刚启用补充模式时，边界会重置到这次操作，因而不会补录
该会话非 current 期间的讨论。

每次实际带入至少一条补充消息时，Netizen 会在提交前公开回复带入条数；若扫描、条数、
文本或不支持类型造成省略，也会在同一回执说明。最多保留最近 50 条补充消息和 64,000
字符可见文本；图片与当前消息、引用消息共同使用每条 prompt 最多 20 张、单图 20 MB、
总计 50 MB 的限制。被选中的消息或图片无法安全读取时，整条当前请求不会执行，修复后需
重新 @ 发送。

### 飞书逐条引用

- 在单聊或群聊主线中使用飞书“回复”后提问，Netizen 会读取被回复的那一条消息，作为单层引用上下文。
- 当前提问者与被引用消息发送者会分别标注，不会混成同一个人。
- 可见文本可来自文本、富文本、卡片、日程、任务、投票和有界的合并转发等类型。
- 引用不会递归追踪更早的回复链。话题中的回复首先属于该话题 Scope，不会再解释成逐条引用。
- 被引用消息被撤回、无权限、超时、类型不支持或准备期间当前会话发生变化时，本条消息不会 start/steer；修复问题后应由用户重新发送。

### 图片与其他附件

- 当前消息、被引用消息和已选中的补充上下文消息中的普通图片、富文本图片可作为 Codex
  原生视觉输入。
- 每条 prompt 最多 20 张图片；单图最多 20 MB；原始字节合计最多 50 MB。支持 PNG、JPEG、GIF 和 WebP。
- 图片按整条消息准入：任一图片不可读、格式不支持或超过限制，整条消息都不会 start/steer，不会只提交可用部分。
- 卡片图片、合并转发中的图片、普通文件、音频和视频不会作为二进制输入；只能保留公开可见的资源信息。

### 查看和发送本轮文件

- 普通 Turn 成功完成后，Codex 的结构化文件修改或图片生成记录若指向当前仍可访问的
  普通文件，最终回复下方会显示“本轮文件”。Progress Card 关闭时，它和最终回复组成
  现有完成卡；Progress Card 开启时，它进入最初的同一张运行卡并随终态折叠。
  Project 不是额外的文件权限边界，只作为相对路径的解析基准，不会过滤 exact Turn
  明确报告的其他目录文件。
- 每页显示 8 个文件、总数和页码；列表不会用 Markdown 表格撑长，也不会静默截断。
  单张卡片最多完整承载 500 个；通过“下一页/回到第一页”循环覆盖全部页面。超过文件数或
  卡片编码容量时会明确提示，不发送残缺清单。
- 图片点击“发送原图到话题”，其他文件点击“发送文件到话题”。文件会作为真实飞书
  图片或文件消息回复该卡片；平面卡片由此形成话题，原本就在话题中的卡片仍留在原话题。
- 卡片的本轮文件区显示脱敏逻辑位置和大小：Project 内是相对路径，原生生成图显示为
  `生成图片/<文件名>`，账号 home 内的其他文件显示 `~/...`，不会显示云主机绝对路径。
  不会自动上传全部文件，也没有预览、diff 或“一键发送全部”。
- 本轮文件不是快照。点击时会重新读取卡片记录的路径并发送当前内容；文件若已删除、变成
  目录或卡片已失效，会明确失败且原卡片保持不变。同一路径后来被替换或重绑时，发送的是
  点击时当前普通文件。
- 新卡片把分页清单保存在飞书 callback payload 中，因此 Netizen 服务或 App Server 正常
  重启后，已发送卡片仍可翻页和发送；不会为此保存本地 card session。
- 文件来源优先采用原生 Turn 的 latest aggregate diff，并用 completed `fileChange` 和
  `imageGeneration` 补充。shell、MCP 或第三方工具的输出若没有进入这些 native 事实，
  不会被扫描补齐；最终答复里写出路径也不会自动把它变成卡片文件。

## 会话与 Project 管理

### 新建与配置 Project

- `/new`：打开唯一的新建卡片。Project 下拉框展示全部 enabled Projects，不由 Netizen
  截断或分页；Model 可继承 Codex，也可显式选择 Model、Effort 和 Speed。Task Reaction
  与 Progress Card 可独立选择且默认关闭；群聊和群话题还可选择 @ 时读取的消息范围。
  提交后创建并切换 Lazy 会话。
- 任何带参数的 `/new ...` 快捷创建都已下线；请在卡片中选择，包括默认目录 `none`。
- `/settings`：打开实例设置卡片。当前可在 Projects 分区启用、停用、创建或登记 Project。
- 停用 Project 只阻止用它创建新会话，已有会话仍可继续；Netizen 不会删除 Project 目录。

实例还默认提供单管理员 Admin Web，用于跨飞书 Scope 集中筛选和管理 Projects、普通
Sessions 与 Side Topics。它与飞书共用同一个 Registry/Runtime，但权限来自独立 Admin
credential，不会因为某人是 Channel 参与者或 Binding 创建者而自动开放。实例管理员可从
部署主机的 `~/.netizen/credentials/admin-web-secret` 获取登录凭据，并在受信内网访问
`http://<服务器 IP>:8787`；普通使用者继续使用 `/settings` 和当前 Scope 的会话命令。

Admin Web 可以管理 inactive/cross-Scope exact Binding，包括创建 Lazy、设为当前、修改
Turn Settings、重命名、归档、恢复或恢复并设为当前、删除 Lazy、Stop 和 Release，也可
结束当前进程仍可控的 Side。它不能发送 Prompt、查看完整历史、启动/推进 Goal、Compact、
删除已有原生历史的 Thread 或执行批量 native mutation。Admin 可查看 @ 时读取的消息
范围；但不能在没有 exact 飞书消息边界时新启用 catch-up，也不能把 catch-up 会话从后台
直接设为 current。页面操作结果未知时应刷新对账，
不要重放；服务重启或 credential 轮换会注销原 session。

### 查找、切换和命名会话

- `/sessions`：用分页卡片列出当前 Scope 的普通会话。当前会话置顶，其他会话可点击
  “设为当前”；这只切换后续普通消息默认进入的会话，不会停止其他会话正在运行的任务。
  已有原生历史且空闲的行还可确认后直接“归档”。归档非当前行不会改变当前会话；归档
  当前行会清空当前会话指针。Lazy、运行中、Goal、压缩中或状态未知的行不提供归档按钮。
  空闲的 Lazy 行和支持原生删除的空闲历史行还可点击“删除”，先打开独立红色确认卡，再
  永久删除目标。删除非当前行不会改变当前会话；删除当前行会清空当前会话指针。运行中、
  Goal、压缩中或状态未知的行不提供删除按钮。
  `/threads` 是兼容别名。
- `/sessions archived`：列出当前 Scope 的已归档会话。
- `/resume <短 ID>`：切换到普通会话。已归档会话要先恢复。
- `/rename [名称]`：重命名当前原生 Thread；省略名称时打开卡片。名称也会显示在 Codex App/CLI。
- `/release`：显式取消当前 active 普通会话在本 Netizen 连接上的订阅。它保留 Binding、
  原生 Thread、历史、配置和 Task Feedback，下一条普通消息仍 resume 同一 native ID。
  运行中、状态未知或有已登记后台 terminal 时会拒绝；Side 使用 `/side close`。

短 ID 只用于当前 Scope 中的会话选择。不能拿另一个聊天或话题里的短 ID 跨 Scope 切换。

### 归档、恢复和删除

- `/archive`：确认后归档当前空闲的原生 Thread，保留会话配置与 Task Feedback，并清空
  当前会话指针；不会自动切换到另一会话。
- `/unarchive <短 ID>`：恢复已归档会话并切换到它。
- `/delete`：Lazy 会话二次确认后只永久删除本地 Binding；已有原生历史的 idle 会话会显示
  更强的红色确认，确认后永久删除原生 Thread、App Server 管理的 spawned descendants、
  Codex App/CLI 历史与本地 Binding，无法恢复。
- 删除失败不会自动再发一次 delete。系统会只读对账原生目录：明确仍存在时保留会话供用户
  重新确认，明确已不存在时收尾删除 Binding，无法判定时暂停新任务并要求重启后再对账。

`/rename`、`/archive` 命令和 `/delete` 只作用于当前会话，不支持在命令后附目标 ID。要
重命名另一个普通会话，应先 `/resume`；`/sessions` 行内的“归档”和经独立红色确认卡的
“删除”是明确例外，不需要先切换，也不改变 `/archive`、`/delete` 命令的语义。

## Model、Effort、Speed、Task Feedback 与 @ 时读取的消息范围

- `/new` 卡片可以为新会话选择继承 Codex 或显式 Model、Effort 和 Speed；群聊和群话题
  还可选择 @ 时读取的消息范围；Task Reaction 与 Progress Card 默认关闭、可独立开启。
- `/config` 原子修改当前会话后续新 Turn 的三项设置、两个 Task Feedback，以及群聊 @ 时
  读取的消息范围。它不创建 Turn，也不能直接配置另一个会话；应先 `/resume`。
- 当前 Turn 运行、停止中或正在压缩时不能修改配置。运行时的普通消息仍只会 steer 当前 Turn。
- 已经开始的 Turn 沿用开始时捕获的 Task Feedback；之后修改只影响后续新 Turn。
- 选项来自 Codex 实时模型目录，并在卡片提交及每次新 Turn 前重新校验；手册不维护静态模型名单。
- 没有 Netizen 会话配置时，三项显示“继承 Codex”。
- 不提供 `/model`、`/effort` 或 `/fast` 独立命令。Fast 是同一模型的 Service Tier；Codex Spark 是独立 Model。

## 状态、压缩与停止

### `/status`

`/status` 是只读快照，通常包含：

- 当前会话短 ID、名称和首条消息预览；
- Project、完整原生 Thread ID 和运行状态；
- 当前 Turn 的原生 checklist 与已接受 steer 次数；
- 最近可观测完成 Turn 的上下文窗口用量；窗口大小可用时同时显示上限和已用百分比；
- Model、Effort、Speed 以及它们来自 Netizen 会话配置还是继承 Codex；群聊会话还显示
  @ 时读取的消息范围。
- 本 Netizen 进程当前观察到的 Thread 订阅状态及自动释放倒计时。

Checklist 可能尚未生成或因兼容门禁暂不可用；上下文用量也可能要等到可观测 Turn 完成后才更新。`/status` 不展示内部推理、完整工具日志或 ETA。

订阅状态是当前进程的瞬态投影，不是 Thread 或 writer 的生命周期证明。取消最后一个订阅
后，App Server 还要求连续三十分钟没有订阅和活动才会卸载 Thread；`/release` 因此不会
声称 writer 已立即释放。

### `/compact`

- 当前固定 `openai-codex 0.147.0` 中 `/compact` 暂不可用，也不会出现在 `/help`。
- 输入 `/compact` 会收到兼容验证未通过的明确说明；Netizen 不会调用原生压缩或改变会话状态。
- 底层兼容探针会继续验证压缩终态和同一 Thread 的后续 Turn；完整序列通过后才会重新开放。

### `/stop`

- `/stop` 中断当前 Scope 的 active Turn；没有运行任务时不会停止其他会话。
- 若当前运行的是 Goal，会先持久化暂停 Goal，再中断其当前精确物理 Turn。
- Netizen 随后请求清理 App Server 已登记的后台 terminal。
- 重要限制：当前接口不保证前台工具进程退出。飞书显示“已中断”表示原生 Turn 终止，不是所有前台子进程都已退出的证明。

## Side 临时话题

Side 适合在不打断 Parent 会话的情况下讨论一个临时分支。

- `/side [首轮问题]` 要求当前会话已经有原生历史。它从当前 Parent Thread 创建临时 fork，并在同一聊天中新建一个 sibling 话题。
- 省略问题时只创建 Side；携带问题时，首轮问题和后续回复只出现在新话题。
- 携带问题时，Codex 看到的首轮来源仍是原 `/side` 消息及其发送者；新话题中的问题副本
  只承载 reaction 和最终回复。后续每条 Side Prompt 使用其实际发送者。
- Parent 和多个 Side 可以并发，但共享同一个真实 Project 目录。
- Side 在同一个临时 fork 上支持多轮：空闲时开始新 Turn，运行中继续 steer。
- Side 内只支持普通 prompt、`//`、`/status`、`/stop`、`/help`、`/` 和 `/side close`。
- `/stop` 只中断当前 Side Turn，Side 仍可继续；`/side close` 才真正结束 Side 并取消订阅。
- Side 空闲两小时或 Netizen 服务重启后过期。旧 Side 话题不会自动变成普通会话。

若 Side 显示创建或关闭结果不确定，应在原 Side 话题按提示重试 `/side close`，不要把它当普通话题使用。

## Goal

Goal 适合让 Codex 围绕一个持续目标自动推进多个物理 Turn。

- `/goal`：查看当前会话的 Goal。
- `/goal <objective>`：启动一个 Goal。
- `/goal pause`、`/goal resume`、`/goal clear`：暂停、恢复或清除 Goal。
- Goal 运行期间，同一会话不接受普通 prompt、`/compact` 或 `/config`。
- `/stop` 会先暂停 Goal，再中断当前物理 Turn；它不会把 Goal 当普通一次性任务清除。
- 当前不支持在 Goal objective 中调用 `$skill`；请先在普通消息中使用 Skill。

Goal 和 Side 都依赖运行时原生能力门禁。如果当前 `/help` 没有显示相应命令，以当前实例能力为准。

## Codex Skills

- 可以直接用自然语言询问 Netizen 的使用方式；本 `netizen-user-guide` Skill 设计为在相关问题上自动匹配。
- 要显式调用其他 Skill，可在普通消息开头写 `$skill-name ...`；同一消息开头可以连续引用多个 Skill。
- Skill 会在 start/steer 前重新发现和校验。名称不存在、已禁用或路径失效时，本条消息不会执行。
- 飞书不提供 `/skills` 浏览命令。想知道当前有哪些 Skill，可以直接用自然语言询问 Codex。
- `$skill` 是普通 prompt 的一部分，不能和飞书 slash control 串成一个消息，也不能放进 Goal objective。

## 与 Codex App/CLI 的差异

Netizen 复用原生 Codex Thread、历史、配置和工具，但飞书不是 CLI/App 宿主界面。

- 可在 CLI/App 中 resume 飞书创建的原生 Thread；CLI/App 新增的消息不会自动回填飞书。
- Codex App 可直接打开本机工作区文件；飞书对支持的本轮文件改为在终态卡片中按需发送
  到话题。飞书不会自动上传整个工作区或保存 Turn 完成时文件版本。
- `/copy`、`/vim`、`/theme`、`/exit` 和 `/quit` 属于宿主界面或生命周期命令，在飞书中不可用。
- `/plan` 和 `/apps` 当前没有安全、公开的高层 SDK 控制面，在飞书中不可用。
- `$app` 当前不会被包装成原生结构化 attachment。
- `/model`、`/effort`、`/fast` 被统一为 `/new` 和 `/config` 卡片。
- 已物化 Thread 只能在 idle、已持久化且 Delete compatibility gate 可用时永久删除；
  不想丢失历史时使用 `/archive`。
- `/release` 只释放飞书服务当前连接的订阅，不删除 Thread；CLI/App 或其他 App Server
  的订阅彼此独立。之后飞书仍可按原 native ID 继续。
- Codex 认证、MCP、Skills、AGENTS、`config.toml`、sandbox 和其他原生配置来自服务用户的标准 Codex 状态，不由每个飞书 Scope 另建一套配置系统。
- Netizen 服务每次启动都会重新读取服务账号的 interactive login shell 导出环境；写入持久 profile 的 NVM PATH、代理和工具路径在重启服务后生效，不维护飞书专用环境文件。
- 后台服务没有真实 TTY，也不会继承某个已打开终端中的临时 `export`、alias 或未导出的 shell function；这部分无法与宿主终端逐项镜像。
- 新 Thread 的 approval mode 使用当前 Python SDK 的公开默认 `auto_review`；飞书不提供 Codex App 的 Ask/Custom 宿主选择器，也不能完整继承它们。

## 常见问题

### “我又发了一条消息，为什么没有新开任务？”

如果普通 Turn 仍在运行，新消息会 steer 它；若恰好撞上完成，本条消息零执行并提示重发。
Goal、停止或压缩状态会直接拒绝普通消息。等待状态回到空闲、先 `/stop`，或 `/new`、
`/resume` 到另一个会话，才能开始独立 Turn。

### “群里机器人为什么没响应？”

群主线和群话题的每条触发消息都必须重新 `@机器人`；未 @ 的消息不会触发响应。若当前
会话选择“自动带上期间的群聊讨论”，这些消息可能在下一次 @ 时作为背景进入 Codex，但
仍不会自己触发 Turn。还应确认机器人仍在群内，且飞书应用的可用范围和消息权限已发布。

### “为什么不能修改配置？”

当前会话可能正在运行、停止或压缩。等待其回到空闲后再用 `/config`；要配置别的会话，先 `/resume`。

### “为什么任务运行时没有表情或进度卡？”

Task Reaction 和 Progress Card 默认都关闭。请在新建会话的 `/new` 卡片中开启，或等当前
会话空闲后通过 `/config` 修改；两项互不依赖。Progress Card 关闭并不影响最终回复：没有
文件时仍回复富文本/静态文本，有文件时仍使用现有完成卡。

### “为什么 `/delete` 删除不了？”

已有原生历史的会话只有在空闲、已持久化且当前实例的 Delete compatibility gate 可用时
才能删除；运行中、Goal、压缩中、ephemeral、未持久化或原生状态不可读都会拒绝。失败
提示若说原生目录仍存在，可重新发送 `/delete` 并再次确认；若说状态 unknown，应先让
部署者正常重启服务再对账，不要连续点击。只想隐藏并保留历史时使用 `/archive`。

### “`/stop` 已完成，为什么进程还在？”

`/stop` 能确认原生 Turn 已中断，并请求清理已登记的后台 terminal，但不能证明前台工具进程已经退出。这是当前原生接口限制。

### “CLI 能找到一个工具，为什么飞书里提示找不到？”

先确认工具路径或变量已经写入服务账号的持久 shell profile，而不是只在当前终端临时
`export`。Netizen 会在每次服务启动时重新读取 interactive login shell 的导出环境；修改
profile 后需要由部署者执行 `service.sh restart`。alias、未导出的 shell function 和依赖
真实 TTY 的初始化不属于后台服务可继承的环境。Bash 用户若只在 `.bashrc` 配置 NVM，需
确认 `.bash_profile` 或 `.profile` 会 source 它。

### “Side 为什么不能继续了？”

Side 可能因空闲两小时、服务重启或关闭流程进入终态而过期。旧 Side 话题不会恢复为普通会话；回到 Parent 后重新 `/side`。

### “引用或图片失败后会不会只发送文字部分？”

不会。引用或图片准备采用整条消息准入；任一必需资源失败时零 start/steer。修复权限、缩小图片或重新选择内容后，由用户重发。

### “为什么任务生成了文件，却没有出现‘本轮文件’？”

当前版本只读取普通成功 Turn 的原生 latest aggregate diff，以及 completed
`fileChange` / `imageGeneration` 记录；不解析最终回复中的路径，也不扫描 Project。
shell、MCP 或第三方工具生成但未进入这些 native 事实的文件，
以及 Goal、Side、压缩的输出，都不会进入卡片。文件必须仍存在且是普通文件；Project
不是额外的文件权限边界，exact Turn 明确报告的其他目录文件仍可出现。

### “点击本轮文件后，拿到的是任务完成时的版本吗？”

不保证。本轮文件不保存快照、摘要或修改检测；点击时发送卡片所记路径当前仍可访问的
内容。若其他会话已经修改、替换或重绑文件，发送的是当前版本；若文件已消失或变成
非普通文件，则拒绝发送并保留原卡片。
