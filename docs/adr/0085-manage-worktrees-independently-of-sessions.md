---
status: accepted
date: 2026-10-19
related: 0031, 0040, 0060, 0073, 0078, 0081, 0083
---

# Worktree 作为独立受管资源，会话使用实际工作目录

**状态：设计与默认策略已确认，用户已授权实施验证；不包含部署或发布。**
2026-10-19 最终范围修订：`/fork` 暂不新增 worktree 创建/选择能力，后续按需求另定；
既有 fork 在来源 worktree 中正确继承实际目录仍属兼容性要求。功能实现与验收结果单独记录。

用户需要通过 Admin 管理独立 checkout，在飞书新建会话时选择它，并让 Agent 复用同一套
创建和发现能力。选择有范围的客户端 Git 管理与最小关联元数据，而不是等待当前固定
SDK 尚未提供的专用管理接口，或复制 Codex App 的全部环境、快照和回收系统。长期目标仍是逐步对齐 App，
但首版不承诺 App/CLI 托管目录、私有索引和生命周期的互认。

## 资源与职责

- Worktree 是独立资源，不属于某个会话私有；多个会话共享同一目录的文件和分支。
  Project 的登记身份、默认目录、worktree 根目录与会话实际 cwd 分开，不能因使用
  worktree 就创建另一个 Project，也不能把对话上下文独立误认为文件独立。
- 仅管理通过本功能创建并登记的 **本实例受管 worktree**。用户手动、Agent shell Git、
  App/CLI 或其他实例创建的工作区不自动发现后收编、导入、删除；主 checkout 也不是删除目标。
- Git 是仓库身份、HEAD、分支与文件状态的事实源；Codex 是已物化 Thread cwd、历史、
  执行、信任和权限的事实源。Netizen 只保存受管资源身份/路径/所属 Project 登记身份、
  必要的创建交接证据、lazy 工作区选择和会话关系，不保存 Git 状态或原生历史副本。
- Admin、飞书创建入口与 Agent 工具共用应用层服务及窄 Git 操作边界；不经 Admin HTTP
  转调、不继承 Admin 权限，不增加 Runtime、Scheduler、长期监控器或配置层。
  将来公开 SDK 提供等价的管理能力时，重新验证归属、共享与失败语义后替换操作后端；
  不通过私有 RPC、写 Codex 私有元数据或仿造 App 目录布局宣称已经互通。
- `thread/attachmentOwner/list` 不是完整的跨客户端引用清单，也不是原子删除证明。
  本版不据此增加通用 Adapter；共享标准 Codex 用户状态不等于共享受管资源所有权。

## 创建与 cwd 交接

| 入口 | 首版决定 |
| --- | --- |
| Admin | 配置、分页查询、刷新、创建和显式删除受管 worktree；不创建普通会话，不提供 “New chat in this worktree”。 |
| 飞书 `/new` | 单个工作区选择：项目目录（默认）、已有可用受管 worktree、新建 worktree。非 Git 项目隐藏 worktree 控件。 |
| 默认会话配置 | Admin 与 `/defaults` 保存项目目录、指定已有受管 worktree、每个自动新建 Binding 创建 detached worktree 三种策略；保存不创建资源，实际自动创建时复用本服务。 |
| 飞书 `/fork` | 本版不增加 worktree 创建或选择，沿用普通持久 fork；来源已在 worktree 时正确继承实际目录和关联。 |
| Agent worktree 工具 | 仅提供发现受管工作区和创建能力，复用同一服务；创建返回资源标识、根目录/实际工作目录及真实分支或 detached 状态。分支由 Agent 按需通过 Git 查询并填写 `ref`，不提供专用分支查询或删除工具。 |
| Agent `create_thread` | 参数保持 CLI 形态，不增加 `cwd`、worktree 或 branch；后台使用来源 Thread 的实际 cwd 创建会话。 |

新建 worktree 提供基线/已有分支选择及 checkout 新分支配置；已有 worktree 只使用其
当前状态，不能借创建会话隐式切分支。不选 worktree 时也不借此切换 Project 的分支。
已被其他 checkout 占用的命名分支不能强占；“选择起点提交”与“checkout 命名分支”必须
在界面和参数中区分。默认检出方式见下文已确认策略。

`/new` 继续只创建 lazy Binding：提交新建选项时先完成 worktree 创建和登记，再建立
Binding；普通首条输入或首条 `/goal` 才通过已有 SDK `thread_start(cwd=所选目录)`
物化 Thread。已有 worktree 则保存明确选择，不重新创建。Project 在仓库子目录时，
区分 checkout 根与相对子目录；目标提交不含对应安全目录则明确失败，不补复制源目录。

2026-10-20 默认会话扩展遵循 [ADR 0073](0073-create-sessions-from-chat-defaults.md) 的
真实 Scope、规则优先级与输入准入。工作区策略与 SessionSettings 分开保存；每个自动
新建 Binding 创建一次工作区，后续消息继续使用该 Binding。自动新建仅建 detached checkout，
可选 `ref` 留空时使用创建时 Project HEAD，不持久化某次解析出的 commit，也不提供重复
使用的命名分支。已有 Binding、显式 `/new` 及定时新话题不读取这项默认策略。

2026-10-19 复审确认：若登记的 Project 目录因用户切分支等操作而缺失，沿用现有
Project 不可用处理，由用户修复 Project；不为 `/new` 复用已有 worktree 增加绕过
Project 目录检查的兜底，也不因另一个 checkout 尚存而恢复或重新关联 Project。
这不等于隐式删除 Project 登记、工作区或原生会话历史。

CLI 模型 `create_thread` 与 SDK `thread_start` 不是同一层接口：Netizen 已有后者，
不因此新增模型可调用的 `thread_start`。来源在主目录就继承主目录，来源在 worktree
就继承该 worktree；若匹配受管资源，新会话关联同一资源，而不是关联到创建者“私有”目录。
`create_worktree` 不改变调用会话 cwd，不扩展文件访问权限，也不创建会话。因此从 A
创建 B 后再调用未改参数的 `create_thread`，新会话仍使用 A，不能隐式采用最近创建的 B。

没有新增模型任意路径输入，不为它设计一套目录白名单。原生路径、信任与执行权限仍由
Codex 处理；Netizen 仍须校验自身选项、Project/资源身份、管理权限与关联，SDK 不代办这些。
已物化 Thread 不因后续输入被重设回 Project.cwd，也不新增已有会话换目录/Handoff。
观察到本实例原生 Thread cwd 改变时，按 exact native ID 同步最小工作目录/受管关联，
只匹配当前 Project 登记代次；旧代次或受管子目录的已知使用仅参与删除保护，不自动收编。
新的目录观察和使用不能越过资源删除预约。本轮 Files/图片固定采用接受时目录，后续
Thread cwd 变化不能重新解释本轮文件；目录证据缺失时不从当前目录或 Project 猜测。
2026-10-19 用户确认修订：提交入口的 cwd 仅是 lazy Thread 的创建目录意图，执行目录
统一由 Runtime 当前使用的 Thread 提供。新建采用选定目录；恢复已有 Thread 后、发送
输入前，使用公开 SDK 读取本次实际 cwd 并继续输入；复用活动 Thread 时同时复用其已确认
目录。原生 ExternalMessage 返回加入当前轮或启动下一轮，都遵循同一 handle/目录边界，
不再混用调用方提前取得的 cwd，也不按客户端或换目录原因枚举补丁。
此前“目录变化即拒绝”是 Netizen 额外策略，并非 CLI 的恢复行为，本版移除该限制。
Channel 不再为已有目标 Thread 提交预读 cwd；创建来源继承、状态等独立目录查询保留。
飞书文本、引用及图片准备不依赖 cwd；真正依赖目录的显式 Skill 解析放在取得该执行目录
之后，保持锁外 discovery 及提交前 exact admission 校验，不重做整套飞书材料准备、
不增加输入重试。Turn 目录记录和本轮结果文件使用同一份执行目录。
Skill 校验失败不发送 Turn/steer；已有空闲 Thread 为取得实际目录可以先恢复，失败后
仍按正常 idle 策略释放订阅，lazy 会话则保持先校验 Skill 再创建 Thread。
读取失败或目录事实无效只拒绝本条尚未发送的输入；不以旧 cwd/Project 兜底，
不覆盖原生目录，也不将只读失败扩大为全局停用。提交阶段的这类拒绝复用现有明确
未接受错误，跨会话回执不得把它报告成投递结果未知。
已确认的目录事实复用既有 exact-native 关联同步和资源删除准入；本条未接受时不推进
消息上下文边界。正在运行的 Turn 仍按原 exact-Turn steer 规则处理，不增加 resume 或
cwd read。恢复路径保留既有的一次原生目录读取，复用路径不读；不新增目录缓存、数据库
字段、Git 扫描或全局锁。
普通 `/fork` 保持公开 native fork 的历史及原生权限继承，不以空白 `thread_start` 或
复制历史模拟 fork，也不为本版增加 fork 的 cwd override。

## 创建内容与部分成功

只从选定已提交 revision 检出；不自动迁入 staged/unstaged 改动、untracked/ignored
文件，不解释 `.worktreeinclude` 复制规则，也不复制 ignored `AGENTS.override.md`。
不执行 App environment/setup。这是两项独立的范围决定，不能由 “No environment”
推导出文件复制策略，也不能承诺复制源目录的依赖、子模块状态或 LFS 实体内容。

创建工作区和创建会话是两步。**工作区创建并登记成功后，后续 Binding、飞书话题或原生
会话创建失败不回收工作区**：它仍可在 Admin 管理，也可在后续 `/new` 使用。会话侧沿用
既有失败和已知副作用反馈。仅 Git 目录生成而登记失败不是完整成功；需保留足以报告、
精确处理残留的创建证据，不能虚报 Admin 已可见或无残留。结果未知不盲目重试，不为此
引入持续补偿队列，也不自动删除可能已被使用的目录。

能够证明尚未进入 worktree 创建的准备失败，报告明确失败并撤销本次尚未使用的创建
预留，不留下 `unknown` 资源；撤销仅针对登记，不操作磁盘目录。Git 创建已可能开始、
响应丢失或后续 checkout/身份确认失败仍保留创建证据。单个后续命令“尚未启动”不能
证明整个工作区尚未创建；创建前 fetch 的失败也不能被描述为仓库引用必然没有变化。

Git 操作沿用 CLI 的命令局部防护：按有效配置中的 filter driver 禁用
`clean`/`smudge`/`process` 并取消 `required`，清空外部 attributes 来源；目标 checkout
创建后重新读取其有效 filter 配置，最终删除同样覆盖目标配置。复用既有 Git 封装，
不写持久 Git 配置，不另建隔离系统，也不承诺枚举与执行之间任意外部配置变更的原子隔离。

## 当前状态与目录缺失

Admin 进入、分页和手动刷新时按需读取当前页；不后台持续扫描，也不只相信创建记录。
列表用轻量目录检查、Git 身份与 HEAD/分支查询，不逐行执行完整 dirty 文件扫描。
仅 `cwd` 存在不足以证明它仍是原 worktree；路径被替换、Git 关系改变、权限不足、
超时/状态未取得与目录确定缺失必须区分。未查询 dirty 状态不能显示为 clean。
仓库查询可去重并设数量、并发和时间预算；完整成果检查留在删除预览/执行复核。

外部删除后保留受管登记和关联，标记目录缺失；不隐式删除登记或会话，也不把路径重新
出现视为原资源恢复。清理缺失登记是显式管理动作，与删除现存磁盘内容区分。
保留会话时保留历史和原 cwd，提示缺失，后续请求以 Codex 的实际返回处理：不制定
“一律禁聊”，不静默切回主目录，不自动重建或恢复快照。该选择不保证缺失 cwd 下的
恢复、纯文本 Turn 或工具一定成功，也不能把目录缺失等同于 native Thread 已删除。

## 删除与共享关系

删除有二次确认；预览和提交绑定 exact 清单、资源身份、关联关系与勾选项，提交前复核。
沿用 Project 删除的停用、墓碑、在途创建交接和部分失败机制；只有本次原生删除明确
成功才提交对应 materialized Binding 删除，不能根据目录、catalog 缺项或 Missing 标签
猜测成功。已成功项不回滚、不自动续删，剩余项和未知结果如实报告。

| 操作 | 已确认行为 |
| --- | --- |
| 删除 Project | 沿用现有会话/Side/定时计划清理，增加“同时删除关联受管 worktree”勾选项；未勾选保留工作区，原 Project 代码目录始终保留。勾选后的失败工作区纳入剩余项，不能提前报整项完成。 |
| 删除 worktree | 增加“同时删除关联会话”勾选项；未勾选保留历史和原 cwd，勾选则通过既有精确原生生命周期处理已确认关联会话，再安全清理目录。 |
| 删除或归档单个会话 | 不自动删除可能共享的 worktree。 |

级联清单中的关联会话指以该工作区为实际 cwd 的会话，而不是调用 `create_worktree`
的创建者。另保留最小创建来源/回执关系用于已知活动和在途删除保护：来源 cwd 仍是 A，
也可能正在通过工具使用新建的 B；这不使创建者自动成为级联删除目标，也不宣称能完整
追踪任意 shell 的 `cd`、路径访问或外部客户端使用。清单须考虑 lazy、归档、Side、
在途创建及活动执行；创建/绑定与删除使用资源范围的短准入交接，
不能扩大为串行化普通 Turn 的 Project 锁。原生会话删除继续由 App Server shutdown，
不改成预先 interrupt/cleanup；直接删除并保留会话时不得破坏已知的活动或在途使用。

二次确认和级联勾选不是丢弃未保存成果的授权。删除需复核身份及未保留成果，包括
untracked/ignored 文件和仅存在于 detached HEAD 的提交；clean 不等于安全。不能用
强制 Git 删除或递归移除兜底。跨客户端引用与活动无法证明全局完备，界面应说明本实例
已知范围，不能宣称不存在外部使用；具体保护条件属于实施验证，不伪装为 App 已提供的保证。

Project 删除而保留工作区时，Admin 全局列表继续展示其原 Project 已删除；本版**不做
重新关联 Project**，也不因同 alias 重建就自动挂回。这些工作区不再出现在 `/new`
候选中，用户自行处理内容后在 Admin 显式删除。

默认配置不是会话关联或删除级联项。删除 Project 或 Worktree 后保留引用它的默认规则，
下次自动创建时明确报告不可用；不换规则、不改用项目目录、不自动重建指定资源。
Project alias 可在显式重新登记后恢复使用；指定已有工作区仍须匹配 exact ID 和新 Project
的登记代次，因此不能因同 alias 或同路径出现而恢复旧资源引用。用户须重新选择有效
工作区或修改策略。保存、修改和删除默认配置都不回收已经创建的工作区。

## 明确不做与默认策略

不做 environment/setup、文件自动复制、快照/恢复、自动回收、Handoff、外部 worktree
收编、孤立工作区重新关联、飞书 `/worktree` 管理、`/fork` 创建/选择 worktree 或 Admin
新会话。既有 Binding 的定时输入必须适配实际 cwd，但本版不新增定时新话题的工作区配置。
两个级联勾选项默认不勾选，不改变已确认的可选级联能力。

以下两组策略已获用户确认：

1. **托管根与创建前 fetch 配置。** 默认使用本实例独立托管根 `<instance-root>/worktrees`，
   Admin 可改以后创建位置，旧目录按登记继续管理；不写 App 全局配置、不默认放进 App
   自动清理池。创建前 fetch 默认关闭，开启后失败明确终止，不静默使用旧 ref。
   路径可打开不等于 App managed 互认；实例卸载/purge 不递归删除
   工作成果，并验证已有精确清理边界。
2. **新建的基线和分支默认。** 人工入口按 CLI 从来源当前 HEAD 建 detached checkout，并允许显式
   checkout 未占用的本地分支或从基线创建新分支；远端分支可作为基线，不冒充本地分支。
   Agent `create_worktree` 的 `ref` 默认按所对齐的 CLI 规则处理并返回解析后的实际结果，
   不把它与人工入口默认 HEAD 混为一个规则；若增加命名分支参数，明确属于 Netizen 扩展。

飞书联动采用显式刷新还是分步表单、窄 Git 服务形态、分页预算、schema 字段和工具命名
可以在上述边界内决定，无须再作产品方向选择。当前 Channel SDK 没有透传所需的单选
change option，不能为联动读取私有回调或保存另一套卡片 session。

## 实施影响与验收

这是共享底层、有限横切的改动，不能称为纯 UI 增量。需统一 ordinary 首轮、首条 Goal、
模型创建/来信、Skills、Files/图片相对路径、状态/分支、Side 目录快照、普通持久 fork、
会话 list/read 和已有 Binding 定时输入的实际 cwd；原生执行与历史继续复用 SDK。
变更前的具体入口包括 `channel_app.py:create_session_thread` 中传入 `project.cwd`，
以及 Runtime lazy 创建使用 `thread_start(cwd=...)`；现状不是模型已有可选 cwd 参数。

实施至少覆盖主目录 A/worktree B 的同名文件与 Skill、来源 B 创建会话仍在 B、创建 C
不迁移来源、普通/Goal/Side/fork/定时路径、非 Git 与仓库子目录、占用分支、外部改分支/
删目录/路径替换、两步创建失败、重复/未知响应、可选级联/部分成功、孤立登记和同 alias
重建。按既有迁移政策增加最小元数据，执行 `make check` 及受影响的 disposable native、
Git 和飞书交互验收。当前实现与验证范围见下节；未验收项不视为已部署。

### 本次实施记录

2026-10-19 已实现共享 Worktree 服务、Admin 配置与管理、飞书 `/new`、两个 Agent
工具及实际 cwd 适配；数据库采用冻结的 v14 → v15 迁移。受管登记记录最小 Git 身份
和创建状态，不缓存分支/dirty 状态或原生会话历史。两个级联选项默认关闭，未知创建与
部分删除保留已知成果，不自动补偿或恢复。没有扩展 `/fork` 或 `create_thread` 参数。
复审后移除 `list_worktree_branches`，保留 `list_worktrees` 与 `create_worktree`；Admin
与飞书表单的内部选项查询保留，不将 Agent 查询需求扩展成另一套分支工具或分页接口。

真实临时 Git、应用层/卡片/Agent 工具/Admin HTTP 与 JavaScript 行为、迁移及删除并发
保护测试已补充。独立 `scripts/probe_worktree_cwd.py` 使用固定 SDK 0.163.0 的 bundled
App Server、隔离状态和 loopback mock 模型，已验证 `AbsolutePathBuf` 返回形态、选定
cwd 的 start/read、冷读取及不覆盖 cwd 的 resume；真实模型调用为零。
该探针现亦通过生产 Runtime 验证跨客户端 A→B 后，准备于 A 的原输入直接在 B 完成，
仅发送一次、不覆盖原生目录，Binding 目录关联与本轮输出目录均采用 B，准入保持开放。
显式 Skill 的恢复后目录解析、锁外发现、原准入及订阅代次复核由行为测试覆盖。
完整门禁结果与
真实飞书、目标平台迁移、native 生命周期专项的待验收边界统一见
[部署验收](../deployment.md#受管-worktree-专项验收)。本次不部署、不发布。

## 固定版本证据

研究基线为 Netizen `4c7f3ad38fd0cd470af3f47c2b04f7e29f867af9`、SDK/CLI 0.163.0。
下列为已核对的固定源码，不外推到 App 私有实现或未来版本；读取上游测试不等于执行测试。

- 模型 `create_thread` 的参数及来源 cwd 继承：[dynamic_tools.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/dynamic_tools.rs#L490)；公开 SDK 的 cwd 参数：[api.py](https://github.com/openai/codex/blob/rust-v0.163.0/sdk/python/src/openai_codex/api.py#L393)。
- 客户端 Git worktree 管理及按 commit 检出：[worktree/src/lib.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/worktree/src/lib.rs#L62)；源未提交内容不迁入的测试：[worktree/tests/worktree.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/worktree/tests/worktree.rs#L533)。
- Agent 创建的 ref/默认基线与结果：[managed_worktree_tools.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/managed_worktree_tools.rs#L250)、[worktree/src/git.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/worktree/src/git.rs#L97)；人工入口：[worktree_startup.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/worktree_startup.rs#L245)。
- 历史 cwd 选择与后续配置加载分开：[session_resume.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/session_resume.rs#L34)、[resume_config.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app/resume_config.rs#L29)。没有统一缺目录禁聊的入口规则，不代表后续 Turn 保证成功；缺目录端到端行为尚未实测。
