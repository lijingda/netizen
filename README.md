# Netizen

Netizen 把飞书单聊、群聊和话题接成 Codex 的一个消息 Channel。它不是另一套
Agent Runtime：飞书侧只负责消息和会话绑定，Agent 过程由官方 Python
`openai-codex` SDK 管理的原生 App Server/CLI 完成。

仓库唯一的生产实现是 `netizen/` 下的 Python 包，以 `pyproject.toml` 构建和安装；
不存在 Node.js/TypeScript 运行时、构建面或 fallback。

## Pilot 形态

- 一个常驻 Python 服务、一个 `FeishuChannel`、一个共享 `AsyncCodex`。
- 一个普通飞书单聊、群聊主线或话题是一个 Binding Scope；每个 Scope 可有多个会话
  Binding 和一个 active pointer。Side 话题由持久 route 保留，不解释为普通 Scope。
- exact `/new` 打开唯一的新建卡片；任何带参数的 `/new ...` 都明确拒绝且零 mutation。
  卡片在一个下拉框中展示全部 enabled Projects，不做应用级截断或分页，并可选择继承
  Codex 或保存 Binding-scoped Model/Effort/Speed。它只创建 lazy Binding，首条真实消息
  才创建 native Thread。
- 同一 Binding 空闲时启动下一 Turn，运行中消息固定调用 native `steer()`；不
  queue、不拼 prompt。`/stop` 先 native `interrupt()`，再请求清理 App Server 为该
  Thread 登记的后台 terminal；当前接口不保证前台工具进程退出。
- 普通持久 Thread 精确回到 idle 后，当前 active Binding 保留十五分钟 warm window；
  切换到其他 Binding 后立即尝试取消旧 idle Thread 的当前连接订阅。Binding、native ID
  和历史都保留，下一条消息仍 resume exact ID。服务重启不扫描或恢复旧 timer，也没有
  Thread 数量上限或 LRU。
- `/side [首轮问题]` 从当前已物化 Parent Thread 创建一个 ephemeral fork，并在同一
  chat 新开 sibling 话题。Side 在同一 fork 上支持多轮：idle 开新 Turn、running
  steer exact Turn；`/stop` 只停当前 Side Turn，`/side close` 才结束 Side。Parent 与
  多个 Side 可并发，但共享同一个真实 Project cwd，文件改动彼此可见。创建时冻结 Parent
  当时的 Model/Effort/Speed、Reaction Pulse 与 Progress Card，Parent 后续配置不传播；
  Side 内仍不允许 Goal。
- Binding 有两个相互独立、默认关闭的 Task Feedback 选项，可在 `/new` 或 `/config`
  按需开启。普通与 Side Turn 始终尽力显示 Lifecycle Reaction：accepted 时使用
  `Typing`，steer 成功使用 `OnIt`，终态使用 `DONE`/`ERROR`/`CrossMark`。
  Reaction Pulse 只控制是否在执行中低频显示/隐藏 `THINKING`；Side Turn 使用创建时
  冻结的选择，Goal 不使用 Lifecycle Reaction。
- Progress Card 开启后，普通或 Side native Turn 接受时回复一张运行卡；Goal 则无论该
  选项是否开启都只使用一张 Goal 回复卡，开启时在其中增加 Activity 模块。顶部展开区只按
  状态、最近完成的 commentary、安全操作类别、子任务聚合和原生 checklist 的变化逐步更新；
  不显示工具名、参数、输出、路径、耗时、百分比、ETA 或 reasoning，所有可见文本还经过
  有界的常见敏感模式过滤。终态在同一卡片折叠过程并
  呈现回答和可用文件，翻页后仍保留全部模块。任一卡片展示失败都不影响原生执行。
- 普通 Turn 成功完成，且 latest native Turn diff 或 completed `fileChange` /
  `imageGeneration` item 指向当前普通文件时，最终回复与“本轮文件”合成一张卡片；Goal
  首期只使用四项终态证据中 exact 最终成功物理 Turn 的 completed structured items，
  Side 也只使用 exact completed Side Turn 的 structured items；两者都不猜测 aggregate
  diff 或更早 Turn 的文件。
  Project 只解析相对路径，不过滤 exact Turn 明确报告的外部文件；文件每页 8 个，最多
  500 个完整循环分页，点击后以原图或文件消息回复到卡片话题。可见正文只显示脱敏逻辑位置；
  普通完成/进度文件卡继续使用 v4 callback；Goal 与 Files 同卡时使用 v5，并在飞书
  callback payload 中明文自带绝对路径、完整清单和有界回复卡模块。因此服务重启后仍可
  翻页和发送，无需保存快照或 card session；v5 翻页会保留 Goal、Activity 与 Result，
  既有 v4 卡继续兼容。不会自动上传，也不会扫描/解析最终回复来补齐
  未进入 Turn diff/items 的输出。Progress Card 关闭且不是 Goal 时
  严格保持现有行为：没有文件用富文本/静态文本回复，有文件才使用完成卡。
- 服务以执行安装的当前用户身份复用其标准 `$CODEX_HOME`（默认 `~/.codex`），因此
  认证、历史、Skills、MCP、AGENTS 和 `config.toml` 仍由 Codex 原生发现；部署只完整管理
  `skills/netizen-user-guide` 这一个随 release 发布的用户咨询 Skill，其他用户 Skill
  不受影响。
- Channel SQLite 只保存 Scope/Binding/Project Registry/去重 TTL、可选的
  Binding-scoped Model/Effort/Speed 选择 ID、两个 Task Feedback 布尔值及 revision、群聊
  Binding 的 Mention Context Mode 与 exact Context Boundary metadata，以及不含 native
  ID/content 的 Side Topic 路由墓碑；不保存 prompt、补充消息正文、回复、cwd 副本、
  Turn Activity Projection、回复卡 identity/session、解析后的 wire value、Codex 已生效
  配置或 Turn 历史。
- 同一进程默认在 `0.0.0.0:8787` 提供单管理员 Admin Web。它与飞书共用唯一的
  application service、Scope coordinator、SQLite、Runtime 和 `AsyncCodex`，集中分页管理
  Projects、普通 Sessions 和 Side Topics；它不能发送 Prompt、浏览完整历史或调用任意
  Codex RPC。`/settings` 仍保留给当前飞书 Scope 的普通使用者。
- 群聊和群话题中，每条触发机器人的输入仍必须重新 `@机器人`；P2P 及 P2P Side
  话题无需 @。普通群聊 Binding 可选择默认的 `current-only`（只提交当前 @ 消息和显式
  引用），或 `catch-up`（下一次 @ 时有界读取同一 Scope 中上次已接受请求之后的成员消息）。
  未 @ 消息只作为 inert supplemental context，不会自动触发 Turn、steer、control 或 Skill；
  P2P 与 Side 固定为 current-only。
- 每条真实普通或 Side Prompt 都会把当前飞书消息的公开发送者信息作为归属元数据交给
  Codex，并随原生输入进入 Thread 历史；它只说明“谁发送了这条请求”，不授予权限、
  owner 或更高指令优先级。Channel SDK 会通过当前 chat 的成员名单补全真实显示名；
  若仍无法解析，不会降级成“未知发送者”，而是零 start/steer 并提示开通
  `im:chat.members:read`。归属 ID 只保留当前应用内的 `open_id`，不把跨应用
  `union_id` 或租户级 `user_id` 写入 Codex 历史。无引用时原始请求仍位于输入最前，
  以保留原生首消息 preview 的可读性。
- 在单聊或群主线中对一条消息使用飞书“回复”后提问，Netizen 会在提交
  Codex 前读取那一条精确消息作为单层上下文。话题回复仍只是话题 Scope，
  不作为逐条引用。

## 命令

- 普通文本：空闲时开始 Turn，运行中时 steer 当前 Turn。若使用飞书逐条引用，
  可读取文本、富文本、卡片、日程/任务/投票等结构化可见文本以及有界的合并
  转发；当前消息与被引用消息各自保留发送者归属。当前消息、被引用消息和
  `catch-up` 选中的补充上下文消息中的普通图片、富文本图片会作为 Codex
  原生视觉输入；历史消息只携带精简的发送者、时间、正文、必要回复/mention/附件关联，
  exact 消息 ID 和原始资源 key 留在 Channel 内部，图片通过本次 prompt 的本地引用关联；
  最多 20 张、单图 20 MB、合计 50 MB，任一图片不可读时整条不执行。卡片图片、
  合并转发图片、文件和音视频仍只保留公开资源元数据，不读取二进制内容。
  普通 Turn 完成后若出现“本轮文件”卡片，可翻页并按需将单个文件发送到该卡片话题；
  发送的是点击时当前仍可访问的内容，不是 Turn 完成时快照。
- `//...`：发送字面 `/...` prompt。
- `/new`：发送唯一的新建表单，在单个下拉框中展示全部 enabled Projects，并选择继承
  Codex 或显式 Model/Effort/Speed，以及是否开启 Reaction Pulse、Progress Card（两项默认
  关闭）；群聊和群话题还可选择 @ 上下文模式。只创建并切换 lazy 会话，不要求任务文本。
  `/new` 不接受任何参数。
- `/side [首轮问题]`：要求当前 active Binding 已有原生历史；在同一 chat 新建一个
  sibling Side 话题。省略问题时只创建，带问题时新话题先显示明确标注的首轮问题，
  随后的模型回复和 reactions 也只出现在新话题；Parent 成功时不再发送导航回复。
  首轮模型来源仍是原 `/side` 消息及其发送者，新话题 seed 只作为完成投递锚点。
  Side 内仅支持普通 Prompt、`//`、`/status`、`/stop`、`/help`、`/` 和
  `/side close`；空闲两小时或服务重启后过期。旧 Side 话题不会变成普通 Binding。Side
  创建时冻结 Parent 当时的 Model/Effort/Speed、Reaction Pulse 与 Progress Card；后续
  `/config` 不影响它，Side 内也不接受 Goal。
- `/config`：选择并保存当前会话后续新 Turn 的 Model、Effort、Speed、Reaction Pulse 与
  Progress Card；群聊和群话题还可切换 @ 上下文模式。不要求任务、不创建空白 Turn，也
  不提供跨会话选择；配置其他会话应先 `/resume`。Binding 已显式配置模型三项时，每条
  需要启动新 Turn 的普通消息都会重新读取 live 模型目录并显式应用；未配置时由 Codex
  继承且不传这三项。running Turn 明确拒绝修改，已开始 Turn 沿用启动时捕获的反馈选项，
  普通消息仍只 steer 当前 exact Turn。
- `/compact`：当前固定 `openai-codex 0.147.0` 中明确不可用，也不进入 `/help`；输入该
  命令会说明兼容验证未通过且不调用原生压缩。底层实现和 live probe 保留，只有同一
  Thread 在压缩后继续 Turn 的完整序列重新通过，命令才会开放。
- `$skill-name ...`：在普通消息开头可连续显式调用多个 Codex Skills；提交前会重新
  discovery/revalidate，并把原文本与多个公开 `SkillInput` 一起交给同一个 Turn 或
  exact running Turn 的 steer。查询当前有哪些 Skill 可用时直接发送普通自然语言消息，
  不提供独立的 `/skills` 浏览命令。
- `/goal [objective|pause|resume|clear]`：查看、启动、暂停、恢复或结束当前会话的原生
  Goal。一个 Goal 可自动产生多个物理 Turn；start、pause、resume 和终态持续更新同一张
  组合卡，卡片按钮进入同一 typed control 路由。只有四项终态证据完整、Goal 为
  `complete` 且 exact 最终物理 Turn 为 `completed` 时自动 clear；paused、blocked、额度
  限制和任何收尾不确定状态都保留 Goal，仍可显式结束。执行期间同一 Binding 不接受普通
  Prompt、`/compact` 或 `/config`。服务重启后的旧 Goal 按钮会过期，重新发送 `/goal`
  可建立当前进程可校验的新控制卡。
- `/settings`：在当前单聊、群聊或话题打开 Netizen 设置卡片。当前 Projects 分区可
  通过下拉表单启停 Project，并在同一卡片内创建或登记 Project。
- `/sessions`：用分页卡片列出当前 Scope 的会话。当前会话置顶显示，其他会话可直接点击
  “设为当前”；切换只改变 Scope 的 active Binding，不会停止其他会话正在运行的任务。
  所有 persisted materialized 行无论是 idle、running、stopping、Turn 观测不可用、Goal
  或 Compaction，都可“归档”或打开红色确认卡后永久“删除”。它们直接委托
  App Server 处理当前原生活动，不要求先恢复观测或停止 Turn。普通 Turn 行还可 exact
  “停止”，`turn-observation-unavailable` 行可立即“重新检查”。idle Lazy 行仍只删除
  本地 Binding。归档/删除非当前行不改变 active Binding，操作当前行才清空 pointer；
  确认期间 exact Binding 或 native Thread identity 改变时零 mutation 地拒绝。
  每个会话优先显示原生 `Thread.name`，没有名称时使用首条用户消息 `preview`，同时保留
  `/resume` 所需的短 ID（`/threads` 是兼容别名）；已归档会话不混入普通列表。
- `/sessions archived`：从 Codex 原生 archived catalog 读取当前 Scope 的归档会话，并
  提供“恢复并切换”和独立红色二阶段“删除”；删除直接使用与 active Thread 相同的
  App Server primitive，不先恢复，也不改变当前 active Binding。
- `/resume <短 ID>`：切换普通会话；已归档会话必须先恢复。
- `/rename [名称]`：重命名当前原生 Thread；省略名称时打开卡片，名称会同步显示在
  Codex App/CLI。
- `/archive`：确认后把当前 persisted 原生 Thread 直接交给 App Server 归档；当前有
  Turn、Goal、Compaction 或观测故障都不剥夺该控制。归档保留 Binding、会话 Turn 配置与
  Task Feedback，并清空当前 active pointer；不会自动切换到另一会话。
- `/unarchive <短 ID>`：恢复指定的已归档会话并切换到它。
- `/delete`：Lazy 会话显示红色二次确认并只删除本地 Binding。已有原生历史的
  persisted 会话无论当前是 running、stopping、Turn 观测不可用、Goal 或
  compaction，都可由用户确认后直接委托 App Server 删除。App Server 负责有界
  shutdown 并级联删除 spawned descendants；成功后再删除本地 Binding。非取消响应异常时
  只做一次 active/archived 四视图对账，不自动重发 mutation；调用取消或对账仍不确定只
  隔离目标 Binding。
- `/status`：逐行显示当前会话短 ID、原生名称与首条消息预览、Project、完整 native
  Thread ID、状态、当前 active Turn 的原生 checklist 与已接受 steer 次数、最近完成且
  可观测 Turn 的上下文窗口用量，以及 Model、Effort、Speed。checklist 使用
  `✓ completed`、`→ inProgress`、`○ pending`；成功 steer 后旧计划会标记为可能尚未反映
  最近调整，下一次原生完整 plan 更新会整体替换并清除标记。未生成或 SDK 观察门禁不可用
  时明确提示，不展示推理、工具日志或 ETA；窗口大小可用时会显示已用量、上限和百分比。
  普通后续 Turn 运行时保留并标注
  “上一轮完成时”的用量，完成后覆盖为本轮值；
  用量不可用时会明确提示并在下一次可观测 Turn 完成后更新。快照不写入 Channel 数据库。
  存在 Netizen 会话配置时显示其动态解析值；没有时三项明确显示“继承 Codex”。
  这表示客户端配置来源，不冒充公开 SDK 无法反查的原生有效值；还会显示本 Netizen
  进程的瞬态 Thread 订阅状态，但“已取消订阅”不代表 writer 已立即释放。
- `/release`：显式取消当前 active 普通会话在本 Netizen 连接上的 Thread 订阅；不删除
  Binding、native Thread、历史、会话配置或 Task Feedback，下一条消息仍 resume 同一
  native ID。运行中、
  状态未知或存在已登记后台 terminal 时拒绝；Side 应使用 `/side close`。最后一个订阅者
  离开后，App Server 仍有连续三十分钟无订阅和活动的卸载宽限期。
- `/stop`：中断当前 Scope 的 active Turn；若运行的是 Goal，则先持久化暂停 Goal、
  再中断其精确物理 Turn。两条路径都会请求清理已登记的后台 terminal；前台工具进程
  可能继续运行，飞书终态会明确提示这一点。
- Ordinary Turn 的 `completed`、`interrupted`、`failed` 都只终止本轮并释放运行槽；
  `failed` 后仍可在同一 Thread 继续对话。一次观测故障最多做 5 秒/三次原生 I/O 的
  短恢复，其中最多一次 resume；恢复 exact `inProgress` 就继续正常无时限轮询，确认终态就
  正常交付。仍不可验证时转为 `turn-observation-unavailable`，保留 exact 槽但停止自动 I/O；
  同一 Turn 的 Reaction 脉冲和 Progress Card 轮询也停止，后续终态仍会用普通回复交付。
  用户可手动重新检查、停止、归档或删除，其他 Binding 不受影响。已确认 terminal 后的
  final response materialization 不再进入观测恢复。
- `/help`：显示帮助。

Model、Effort 与 Speed 选项每次都从 `codex.models()` 动态读取，并在卡片提交时再次
校验；每条新 Turn 启动前还会再次校验，不在 Netizen 中维护模型名单。Fast 是模型目录中的 Service Tier，独立的 Codex
Spark 仍是独立 Model。若目录响应要求分页，而固定高层 facade 无法传 cursor，则整体
拒绝而不静默漏模型。三项配置不再注册 `/model`、`/effort`、`/fast` 快捷命令。

slash command 由同一注册表区分 Channel control 与 native capability。固定
`openai-codex==0.147.0` 的高层 facade 尚无完整 Goal、Skills discovery、Side boundary
inject、Thread unsubscribe 与 Thread Delete。ADR 0014 用同一 SDK client 上的窄 Adapter
暂时补齐 Goal/Skills；ADR 0021 只为 Side 暴露固定 boundary inject，ADR 0028 将普通
Binding 与 Side 共用的 exact-thread unsubscribe 拆成独立窄 Adapter；ADR 0037 只为
Thread Delete 暴露固定 `thread/delete`，由 Runtime 承担一次四视图失败对账。
rename/archive/unarchive 始终使用公开 SDK。SDK 高层支持任一缺口后，升级 harness 会
要求切回公开 provider 并删除对应 shim。
ADR 0020/0052 另行批准一个精确版本/源码指纹门禁的只读 Activity observer：它只在
`/status`、steer freshness bookkeeping，或普通/Side 已开启 Progress Card 的有界刷新中
快照 current exact Turn 已登记的通知队列，不消费、注册或修改通知，不新建 worker/RPC/
App Server；Goal Activity 只在现有 logical stream 的唯一消费链内安全 tap。关闭 Progress
Card 时没有后台 Activity observation。投影只包含 checklist、脱敏 completed commentary
和命令/工具/文件/搜索/图片/子任务/review/compaction 的通用状态，不含 reasoning、原始
参数/输出、路径、耗时、百分比或 ETA。公开接口可替代后必须删除，且不得扩展为任意通知
浏览器。
按 ADR 0018，Skills discovery 只服务于普通消息的显式 `$skill-name` 校验，不再注册
`/skills` 浏览 control。
`/plan`、`/apps` 仍显式拒绝且不进入帮助；
`$app` 也不被包装成结构化 attachment。`/copy`、`/vim`、`/theme`、`/exit` 等宿主界面
命令同样不展示。一个飞书消息仍只表示一个 control 或一个 prompt，不解释任意多
slash control 串联；`$skill` 使用 Codex 原生语义，不占用飞书的 `@成员`。

飞书应用后台负责控制可用用户和群。Netizen 不维护 user/chat/role allowlist；只要消息
被飞书投递，参与者就能管理该 Scope 的 Binding 或当前 Side。群主线和群话题仍要求
每条消息重新 `@机器人`；P2P 话题按 underlying chat type 免 @。

## Admin Web

实例管理员可直接在受信内网访问 `http://<服务器 IP>:8787`；未登录时根路径会跳转到
`/login`。首次安装自动生成独立的 `~/.netizen/credentials/admin-web-secret`；它不是 Feishu
App Secret，也不写入 YAML。登录后：

- **Projects** 可查看会话聚合、登记已有目录、在 `projectRoot` 内创建空目录及启停；
- **Sessions** 可按 Project、Scope、chat/topic、当前指针和统一会话状态筛选；状态包括默认
  `Active` 以及 `Lazy`、`Archived`、`Missing`、`全部`。页面以 10/20/50/100 条 keyset
  分页查看当前页的单聊、群聊或话题群名称，并从名称打开对应飞书会话；页面区分消息/话题、
  当前/非当前与独立运行态。可管理创建、切换、配置、
  重命名、归档、两种恢复、Lazy 删除、exact Stop 和 Release；名称仅使用有界进程缓存，
  不写入 Channel 数据库；
- **Side Topics** 可按 Project、chat 和 route 状态筛选，并结束当前进程仍可控的 exact Side。

每个写操作都使用一次性 action/CSRF token，并在共享锁内重读 exact target；页面断线后只
能刷新对账，不会自动重放。服务重启或合法轮换 credential 会立即使旧 session 失效。V1
有意使用受信内网 HTTP，不提供 TLS、OIDC、多管理员、RBAC、批量 native mutation、Prompt
入口或 materialized Thread Delete；若地址暴露到不受信网络，应先增加独立的安全架构。

## 用户指南 Skill

仓库 [netizen-user-guide](skills/netizen-user-guide/SKILL.md) 是随 release 发布的原生
Codex Skill。用户在飞书中自然语言询问 Netizen 用法、命令、会话、与 Codex App/CLI
差异或常见限制时，Codex 可按 Skill 描述隐式选择它；也可显式发送
`$netizen-user-guide <问题>`。它读取同目录的用户手册并按问题回答，补充而不替代
运行时 `/help`。

受管安装器在接受 release 时调用受测的 Skill 安装器，把 release 中的 Skill 完整替换到
`$CODEX_HOME/skills/netizen-user-guide`。该路径内的人工修改会在下一次部署丢失；其他
全局 Skill 不会被修改。卸载也只删除这个受管 Skill。

## 安装、升级与启停

正式部署支持 Linux + systemd user manager，以及 macOS 14+ 当前登录用户的 LaunchAgent；
Apple Silicon 与 Intel Mac 都在正式支持范围内，并已完成服务运行真机验证。两种平台都以
准备运行服务的当前用户执行，不使用 sudo；macOS 的 LaunchAgent 会在退出登录时停止、下次
登录自动启动，不提供 logout 后常驻的 LaunchDaemon。普通用户安装 Published Release；
仓库开发者安装当前工作区。macOS 服务使用系统钥匙串验证 TLS，不要求 Netizen 维护第二份
CA bundle：

```bash
# 仓库中的 install.sh 下载并运行最新稳定 Published Release
./install.sh

# 等价的一行正式安装
curl -fsSL https://github.com/lijingda/netizen/releases/latest/download/install.sh | sh

# 开发者：安装当前工作区（包括未提交修改）并执行完整本地门禁
./dev-install.sh
```

需要固定版本时，直接下载该 tag 的 `install.sh`，例如
`https://github.com/lijingda/netizen/releases/download/v0.4.3/install.sh`。Agent、CI 或后台
shell 不使用 pipe：先把 latest 或 exact-tag `install.sh` 下载到文件，再运行
`sh install.sh </dev/null`。凭据缺失时它会生成文件和精确后续步骤，不等待交互。

只有在命令工具能跨对话轮次保留同一个 PTY/后台进程、读取中间输出并继续写入 stdin 时，
Agent 才应代用户承载交互浏览器初始化；具体交接流程见
[部署文档](docs/deployment.md#agent-驱动首次安装)。

`install.sh`、`dev-install.sh` 和 `uninstall.sh` 不接收参数；`service.sh` 只接收
`start|stop|restart|status` 中的一个动作。这些公开脚本都不会执行 `git pull`。
正式 bootstrap 下载自己固定 tag 的
项目构建 tarball，校验内嵌 SHA-256 和 Published Release manifest 后安装；
该 tag 的 exact main commit 已在 Python 3.11/3.12 CI 通过统一代码门禁；
发布流水线复用该 exact SHA 并验证
同一次构建制品的身份和摘要，因此目标机不重复运行完整 unittest。`dev-install.sh` 则把当前
工作区做成隔离的内容寻址候选，并在本机运行完整门禁。
两条路径都安装到 `~/.netizen/releases/<digest>`，汇入同一个 `current` / `previous` 原子
切换、ready 和回滚事务。配置、数据库与 Codex 状态位于 release 外：

```text
~/.netizen/                                  # 配置、凭据、状态、缓存与 releases
${CODEX_HOME:-~/.codex}/                     # Codex 登录、历史、配置与 Skills
~/.config/systemd/user/netizen.service       # Linux: systemd user unit
~/Library/LaunchAgents/io.github.lijingda.netizen.plist  # macOS: LaunchAgent
```

Netizen 产品根始终是当前账号数据库中 home 下的 `~/.netizen`，明确忽略 `XDG_DATA_HOME`、
`XDG_CONFIG_HOME`、`XDG_STATE_HOME` 和 `XDG_CACHE_HOME`，避免不同 shell、Agent 或 sudo
环境把同一 user service 的安装身份漂移到另一套目录。`CODEX_HOME` 仍按 Codex 原生语义
生效。源码 checkout 可以在任意非受管目录，本机或云上修改后用 `./dev-install.sh`
部署；同一 Unix 用户不支持并行的第二套 Netizen。需要隔离验证安装器时使用临时用户、
容器或 VM。`releases` 和 `cache` 必须为空或带安装器 ownership marker；非空的无标记目录
不会被认领或卸载。Linux 上若 systemd user manager 自身配置了另一套 `XDG_CONFIG_HOME`，且其
`SYSTEMD_UNIT_PATH` 不包含固定 unit 目录，安装器会明确拒绝而不是生成无法加载的 unit。

首次交互安装发现 Feishu/Lark 凭据不完整时，默认显示官方浏览器链接和终端二维码：官方
页面可创建新 Bot 应用或选择已有应用；已有 `appId` 且受保护的 Secret 文件存在但内容为空
时只更新该 exact 应用。部署后若要更换应用，无需卸载 Netizen；如需保留人工回退能力，先
成对备份 `~/.netizen/config.yaml` 与 `~/.netizen/credentials/feishu-app-secret`，再删除
Secret 文件并运行原安装入口。安装器会把文件缺失解释为显式的飞书应用绑定重置，再次打开
官方创建/选择页面，并允许选择结果替换原 App ID。
两种路径都会预填 Netizen 所需的应用身份权限、`im.message.receive_v1` 事件和
`card.action.trigger` 回调。
该流程使用随 release 固定的官方 Python SDK，不安装或依赖 Lark CLI，也不申请用户身份
scope/token；菜单中的手工 App ID + 隐藏 App Secret 输入始终可用，浏览器流程失败也会
安全回退到它。App Secret 只经父子进程 pipe 写入受保护文件，不显示、不进入 argv、环境、
YAML、unit 或日志。每次安装都会在切换 release 前通过官方 API 确认全部必需 tenant 权限能力
已经授权；已有完整凭据的应用缺权限时，无论是否有 TTY 都只尝试一次有界的 exact-App 官方
修复，输出验证 URL/二维码并在修复后重新查询一次。该流程不读取 stdin；用户仍需按租户
策略完成管理员审批/应用发布与租户安装，设置可用范围，并把机器人加入目标群。二次查询
仍缺失时安装器在激活前退出，外部步骤完成后重新运行同一个正式或开发安装入口。

应用重绑定与 release 激活是明确的两阶段过程：新 App ID/Secret 一旦成功写入，就作为用户
选择的新绑定保留；后续权限门禁或候选启动失败不会恢复旧凭据，但不会切换 `current`，已经
进入激活的失败还会恢复旧 release、服务定义、数据库和 Skill。完成新应用审批后重跑同一
入口会复用新绑定继续安装，无需重复选择应用；若要放弃本次重绑定，必须成对恢复此前备份的
`config.yaml` 和 `feishu-app-secret`。权限门禁失败且旧进程未停止时，它继续使用启动时已
加载的旧凭据；候选启动失败回滚或任何后续服务启动，都以磁盘上的新绑定为准。

飞书应用绑定重置不会迁移旧 App ID 下的飞书 Scope/Binding；Channel 数据库和 Codex 原生
历史仍会保留，但新应用从自己的飞书会话命名空间开始。正常代码升级不要删除 Secret 文件，
只需在更新后的源码目录再次运行 `./dev-install.sh`；正式升级则重新运行 latest 或指定版本的
官方 `install.sh`。

安装器同时自动生成一次高熵 `0600` Admin Web credential。凭据不完整时，Agent、CI 或其他
无 TTY 调用仍不等待输入或启动首次应用选择：脚本会创建配置骨架、空的 Feishu Secret 文件
和可立即保留使用的 Admin credential，明确退出并给出路径；调用方填好 App ID 与 Feishu
Secret 后再次执行同一个下载到文件的正式 installer。已有完整凭据后的权限修复是唯一例外：
它不读取 stdin，会输出 URL/二维码并有界等待最多约 660 秒。能保留进程和转交中间输出的
Agent 可把链接交给用户，不需要分配伪终端或写 stdin。会主动分配伪终端但只想取得首次配置
路径的 Agent 仍应显式使用 `sh install.sh </dev/null`。不要把 Secret 放进命令参数、仓库或
YAML。

systemd 和 launchd 本身都不会读取 `.bashrc`、`.profile`，也不会替 Netizen 取得账号
终端里的完整工具环境。短生命周期
launcher 会在每次
服务启动时运行当前账号的无 TTY interactive login shell，读取其完整导出环境，再用
`exec` 探针替换 shell（不会触发 `.bash_logout`/`.zlogout`），随后原位启动 release
Python。因此 profile 中的 NVM PATH、代理、CA 和其他新工具在
`./service.sh restart` 后自动生效，不需要维护第二份变量清单；profile 输出和环境值既不
落盘，也不写入 journal 或 launchd stderr。服务定义只覆盖 `HOME`、`CODEX_HOME`、
配置/Secret 路径和 Python
runtime 等 Netizen 自己拥有的启动值；launcher 和服务解释器使用 `-E -B -u`，避免
profile 中的 `PYTHON*` 变量改变受管 runtime。Codex 工具子进程仍使用同一份原生
`~/.codex/config.toml` shell environment policy；Netizen 只固定公开的
`allow_login_shell=false`，避免工具的第二次 non-interactive login shell 覆盖 launcher
已经取得的 NVM PATH。没有 PATH、NVM 版本或具体工具清单被复制到另一份配置。

launcher 不模拟真实 TTY，也不继承某个现有终端中未写入 profile 的临时 `export`、alias
或未导出的 shell function。profile 退出、等待交互超过 10 秒或返回异常环境时服务会
fail closed；先修复账号 login shell 的启动文件再 restart。Bash 仍遵循自身的 login 规则：
若 NVM 只写在 `~/.bashrc`，应由 `~/.bash_profile` 或 `~/.profile` 正常 source 它；launcher
不会额外强制 source，避免同一启动文件执行两遍。安装器不会为了预检而在 service
cgroup 之外额外执行一次任意 profile。

安装器在候选 release 中新建 venv；源码候选运行完整本地门禁，正式候选运行每台主机必需
的 package、compile、依赖和 SDK probes。两路都验证配置、固定 Codex CLI 登录、飞书权限
和已安装包一致性后才切换。profile 只在真实 user service 的 cgroup 内执行；首次
安装自动 enable 并启动，升级前服务若在运行则
切换后启动并等待 ready，若已停止则保持当前会话停止。Linux 保持原 enabled/disabled
意图；macOS 保证 plist 已安装并清除 sticky disabled 状态，使它在下次登录自动启动。
候选启动前会在旧服务停止后预检配置的 Admin Web address，再快照 Channel SQLite；
服务定义、release、数据库或全局用户指南 Skill 发布失败时会恢复旧版本。停止确认同时
要求服务管理器已卸载目标且主进程 lifetime lock 已释放，未确认前不会恢复数据库或 Skill。
Linux systemd user service 要在注销后继续运行需要 linger；尚未启用时，交互安装可能
请求一次 `sudo loginctl enable-linger <当前用户>` 授权，无 TTY 时会返回可单独执行的命令。
从旧版 system-level unit 迁移也遵循相同的一次性授权规则。macOS 不配置 linger，
LaunchAgent 只属于当前 GUI 登录会话。
切换前写入的 activation intent 会记录原服务应当 active/enabled 的状态；即使安装进程在
停止旧服务或发布 `current` 后被 `SIGKILL`，再次执行原安装入口也会继续恢复该意图，
不会把异常中断误判成用户主动停服。

常规 Published Release 升级以下载到文件运行的官方 installer 返回 0 为成功判据：原服务
active 时它已经等待 ready，原服务主动停止时则保持停止，无需再重复人工验收。详细边界与
需要展开检查的例外见[部署手册](docs/deployment.md#升级启停和卸载)。

日常启停只使用 user manager，脚本内部没有 `sudo`，调用时也不要加 `sudo`：

```bash
./service.sh start
./service.sh stop
./service.sh restart
./service.sh status
```

`start` 在服务已经 loaded 且 ready 时幂等返回；loaded 但尚未 ready 时只做有界等待，不会
另起第二个进程。它和 `restart` 最多等待 45 秒，只有服务管理器仍保持 loaded 且主进程在
admission 开放后发布了私有 ready marker 才返回成功。profile 超时、shell 失败或主服务未
就绪会直接返回非零；macOS `status` 会显示 installed、loaded、ready 与两个日志路径。

仓库删除后仍可从已安装 release 调用
`$HOME/.netizen/current/source/service.sh`。正式升级重新运行 latest 或 exact-tag installer；
开发目录升级运行 `./dev-install.sh`。卸载同样不接收参数：

```bash
./uninstall.sh
```

它停止并移除 systemd user unit 或 LaunchAgent plist、程序 release、安装缓存和受管用户
指南 Skill；保留配置、两个 Secret、Channel SQLite、Project 目录、其他 Skills 以及全部
Codex 原生历史。Linux 上不会关闭 linger，因为同一用户的其他 user service 也可能依赖它。
完整发布与迁移细节见
[部署手册](docs/deployment.md)。

## 本地开发

源码开发需要 Python 3.11 或 3.12 和 `venv`。macOS 和 Linux 都可以运行下面的源码开发、安全
本地门禁以及相同的安装/服务命令；正式服务分别使用 macOS LaunchAgent 与 Linux systemd
user manager。`make check` 不创建真实 Codex Thread，也不要求 Codex 登录：SDK 合同测试主要
使用 fake App Server，受管 Skill discovery 另启动真实 bundled App Server 做只读发现。
启动 Netizen、执行真实 Turn 或运行 live probe 前，先确认当前账号的 Codex 登录有效：

```bash
codex login status
codex exec --skip-git-repo-check "Reply exactly: CLI-AUTH"
```

维护者可以把某个 checkout 专属的开发机路径、SSH 目标、远端账号、Admin URL 和私有
发布记录保存在 `LOCAL_ENVIRONMENT.md`。先复制公开模板：

```bash
cp LOCAL_ENVIRONMENT.example.md LOCAL_ENVIRONMENT.md
chmod 600 LOCAL_ENVIRONMENT.md
```

该文件被 Git 忽略且不会进入安装 release；不得写入 raw Secret。它只是可选的维护者
运维档案，不存在时仍按下面的通用步骤开发，并按
[部署手册](docs/deployment.md)选择自己的 Linux 或 macOS 目标。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e .
make check
```

复制 `config.example.yaml`，设置绝对 `defaultCwd` 和 `projectRoot`；旧
`projects` mapping 仍会在首次启动时导入，此后可在飞书 `/settings` 完成 Project
管理。飞书应用所需的 tenant 权限、消息事件与卡片回调契约只在
[部署与验收](docs/deployment.md#前置门禁)中逐项维护；受管安装器会自动请求该契约，
本地手工准备应用时需在飞书应用后台逐项配置。两种路径都还要完成租户审批/发布、配置
可用用户和群并把机器人加入目标群；权限变更必须随应用版本发布。开发可用 `FEISHU_APP_SECRET`；两种受管服务都使用 `FEISHU_APP_SECRET_FILE` 指向 0600 的纯
Secret 文件。Admin Web 不接受 raw secret 环境变量：启用时还必须设置绝对的
`NETIZEN_ADMIN_SECRET_FILE`；若本地只调试飞书入口，可在 YAML 中显式设置
`adminWeb.enabled: false`。

```bash
umask 077
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > /absolute/path/admin-web-secret
export NETIZEN_CONFIG_PATH=/absolute/path/config.yaml
export FEISHU_APP_SECRET=...
export NETIZEN_ADMIN_SECRET_FILE=/absolute/path/admin-web-secret
.venv/bin/python -m netizen.main
```

## 开发与兼容性验证

PR 和 main push 统一运行 `make check`；正式 Release 复用 exact main commit 的成功 CI，
不重复运行测试或账号级 live probe。`make check` 是不创建原生 Thread 的本地代码门禁；
按变更触发的 `scripts/probe_python_sdk.py` live phases 则验证真实账号、App Server 和
目标环境，执行前必须确认 Codex 登录有效。具体 phase、命令和触发条件只在
[部署与验收](docs/deployment.md)中维护。

`scripts/probe_python_sdk.py --phase compact` 验证公开 `compact()` 的异步语义和公开
`thread.read()` 完成证据。固定 `0.147.0` 在 2026-08-25 的重新验证中能观察到 completed
`contextCompaction`，但未能成功完成同连接的后续 Turn；隔离使用 App Server `0.149.0`
已通过同一序列。因此当前 `/compact` 不开放，也不增加临时 workaround；待匹配的 Python
SDK/App Server `0.149` 发布后随依赖升级重新验证。

- [当前架构与运行语义](docs/design.md)
- [部署、测试与验收](docs/deployment.md)
- [领域词汇](CONTEXT.md)
- [架构决策记录](docs/adr/)
- [官方 App Server API](https://developers.openai.com/codex/app-server/)
