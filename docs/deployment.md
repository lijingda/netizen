# Linux 与 macOS 独立实例部署

> 当前程序分发与维护遵循 [ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md)
> 和 [CLI 安装与维护](cli.md)。`netizen-cli` 自 0.10.0 起通过 PyPI 发行。
> 本文保留的旧 release／激活回滚记录均为历史证据，不能证明 CLI 的安装、更新或两平台
> 服务验收已经通过。源码测试、实际 SDK 执行、浏览器流程与平台服务验收分别记录。

Netizen 使用用户选择的 Python 安装，服务以当前账号运行，共享原生 Codex 状态。
不下载 Python、不创建隐藏 venv，不在每个实例保存程序 release。Linux 使用 systemd
user manager，macOS 14+ 使用当前 GUI 用户的 LaunchAgent，不提供 LaunchDaemon。
一个账号可运行多个独立 root，每实例一个机器人、Channel DB、Admin listener 和业务
服务；这不是跨实例权限隔离、网关或同 App 高可用模式。

## 按任务查阅

| 要完成的任务 | 阅读入口 |
| --- | --- |
| 首次安装 | [选择部署目标](#选择部署目标) → [前置门禁](#前置门禁) → [安装](#安装)；Agent 使用 [CLI 安装与 setup](cli.md#安装与首次使用)，按输出继续。 |
| 更换飞书应用或补齐权限 | [更换飞书应用与权限修复](#更换飞书应用与权限修复)；新增权限契约先看[维护飞书权限契约](#维护飞书权限契约)。 |
| 升级、启停或卸载 | [CLI 操作与成功判据](#升级启停和卸载)、[程序更新边界](#从-admin-升级)、[管理页重启](#从-admin-重启)。 |
| 调整工具环境、代理或证书 | [服务环境](#服务环境)及[安装前提](#安装)；修改账号 profile 后重启服务。 |
| 配置项目、访问或轮换 Admin 凭据 | [配置与管理页访问](#配置与管理页访问)。 |
| 排查启动失败、未知状态或迁移失败 | [候选验证与切换](#候选验证与切换)、[Fail-closed 运维语义](#fail-closed-运维语义)、[平台服务管理器与日志](#平台服务管理器)。 |
| 开发与发布 | [本地开发](CONTRIBUTING.md)、[Codex SDK 升级审查](#codex-sdk-升级审查)、[代码门禁与 live 触发条件](#代码门禁与按需实时兼容性验证)、[兼容性结论](#已验证的兼容性结论)、[正式发布](#发布正式-release)。 |

## 选择部署目标

仓库不定义默认服务器、SSH alias、账号或远端 checkout。选择满足本文前置条件的 Linux
主机，或由实际桌面用户登录的 macOS 14+ 主机（Apple Silicon 或 Intel）。执行同步、远端
调试、候选门禁、安装、升级或运行验收时显式使用同一个 `<deployment-host>`；它可以是本机
SSH config 中的 alias，也可以是 `<user>@<hostname>`。LaunchAgent 的首次 bootstrap 还要求
该用户的 `gui/<uid>` launchd domain 已存在；只有 SSH 登录、没有 GUI 登录会明确失败且
不写入安装状态。

维护者若需要在某个 checkout 中保存自己的主机、路径和私有验收记录，可复制
`LOCAL_ENVIRONMENT.example.md` 为被 Git 忽略的 `LOCAL_ENVIRONMENT.md`。该文件不是
运行时配置；没有它的全新 clone 仍应完全按本文完成部署。

程序安装在选定的长期 Python 环境，实例 root 只保存数据。ADR 0014/0074 的
Goal/Skills、ADR 0021 的 Side 与 ADR 0037 的 Thread Delete Adapter 不做运行时版本
allowlist；修改 pinned SDK/App Server 或这些 Adapter 时，开发迭代必须对实际 resolved
组合运行受影响的 capability harness。Delete 能力变更还必须覆盖 disposable lifecycle
live probe 与 Runtime 原生返回分类、局部 unknown 和零自动重试测试（ADR 0078）。ADR 0020/0052 的 active-Turn Activity observer
另行精确锁定 SDK 版本、源码指纹、generated shape 和非消费 event-store contract；门禁失败只
关闭 checklist/Activity 展示，不关闭普通 Turn。这个降级以 ADR 0009 独立的 service-wide
SDK/cleanup 启动门禁通过为前提；不能用 Activity 的展示降级绕过该门禁。

## 目录

```text
<selected-python-environment>/
  .../site-packages/netizen_cli/               # 程序、Admin 静态资源
    resources/skills/                         # 两个内置 Skills
    resources/config.example.yaml
  bin/netizen                                 # console entry point
<NETIZEN_ROOT>/                                # 默认有效账号 ~/.netizen
  .netizen-root                               # 私有归属标记
  .netizen-initialized                        # 显式初始化／清理阶段证据
  config.yaml                                # 私有 Channel 配置
  lark-app/config.json                       # 固定 netizen profile
  credentials/admin-web-secret
  state/
    channel.sqlite3[-wal|-shm|-journal]
    .install.lock                            # 本实例维护互斥
    service.lifetime.lock / service.ready
    service.identity.json                    # PID/解释器/prefix/root；不是注册表
    update.json                              # 最近一次 Admin 重启摘要
    netizen.log*
    migration-backups/                       # 需要迁移时的私有恢复材料
~/.config/systemd/user/netizen-<rootDigest>.service
~/Library/LaunchAgents/io.github.lijingda.netizen.<rootDigest>.plist
${CODEX_HOME:-~/.codex}/                      # 用户共享原生状态，不属于实例清理范围
```

当前布局没有 releases、current、previous 或每实例 venv。root 选择为 `--root` >
`NETIZEN_ROOT` > 有效账号 ~/.netizen，显式空值拒绝，相对路径以调用 cwd 为基准，
解析为 canonical absolute path。服务定义固定该 root 与绝对 Python 入口，终端切换 venv
不改变已有绑定；rootDigest 只是 canonical root 的 SHA-256 前 24 个十六进制字符。

实例目录属于当前账号且不可被组／其他用户写入。setup 只认领符合契约的空命名空间或
安全预配置文件，原子创建归属标记；已有外来 state、损坏标记、symlink 或混合 root
明确拒绝，不以名称猜归属。初始化证据区分首次创建、已有实例缺库和未完成 purge。
配置 `instance.dataDir` 必须是该 root 的 state，Project 与共享 Codex 数据不搬动。

remove 默认仅解除系统服务及自启，保留数据；--purge 先展示并确认有限、已验证的文件
清单，永不递归删除 root／home，不删除未知文件、Project 或共享 Codex 状态。
移除实例不卸载 Python 环境中的共享包。
可选[netizen-herdr 扩展](../extensions/netizen-herdr/README.md)仍由用户从源码按说明安装，
不是 wheel 自动安装的全局 Skill。

## 前置门禁

先在将运行 Netizen 的账户中，通过 Codex CLI 或 Codex App 完成登录。若选择 CLI，可以
独立确认：

```bash
codex login
codex login status
codex exec --skip-git-repo-check "Reply exactly: CLI-AUTH"
```

CLI setup 不安装全局 Codex CLI/App、不代用户登录，也不要求 PATH 上存在 codex。
它用当前安装所带的固定 bundled runtime 在当前账号环境执行 login status；显式
CODEX_HOME 与后续绑定保持一致，未指定则使用账号默认。setup 不执行账号 profile，
profile 只在受管服务启动边界内加载；若用户依赖 profile 导出的自定义 Codex home，
应从正常账号环境执行 setup 或显式提供该路径。登录错误先独立修复，不把一次检查
当作长期有效保证。

逐条引用会调用“获取指定消息”，普通/富文本图片还会调用“获取消息中的资源文件”。群聊
Binding 的 catch-up 模式会在收到有效 @ 后，通过同一应用身份调用“获取会话历史消息”和
exact “获取指定消息”；群主线使用 chat container，普通话题使用 thread container。
CLI setup 的官方 SDK 浏览器流程会用最小模板请求下面的应用身份权限、
`im.message.receive_v1` 应用身份事件和 `card.action.trigger` 回调，不申请用户身份权限或
token；手工准备的应用必须逐项配置。无论来源，飞书应用版本在发布前都必须确认：

- 单聊事件投递具备 `im:message.p2p_msg:readonly`，普通消息能力具备 `im:message`；
- `/settings` 等卡片回调识别会话类型具备 canonical `im:chat:read`；官方“获取群信息”接口
  支持的 `im:chat`、`im:chat:read`、`im:chat:readonly` 三者任一 tenant 授权都满足门禁；
- 群聊回查额外具备 `im:message.group_msg`，不能只有接收 @ 消息的
  `im:message.group_at_msg`/readonly；
- 接收群内其他机器人 @ Netizen 的消息具备
  `im:message.group_at_msg.include_bot:readonly`，默认授权与安装期有效权限检查均包含它；
  普通 `im:message.group_msg` 不包含机器人消息事件，不能替代这项权限；
- 当前 Prompt 发送者姓名解析具备 `im:chat.members:read`；权限不足时 Channel SDK
  无法从 chat member roster 补全真实显示名，Netizen 会零 start/steer；
- 补充上下文历史消息的发送者姓名使用消息 API 自带的 `sender_name` 投影，不需要
  通讯录权限，也不受应用通讯录权限范围限制；
- Lifecycle Reaction 与可选 Reaction Pulse 由当前必需的
  `im:message` 覆盖；官方提供的
  `im:message.reactions:write_only` 是替代权限，不作为 Netizen 的另一项独立必需权限；
- 本轮文件具备 `im:resource` 与 `im:message:send_as_bot`，允许机器人上传图片/文件并
  回复消息；“事件与回调 → 回调配置”已开启，按钮 callback 能到达当前 WebSocket；
- 机器人仍在目标群中，新权限已随应用版本发布而非只保存在开发者后台。

权限不足时不降级为忽略引用或图片的普通 prompt；当前消息必须显式失败且
不调用 Codex。Lifecycle Reaction、Reaction Pulse 与 Progress Card 都是展示层的尽力操作：
reaction/card
权限或单次请求失败只记录脱敏日志，不得阻断已经启动的 Turn 或最终结果。Lifecycle
Reaction 中的 `OnIt` 失败时，已成功的 native steer 始终回退一条简短确认。
Progress Card 中间投递失败时由既有轮询重试最新快照，连续三次失败停止轮询；读取或
渲染失败仍立即停止。普通 Turn、Side 与 Goal 终态对身份有效的原卡独立尝试最多三次
相同更新，失败间隔 0.5 秒，整组请求与间隔共用 5 秒预算。原卡不可用、终态渲染失败或
重试耗尽时，普通/Side Turn 才回退到无文件
富文本/静态文本、有文件完成卡的标准路径；Goal 使用新的自包含组合卡或明确的文本
fallback，不能因为更新失败丢掉权威结果。

引用功能发布后要用真实消息手工验收普通文本与 CardKit 2.0 应用卡片。卡片用例应
确认 header/body 可见文本进入 Codex，而按钮 value、确认弹窗和隐藏 option 不进入；
确认两次 SDK 读取分别使用 10 秒预算，不得退回共享总预算而产生假超时。
发布 ADR 0064 的材料输入前，还须在真实客户端分别直接发送与引用 Card 2.0、
合并转发和转发话题，并验证普通会话及 Side；群聊/话题仍逐条 @。混合材料至少包含
文字、图片、文件、音视频、嵌套转发及卡片：可读内容和未读附件说明进入 Codex，内部
图片不作为视觉输入，`/stop`、`/new`、`$skill` 不执行。检查全包 50 项、16,000 字符
限制和截断提示；最外层 depth=0，内嵌 depth=1..3 可读，depth>3 或 SDK
`max_depth_exceeded` 必须整条拒绝。Card 1.0 在直接、引用、补充历史和转发子项中均须
明确提示不支持；卡片隐藏交互文本、不可渲染子项、读取失败或缺权限同样必须零
start/steer。转发富文本还须验证多语言及 `content`/`content_v2` 并存时，附件描述与
实际可见正文一致。正常按钮 label 和 scalar value 不应误拒绝。真实转发话题的 SDK 返回
内容与目标应用访问权限须单独确认，不以官方示例或本地测试视为通过。
当前消息来源还要由两名真实参与者交叉验收：A 启动长 Turn，B 发送 steer，Codex 应分别
看到两条消息的实际发送者，但完成回复仍锚定 A 的原任务消息。随后从 Codex App/CLI 查看
同一 native Thread，确认公开身份字段进入原生历史，并确认 `/sessions`、`/status` 的首条
preview 仍以 A 的真实请求开头，而不是 attribution 元数据。此验收只确认归属可见性，
不得把显示名或 ID 当作额外权限。临时撤销 `im:chat.members:read` 或使用未发布该权限的
应用版本重试时，消息必须明确提示开通权限且零 start/steer，不能出现“未知发送者”
Prompt；恢复并发布权限后同一成员应重新解析出真实姓名。
机器人 @ 场景还须在目标应用确认新权限可申请、完成授权与发布后，以同群另一机器人
发送真实 @ 消息验收：消息进入当前会话，发送者保留机器人身份和真实名称，未 @ 不触发；
catch-up 仍只补充非机器人成员消息。权限清单与本地测试不代表这项平台验收已通过。
图片用例还要覆盖：单聊普通图片、群聊 @机器人富文本多图、文字引用图片、当前图文
引用另一条图文。确认图片以原生视觉输入提交；任一资源删除/保密/无权时零
start/steer。另发送 locale 正文与顶层 `post.files` 并存的真实文件和文件夹；两者都必须
明确提示附件不受支持且零 start/steer，文件夹不能因 SDK 不生成资源描述符而绕过。
观察进程 RSS：固定 Channel SDK 在 20 MB 应用层门禁前可能完整读取
飞书允许的最大 100 MB 单资源，data URL 和 native RPC JSON 还会产生额外副本。
不同 Binding 的图片准备保持并发，没有全局容量 gate；asyncio 超时也不能停止已经进入
worker thread 的阻塞读取。Pilot 依赖受控用户范围、低频小图，并需持续观察并发图片时
的 RSS；若实际使用模式变化，应先推动 Channel SDK 的有界流式下载能力。

catch-up 上线前必须使用目标租户和目标应用做群主线 chat container、普通话题 thread
container 两类 live probe。probe 要同时证明 lower/upper exact endpoint 可见、同秒消息、
倒序分页、sender name、机器人加入前已创建/历史受限的话题，以及早于 upper 但首个快照
暂不可见的消息能在一次有界重读中收敛或被明确标成不完整。若平台表现为静默遗漏，必须
保持 catch-up unavailable，不能用 generated SDK shape 或本地 Fake 代替这个 rollout gate。

### Codex SDK 升级审查

每次升级 `openai-codex` 或随附 App Server（包括 patch 版本），都必须完成以下审查，
并执行后文规定的门禁。该流程落实 [ADR 0014](adr/0014-use-removable-sdk-gap-adapters.md#sdk-升级-harness)
及[SDK 适配边界](design.md#sdk-适配边界)；测试通过只证明已覆盖行为，不能代替能力与产品语义审查。

1. **确定比较基线。** 记录升级前实际锁定的 SDK/CLI 版本与目标版本；请求“最新版”时
   核实最新稳定发布，区分 SDK 包、随附 CLI/App Server 与模型目录的变化。阅读对应官方更新
   记录，并比较两个精确版本的安装包或源码；开发主线文档不能代替目标发布的实际实现。
2. **检查完整差异。** 比较公开导出、类/子资源、方法、属性、签名和返回类型，以及
   generated models、字段可空性/默认值、枚举、错误、通知和序列化语义。对 adapter
   依赖的私有 ownership、事件存储、消费与取消逻辑另做源码复核。区分公开高层 API、
   低层 client、协议模型和 App Server 行为；高层源码未变不代表原生执行语义未变。
   现有 `facade_migration_requirements()` 仅检查预列候选名；空结果不能替代完整 API
   差异审查，也不能证明换名、新子资源、cleanup 或 Activity 等能力仍无公开替代。
3. **逐项复核兼容债务。** 从当前实现、[适配边界](design.md#sdk-适配边界)及各自 ADR
   建立本次清单，覆盖 gap adapters、pinned cleanup/Activity，以及 SDK 限制造成的
   产品缺口和临时处理；不要只审查本次测试失败的模块。对每项记录“迁移/删除”“保留”
   或“待验证”，附目标 API/行为证据、保留原因和解除条件。公开等价能力可用时，遵循
   对应 ADR 的 migration-required 与 parity 门禁，在本次升级中切换 provider、删除
   对应 shim/私有依赖并保留行为测试；仅有协议字段不算高层替代。观测恢复、压缩归属、
   Goal 重挂、fork/命名等待、订阅与进程清理等处理须核对原约束是否真的消失，不能仅
   因原生发布说明声称修复就删除。
4. **判定新增能力的归属。** 对与 Netizen 有关的变化分别说明：必须适配的契约变化、
   已由 SDK/原生配置生效、可选产品能力、仍有接口或验证缺口。核对错误/终态、身份、
   时间字段、用户反馈及配置生效范围；不能仅以“字段可解析”宣称语义已适配。属于
   Codex 的模型、工具、权限与配置能力继续由 Codex 管理；不因升级自动增加 Netizen
   开关、配置副本或新私有 adapter。无须修改的项也给出依据。
5. **按结论实施与验证。** 源码复核后再更新精确依赖、必要指纹和契约断言；完成
   `make check`、[完整原生集合及专项门禁](#代码门禁与按需实时兼容性验证)。对新增或
   声称已修复但既有探针未覆盖的行为，补有界、disposable 的针对性验证；协议 shape、
   synthetic、真实原生执行、飞书客户端和平台部署分别报告，不能互相替代。保留首次
   失败、诊断与复验结果，无法验证的项明确标记，不能通过放宽断言或修改期望来制造
   成功。顺带发现的旧缺陷、超时调参和产品扩展须单独说明理由与升级的因果关系，避免
   把它们混同于必需适配。
6. **交付审查结论。** 在变更说明中记录版本基线、官方来源/源码差异、逐项兼容债务
   决定、新增能力分类、实际改动、检查结果及未覆盖边界；没有可删除封装或新增必需适配
   也明确写出。同步受影响的设计/用户行为契约，通用兼容性摘要写入
   [兼容性结论](#已验证的兼容性结论)，单次日志、exact IDs 和私有环境信息按该节约定
   留在验证产物中。升级审查不授权发布或部署。

### 代码门禁与按需实时兼容性验证

所有面向 `main` 的代码先通过 `make check`；PR 和 main push 的 GitHub CI 都在 Linux x64
标准 CPython 3.11-3.14，以及 macOS arm64 标准 CPython 3.13/3.14 执行这一个统一本地门禁。
发布可复用 exact main commit 的成功 CI 结论，但不能用旧 archive 门禁替代新包验收。
固定 SDK synthetic probes 的命令、参数和执行顺序统一由 `scripts/check_sdk.py` 维护；
`make check` 调用该入口，失败即停止后续探针；新 CLI 包安装不重复运行完整测试。
SDK synthetic 门禁还要求 20 次快速完成通过原生 handle、20 次公开 read 恢复和
40 次 usage/diff drain，交替覆盖先观察到运行中和启动响应前已经完成的 Turn，
验证开始请求窗口内的通知保留与终态后唯一消费。
Main Qualification 的 Linux 与 macOS jobs 均显式安装 Node.js 22，执行 Admin JavaScript
行为测试；在 `CI=true` 时缺少 Node.js 会使测试失败。Node.js 是开发和 CI 的测试工具，
不参与前端构建，也不增加生产运行的前置依赖。本地开发
缺少 Node.js 时该项测试会明确跳过，门禁通过不代表 JavaScript 行为测试已完成；其必跑保证
由 Main Qualification CI 提供。
macOS job 还会从安装后的 wheel 实际初始化系统钥匙串 truststore；CI 没有真实应用凭据与
用户 GUI 会话，因此 Python 支持矩阵扩展仍须在正式发布前完成一次 macOS arm64 当前用户的
LaunchAgent 安装、启动和 ready 冒烟，不能用 CI 代替。

真实账号 live probes 是开发阶段按变更触发的兼容性工具，不是普通 merge 或正式 Release
门禁。升级 pinned SDK/App Server 时完成[升级审查](#codex-sdk-升级审查)并运行完整集合；
修改 SDK Gap Adapter、相关原生生命周期、模型提供方、飞书租户能力或服务环境时，
只运行受影响的 phase。没有触及这些边界的迭代无需
运行 live probe。

账号额度适配（ADR 0077）变更时，在目标服务账号环境运行
`.venv/bin/python scripts/probe_account_rate_limits.py --live`，并按下文方式加外部
进程 deadline。它只读一次原生额度，不创建 Thread 或请求模型，输出精确 SDK/CLI
版本、结果与窗口数量，不输出账号或余额。省略 `--live` 时使用临时未登录环境，
只验证认证错误路径，不能替代成功读取。飞书 `/usage` 的展示仍需单独验收。

持久 fork、直接恢复或原生删除边界（ADR 0078）变更时，使用
`.venv/bin/python scripts/probe_persistent_fork.py` 并加外部 420 秒 deadline。
它创建一条短模型回复和两个自有持久 Thread，验证完整绑定、同 handle 订阅、
分支零自身 Turn 冷恢复、历史引用拒绝与先分支后来源删除；使用临时本地话题身份，
不调用飞书。临时 cwd 的 trust 仅用公开进程级配置传入，断言用户配置字节不变；
与其他检查配置不变的探针串行执行。未知删除结果不自动重试，保留输出的 exact ID
供人工核查；飞书创建、选群和回调另行验收。

按[职责边界](design.md#netizen-与-codex-的职责边界)分别记录适配行为与原生能力结果。
要求正常回复的 smoke/resume 场景收到 `interrupted` 或 `failed` 时仍记失败，保留首次
失败的版本、exact IDs、原始返回与清理范围；复验结果另行记录。

已执行的版本、覆盖范围及未覆盖边界统一记录在
[兼容性结论](#已验证的兼容性结论)；本节维护检查命令与变更触发条件。

macOS 系统不自带 GNU `timeout`；只在执行 live probes 时先用
`brew install coreutils` 提供 `gtimeout`。最终用户安装和日常服务运行不依赖 Homebrew
coreutils。下面的块会按平台选择命令，并在缺失时明确失败：

```bash
set -euo pipefail

case "$(uname -s)" in
  Darwin) deadline=gtimeout ;;
  Linux) deadline=timeout ;;
  *) echo "unsupported live-probe platform" >&2; exit 1 ;;
esac
command -v "$deadline" >/dev/null || {
  echo "missing $deadline (macOS: brew install coreutils)" >&2
  exit 1
}

probe_cwd=$(mktemp -d "${TMPDIR:-/tmp}/netizen-live-probe.XXXXXX")
test -d "$probe_cwd"
trap 'rm -rf -- "$probe_cwd"' EXIT
git -C "$probe_cwd" init --quiet

for phase in models turn-settings smoke usage steer plan polling compact concurrency interrupt skills lifecycle side; do
  "$deadline" --signal=INT --kill-after=10s 420s \
    .venv/bin/python scripts/probe_python_sdk.py \
    --cwd "$probe_cwd" --phase "$phase"
done
"$deadline" --signal=INT --kill-after=10s 420s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$probe_cwd" --phase release
"$deadline" --signal=INT --kill-after=10s 660s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$probe_cwd" --phase config
"$deadline" --signal=INT --kill-after=10s 660s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$probe_cwd" --phase goal
"$deadline" --signal=INT --kill-after=10s 300s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$probe_cwd" --phase sandbox
```

整个块以任一 phase 非零即失败关闭，并为本轮显式创建、验证和最终删除独立的 disposable
Git cwd；不要复用业务 Project。这里的 live commands 只在需要更新兼容性结论时执行，并且
必须位于与服务相同的账号
interactive login 环境。人工验证先
正常登录 `ssh -t <deployment-host>` 再执行；自动化 remote command 必须显式采用该账号
shell 的 login 模式（Bash 示例为 `/bin/bash -lic '<commands>'`）。普通
`ssh <deployment-host> '<commands>'` 的
non-interactive shell 不等价：它可能缺少 profile 导出的代理/CA/PATH，使 models 等只读
请求成功而真实 Turn 持续等待。诊断环境差异时只比较变量名或摘要，不得把值写入日志。

`make check` 是不创建真实 Codex Thread 的统一本地门禁。另在带 `.git` 的源码 checkout
运行 `git diff --check`；内容寻址的安装快照没有 `.git`，不要在那里执行该命令。按需
live probe 会创建原生 Thread，只在已登录的目标部署账号
执行。每个 phase 会把 started/passed/failed 进度写到 stderr，并只把最终 JSON 写到
stdout；异常路径的二次 interrupt、terminal cleanup 和 task drain 都有界，外层
`timeout` 是整个 phase 的最后兜底。

`make check` 还固定验证公开 `AsyncTurnHandle.stream()` 的 exact
`turn/diff/updated` latest aggregate 文件发现、公开 `ThreadItem.root` 的 completed
`fileChange` 累计统计和 `imageGeneration.saved_path`、完整 add/delete 正文及 validated
update hunk、rename、重复改动与改回原文、缺失 patch 的逐文件降级、跨 Project 路径，
以及公开 child Thread 读取的 v1/v2 关系、递归归属、继承历史排除、去重和有界降级。另覆盖 v3
过期拒绝、当前 v4/v5 完整 Reply Card manifest 的页码表单跳转、固定 SDK 实际
JSON UTF-8 bytes 的逐页容量检查、规范页码验证、单页无导航、无标记的旧 PAGE 解码后
统一重绘页码表单、重启后完整模块和统计保留、100/400 完整统计 manifest、401 明确拒绝、超限时省略
整个 Files 模块、可重复 Card Action 的 per-render nonce、transport-only decoder 与同一 render
重投递去重，以及 Lark
`OutboundImage`/`OutboundFile`/`SendOpts` 合同。任一固定 SDK/Channel shape 变化都必须先
更新兼容性结论，不能把本轮文件降级成工作区扫描、最终文本解析、私有 RPC 或静默截断。

`models` phase 是只读探针：它必须通过公共 `codex.models()` 输出一个且仅一个默认
Model，并列出每个 Model 的默认/支持 Effort、默认/支持 Speed。输出只用于核对本次
目标环境，不能复制进生产代码或文档作为静态选项。它不会创建 Thread 或 Turn。
若返回非空 `next_cursor`，固定高层 facade 无法翻页，phase 必须失败，不能只展示
第一页。

`smoke` phase 使用与生产无引用文本相同的 Current Prompt Message renderer；除了要求
Turn 正常完成、exact ID 可 resume 外，还要求 `thread_list.preview` 以真实请求正文开头。
这只验证原生 preview 兼容性；两名飞书参与者的实际身份归属仍按下文人工验收。

`turn-settings` phase 从同一 live catalog 选择默认 Model/Effort，显式提交 Standard
Service Tier 的 configured Turn，再按 exact Thread ID resume 并重复提交同一组三项
override；两轮都必须完成，并在 `include_turns=False` 恢复后要求模型回忆前一轮随机
marker，验证省略返回历史时的上下文连续性。它是 SDK/App Server 升级时对持久 Binding
配置重复应用的端到端 shape/连续性验证，不声称能读取或证明 Thread 内部当前值。SQLite 持久化、每轮
live revalidation、admission revision 和 steer 不应用由 `make check` 的 synthetic
Runtime/SQLite 测试负责。

`usage` phase 先通过公开 `thread/read(include_turns=True)` 观察 exact Turn 为
`inProgress`，再用公开 read 确认持久化终态，最后排空该 handle 的公开 stream。它必须
收到 identity 匹配的 `thread/tokenUsage/updated`，且 `last.total_tokens` 非负、
`model_context_window` 为正数。这个 probe 验证 `/status` 的先运行后完成时序；首次读取
就已完成的 Turn 由 SDK synthetic 门禁覆盖。外层进程 `timeout`
负责 SDK/App Server 违约时的最终隔离。生产在已确认 persisted terminal 后，对纯元数据
stream 收尾设一秒上限；completion 通知缺失不能阻止失败交付与原会话续聊。
SDK synthetic completion/usage probe 另验证公开 async stream 超时取消会唤醒其阻塞
worker，且不依赖关闭整个 client；这不允许取消尚未确认终态的执行并冒充任务结束。

所有依赖普通 Turn final response 的 live phase 都必须在公开 full-history 中同时看到
terminal status 与 final agent message；若 App Server 短暂先暴露 completed 状态，探针
继续有界重读，不能把部分 materialized Turn 当成最终结果。生产 Runtime 对同一窗口最多
重读 4 次，并保留无文本 Turn 的既有显式兜底。

`make check` 会运行 `probe_sdk_turn_plan.py`：真实安装 SDK 连接 fake App Server，
`PinnedTurnActivityObserver` 先从 exact active Turn 非消费地投影 plan、completed commentary
和 command lifecycle，证明 exact `startedAtMs`/`completedAtMs`、typed command action 的
路径/查询预览、保留区间、顺序、对象身份及订阅者游标未变，且凭据文本未进入投影，
最后由公开 stream 收到同一对象并排空 completion。SDK `0.154.0` 默认关闭原生
`update_plan` 工具；`plan` live phase 只对测试 Thread 通过公开 `thread_start(config=...)`
显式开启 `tools.update_plan.enabled`，不写用户配置，也不改变生产 Thread。随后要求模型先生成
checklist 和至少一种安全 Activity item，在有界延迟 Turn 中接受一次 steer，再观察完整 plan
replacement、最终 steered 回复和终态后公开 stream 中仍存在这些通知。相关 SDK/Activity
迭代必须在合入前解释并处理任一步失败。

每次 probe 都输出实际 `openai_codex_version` 并先运行 facade inventory。若 Goal、
Skills、Side boundary inject、Thread unsubscribe、Apps 或 Thread Delete 出现候选高层 API，
`sdk_gap_facade_migrations` 必须使候选
失败，直到对应 port 切回公开 provider 并删除 shim。`make check` 还会让真实安装 SDK client 连接 fake
stdio App Server，按能力验证 fixed method/params/generated model、Goal 的即时通知与
多 Turn logical stream、resume route-before-mutation、Thread Delete 空响应与 response-loss
unknown、明确 Delete RPC 拒绝、Side 固定 boundary、三种 unsubscribe status 与 response-loss 不重试，以及无
版本/experimental gate；
不能用 mock 私有 helper 代替，也不能因一个能力 shape 失败连带关闭另一个能力。

`skills` phase 在临时 Project 中创建两个受控 Skill，先经 `skills/list` discovery，再
验证一条 Turn 同时携带文本 marker 与两个 typed `SkillInput`，并验证 running Turn 的
typed steer。目录错误、disabled/重名/stale Skill 或 name/path 不一致都必须在 start/
steer 前失败；结果不能复制成生产静态目录。

`lifecycle` phase 只管理自己创建的原生 Thread：完成一个 seed Turn 后，依次用公开 SDK
重命名、归档、恢复。每一步都通过显式 `thread_list(archived=False|True)` 分页目录验证
名称保留、归档只出现在 archived catalog、恢复保持同一 native ID；随后通过薄 Adapter
调用 `thread/delete`，并确认 rollout scan/state-db 两种来源的 active/archived 四视图
全部 absent。该 phase 还创建两个独立 disposable fixture：一个在 archived catalog 中直接
Delete、不先恢复；另一个在 marker Turn 仍为 running 时直接 Delete，不先 interrupt、cleanup、
等待 terminal 或读取 idle。running fixture 的 marker 必须随 App Server removal 退出。
descendant cascade 由 0.154.0 源码复核和 ADR 0037 已记录的真实 root→child→grandchild
实测约束；routine phase 不依赖模型临时生成一棵非确定性 agent tree。探针不触碰任何既有
Thread；delete 响应失败时也不得自动重发。

`0.154.0` 源码契约还会拒绝删除由其它 App Server 持有 writer 的 Thread，或仍被
外部持久 fork 引用的历史。Netizen 不扩大 Project 的 Binding/Side 清单去删除这些外部
对象，也不绕过拒绝；按原生返回保留 Binding，并报告 remaining/unknown 结果，不以目录
缺项收尾删除。live 探针可在已知成功后读取目录验证存储效果，这不属于生产删除对账。MCP 冷恢复探针
只对自己显式创建的已知 fork 先执行 delete，再删除 parent，以遵守该历史依赖；失败
输出安全分类及自有 ID，不能通过重试未知 delete 取得通过结果。

Project 级联删除边界变更还应运行 `.venv/bin/python scripts/probe_project_delete.py`。
该探针在临时 cwd/数据库中创建自己的 Lazy、active、archived 会话和 OPEN Side，并验证
Parent 已被单独删除时的孤立 Runtime Side；检查原生四视图消失、每个 ID 至多一次 delete、
目录保留，以及数据库重开和 YAML bootstrap 后 Project/Side 墓碑仍有效。它只清理自己创建
的样本，不使用既有 Project。应记录运行账户、SDK 版本与模型；必要时可用 `--model`
为探针自己的 Binding/Thread 选择该账户支持的模型，不修改共享配置。此探针不包含真实
飞书 topic 发布或跨主机浏览器传输验收，不能代替这些入口的独立证据。

`release` phase 是普通持久 Thread 空闲订阅释放的原生兼容性探针。它先在 App Server A 创建
并完成一个 Thread，确认 `thread/backgroundTerminals/list(limit=1)` 为空后取消当前连接
订阅，再在同一连接按 exact ID resume 并完成后续 Turn。关闭 A 后，App Server B 必须按
同一 ID 接管、继续 Turn 并再次取消订阅；这证明 Binding 可以保留 ID/历史而无需常驻订阅。
该 phase 不等待也不冒充 App Server 最后订阅者离开后的 30 分钟卸载宽限期。当前协议没有
稳定、无副作用的 live registered-terminal fixture，因此 list 非空、检查错误与 unsubscribe
响应未知的阻断/重试由真实 SDK fake-server harness 和 Runtime 测试作为本地代码门禁。

`side` phase 是 Side 上线的原生硬门禁：先创建并物化 Parent，再启动一个可观察的普通
Parent Turn；在该 Turn 仍 running 时用公开 `thread_fork(..., ephemeral=True, include_turns=False)` 验证 exact
ID、ephemeral 与 parent shape，通过固定 Adapter 注入 boundary，并在 Parent marker 仍存活
时启动 Side Turn，证明 Parent/Side 真并发。随后在同一 Side Thread 连续完成至少两轮，
请求 terminal cleanup 和 unsubscribe，最后证明 Parent 仍能继续并将 Parent 归档。Parent
的 seed、并发 Turn 和 after Turn 都使用公开 full-history 终态恢复；只有 ephemeral Side
使用 `handle.run()`。它不增加 Side 专项 completion-race gate；普通持久 Thread 的
read-recovery 门禁仍由 `make check` 保留。
Runtime synthetic 门禁须覆盖 steer 前刷新先读取 exact completion 并推进 cursor
后，active Turn 保留的 `completion_notification_seen` 仍触发唯一 `handle.run()`；
该标记不能代替 `run()` 的终态返回值。
飞书 topic 能力另做下文五入口 live 验收，尤其不能用 FakeChannel 宣称 P2P Topic 已支持。

自动命名边界或 SDK/App Server 变化时，另在同一 interactive login 环境运行
`timeout --signal=INT --kill-after=10s 420s .venv/bin/python scripts/probe_thread_naming.py`
（macOS 用 `gtimeout`）。它只在 disposable Git cwd 中创建自己的 Parent 和 ephemeral
命名分支，直接通过生产 Runtime 默认入口验证后台补名：确认首轮输入可见后 fork 继承
本轮唯一标记、命名输出无工具调用、临时分支在 active/archived × rollout/state-db
四视图均 absent、成功和中断路径取消订阅，以及父会话
续聊、历史和统计保持独立。独立的中断检查复用同一探针 Parent；不另建一套成功命名流程。
`--runtime-only` 可用于只验证生产路径的局部修改，完整命名兼容门禁仍使用默认模式。
固定 `0.154.0` 的原生验证已确认首轮 ACK 早于输入落盘的竞态；生产只在后台有界等待 exact Turn 输入，
不把 `turn/start` ACK 或 `include_turns=False` 当作上下文已经可 fork 的证明。取消订阅
不证明立即卸载，仍遵循 App Server 的 idle 宽限期。

`goal` phase 是 Goal 上线的硬门禁。它首先创建零 Turn Thread，并用公开 read 证明该
Thread 已是 idle、非 ephemeral 且返回原生 path；这不证明零 Turn 可冷恢复。
这一项失败时 Goal 必须保持 unavailable，
不能用 dummy Turn 或 synthetic 结果替代。随后用有界、无破坏 objective 验证 start ->
pause -> exact physical Turn interrupt -> resume rollover -> terminal -> same-Thread normal
Turn，并由第二个无本地 route 的 SDK client 只读确认 persisted active Goal。探针
按生产四证明确认公开 Thread idle 与 exact 最终 Turn；成功 complete 的 Goal 只 clear
一次并确认 absent 后再验证普通续聊。文件 fixture 显式使用 `apply_patch`，使 aggregate
diff 断言验证原生 `fileChange`，而非仅验证 shell/Python 已写入文件。灰度前还
必须记录目标环境实际 sandbox/approval 姿态，确认原生自动 continuation
适合无人值守执行。进程重启后 external-active Goal 的隔离仍需手工验证；当前版本不会
安全重挂或替用户暂停它。

`compact` phase 创建一条短原生会话，要求公开 `compact()` 的立即 acknowledgement
之后，公开 `thread.read(include_turns=True)` 能观察到 baseline 之后新增的 completed
`contextCompaction` Turn，并在同一 Thread 完成 `COMPACT-AFTER`。只看到空响应或
Thread idle 不算通过；phase 会记录实际状态序列和 compact Turn/item 类型。
生产命令还要求 baseline 后候选唯一，多个候选或 10 分钟无终态均 fail closed。
启动前 baseline 另有独立 5 秒、至多 3 次公开 read 预算；持续 Internal、`notLoaded`
和悬挂 read 的本地门禁必须证明耗尽预算后零 compact 调用、释放 Binding 锁，并保持
已有的全局 admission 状态。这与 compact 已发出后结果未知的失败边界分开验证。

ADR 0009 的 fail-closed 门禁会校验整个 pinned `openai_codex` Python 源码树的
确定性聚合指纹；部署包必须保留 `.py` 源文件。只有 `.pyc`、无法读取源码或任一源码
文件不匹配的安装都会按设计拒绝启动，不能绕过该门禁。

上述版本/指纹规则只属于 terminal inspection/cleanup。Goal/Skills SDK Gap Adapter 按
ADR 0014、Side boundary 与 Thread subscription Adapter 按 ADR 0021/0028、Thread Delete
Adapter 按 ADR 0037 使用 capability shape + synthetic + live harness，不得新增另一套
运行时版本白名单。Delete 的生产调用还必须固定为一个 method，并由 Runtime 承担
present/absent/unknown 对账；同样不得删除 ADR 0009 的既有门禁来“统一”两类 Adapter。

SDK `0.154.0` 保留请求窗口内快速完成 Turn 的通知；原生 `handle.run()` completion
synthetic 探针须验证这一契约。普通持久 Thread 仍以公开 read
核验终态；ephemeral Side 是明确例外，并由
`side` phase 的多 Turn live gate 覆盖。普通持久 Thread 带 `--read-recovery` 的公开 polling
门禁必须通过。
interrupt phase 按 ADR 0010 精确等待 `argv[0] == marker`，执行 exact Turn
interrupt，并为 exact Thread 请求清理 App Server 已登记的后台 terminal。它记录
`foreground_process_exited_within_5s`，但该值是版本能力分类，不是 cleanup 成功
证明，也不是硬门禁。phase 必须观察 native `interrupted`、有界等待自己的 marker
自然退出而不留孤儿，并在同一 native Thread 上完成 `AFTER-CLEANUP` 新 Turn。

### 定时任务兼容性与验收

定时任务的行为与失败边界见[设计文档](design.md#定时任务)。相关变更先通过 `make check`，
再按触及的边界选择 disposable SDK、真实飞书、跨主机 Admin 和安装回滚验收；普通安装
不自动创建计划或发消息。源码检查、传输替身、API 接受和客户端点击分别记录，不互相
替代，也不把旧候选结果当作后来修改边界的验收结果。

2026-09-23 原会话目标以 `gpt-5.6-sol`、SDK/CLI `0.155.1` 通过
`scripts/probe_scheduled_tasks.py --phase binding`：同一持久 Thread 保留之前的随机
暗号上下文，真实 Scheduler → Channel → 普通 Runtime 输入先 start、再 steer 当前
物理 Turn，没有额外 Turn，且保留原 owner/完成消息来源。两次 Run 的输入接收回执
独立收尾。自有原生资源确认删除，用户 MCP 和 Project trust 配置均未改变。
本次飞书传输使用替身，不证明真实主线/话题锚点、catch-up 可见性、客户端反馈或
归档/恢复的完整端到端表现；这些仍须在明确测试会话中验收。

同日扩展的 `--phase mcp` 也通过：同 cwd 的两条原生 Thread 分别完成新话题和
原会话计划的自然语言 CRUD。原会话请求省略 target_binding_id、chat_id 和 project，
通过真实原生调用身份映射 exact Binding；创建及更新后均不保存独立 session_settings。
两个计划始终暂停、没有 Run，未启动 Scheduler；探针所属资源已清理，用户 MCP 与
Project trust 配置未改变，没有真实飞书调用。这不替代飞书客户端入口验收。

2026-09-22 SDK/CLI `0.155.1` 已通过 MCP、冷恢复/fork、dispatch 与手动触发四个
原生 phase。四项均确认用户 MCP/Project trust 配置不变、自有原生资源已清理；
未发送真实飞书消息。真实飞书链路的已验证版本仍为 `0.147.0`，不代表 `0.155.1`
的端到端验收：

- 生产 MCP 框架的真实 CRUD、同 cwd 不同 Thread 的调用身份，以及服务端地址/凭据
  轮换后的冷恢复与 fork。`params._meta.threadId` 可用于 exact Binding 默认值映射，
  不依赖 HTTP header 一定存在。`scripts/probe_scheduled_tasks.py` 提供 mcp、mcp-recovery、
  dispatch、manual 和 binding 阶段；它使用隔离资源和显式模型，只测试原生执行，不发送飞书消息。
- 真实飞书链路的覆盖范围包括五类来源（私聊主线、普通群主线、话题群、私聊转话题、
  群聊转话题）的自然语言默认目标与 Project，以及私聊、普通群、话题群的
  Scheduler → Channel → Runtime 首轮、结果 chat/thread/root 和来源 pointer。
  这不代表任意自然语言都无歧义，也不承诺模型发现与生成能即时完成。
- dispatch 探针覆盖普通原生首轮、exact initial Turn 读取、首次屏障释放、同 Thread
  续聊、停止、归档与删除，并核验用户 MCP 与 Project trust 配置不变。
  新 Thread metadata 尚为空及 full read 内部分页列表暂不可用时，读取按 ADR 0049
  使用 5 秒/3 次 I/O 预算，失败后停止自动读取。
- manual 探针验证真实模型从自然语言定位计划并调用 `run_now`、暂停计划及游标保持
  不变、独立普通持久 Thread、exact initial Turn 完成与结果投递替身。它不代表真实
  飞书点击或客户端渲染已经通过。

2026-09-21 以 `gpt-5.6-sol`、SDK/CLI `0.154.0` 通过 `--phase manual`：一次自然语言
请求只产生一个手动 Run，计划指令/配置、暂停意图与时间游标保持不变，独立持久 Thread
的首轮完成、屏障释放及测试传输回执均确认。探针所属原生资源已清理，用户 MCP 与
Project trust 配置不变；本次没有真实飞书投递、客户端点击或目标主机安装回滚验收。

尚未完整覆盖客户端全部点击路径、移动端布局、原生权限和网络故障组合，以及目标主机
真实失败回滚。当前证据未直接观察 deferred search；固定版本的跨会话 MCP catalog cache
仅适用于 stdio，不给本 HTTP adapter 添加相应兼容层。`0.154.0` 支持普通会话压缩后
续聊，但压缩后的 MCP 管理组合尚未覆盖，不标为通过。

触及对应边界时，候选须完成以下验收：

| 边界 | 验证内容 |
| --- | --- |
| MCP 接入 | 生产传输鉴别、Host/Origin、大小/超时、无 Admin 可用、同 cwd 身份隔离、缺失/冲突元数据、用户 MCP 和指令继承、冷恢复/fork/服务重启与原生权限组合 |
| 调度与存储 | 四类规则、时区/DST、截止、高水位、宽限/missed、两类目标的交接屏障、原会话未知不阻塞下一到期点、手动不改时间游标、不同计划并发、CAS/幂等、裁剪后原 Run 回执及每个交接断点的重启行为 |
| 原会话输入 | exact Binding/Turn 身份、空闲 start 与运行中 steer、Goal 换轮、配置/上下文竞态、系统来源、原发起人及结束提及保留、catch-up 锚点与接收后游标提交 |
| 飞书与普通生命周期 | 五类来源的自然语言和 /cron；真实 root/seed 或原位置锚点、结果精确归属、卡片完整表单/重试/分页、Activity/Files、停止/归档/删除及极快终态；原会话切换/归档暂停、恢复不补跑及删除联动 |
| Admin 与安装 | 跨主机认证/CSRF、三个入口一致性、Project 删除与在途交接、App 切换、当前库重装、受支持旧库迁移与不受支持库只读拒绝、启动迁移提交前后失败、实例恢复报告、内置 Skills 来自实际安装包 |

服务只新增同一 background loop 内的 Scheduler 和 loopback 动态端口 MCP；无需用户安装
新 Skill、手动配置 MCP 或启动另一个服务。公开 CodexConfig 只追加本次进程专属随机
MCP entry，环境完整继承后仅增加一个随机名称的临时 bearer key；不改用户 config.toml
和其他 MCP。端点先于 App Server 初始化，管理与调度在 shared application ready 后开放，
Admin 关闭时 MCP 仍可用。停止先关闭认领和管理 admission、排空在途交接，再执行既有
普通 Turn shutdown 并关闭传输；重启不补跑错过的时间，也不重发结果未知的执行。

启动边界验收包括当前 schema 显式初始化、受支持旧库迁移、不受支持库只读拒绝、
元数据与 Side/Project 墓碑保留。迁移持有 lifetime lock；提交后失败不自动恢复旧数据库。

### 持久分支、恢复与删除验收

[ADR 0078](adr/0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md) 的三项边界
分别验证，不能用临时 Side 成功代替持久 fork，也不能用目录展示代替恢复或删除的结果。

| 范围 | 自动化与原生验证 | 飞书客户端验证 |
| --- | --- | --- |
| 创建与继承 | exact idle 来源、原生历史/权限、同 cwd、显式设置复制、目标 catch-up 新边界；零新增 Turn 的完整 Binding 与原 handle 订阅/释放 | 当前聊天与其他群各一个新话题；创建中和成功更新同一根卡；名称、链接和来源保持正确 |
| 群选择与提交 | 复用共享目录查询/复核，来源/current/revision 变化、目标不可用或占用时不覆盖；公开表单区分搜索/选择/创建 | 搜索、翻页、下拉和最终名称；同类公共卡片去重，受理后禁用提交、成功移除按钮 |
| 创建交接 | root/seed 响应阻塞时提前输入；默认配置先创建则 fork 冲突，fork 先提交则输入进入分支；每阶段失败、Project 删除和 shutdown | 提示等待完成后再发消息，失败只报告已知事实，已成功会话不会因回执失败被撤销 |
| 普通恢复 | `activate_exact` 直接 resume，无 list/read 预检；未发新消息且列表缺席分支、Lazy、运行中 rejoin、明确拒绝、未知结果、本地提交失败和旧输入失效 | `/resume`、列表切换、Admin 同一语义；成功后反馈，拒绝保留 current，不卡在目录“未确认”状态 |
| 普通删除 | 成功才删 Binding，RPC 拒绝保留并清理失效投影，传输未知局部隔离；零自动 list/read/retry；来源受分支历史引用限制 | 普通/归档/分支相同确认与反馈；不会把源会话删除解释为独立分支级联删除 |

真实原生持久性验证仅使用 disposable cwd 和自建 Thread：有历史的来源创建持久 fork，
在分支尚无新增 Turn 时同连接 resume、关闭首个 App Server 后冷 resume，核对 exact ID
和继承上下文。目录可能漏列，但不影响直接恢复。随后验证来源的历史引用删除拒绝，
只清理本次自建且身份明确的资源，不能重试结果未知的原生删除或按名称猜认资源。
`/fork` 的五种来源入口（P2P、P2P 话题、普通群主线、普通群话题、话题模式群）和跨群
真实发布需单独验收；不以 FakeChannel 或 native-only 探针宣称通过。

`/usage` 在无 Project/Binding、普通群话题和有效 Side 分别验收，群聊保留 @ 规则。
由客户端显示同一 epoch 的日期、时间和时区；至少在两个设备时区核对 `local_datetime`。
确认有限额度的剩余条与百分比一致、普通 Codex 额度优先、实际存在的模型/Credits/月度
字段展示正确，缺字段不显示为充足或零；未登录/超时使用现有失败反馈。无需为此创建
任务、购买额度或触发 reset。MCP 指南只核对原生登录与同环境凭证复用说明；文档存在
不表示已验收真实 OAuth。

### 数据库迁移与中断恢复验收

[ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md) 将迁移移到每次实际
启动，保留 ADR 0075 的 v14 基线与冻结步骤。当前仍为 v14，没有虚构生产 v15；
旧 schema／布局的转换不因此自动得到支持。以下为新 CLI 的验收要求，不是通过记录：

- setup 才创建新库；start、自动重启及恢复启动都校验已有库，缺库不能重新初始化。
- lifetime lock 从迁移准备持续持有到服务退出；同实例并发启动、查询和更新不得成为
  第二个 writer。Runtime 仅在完整校验后开放输入。
- 冻结历史夹具覆盖相邻及跨版本路径、关联记录和墓碑、结构／约束等价性。较新／未知、
  坏约束及缺路径拒绝业务启动，不改库。
- 只在有迁移时备份；事务内异常／中断使全部步骤和版本一起回滚。事务提交后即使
  配置、SDK、权限、端口或 ready 失败，也保留新库，不自动还原备份。
- 确定的配置／schema 准入失败在受管入口不发布 ready，也不进入失败自动重试循环；
  CLI start 仍报告失败，手工 Runtime 启动非零退出。修复后显式 start。
- 包更新成功但部分实例启动失败逐项报告，不回滚其他实例；运行／停止与自启意图区分。
  purge 中断保留阶段证据，不能误判为空的新实例。

每个平台记录 exact Python／包工具／CLI 版本、源目标 schema、故障点和最终状态。
内存 SQLite、fake manager 或旧安装器成功均不能替代新 CLI 的实机证据。

#### 历史证据：旧 release 安装事务（不适用于 CLI 验收）

2026-09-28 的隔离实机记录：`tests/support/deployment_migration_probe.py` 在 macOS
LaunchAgent 与 Linux systemd user manager 各通过 active/stopped 升级、admission 前
失败回滚、admission 后保留新数据并 exact 恢复，以及首次安装在服务定义发布前因端口
冲突失败后的完整回滚与重试，共 10 个场景。两平台使用相同冻结代码摘要
`8e20c46d652f7598fa11584b36d00c98a6dc14b32abe81b4b0b9b763e6da6494`，覆盖当前
guarded-current／`database_restored` 恢复路径及本轮轻量简化；原有业务夹具值与完整性
校验通过。首次安装由生产建库逻辑创建 v14；Linux 缺失 unit 的真实查询返回 0，状态
为 `not-found`／`inactive`，安装器随后仍确认 lifetime lock 释放。临时
服务与夹具均已清理。探针使用真实服务管理器和生产安装／迁移代码，服务本体是最小
夹具；不加载 SDK 或飞书，不证明正式 Release 下载、完整 Runtime 或消息端到端行为。
SIGKILL、WAL 半恢复及旧服务重启后新写入由真实 SQLite／子进程故障测试另行覆盖，
不记作平台探针覆盖。该探针随旧 release 部署链退出当前源码，可在 Git 历史中查阅；
不再作为新 CLI 的可执行验收入口，不能直接复用这条历史通过记录。

### 会话默认配置与自动创建验收

行为见[聊天默认配置](design.md#聊天默认配置与自动创建)及
[ADR 0073](adr/0073-create-sessions-from-chat-defaults.md)。相关变更先通过 `make check`，
覆盖 App 隔离、精确匹配优先、群名规则顺序及大小写、配置失效引导、创建竞态和输入准入。
本功能复用现有 native lifecycle，不因增加默认配置重跑无关原生 phase；触及 SDK 或
Adapter 边界时仍按[既有触发条件](#代码门禁与按需实时兼容性验证)选择受影响 phase。

以下为本功能相关边界的待验收项目，文档和本地替身测试不代表已经通过真实平台验收：

- 飞书单聊、普通群主线和真实话题中打开 `/defaults`，确认卡片明确显示所属聊天；验证
  精确配置回填、群名规则继承回填、未配置空态，以及同一表单保存和删除。继承项保存后
  应成为本聊天的精确配置，删除不得修改群名规则；并发修改后的旧卡提交须按 revision
  明确拒绝。
- 在受信内网浏览器验证 Admin 精确配置的逐条管理、群名规则创建／修改／删除／调序，
  与飞书入口共享配置；顺序改变后新会话使用新的首条匹配，已有会话不变。继续核查
  认证、Origin/CSRF 与陈旧页面保护，不提供精确配置批量设置或即时 Prompt 入口。
- 在明确测试聊天中验证无当前会话时普通消息自动创建并执行，主线与两个话题互不串线；
  群聊仍逐条 @，单聊无需 @，`/new` 保留原有表单和手动另建语义。无匹配或默认配置
  失效应给出原有引导，不执行这条任务；失效规则不改为下一条规则。
- catch-up 首轮使用真实触发消息作为初始边界，当前正文／引用／图片正常提交且不补读
  更早讨论；随后在群主线与话题分别验证从该消息之后读取。继续满足既有 chat/thread
  history rollout gate；本地“lower 等于 upper”的测试不能替代真实消息及客户端验收。
- schema v14 新库完整初始化、当前库重装保留默认配置和规则顺序，v13 及更早库只读
  拒绝。后续版本迁移遵循 ADR 0076；启动迁移持续持有 lifetime lock，不提供自动旧库恢复。
  Project 删除后默认配置保留，使用时按不可用提示降级，不新增联动删除。

真实群聊消息和客户端操作须在指定的测试环境验收；缺少该条件时分别记录未验证范围，
不能用 synthetic 结果声明飞书卡片、catch-up 或目标机安装回滚已通过。

### Admin 群选择器验证

2026-10-08 在显式选定的当前实例使用同一机器人凭据完成只读验证：群列表返回 8 项，
选中群的成员关系及详情校验通过，按群名搜索返回包含该群的结果。没有发送消息、加入群、
修改应用权限或持久化群目录。实测 v2 搜索的 `chat_mode` 为 `DEFAULT`，与文档示例的
`group/topic` 不同；搜索结果保留未知子类型，选中后仍通过成员检查和 v1 详情确认群聊。

本地 Chromium 配合模拟管理接口验证了关键词分页、单聊手填提示、跨页草稿恢复、从
Sessions 创建精确 Binding 计划及保留筛选状态，并检查桌面和移动宽度下的抽屉。
这不是部署后 Admin 认证链路或真实计划创建的端到端通过记录。当前实例群数不足以触发
真实接口第二页；分页、权限失败、机器人退群和竞态仅有自动化替身覆盖，未在真实群中
制造这些状态。不得由这项只读验证推导出消息投递、计划执行或部署验收已通过。

### 结束提及的客户端验收

结束提及见 [ADR 0063](adr/0063-mention-task-initiators-in-terminal-results.md)。候选需要
分别验证私聊、群主线和话题：运行卡与终态更新均不含 @，结果送达后单独发送真实 @ 的
短文本，以实际结果卡作为回复锚点。私聊和群主线使用普通引用回复，已有话题保持原话题。
测试者应先读过运行卡，再切离聊天，检查提及、未读标记与客户端通知，并确认普通引用
回复没有创建话题或产生额外的话题通知；文件翻页和 Goal 控制重绘不再次通知。
Goal 需分别验证进度卡开关两种配置：已有卡片更新后都应独立提醒。新发送的最终回复、
卡片不可用或更新失败后的新回复，以及 Goal 正文过长另行回复时，直接在新回复内提及，
不再单独提醒；旧卡更新失败不消耗新回复的首次 @ 机会。
现有本地测试和 SDK 出站序列化只能证明真实 at 节点与请求归属，
不能证明客户端通知。当前这项客户端通知验收尚未执行。

可先运行无网络、无凭据读取的预览，再在明确的测试会话运行同一探针：

```bash
.venv/bin/python scripts/probe_feishu_completion_mention.py \
  --config /absolute/path/config.yaml --chat-id oc_test --user-id ou_test --dry-run
```

去掉 `--dry-run` 后，探针会发送一张明确标记的验收卡，默认等待 15 秒，再更新同一张
卡片，随后单独发送一次引用该卡片的 @ 提醒。预览会展示卡片更新、提醒正文、提及对象和精确
回复选项。可通过 `--reply-to-message-id` 与 `--reply-in-thread` 指定已有话题的精确
回复锚点；`--delay-seconds` 可在 0–60 秒间调整。探针校验卡片与提醒返回的消息、聊天、
话题、根消息与父消息身份；普通引用回复须无话题且父消息为卡片，已有话题须保持原话题。
失败或结果未知时不补发、不换位置。API 成功时仍输出
`client_notification_verified: false`，需要被提及的测试者确认客户端效果。普通升级不自动
运行这个会发消息的探针，测试目标和通知观察结果按实例私有验收记录保存。

### 多实例与内置 Skills 验收

ADR 0076 改变程序归属、服务绑定与数据准入。以下要求须以新 CLI 单独验收，不复用旧
安装器的历史绿灯。先运行 `make check`；包资源门禁必须覆盖 wheel、sdist 重建、包外 cwd
和隔离导入，不允许依赖源码目录或实例 release。

Linux 和 macOS 均需同一账号两个明确 root、两份独立 Python 安装，验证 setup 不启动、
start 等待 ready、stop 确认退出、remove 保留数据、--purge／-y 精确范围、B 控制 A 但仍
运行 A、remove→B start 的显式切换。服务定义损坏、环境消失、stop 超时、锁占用、
stale ready／runtime identity 和异常 manager 查询必须失败关闭，不能误接管。

环境更新分别验证普通 pip、uv pip、uv tool 的有限安装识别；自定义目录、symlink、
同名多份安装、缺损元数据、无 pip 环境及不支持工具版本需要明确分类。pip／uv pip
可靠无变化不停止实例，uv tool 允许无变化重启；停服失败不补偿、包失败不盲启、部分
恢复失败不全局回滚。更新期间外部启动／外部包写入不在强互斥保证内，报告不得伪造状态。

内置 Skills 仍只注册到同一初始化 App Server 的额外根，不写 config.toml 或全局 Skills。
只读发现可用：

```bash
.venv/bin/python scripts/probe_skill_roots.py --source-root "$PWD" --timeout 15
```

这只验证 catalog 与路径，不证明模型实际执行。SDK／adapter／资源加载边界变化时，
验证普通新建／冷恢复、Side/fork、Goal 后续 Turn 与 native 子 agent 的实际加载，
自然匹配和显式调用都须覆盖；两实例额外根不串线，Lark Skill 使用所属 root，
资源损坏或注册失败不得 ready。普通用户同名、禁用、歧义 Skill 保持原生规则。

同时验证双 Admin 登录与 root 标识、端口首次实际分配及固化、端口冲突不漂移、显式
端口／禁用 Admin、服务 profile 导出别的 root 后仍重申绑定。Project、用户 Codex
状态与外来文件不得因其他实例生命周期而被修改。

### 已验证的兼容性结论

实例专属的主机、账号、PID、native ID、release/备份路径、数据库行数和私网访问结果不属于
公共部署契约；维护者应把这类记录保存在被忽略的 `LOCAL_ENVIRONMENT.md` 或自己的运维
系统中。下列结论用于维护当前兼容边界和选择受影响的 live probe，不能替代具体候选的
验收或目标主机自己的 Host Validation。变更原因和验证摘要保留在 commit/PR 说明中，
单次运行的日志、失败诊断和复验过程留在验证产物中。

SDK 精确依赖以 [pyproject.toml](../pyproject.toml) 和
[requirements.lock](../requirements.lock) 为准。以下证据保留实际验证时的版本；旧版本的
结果不会自动变成新版本、真实飞书链路或目标主机的验收结论。定时任务另见
[专属兼容性记录](#定时任务兼容性与验收)。

2026-10-08 至 10-09 审查并迁移 SDK/CLI `0.160.0` → `0.161.0`。比较官方两个精确版本的完整
Python 包（22 个文件），仅 generated models 和 notification registry 两个文件变化；
公开 facade、client/router、Goal/Turn 消费与取消实现未改变，没有可直接替换现有窄适配
的新增高层 API。两个 pinned adapter 使用已复核的 Python 源码树指纹
`ab78afdc53e5cad9c812066f93a08927ac3bed3246471f5df498d9cb01f4a36e`。
SDK wheel SHA-256 为 `41823fb522572bcbd5acee7123947ba81d7eb60c69e9b237`；
版本与完整原生差异依据[官方发布](https://github.com/openai/codex/releases/tag/rust-v0.161.0)
和[固定 tag 比较](https://github.com/openai/codex/compare/rust-v0.160.0...rust-v0.161.0)。
原生差异已枚举并重点复核与 Netizen 有关边界，不代表逐行审查全部 Rust 或运行上游测试。

| 变化或能力 | 迁移决定与边界 |
| --- | --- |
| 开放式 `CodexErrorInfo` 字符串/对象 | 保留受限错误投影；回归 unknown、损坏 known 与 HTTP variant，不能展示任意对象或改变 exact Turn 归属。 |
| `thread/prediction/updated` | 允许 SDK 注册/解析；`sourceTurnId` 不视为活动 `turnId`，不生成终态、Activity 或回复模块。 |
| Goal set/clear `origin` | **本次不接入的明确缺口。** 协议支持，但现有 SDK Goal helper 不透传；保留生命周期，缺少新增用户来源记录。待原 helper 可透传或公开等价 API 可用时重新评估，不另写 Goal 编排。 |
| MCP OAuth `loginId` 与 TUI `/mcp login` | 原生处理登录关联；Netizen 仅补登录/凭证复用指南，不加 OAuth UI、凭证库或通知消费者。 |
| 账号额度 `/usage` | 已有原生能力的可选产品接入，按 ADR 0077 只读同次额度快照，飞书 Card 2.0 使用阅读者时区。 |
| 普通持久 `/fork` | 已有公开能力的可选接入，按 ADR 0078 使用普通 Binding 和共享群目录；配套直接 resume/native-response delete 不属于 SDK 必需协议迁移。 |
| 动态模型目录、API-key discovery、原生 retry/fallback、权限及存储修复 | 继续由 Codex 管理，不硬编码默认模型、复制配置或增加 Netizen 重试。API-key/Bedrock、特定 provider 和权限分支须有专属证据，普通账号 smoke 不代表已覆盖。 |
| TUI/音频/其他客户端能力 | 不自动接入飞书；标题/摘要搜索沿用现有 Admin，最近回答沿用飞书消息记录。 |

兼容债务逐项保留：Goal、Skills catalog/extra roots、Side boundary、unsubscribe、Delete
仍缺等价高层接口，沿各 ADR 的 capability/synthetic/live 和公开替代触发器迁移；新增
账号额度适配遵守相同规则。cleanup/后台 terminal inspector 与非消费 Activity/question
observer 的所有权、方法、retained events 和指纹门禁保持。原生 replay/abort 改动不能
证明客户端的普通 Turn 有界 read 恢复、interrupted 复查、终态正文补读、compaction 唯一
候选归属或命名输入可见性等待已不必要，因此保留这些措施。Lazy Binding、Goal 重挂与
clear generation CAS、完整 Plan/Apps/idle settings/models 分页等公开能力缺口未解除。
原生 cold resume 后继续 active Goal 在 `0.160.0` 已存在，本次不借升级增加修复或保护流程。

当前 `0.161.0` 本机 macOS arm64 的合并实现通过 `make check`：2,665 项测试、
16 项按条件跳过，含 wheel/sdist 隔离验证及 SDK synthetic 门禁；新协议、额度、持久分支、
直接恢复与删除结果处理的行为回归通过。额度排序修正另通过 15 项相关测试；
Admin 的 60 项 HTTP 回归通过，包含目录漏列的当前分支实际提交归档、删除确认。
跳过项为平台特定及需显式启用的包管理器探针。
全部 17 个原生 phase 已通过：models、turn-settings、smoke、usage、steer、plan、polling、
compact、concurrency、interrupt、skills、lifecycle、side、release、config、goal、sandbox。
只读账号额度 probe 已验证登录账号返回成功，临时未登录环境返回认证错误。
额外命名、Project 删除 mixed-sessions/orphan-Side、定时 Binding 输入及六项 Skill roots
实际执行专项通过。真实 Runtime/Store 持久 fork 验证了原位置不变、完整原子绑定、同
handle 订阅、零新增 Turn 的冷恢复与继承上下文、原生历史引用拒绝删除来源并保留 Binding，
以及先删除分支再删除来源。该探针使用临时本地话题身份，不代表飞书发布链路。
对应流程已沉淀为 `scripts/probe_persistent_fork.py`，通过编译和 help 检查；原生证据
来自等价临时探针及同一对资源的接续，未为脚本落盘重复请求模型。
早期并行探针因原生为临时 cwd 自动保存 trust 而未通过配置不变断言；定时输入改用
公开进程级 exact cwd trust override 串行复验通过，用户配置的字节摘要不变。没有为此
修改产品配置策略或放宽探针断言；首次失败仍保留在运行证据中。
本轮能够证明归属的三个临时 cwd trust 项已精确清理，保留其他配置，没有整文件回滚。
真实飞书 `/usage` 的客户端时间展示、跨群 `/fork` 卡片交互尚未验收；以上不代表已发布、
部署或重启服务。真实 OAuth、API-key/Bedrock 和未执行的权限专项不在上述通过范围内。
本轮 Scheduler 专项仅执行了 `binding`；`mcp`、`mcp-recovery`、`dispatch`、`manual`
以及独立 child-files 专项没有本版执行记录，不能以 17 phases 或 Skill roots 子任务验证替代。

历史基线：2026-10-08 新增按需账号额度读取（ADR 0077），当时 SDK/CLI 保持 `0.160.0`。`make check`
通过 2,565 项测试（16 项按条件跳过），包括 wheel/sdist 与隔离资源、SDK synthetic 门禁。
额度适配专属的 13 项测试同时通过 `0.160.0` 和候选 `0.161.0`，含真实 SDK client
往返、legacy 严格校验及超时/取消后的连接复用。`0.160.0` bundled App Server 的
只读 live probe 已验证已登录账号成功返回额度窗口，临时未登录环境返回认证错误；
未创建 Thread 或请求模型。真实飞书 `/usage` 展示与其他部署账号尚未验收，以上不表示
已经升级到 `0.161.0` 或完成该候选的全部升级门禁。

2026-10-05 SDK/CLI `0.160.0` 通过当前代码的 `make check`（2,505 项测试，16 项按条件
跳过，含包构建及 SDK synthetic 门禁）。10 月 4 日同版本候选已通过全部 17 个原生
phase、完整 Thread naming、额外 Skill roots 实际执行和新增行为专项；对应 SDK
适配器、Runtime 与探针未变。上述本机证据不代表真实飞书或目标主机部署已验收。

与 `0.159.2` 相比，Python SDK 源码及指纹未变，没有新增可替代现有适配器的公开
高层 API。本次仅同步精确版本声明，保留现有适配器与恢复措施，无业务或数据迁移。
原生显式 `model_catalog_url` 失败或对应 discovery 未开启时不再回退 bundled 目录；
Netizen 沿用空目录拒绝，使用自定义目录的部署须验证其 endpoint 和 discovery 配置。

2026-09-30 SDK/CLI `0.159.2` 已通过 `make check`（2,482 项测试、16 项按本机环境
跳过，含包构建/隔离资源验证、编译、依赖与全部 SDK synthetic probes），以及
`probe_python_sdk.py` 全部 17 个原生 phase、完整 Thread naming 与 Project delete
两种场景。两个 pinned adapter 的完整 Python
源码指纹为 `ce2e5e94cf00a499ae03b31e70ace1ca889501e5c0a64e62b6448c2516aae65f`；
源码复核确认 client/router 的所有权和 retained-event 形状未变，facade migration
inventory 为空；Project config 仍分类为 `hot-reloaded`。

同日补做零 Turn 持久性评估：`thread_start` 返回的 Thread 虽可用
`read(include_turns=False)` 读取 idle、非 ephemeral 和 path，但这些 metadata 不能证明
rollout 已落盘。独立临时 Thread 实测中，创建后等待两秒、只读摘要后等待两秒，以及
关闭首个 App Server 再用第二个恢复，`thread_resume` 均返回 `no rollout found`；
`include_turns=False` 也不能绕过。完整 history read 另返回 `list_turns is not supported yet`，
不能拿失败读取的潜在副作用当持久化接口。新版空 Thread 归档之所以可用，是
[固定版本原生源码](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/app-server/src/request_processors/thread_processor.rs)
在归档前主动调用内部 persist；“先归档再恢复”实测可让零 Turn Thread 恢复，但属于额外
生命周期操作，不作为普通创建方案。因此继续保留 Lazy Binding；移除的前提是公开创建
流程能在没有真实 Turn、额外归档恢复或私有 RPC 的情况下，可靠支持 exact ID 的同连接
及冷恢复。既有 Goal 路径直接沿用刚创建的 handle，不能作为这一恢复能力的证明。
本次试作已撤回，SDK 升级及此前修复保留；探针仅操作自建临时 Thread，未发送模型请求。

本轮保留了 interrupt、release 和命名的首次失败记录。带诊断的独立观测确认：恢复后
新 Turn 的公开 read 可短暂返回 `interrupted`、有开始时间而无完成时间，随后同一 exact
Turn 变为 `inProgress` 并正常 completed；不能以首次状态立即结束观察。普通 Turn、
Scheduled 查询、压缩与探针对首次 `interrupted` 固定等待 2 秒再读取同一 exact Turn
一次，以第二次有效读取结果继续原流程，不再依赖 timing 字段。复查失败沿用已有
观测失败边界；首次结果不能代替确认。携带原生 error 的已确认中断
会显示过滤后的原因，仍允许同 Thread 续聊。命名复验观察到 exact 输入接近原 5 秒
期限才可见，因此局部等待扩大到 10 秒，整体 120 秒预算不变；未断言首次命名失败的
确切原因。简化为两秒复查后，受影响的 interrupt、release、usage、polling、compact
五个原生 phase、命名 Runtime 路径及定时任务 dispatch 再次通过；dispatch 覆盖
exact 初始状态读取、停止、续聊与归档删除，飞书传输使用替身，用户 MCP 和 Project
trust 配置不变。首次记录、诊断实验和复验结果分别留在验证产物中。

额外 Skill roots 还完成真实模型执行验证：普通新建和冷恢复自然匹配、ephemeral Side
显式 typed Skill 调用、Goal pause/resume 后续物理 Turn、直接读取原生 child 的 final，
以及同 HOME 两 App Server 注册同名不同根的实际隔离。该专项使用一次性自建 Skill，
不代表 Lark 真实调用、同名/禁用/歧义规则或 Goal 自动 rollover 的验收。以上原生探针
使用本机登录账号和隔离临时 cwd，不覆盖真实飞书客户端、目标主机安装或正式发布；
没有部署或重启 Netizen 服务。

2026-09-24 Binding/Side 共用问答交互通过 `make check`（2,211 项测试、编译、依赖与全部
SDK synthetic probes），最后补充的错误反馈分支另通过 13 项问答目标矩阵。原生 Side
问答探针及一次限定复验均未收到结构化问题；复验在中断/清理前确认 exact Side Turn
自然 completed、无执行错误，但原生 items 的 `questions` 数量为零。因此不能把这两次
执行记为原生 Side 问答通过，也不能据此断言 SDK 不支持；该实时链路与真实飞书卡片
展示/点击仍待验收。探针只使用隔离 cwd 和自建 Thread，已清理并归档自建 Parent，
没有部署或重启服务。
独立 Side Runtime smoke 已通过 SDK/CLI `0.156.1`：注册问题 handler 且关闭 Progress Card
时先自然完成普通 Side Turn，再在后续 exact 命令运行中 close，确认原生 interrupted、
cleanup/unsubscribe、closed 墓碑和 registry 清除；这证明修改后的观察/关闭链路可用，
不替代上述结构化问答与飞书验收。

2026-09-24 SDK/CLI `0.156.1` 的后台命名改用公开 `output_schema` 后，
`probe_thread_naming.py --runtime-only` 通过：生产入口生成有效标题且无工具调用，
临时 fork 在四种目录视图中均不可见、取消订阅完成，父会话历史与后续对话不受污染。
本次局部验证未重跑独立中断场景；模型介绍折叠面板尚未做真实飞书客户端展示验收。

2026-09-23 SDK/CLI `0.156.1` 已通过 `make check`、固定 SDK synthetic probes、
`probe_python_sdk.py` 全部 17 个原生 phase、完整 Thread naming 与 Project delete
两种场景。两个 pinned adapter 的完整 Python 源码指纹更新为
`7d0a2267e45d39934c64d2c01bddde8b36eacb2d771dd6903a056c6abef8e94f`；源码复核确认
client/router 的所有权和 retained-event 形状未变，facade migration inventory 为空。
独立的原生问题探针还确认 `agentMessage.questions` 经非消费 observer 投影后仍保留在
公开 stream；原提问 Turn 完成后，使用 `0.156.1` 的
`send_user_message_question_reply` 格式在同一 Thread 启动后续 Turn，模型正确采用
所选答案。探针使用本机登录账号和隔离临时 Git cwd；这些证据不覆盖真实飞书卡片
投递/点击、目标主机安装或正式发布，没有部署或重启 Netizen 服务。

2026-09-22 SDK/CLI `0.155.1` 已通过 `make check`，以及 `probe_python_sdk.py` 的完整
17 个原生 phase、Thread naming 和 Project delete probes。Goal phase 额外验证当前
物理 Turn 可追加消息、resume 后旧 expected Turn 明确拒绝、新 Turn 的追加改变最终
回复，同时保留原 Goal 文件 diff 与同 Thread 后续对话。两个 pinned adapter 的 SDK
源码 fingerprint 与前一验证版本相同，仅更新 exact version；其他能力仍按 shape 与行为门禁验收。
相关本地回归覆盖追加回执未知时保留已确认的最终回答、模型自行暂停时不误报后台终端清理，
以及 Goal 清单和分页。原生 probes 使用本机登录账号和隔离临时 Git cwd；本次证据不覆盖
飞书客户端投递、目标主机安装或正式发布，也没有部署或重启 Netizen 服务。

- SDK/CLI `0.154.0` 支持公开压缩终态确认及同连接、同 Thread 后续 Turn，`/compact`
  可用。唯一候选、启动前有界 baseline 与结果未知时的失败边界见 ADR 0013。
- child/fileChange 支持新子任务 patch 归属、child→root 消息和 v2 空目标 wait；祖先
  patch 不混入本轮统计，无法定位本轮 Turn 的旧 child 使总计保持未知，见 ADR 0056。
  root 归档由 App Server 级联处理后代。fixture trust 清理的成功、失败和取消分支
  有 synthetic 覆盖；原生自动新增 trust 后再移除的 live 路径尚未验证。
- `include_turns=False` 的 resume 保持 exact Thread 和模型上下文连续性；该选项仅省略
  返回历史。普通持久 Thread 需要终态或 Files 证据时另做公开 read；ephemeral Side
  fork 同样使用此选项，但仍由唯一 `handle.run()` 确认终态。
- 原生 `update_plan` 默认关闭，plan fixture 显式开启；生产 Thread 继承用户工具配置。
  checklist 整体替换与非消费 Activity 观察必须保留同一公开 stream 的原始通知。
- Project 删除的原生兼容性覆盖 mixed-sessions 与 orphan-Side；四视图 absent、
  Side 关闭、跨重启 tombstone 和 cwd 保留是验收要求。真实飞书 topic 与浏览器传输
  不在这项原生探针的覆盖范围内。
- SDK `0.154.0` 验证时的精确源码指纹为
  `9db021b08bbcc75f18206d64ecf8a7d5ba63b380d91181718a3c9153ed4a053f`。
  该值属于 ADR 0009/0020 的版本兼容门禁，不是某台主机的环境配置。
- foreground tool process 不属于 background-terminal registry；
  `interrupt` 和 terminal cleanup 成功不证明前台进程已退出。
  `foreground_process_exited_within_5s=false` 是受支持分类，但 native Turn 必须进入
  `interrupted`、same-Thread resume 必须成功，probe 自己不得遗留 marker。
- SDK/CLI `0.154.0` 的 Project config 探针分类为 `hot-reloaded`：同进程及重启后均读取
  新配置，探针不修改用户全局配置。未来版本的 `restart-required` 仍可接受，但须更新
  兼容性结论。
- sandbox probe 只报告 `workspace-write-or-full` 或 `read-only-or-denied` 的端到端
  体感分类，不识别配置来源，也不能替代目标账号的真实权限验收。
- Admin Web 首次上线或相关边界变更时，必须从另一台受信内网主机直接验证 readiness、
  login、CSRF/Origin/Host、
  inventory、mutation、restart session invalidation、平台日志脱敏和数据库完整性；
  使用 `http://<server-ip>:<port>`，不得把真实实例地址写回本文。
- non-interactive SSH 可能缺少账号 profile 中的代理、CA 和 PATH。所有 live probe 必须在与
  服务相同的账号 login 环境运行，只比较必要变量名或摘要，不记录环境值。
- 修改 migration、rollback、installed-source equality、database integrity、service ready、
  Linux `NRestarts` 或 macOS failure restart、遗留进程等边界时，必须针对变更候选重跑对应
  验证，历史绿灯不能作为新边界的证据；新 CLI 不继承旧 installer 零退出的安装合格结论。
  未触及相应 live 边界时不机械重复整套实机验收。

`--phase config` 只在给定测试 cwd 下创建临时 Project，验证同一 App Server 的
Project config 重载后自动清理；它不会读写全局 `config.toml`。分类为
`hot-reloaded` 时表示同进程可读取新配置；`restart-required` 也是受支持结果，但必须
更新兼容性判断并按重启语义验收。用户级
`~/.codex/config.toml` 不由 Netizen 监听；修改官方要求重启的键后，可在 Admin 系统维护页
点击“重启服务”，或执行下列命令。重启不保证所有配置作用于已有 Thread：

```bash
netizen restart --root "<NETIZEN_ROOT>"
```

然后重跑对应 probe，不能拿重启前的分类替代新配置的实际效果。

`--phase sandbox` 同样只使用临时 Project：它不向 SDK 传 sandbox/approval override，
也不写任何 Codex 配置，只通过一次 cwd marker 写入报告当前实际权限为
`workspace-write-or-full` 或 `read-only-or-denied`。这是端到端体感分类，不声称识别了
具体配置层；修改用户级 profile 后可重跑比较。

不要把某次 sandbox 分类固化成现役状态，也不能拿全局 CLI 的显示替代 SDK 探针。
需要改变 Codex sandbox/approval 行为时，修改目标账号的用户级 Codex 配置、按配置语义
重启服务，并重跑此
phase，以新结果为准。

## 发布正式 Release

发行包为 PyPI 的 `netizen-cli`。[0.10.0](https://pypi.org/project/netizen-cli/0.10.0/)
已通过本流程正式发布，并完成公开索引隔离安装验证。代码开发、构建 wheel／sdist、
通过 CI 不等于上传或正式发布；后续发布仍须由维护者显式触发。

正式发布前，维护者须确认 `netizen-cli` 的 PyPI 项目归属，以及与实际 GitHub owner、
repository、`release.yml` workflow 和 `published-release` environment 对应的 Trusted
Publisher。流程使用 OIDC，不新增长期 PyPI token secret。GitHub 的 Immutable Releases、
版本 tag 保护及 environment 权限也需单独确认；工作流文件不会自动创建这些外部设置。

### 发布步骤

1. 维护者明确决定发布并完成上述外部准备；按实际变更补齐 SDK、浏览器及两平台 live
   验收，明确记录未完成范围。`scripts/release.py` 执行版本推导／指定、notes、版本 PR、
   exact main CI、受保护 annotated tag 与 workflow dispatch 的完整链，不自动响应 push
   或 tag 发布。两处包版本均已准备为目标版本时复用当前 main，仍等待该精确提交的成功
   CI；均为上一版本时才创建版本 PR，混合或其他版本在修改前拒绝。
   运行该脚本意味着请求正式发布，不是只构建或 dry-run。
2. `release.yml` 只接受显式 workflow_dispatch，核对 exact tag 和同一 main commit 的
   成功 CI；固定 `setuptools==80.9.0`，由 `scripts/build_cli_distribution.py` 先构建
   sdist，再从该 sdist 构建 wheel，生成带 SHA-256 的 `netizen-cli-release.json`。
3. 后续 job 使用同一份制品，不重新构建；校验名称、版本、资源和摘要，并在全新临时
   Python 环境安装 wheel，从 checkout 之外检查 CLI、模块和资源，不启动真实实例。
4. 默认手工 dispatch 的 `publish=false` 只构建、验证并保留 Actions 制品，不创建
   GitHub draft，也不调用 PyPI；不占用后续同 tag 的正式发布入口。
   `scripts/release.py` 明确提交 `publish=true`：建立 draft、再次核对 tag／摘要后，通过配置好的
   Trusted Publisher 上传 wheel 和 sdist；仅在 PyPI 步骤成功后才正式发布 GitHub Release。
   发布完成后仍应独立验证实际索引安装，不把构建日志当作最终用户可安装证明。

PyPI 或后续步骤失败时保留 draft 与可核查状态，不自动换凭据、覆盖资产或回滚索引。
尤其上传可能部分完成，而 PyPI 已上传版本文件不可当作普通可覆盖文件；先核对索引中
实际存在的同版本文件、摘要和 GitHub draft，再由维护者决定修复方式，不盲目重跑或
重新构建同版本冒充原制品。已有同 tag Release 会使自动链拒绝，以免替换中断证据。

历史 GitHub archive／bootstrap 流程记录在 ADR 0050 及其相关 ADR 和 Git 历史中。
它证明的是旧 immutable source archive，不是新 wheel／PyPI 发行，更不能用旧
install.sh 的零退出证明 CLI 环境识别、schema 启动迁移或 update 协调器已经验收。

## 安装

完整用户流程、命令与支持边界见[CLI 安装与维护](cli.md)。选择长期 Python 环境安装
`netizen-cli`（不是第三方同名 `netizen` 包），然后分别 setup 和 start。包安装无实例
副作用；setup 不默认启动。当前 checkout 的旧 install.sh、dev-install.sh、service.sh、
uninstall.sh 入口已停用，只返回 CLI 指引；不会生成 release/current 或隐藏 venv。
源码安装使用选定 Python 的 `pip install .` 或开发期 `pip install -e .`，再 setup/start。

### 维护飞书权限契约

权限和回调的单一契约仍是[前置门禁](#前置门禁)。修改
`netizen_cli/feishu_app_onboarding.py` 的 tenant addons 时，同步更新权限检查、
用户说明及相关行为测试；不新增 CLI 运行依赖或自行实现飞书授权协议。审批、发布、
租户安装与入群须按真实流程确认，SDK 返回凭据不等于全部权限已生效。

### 更换飞书应用与权限修复

setup 对已有服务绑定仅报告 already_registered，不静默修改或接管。需要修复应用时，
先用原环境 stop／remove 保留数据，安全备份并按意图调整 profile，再在选定环境 setup、
start。已有有效 App ID 加空 Secret 是 exact-App 修复；删除整个 profile 文件表示
重新选择应用，别在普通更新时删除。新凭据成功保存后即为持久配置意图，后续权限或
启动失败不还原旧凭据。App ID 改变不会迁移旧 Scope 或原生历史。

### Agent 驱动首次安装

使用选定环境的 `netizen setup --root "<NETIZEN_ROOT>" </dev/null`，在出现验证链接时
转交给用户并保留同一进程，继续读取 stderr 进度。stdout 的 helper 凭据通道只由程序
私下消费，不能展示到聊天。等待最多 660 秒；取消／失败后按输出修复再显式重试，
不要循环申请，不要求用户粘贴 App Secret。setup 成功后另行执行 start 并等 ready。

### 浏览器安装路径验收

在授权的隔离应用／账号完成无 TTY URL 交接、相同进程继续、首次选择／exact-App 修复、
权限不足、取消和超时；验证凭据不进入输出、argv、日志或 YAML。TTY 浏览器失败只
允许一次手工回退，Ctrl-C 不回退，成功后不再次发起浏览器补权。此项须单独记录，
fake helper 和代码门禁不能冒充真实应用验收。

### 服务环境

systemd／launchd 不负责加载完整用户 profile。包内 launcher 每次实际启动时按有效 uid
查询 home／login shell，执行一次有界、无 TTY 的 interactive login shell，取得导出
环境后原位 exec 绑定的 Python。探针仍使用随机 NUL framing、长度和摘要校验，
10 秒／4 MiB 上限；stdout／stderr 不进入日志，不泄露环境。探针 exec 替换 shell，
避免 logout hooks。超时、非零退出、不完整输出或不支持 shell 明确失败。

保留 PATH、代理、CA、语言及普通工具变量，重申账号 HOME、canonical root、配置与
凭据路径，清理直接 Secret 和 Python／venv 覆盖。解释器、探针和最终进程使用固定
绝对 Python 及 `-E -P`，最终 Runtime 另用 `-B -u`；不从 profile PATH 重新找 Python，
也不以 -I／-s 隐藏用户 site。显式注册 CODEX_HOME 优先；否则采用加载后的 profile
值，再缺省为账号 ~/.codex。setup 登录检查不预执行这个 profile。

遵守 ADR 0022／0023 的单一环境事实源和 `allow_login_shell=false` 公开 override；
不维护 PATH 快照、环境副本或实例私有 Codex 状态。修改持久 profile 后执行
`netizen restart --root "<NETIZEN_ROOT>"`。别把终端临时 export、alias 或 TTY 状态
当作后台服务自动继承契约。

### 候选验证与切换

此锚点保留给旧链接；当前没有 release/current 候选切换或跨程序／数据库自动回滚。
实际启动在 lifetime lock 内校验实例并按需迁移，全部通过后才开放 Runtime 和 ready。
SQL 提交前失败回滚事务，提交后失败保留新库。确定的配置／schema 拒绝不发布 ready，
受管入口不进入失败重启循环；CLI start 仍失败，修复后显式 start。详见
[启动迁移契约](design.md#channel-数据库与结构校验)与[CLI 更新](cli.md#程序更新)。

### 升级、启停和卸载

实例命令为 `netizen start|stop|restart|status|logs --root ...`，控制绑定的服务。
程序更新使用独立终端中的 `netizen update`，按调用环境发现关联实例，拒绝 --root。
先记录运行集合、停止且确认退出，再更新包并仅恢复原运行集合；pip／uv pip 预检无
变化时可省略停启，uv tool 可能无变化也重启。阶段／状态／失败理由与后续建议分别报告，
不解析安装日志来判断成功，不自动换包工具。

`netizen remove` 默认保留数据，--purge 仅清理显示的已验证文件，-y 不免除安全门禁。
程序卸载交给原包管理器，先处理该环境全部关联服务（含停止实例）。原生包卸载不会
自动停服，也不删除保留数据、用户 Python 或共享 Codex 状态。具体流程见
[CLI 生命周期](cli.md#移除实例清理数据切换环境)。

### 从 Admin 升级

当前不支持。Admin 不具备共享 Python 安装的程序升级权限，也不会从旧 install API
重新进入历史安装器。使用独立终端的 `netizen update`；不要在将被停止的服务上下文
执行该命令。

### 从 Admin 重启

受管实例可在系统维护页显式重启本实例，由独立临时 manager job 控制绑定的服务。
不安装包、不重绑环境、不等待任务空闲、不自动续跑；重启前确认停机影响。
返回的是操作受理／进展，不把 HTTP 成功、页面重连或 PID 存在当作整个重启成功。
断线后重新登录读取同一结果；未知状态先查 status/logs，不编辑记录伪造成功。


## 配置与管理页访问

`config.yaml` 采用仓库示例的 mapping 形态，不再配置 `instance.appId`。
飞书应用凭据位于当前 `<NETIZEN_ROOT>/lark-app/config.json` 的固定 `netizen` profile：

```json
{
  "currentApp": "netizen",
  "apps": [{
    "name": "netizen",
    "appId": "cli_example",
    "appSecret": "replace-locally",
    "brand": "feishu",
    "defaultAs": "bot",
    "users": []
  }]
}
```

示例中的 ID 和 Secret 只是占位符，不要直接用于安装。目录由 setup 设为 `0700`，文件为
当前用户拥有的普通非 symlink 文件，权限为 `0600` 或更严格。Netizen 固定读取名为
`netizen` 的 profile，忽略 `currentApp`，不解析 Secret 引用或用户 token；setup 原子保存
raw App ID／Secret。重复 JSON 字段、重复的 `netizen` profile、错误品牌或不合法凭据会
明确拒绝，报错不含 Secret。

服务启动入口统一设置绝对路径 `NETIZEN_LARK_APP_CONFIG` 为
`<NETIZEN_ROOT>/lark-app/config.json`。手工启动遵守相同布局，未指定 root 时选择有效账号
`~/.netizen`；配置、凭据或状态路径显式指向别处时拒绝启动。不再支持 `FEISHU_APP_SECRET` 或
`FEISHU_APP_SECRET_FILE`。这一文件采用 Lark CLI profile 格式，Netizen 只用 Python
标准库读取；安装、服务启动和消息处理都不需要 `lark-cli`。可选 CLI 按
[netizen-lark Skill](../skills/netizen-lark/SKILL.md) 选择该目录和 `--profile netizen --as bot`，
自行换取应用令牌；不要把 profile 内容输出到聊天或模型上下文。格式兼容基线为官方
CLI `1.0.95`，需用不含真实凭据的隔离 profile 验证目录选择、固定 profile 和自动换令牌
分支；这不能替代真实应用授权或飞书验收。

Admin credential 仍独立保存，必须是 `token_urlsafe(32)` 的 canonical base64url 单行、
无尾换行，且 mode 必须精确为 `0600`。若需要由 Agent 预配置，可安全地以文件写入 API/
受控 stdin 写入；不要把 Secret 放在 CLI 参数或 shell history 中。首次无 TTY 安装可以
直接完成官方浏览器初始化：

```bash
netizen setup --root "<NETIZEN_ROOT>" </dev/null  # 新实例或已解除绑定实例；按输出转交验证链接
```

从带 Netizen allowlist 的旧版本升级时，必须先从 live `config.yaml` 删除整个
`access:` 段。用户和群的可用范围改由飞书应用后台管理；新版若发现旧段会明确拒绝
启动，避免部署者误以为这些字段仍然生效。

不要把 Secret 内容写入 YAML、仓库、shell 参数、shell profile、systemd `Environment=`
或 LaunchAgent plist。服务只传递应用配置与 Admin credential 的受保护文件路径。

`adminWeb` 首次未指定端口时等价于：

```yaml
adminWeb:
  enabled: true
  host: 0.0.0.0
  # port 缺失时首次实际绑定 8787–8886 中可用的端口，随后写回此字段
  # accessHost: netizen.internal  # 可选访问 hostname/IP，不含协议、端口或路径
```

可覆盖 host/port 或显式关闭；启用时使用 `<NETIZEN_ROOT>/credentials/admin-web-secret`，
启动入口设置 NETIZEN_ADMIN_SECRET_FILE，显式提供时必须与该路径一致。
端口字段存在时严格使用，冲突报错；只有缺失才自动分配，`0`、`null`、空字符串无效。
启动进程保留已绑定 listener 后原子更新 YAML，检测人工修改或写入失败即关闭 listener，
不以探测空闲后释放 socket 的方式分配。只有 EADDRINUSE 才尝试下一端口，最多 100 个；
其他错误或耗尽均启动失败。端口一旦写入，即使后续初始化失败也保留；删除 port 再启动
才重新请求分配。禁用 Admin 时不绑定、不分配。停止实例的已保存端口不登记或预留。

在飞书发送 `/admin` 获取成功绑定的管理地址和实例根目录，不要求 Project/会话，也不
调用模型。有效 Side 同样可用，已关闭 Side 保持原路由；未启用时明确说明。命令不返回
credential、session token 或免登录链接。访问 URL 不使用 `0.0.0.0`/`::`；通配监听自动选择
与实际监听地址族及端口匹配的本机 IP，没有匹配项时回退同族 loopback。多网卡可设置
`accessHost`，它连同实际端口加入已有 Host/Origin 精确校验，不增加代理信任或 NAT 映射。
只有 loopback 地址时使用服务器本机或自行隧道。未登录时根路径跳转到 `/login`。
页面和登录页均显示实例 root。登录凭据可由
实例管理员在受控终端读取：

```bash
cat "<NETIZEN_ROOT>/credentials/admin-web-secret"
```

Admin 登录会话不设闲置或绝对时间过期；退出登录、服务重启或凭据轮换会使其失效。
浏览器仍使用会话 Cookie，清除 Cookie 后需重新登录。同一来源最多保留 16 个、全局最多
256 个登录会话；验证成功的新登录在达到上限时替换最早会话，优先在已满的同一来源内
替换，并撤销旧会话的操作凭据。

登录页的一次性校验码也不设时间过期；已提交、服务重启、凭据轮换或待提交校验码达到
容量上限后被替换时，需重新打开 `/login`。待提交校验码最多每来源 16 个、全局 1,024 个，
新签发优先替换已满来源的最早记录，否则在全局满时替换全局最早记录。多个登录页共用
Cookie，打开同一实例的新页可能使旧页无法提交。登录失败后自动跳回带提示的新登录页，
不要后退重交旧表单；核对凭据来自该部署主机的当前实例。
每来源五分钟内最多五次失败、全局最多二十次；达到限制后登录页显示等待提示与重试
入口，暂不提供提交表单。暂停尝试约五分钟后重新打开 `/login`，无需重启服务；刷新
等待页不会增加失败计数。登录后的操作凭据十分钟有效期保持不变。
两个 Cookie 名都带 rootDigest；同一 host 不同端口的实例可并行登录，互不覆盖。

轮换时用安全的原子文件写入替换同一路径并保持 0600，然后刷新页面；运行中 auth 会在下一
认证边界检测到合法 identity/content 变化并立即注销全部旧 session。非法替换会锁闭 Admin
admission，修复文件后仍需 `netizen restart --root "<NETIZEN_ROOT>"`，不会自动重新开放。V1 使用不加密的内网
HTTP；不得把该端口直接暴露到不受信网络。

`instance.projectRoot` 是必填的绝对路径，用于限制从飞书自动创建的空 Project；它不是
Binding 的默认 cwd。Channel 业务只支持当前完整 schema v14；实际启动入口按
[ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md) 在 lifetime lock 内
检查数据并按需迁移，setup 才创建新库。当前没有生产迁移步骤；未来冻结步骤从 v14
持续维护。提交前错误回滚事务，提交后服务失败保留新库，不自动恢复旧备份。

`session_defaults` 和 `session_defaults_order` 仅保存 App 隔离的聊天默认配置、群名匹配
条件与有序规则元数据，不保存聊天正文或有效 Codex 配置。
`schedule_plans`、`schedule_runs` 和 `schedule_requests` 仅保存当前计划指令与会话配置、
最小调度交接/initial Turn 引用及有界管理请求去重，不复制原生历史。
当前库重装及后续受支持迁移保留 Scope/Binding/Project、会话默认配置及规则顺序、去重
记录和 `side_topics` 永久墓碑。迁移全程持有 lifetime lock，
失败／中断按[启动边界](#候选验证与切换)报告，不覆盖已提交的新数据。
低于 v14、未知／较新版本、缺失迁移路径或损坏数据库明确拒绝，不自动删除或重建空库。
早期试验版数据的一次性转换仍需单独停服、备份并校验，不属于自动升级流程。
配置的 `projects` mapping 启动时仍只做 `INSERT OR IGNORE`，停用、动态登记和已删除记录
始终优先；已删除 alias 只有显式重新登记才能复用，revision 继续递增。Project 删除保留
磁盘代码目录。Project 删除清单同时纳入定时计划和在途定时创建；提交后删除关联计划，
即使后续部分失败或同名重登记也不复活计划。删除确认、进度和结果不写入 SQLite；部分失败或结果未知后 Project 保持
停用，管理员应刷新查看剩余项并重新确认，不会在重启后自动续删。会话默认配置不参与
Project 删除联动，保留原记录并在使用时重新校验。不要手工编辑表。

浏览器初始化会请求 `card.action.trigger`；手工准备应用时，使用卡片前须在飞书开发者后台
打开“事件与回调 → 回调配置”。回调仍走现有 WebSocket 长连接，不需要公网 callback URL；
若未启用，文本命令正常但点击卡片不会产生事件。

## Fail-closed 运维语义

- 若 Netizen 提示 admission 已关闭或要求重启，表示一次 native start/turn/terminal
  结果无法安全判定。Pilot 有意不自动重试，也不自动修改 Binding；检查平台日志后
  人工执行 `netizen restart --root "<NETIZEN_ROOT>"`。
- 若某个 Binding 长时间停留在 running，公开 native read 会继续保留该 slot 并周期
  记录 warning，避免在未知终态下误开第二轮。其他 Binding 不受影响；持续异常时检查
  App Server/平台日志，并通过正常 `/stop` 或服务重启恢复，禁止手工清理 SQLite 或
  `.codex` 状态。
- 若引用 prompt 提示超时、撤回/删除、权限不足或准备期间任务状态已变，
  该次输入没有 start/steer。请先修复权限或确认当前 Turn，然后由用户重新发送；
  不要在运维层自动重放旧消息。
- 若 catch-up 提示历史读取、Scope/identity、分页或被选中消息失败，本条同样没有
  start/steer，Context Boundary 也不会推进；让用户修复后重新 @。若提示“任务已接受但
  上下文边界未持久化”，native Turn 已经开始，必须停止新 admission、检查 SQLite/磁盘并
  重启，不得自动重放当前消息。截断或 unsupported omission 的可见回执不是失败；边界会在
  native 接受后推进，已省略的较早内容不会在下一轮重复补入。
- 若图片 prompt 提示不可读、格式不支持、超过 20 张、单图 20 MB 或合计 50 MB，
  该次输入同样没有 start/steer；让用户压缩或拆分后重新发送，不做部分重放。
- 若本轮文件按钮提示文件已不可用、卡片已删除或话题关系未确认，
  原终态卡应保持不变。先检查当前文件和
  `im:resource`/`im:message:send_as_bot` 已发布权限；
  v4/v5 callback payload 明文包含路径和分页 manifest，这是已接受的飞书应用边界，不是
  下载凭证或快照；不要手工写 SQLite，也不要把失败文件补发到主聊天。
- 若 Side 显示 `creating`、清理未确认或要求再次结束，不要删除 SQLite route 或把该话题
  当普通 Binding 使用；在原 Side 话题重试 `/side close`，或正常重启让遗留 open route
  转为 expired。Side 卡在 active close 时先检查 App Server/平台日志；禁止猜测 native ID。
- 正常停机会停止所有 pulse 和 Reply Card Activity updater，并用已记录的 exact reaction ID
  清理常驻的 `Typing` 与当时可见的 `THINKING`；没有 native 终态时不把运行卡伪装成完成。
  若进程被 `SIGKILL`、主机掉电或崩溃，运行态表情或最后一次更新的运行卡可能留在原消息；
  在“不持久化飞书展示状态”的边界下无法安全恢复，禁止为此扫描/猜测 reaction/card
  identity 或修改 SQLite。

## 平台服务管理器

### Linux systemd

服务位于当前账号固定 user unit 目录，以 canonical root 派生名称，ExecStart 固定
Python、CLI 内部 _serve 入口及 root。不请求 sudo，不保存调用者 PATH；用户注销后
持续运行所需 linger 由用户／运维自行配置，不替其他 user units 改变全局策略。

状态来自 systemctl 的属性／JSON 查询及实例锁、ready。未知 drop-in、需要 daemon-reload、
FragmentPath 冲突或关联服务无法确认时拒绝修改。stop 会取消 manager 重启意图并确认
进程和锁已释放；它不解除绑定，也不修改下一次机器启动的自启配置。

### macOS LaunchAgent

服务属于当前 GUI 用户的 LaunchAgent，要求 gui domain 存在；仅 SSH 无 GUI 会明确
失败。服务定义固定绑定，使用官方 launchctl 查询，print 只用于 exact target 存在性，
不解析其诊断正文。运行 PID 与私有 service.identity.json、lifetime lock、定义同时
核对，不把 loaded 当运行。无法可靠读取的 enabled 状态报告 unknown，不猜测。

stop 使用 bootout 并确认退出，保留 plist 与既有登录自启意图；remove 才禁用并删除
经过校验的服务定义。start 不偷偷清除用户 disable override，失败时报告诊断。
日志用于展示，不作为状态解析协议。两平台真实 manager 验收须各自执行。

## 管理页升级验收

此标题保留旧链接；新 CLI 的 Admin 不允许程序升级。必须验证旧 install API 拒绝、
UI 不提供升级动作、check 只刷新本地信息、无远程 Release 请求／包工具调用，且不能
通过任意 root／URL／路径注入绕过。环境级 update 的验收见
[多实例与内置 Skills](#多实例与内置-skills-验收)和[CLI 手册](cli.md#验证与发布状态)。
历史 ADR 0057 的正式 release 升级证据不能替代这些新门禁。

### 管理页重启验收

在 Linux 与 macOS 隔离实例验证：一次性授权绑定实际安装身份；不同实例不能互相操作；
独立 job 不随主服务停机而结束；重启固定原绑定环境；实例维护锁和环境 update 的交错
不重复提交；结果未知先对账，不虚构成功；HTTP 断线／重登不重发。同时验证真实 ready、
任务中断／Goal 暂停／Side 关闭、重启后的 session 注销以及迁移提交后失败保留数据。

伪 manager 和单元测试只证明本地逻辑；每项实机结果记录 exact 候选、平台、命令、
故障点与清理情况。没有执行的项目明确未验证，不复用旧 release 的成功记录。

## 验收顺序

改变 CLI 绑定、launcher、迁移或 ready 边界时，按受影响范围完成两平台验收：Linux 的
systemd user，以及 macOS 14+ 实际 GUI 用户的 LaunchAgent。覆盖 setup/start 分离、
启停与 remove、环境 update 的原运行集合、失败报告、数据提交后不回滚，以及 sleep/wake、
logout/login 自启意图。确定的配置／schema 拒绝不无限重启；异常运行退出仍按服务策略处理。
lifetime lock 非继承须有独立的实进程证据：exec 子进程未拿到锁 FD，持有者仅关闭
自身 FD（不预先 unlock）后，另一个进程在该子进程仍存活时能获取同一锁。可用有界隔离
探针验证，不要求为此执行模型；子进程已退出后能获取锁，不能单独证明此前未继承。
该锁机制证据不替代真实 manager 停服：仍须确认本实例服务退出、manager 不会再拉起
且 lifetime lock 可获取。
macOS 停服时核对现有 interrupt、terminal cleanup 与有界 SDK 关闭请求，记录后台
terminal 继续运行或退出的实际结果，任一种结果均不作为独立通过或失败判据。缺少锁
非继承证据时明确记为未验证；测试进程须有预设期限，结束后确认清理且不影响无关进程。
检查 plist、进程 argv/environment、`netizen.log` 和 `launchd.stderr.log` 均不含 App/Admin
Secret。最后在两平台重跑真实 Codex Thread、steer、cleanup 和 exact-ID resume probes，
按上述职责分别记录适配行为与原生能力结果；fake launchctl/systemctl 单测不能替代这些真机门禁。

先通过 `/admin` 获取当前实例的实际端口，从另一台受信内网主机直接访问
`http://<服务器 IP>:<实际端口>`：未登录的 `/` 返回 303
重定向到 `/login`（不返回 HTML 或状态），未知 route 和 API 必须返回 401，只有登录页复用的
无状态 CSS 可匿名读取，`/health/ready` 只返回无细节状态；
使用独立 credential 登录后，检查四个一级
页面、筛选、分页和五秒 runtime polling。Sessions 默认显示 Active + Lazy，Project 可搜索
并包含停用项；Project/Scope/状态/current 的多选应满足同项“或”、跨项“且”，无 ID 输入框，
列表 ID 仍可见。验证重置恢复默认状态、全部时间和每页 20 条。分别选择 10/20/50/100，确认前后翻页、
页码与当前页条数正确；在 P2P、普通群和话题群 Binding 上确认显示真人/群名称与正确类型，
重复刷新命中缓存，名称链接能打开对应飞书会话，话题行只承诺打开所在会话。100 条页面的
首屏和 polling 都不得向单次 runtime snapshot 请求发送超过 50 个 ID。依次验收 Project
register/create/enable/disable，
两个 Scope 中 active/inactive、Lazy/materialized/archived Binding 的 create/activate/
configure/rename/archive/两种 unarchive/delete-lazy/Stop/Release，以及一个 open Side 的 exact
Close。再创建两个 disposable materialized Binding，分别保持 active catalog 与 archived
catalog：两行仅在 Delete capability 可用时显示删除；点击后浏览器二次确认必须显示会话、
Scope、short ID 与完整永久级联后果，取消必须零 mutation，确认后才删除对应
Thread/descendants 与 Binding。删除 inactive 行不得改变其他 active pointer，删除 current 行
才清空 pointer；不得先 activate、unarchive 或 Stop。Lazy 行仍通过既有二次确认删除本地
Binding。
另以 disposable Project 关联 Lazy、active、archived 会话和一个 open Side，确认 Project
删除窗口计入所有关联项，不受 Sessions 筛选影响；取消零 mutation，确认后关闭 Side 并
逐个删除会话，磁盘标记文件保留，Project 消失而 Side 墓碑仍在。执行期间从另一入口尝试
新建关联会话、Side 或重新启用，均应拒绝。验证部分失败/断线后 Project 仍停用且显示
剩余项，不把结果未知显示为整体成功。重启时 YAML 不应恢复已删除 Project；显式复用
alias 后旧 revision/action 必须失效。数据库边界变更还应针对候选验证受支持版本迁移与
不受支持库只读拒绝、当前库重装的 metadata 保留，以及失败／中断恢复和候选新数据
保留；按[数据库迁移验收](#数据库迁移与中断恢复验收)记录与此边界相关的真实结果。
双击同一 action 应返回 stale/consumed；与飞书并发操作同一目标时只允许符合 exact native
identity 的一方提交。重启服务后旧 Admin session 必须失效，持久 Binding/设置/Side
墓碑不变；journal 不得出现 credential、cookie/action token、cwd、name/preview 或 body。
最后占用 configured port 再启动实例，确认明确报错、不漂移端口，并保留已经提交的迁移；
释放端口后显式 start。以上真实浏览器、跨主机与端口失败边界属于 Admin Web 首次
上线或相关边界变更的 live 验收，本地 loopback 单测不能替代。

1. P2P `/help`、exact `/new`、`/sessions`、`/status`（native=pending）；`/sessions`
   返回分页卡片，将 active Binding 置顶、将 lazy Binding 显示为“新会话”，有原生
   Thread 时优先显示 `name`、否则显示 `preview`。创建第二个会话后点击第一项的
   “设为当前”，原卡必须刷新 active 标记，且不得停止另一会话仍在运行的 Turn；归档项
   不得混入普通卡片。每个 persisted、non-ephemeral materialized 行都显示带确认的“归档”，
   Delete capability 可用时也显示“删除”，不因 Ordinary Turn running/stopping、Turn 观测
   不可用、Goal 或 Compaction 隐藏生命周期入口；第一次点击删除只打开独立红色确认卡且
   不得 mutation。running/stopping/观测不可用的 Ordinary Turn 还显示“停止”，观测不可用
   显示“重新检查”。idle Lazy 行显示“删除”。lifecycle-unknown 只阻断该 exact Binding 并
   显示明确错误，不能关闭其他 Binding admission。`/sessions archived` 同时显示
   “恢复并切换”和独立两阶段“删除”。`/status` 分行显示 `name`、`preview`
   与“上下文窗口：暂无（首条消息后生成）”。帮助包含 `/config`、`/goal`、
   `/rename`、`/archive`、`/delete`、
   `/unarchive`，不包含
   `/compact`、`/skills`、`/model`、`/effort`、`/fast` 或当前不可用的
   `/plan`、`/apps`；`/copy`、`/vim`、`/theme`、`/exit` 也不展示。
   另发送 `/new test`、`/new demo`、带引号和坏引号的 `/new ...`，都必须得到同一迁移提示、
   零 Binding mutation；`//new test` 仍作为字面 prompt。
2. 通过 `/new` 卡片选择 `test` Project、inherit Codex，保持 Reaction Pulse 关闭并手动关闭
   Progress Card 后发送首条 prompt：native accepted 后原消息常驻 `Typing`，但整轮零
   `THINKING`、零进度卡和零心跳回复；成功 steer 的消息添加 `OnIt`，原任务锚点不迁移。
   终态先添加 completed/failed/interrupted 对应的 `DONE`/`ERROR`/`CrossMark`，再移除
   `Typing`。无文件终态仍是富文本/静态文本，有文件终态仍只有现有“最终回复 +
   本轮文件”卡片。手机端可能把这些稀疏生命周期表情显示为少量独立消息，这是已接受的
   取舍。故意使 `OnIt` 失败时，只在 native steer 已成功后收到文字 fallback。

   用 `/config` 只开启 Reaction Pulse 后再启动长 Turn：Lifecycle Reaction 与上述关闭组
   相同，但 `THINKING` 按低频节奏显示/隐藏，终态再与 `Typing` 一起清理。

   再只开启 Progress Card：native accepted 后除 Lifecycle Reaction 外只出现一张运行卡，
   顶部过程区展开；status 与
   原生 checklist（`✓/→/○`）变化必须更新同一个 message ID，无 plan 时显示“Codex 尚未
   生成”，observer unavailable 时显示“暂不可用”。卡片不生成耗时、百分比或 ETA，
   不显示 reasoning、raw command/tool output、tool arguments 或 MCP server。最近进展和
   最近操作必须各验证至少一条 Card 2.0 本地化事件日期和分钟；started 操作完成后时间切换为
   exact completion 时间，checklist 不显示时间。命令须展示原生 action 的路径/查询，分类
   未知、复合或对象字段为空时显示原命令预览，存在非零退出码时保留该值；不猜测命令意图。
   文件修改和网页操作须展示原生对象信息，进展文字须保留普通路径、链接和代码片段，明确
   凭据仍隐藏。验证操作单行和 120 字符限制、文件/多查询最多前三项、分页后详情不丢失。
   最近进展最多四条，每条正文直接跟在时间分隔符 `·` 后，不额外显示 `•`。
   MCP/dynamic tool 须显示 exact 工具名，动态文本中的 Markdown 控制字符只影响文本、
   不注入标签。
   成功 steer 后旧 checklist 在新
   plan 到达前标记可能过期，之后整体替换。终态在同一卡片折叠过程并显示结果；有文件时
   同卡保留既有 v4 文件分页/callback。分别使 initial、中间和终态 card update 失败，native
   Turn 都必须继续。单次中间失败后，下一轮应更新原卡；失败期间出现多个 revision 时只
   发送最新快照，成功后清零失败计数。连续三次中间失败即停止轮询，此后恢复服务，终态
   仍应独立更新原卡且不新增回复；单次终态失败后应重试同一原卡，成功后只发送一次结束提醒。
   终态连续失败时，最多尝试三次原卡更新、间隔 0.5 秒且整组不超过 5 秒，再回退标准投递。
   终态或关闭应立即唤醒轮询等待，不等待剩余重试次数。initial 失败仍直接回退，均不产生
   重试风暴。最后同时开启两项，确认两套 presenter
   并存而不重复最终结果，并在 P2P、群主线和普通话题各验证一次。

   running 时 `/status` 仍出现完整 native ID、已接受 steer 次数和同一 checklist。在可观测
   Turn 完成、公开 usage 通知已排空后 `/status` 显示当前
   窗口已用 tokens、窗口上限和百分比。再启动一个普通 Turn 时，running `/status` 保留
   并标注“上一轮完成时”的快照；本轮完成后覆盖为新值，快速完成的 Turn 也读取原始
   handle 保留的通知。没有可用的新 usage 时清除旧值并显示暂不可用。该继承路径不得读取模型
   目录或向 SDK 传 Model/Effort/Speed override。
3. 零参数 `/new` 在存在 enabled Project 时只显示一个创建 form，不显示任务输入或快速按钮。
   Project 使用现有单个静态下拉框，并展示 Registry 中全部 enabled 项；同 Scope 当前或
   最近使用且仍 enabled 的 Project 可基于现有 Binding 记录预选，不新增 recent 状态；没有
   该记录时下拉保持未选。零 enabled Project 时不显示 form，只引导 `/settings`，且零 Binding
   mutation。准备 13 个以上 enabled Projects 验证没有 12 项截断、分页控件或命令兜底，
   disabled 项不出现。P2P 表单不显示
   @ 时读取的消息范围；群主线和普通群话题显示“仅这条 @ 消息（默认）”与“自动带上期间
   的群聊讨论”，下拉框下方有灰色说明。三个 Task Feedback 控件在所有普通 Scope 都显示
   且 Reaction Pulse 默认关闭、Progress Card 和结束提及默认开启。选择 inherit Codex 时不保存
   Model/Effort/Speed override；选择实际 Model 时三项必须与本机 `models` phase 一致并
   全部保存。模型目录不可用时仍显示可提交的
   Project + inherit + Task Feedback 表单。成功卡片显示 Project、会话短 ID、Model 来源、
   三项 Task Feedback 与 @ 时读取的消息范围；即使原卡更新失败，同一 Scope 也应收到等价
   兜底回复。再用足够大的 Registry 触发真实平台容量错误，必须明确说明没有截断、分页或
   快捷创建，且零 Binding mutation。
4. 在 idle active Binding 上发送 `/config`，选择三项、三个 Task Feedback 和群聊 @ 时读取
   的消息范围后原子保存；
   不得要求任务或立即启动 Turn，也不得显示目标会话；配置其他会话必须先 `/resume`。
   后续每条需要启动新 Turn 的普通消息都在 exact native Thread 重新校验并显式应用，
   配置不会在首轮后清除。对目录中支持加速 Tier 的模型依次验收
   `Fast/priority -> Fast/priority -> /config Standard/default -> Standard/default`，
   四轮必须在同一 native Thread 连续成功；卡片只显示动态名称，不显示协议 ID，也不
   出现费用提示。打开卡片后先
   `/resume` 另一个 Binding，再提交旧卡片，必须零 Codex mutation 并提示重开；两张
   `/config` 卡也必须分别由 settings/feedback/context revision 拒绝后提交的旧卡。启用
   catch-up 时 exact card anchor 读取失败必须同时保持旧 Model、旧 Task Feedback 与旧 mode；
   running Turn 上 `/config` 必须拒绝，running steer 不得解析或应用 Binding 配置，已经
   开始的 Turn 也不得被后来保存的反馈开关改变。
5. `/compact` 出现在帮助中；对有历史且空闲的普通会话提交后，必须显示开始回执和
   `compacting`，完成唯一新 `contextCompaction` Turn 的终态确认后才能继续普通任务。
   lazy、running/stopping、Goal 或 Side 上不得启动压缩，额外参数必须拒绝；回执发送失败
   不改变原生压缩进程，也不能阻止终态交付。底层测试继续覆盖候选歧义和终态不确定时的
   fail-closed 行为，以及启动前 baseline 的独立 5 秒/3 次 read 上限；未发出 compact
   时读取失败应释放 Binding 锁，保持已有的全局 admission。不能只以 ACK 或首次 idle
   判定完成。匹配 SDK/CLI `0.154.0` 已通过
   compact phase 及同一 Thread 的 `COMPACT-AFTER`；后续 SDK 升级须重验完整序列。
6. `/skills` 必须作为未知命令拒绝且零 Codex mutation；用自然语言询问当前可用 Skill
   必须按普通 Prompt 启动或 steer。在消息开头连续输入两个 `$skill-name`，只启动一个
   原生 Turn；running 时同样只 steer exact Turn 一次。未知、
   disabled、重名或 stale 引用必须零 Codex mutation；引用消息历史里的 `$skill` 不得
   激活，当前消息的引用仍正常。
7. 在已通过 zero-Turn live gate 的环境发送 `/goal <objective>`，只出现一张组合卡，并与
   `/status`、`/sessions` 一致显示 Goal 状态；同一 Binding 的普通 Prompt steer 当前
   exact 物理 Turn，`/config`、`/compact` 仍被拒绝。准备期间换轮必须明确拒绝旧目标，
   不重投或新增 Turn；启动、暂停、收尾、unknown 与无安全 route 时仍拒绝。原生 Goal
   phase 验证当前 Turn steer、resume 后旧 expected-ID 拒绝及新 Turn steer 改变结果。
   Progress Card 关闭时该卡只有 Goal/终态 Result/可选 Files，开启时
   增加 Activity，且 start、rollover、pause、resume、terminal 都更新同一个 message ID；
   rollover 后 Activity 只显示新物理 Turn 的原生事件时间和操作信息。
   `/goal pause` 与 `/stop` 都先暂停 Goal、中断 exact 物理 Turn 并请求 terminal cleanup，
   随后卡片或 `/goal resume` 可继续；paused、blocked、usage/budget limited 都不得自动
   clear，并保留“结束 Goal”。只有 logical stream、persisted Goal、exact final Turn 与
   Thread idle 四项证据完整，Goal 为 complete 且 final Turn 为 completed 时才自动 clear；
   清理后卡片仍显示冻结的最终回答和 exact final Turn 文件，`/sessions` 恢复 idle 的归档/
   删除按钮。模拟 clear false、响应丢失和清理后仍可读，必须显示收尾不确定、保留 unknown
   slot、关闭 admission、零自动重试但不丢最终回答；即使 clear 已生效且后续 get absent，
   `/goal` 也必须显示冻结的 goal-unknown，`/goal clear` 必须拒绝。用阻塞终态投递证明
   handoff 返回前显式 clear 与同秒同 objective 新 Goal 都被拒绝，handoff 超时后非 unknown
   slot 会释放，shutdown 对已终态 Goal 零 pause/cleanup。使初始 Goal 卡发送失败后发送
   `/goal`，恢复卡必须复用 exact logical run、继续 Activity 并接住最终 Result/Files。
   重启期间保留的 active Goal 必须显示为外部活跃并拒绝 mutation，提示在原生 Codex 暂停，
   不能自动重挂；重启前旧控制按钮 stale，新的 `/goal` 卡按钮才可用。生命周期内不得用外部
   CLI/App Server 并发改写同一 Thread Goal，因为 thread-scoped clear 没有 generation CAS。
8. 长 Turn 中发第二条消息，结果必须被 steer 改变；native steer 失败时不得出现
   `OnIt` 或 steer count，必须明确提示本条未执行。故意使 `OnIt` 投递失败时，只在 native
   steer 已成功后收到“已接收调整”兜底，原 Turn pulse 不受影响。
9. 长 Turn `/stop`，确认先收到“正在中断当前 Codex Turn”，再收到明确警告前台工具
   进程可能继续运行的唯一终态；native Turn 为 `interrupted`，之后同一 Thread 可
   继续。不得把 cleanup 空响应当作前台进程退出证明。
10. 在空闲 active 普通会话查看 `/status` 的“Netizen 订阅”行；等待十五分钟后应显示当前
    连接已取消订阅，但不得声称 writer 已立即释放。再次发送消息必须 resume 同一 native
    ID 并保留上下文。再用 `/new` 或 `/resume` 切走一个 idle 会话，确认旧订阅立即释放；
    `/release` 应得到相同的保留 Binding/历史语义，running、后台 terminal 或状态未知时
    必须拒绝。重启服务后不扫描旧 Binding 或重建 timer。最后验证全局
    `codex exec resume <native-id> "..."` 能接续飞书 Thread；必要时要等 App Server 的
    最后订阅宽限期释放 writer，不能把 unsubscribe 返回当成 writer 已释放证明。
11. 同一 Project 两个 Binding 同时运行，cwd 相同、native ID 不同。
12. 运行 exact-`argv[0]` interrupt probe，记录 foreground 5 秒退出分类；probe 有界
   等待其测试 marker 自然退出后，检查无遗留 exact marker/App Server probe 进程。
13. 加一个测试群：未 @ 不触发，每条 @ 可用。在话题群中分别用纯文本根消息
   `@机器人 /new` 打开卡片并创建两个话题会话，再进入各话题逐条 @机器人；话题 A 的
   Binding/上下文不得出现在话题 B，群主线与两个话题也必须是三个不同 Scope。分别在群
   主线和两个普通话题验收 current-only 与 catch-up：current-only 只看到当前 @ 消息；
   catch-up 能按原顺序看到上一次已接受请求之后、当前请求之前的非 bot 成员消息，历史中
   的 `/stop`、`/new`、`$skill` 均不激活，当前消息仍位于 envelope 最后。实际带入时必须
   在 native submission 前公开回复条数，截断/unsupported omission 同时可见。并发发送
   两条 @ 时旧 boundary 最多被一条兑换；失败/竞态拒绝不推进，start 与 running steer
   成功才推进。切换/恢复 catch-up 会话或刚从 current-only 启用时重置边界，不得补录非
   active 期间讨论；服务重启后从持久边界继续且不重复已提交区间。P2P 和 Side 必须零
   history list call。以上依赖前置的 chat/thread live history probe 通过。
14. 验收逐条引用：在 P2P 和普通群主线分别验证首层与嵌套文本/富文本；
    由 A 发送被引用消息、B 发送当前提问，模型输入必须把两名发送者分别归到
    `quoted_message` 与 `current_message`；群内当前提问仍要 `@机器人`。再验证
    Card 1.0/default 和 Card 2.0 的可见文本、
    图片/文件的“可推理附件类型/名称，而非正文或原始资源 key”提示、撤回目标与临时去掉
    `im:message.group_msg`
    后的零 Codex 提交。在真实话题内回复根消息时不应混入“被引用消息”
    上下文；话题 Scope 和原有上下文仍正常。
15. 在单聊发送普通图片；在群聊发送 `@机器人` 的多图富文本；再分别验证文字引用
    图片、当前图文引用另一条图文和 catch-up 补充消息中的图片。模型必须能描述真实像素
    且正确区分 supplemental/当前/引用来源；检查最终 native input 使用连续 `hN/imgN`
    本地引用，历史消息和图片 label 不包含 exact message ID 或 `file_key`，当前请求仍是
    唯一 `request_text`；
    删除其中一张资源后重试必须零提交。再分别发送 locale 正文与顶层 `post.files`
    并存的真实文件、文件夹，两者都必须明确拒绝且零提交。连续发送的多条独立图片不要求
    自动合并。
16. 在单聊、群聊、话题分别发送 `/settings` 和零参数 `/new`；卡片必须留在原
    Scope。Settings 只显示已实现分区，Projects 使用下拉管理且新增表单留在同一卡片；
    创建/登记/启停或业务错误后仍显示原 Projects 分区。重启服务后 Registry 仍存在；
    停用条目不能新建 Binding，但旧 Binding 仍可继续。`/new` Project 下拉应同步展示全部
    enabled 条目而不分页。
17. 首次上线或 callback operator/Scope 路由边界变化时，由群内另一名真实参与者点击设置
    卡片的刷新操作，确认 callback operator 不受 Netizen allowlist/ACL 限制且响应不转为
    私聊；单账号自动化契约不能替代这项跨账号验收。
18. 在一个 idle materialized 当前会话上验收 `/rename` 直接参数和无参数卡片，Codex
    App/CLI 与 `/sessions`、`/status` 都应看到同一原生名称。打开 `/archive` 卡后先切换
    会话，旧卡仍应按 exact Binding/native Thread 身份归档原目标；新 current pointer 保持
    不变。归档当前会话时 pointer 才清空，Binding 配置保留，普通 `/sessions` 不显示它而
    `/sessions archived` 显示。用卡片或 `/unarchive <短 ID>` 恢复并自动切换。Lazy 会话的
    `/delete` 必须显示红色不可恢复二次确认并只删 Binding；确认前若它已物化则旧卡必须
    stale。materialized `/delete` 必须显示原生 Thread、spawned descendants、Codex App/CLI
    历史与 Binding 均永久删除的红色确认卡；切换 current 或 exact Turn/Goal/Compaction
    状态变化不能使其失效，只有 Binding/native Thread 身份变化才必须零 mutation。正常确认
    后仅根据原生成功响应删除 Binding/pointer；另对自建样本核查 root 与 descendants 已从
    四个目录视图消失，检查不得变成生产删除的前置或失败收尾条件。

    `thread/delete` response loss 与明确拒绝由 synthetic fault 覆盖，不在真实账号手工制造；
    `make check` 验证：仅原生成功响应提交 Binding 删除，明确 RPC 错误保留 Binding、清理
    可能失效的活动投影后允许重新确认，传输/响应未知保留 Binding-local lifecycle-unknown。
    not-found 不等同成功，任何失败都不能追加 list/read 对账或自动再次调用 delete。
    同一 Scope 的其他 Binding 必须仍可 start/steer/lifecycle。archive 的响应不确定性同样只做
    一次 active/archived 目录对账：exact ID 只在 archived 时提交，仍 active 时释放 reservation。

    分别在 running、stopping、`turn-observation-unavailable`、Goal 和 Compaction 上发起
    Archive/Delete；Netizen 必须直接委托 App Server，不先 interrupt、pause、terminal cleanup、
    恢复/读取 Turn、等待 terminal 或证明 idle。归档只进入 archived catalog，删除从四视图
    消失；原生成功后本地 observer 被取消。ephemeral、未持久化、App Server/存储不可达和
    compatibility gate 不可用仍必须明确失败并保留 Binding，不能伪装成新的 Thread 状态。

    另在 `/sessions` 中归档 materialized 非当前行，确认真实 active pointer 不变、目标移入
    archived catalog、原卡刷新并在删除末页唯一项时夹取页码；再归档当前行，确认 pointer
    为空。确认期间切换 active 或令目标替换 exact Turn 不使旧生命周期按钮 stale。再从
    `/sessions` 分别删除 idle Lazy 与 materialized 非当前行：第一次点击只出现带 exact 目标
    的红色确认卡，最终确认后 active pointer 保持不变；删除当前行时 pointer 清空。切换
    active 或改变目标运行状态不使 materialized 确认失效，Lazy 物化或改变 exact native ID
    必须使旧按钮零 mutation；删除末页唯一项后页码夹取。另从 `/sessions archived` 对 exact
    archived 行完成独立二次确认 Delete，确认不先 unarchive、active pointer 不变且 spawned
    descendants 随 root 从四视图消失。active 与 archived materialized delete 必须复用同一
    native-first primitive 和原生返回处理。`/archive` 与 `/delete` 仍不接受目标参数且保持
    current-only；归档列表恢复仍校验 archived catalog，删除只复核 exact Scope/Binding/native
    身份并交给 App Server，不额外目录预检。
19. 在已物化 Parent 上分别从 P2P、P2P 话题、普通群主线、普通群话题和话题模式群触发
    `/side`；从已有话题触发必须得到同 chat 的 sibling，不得留在或嵌套原话题。P2P 与
    P2P Side 话题无需 @，三类群入口及 Side 后续每条消息都必须 @。每个 Side 连续完成
    至少三轮，并在 running 时验证下一条只 steer；`/stop` 后仍可继续，`/side close` 和
    根卡片按钮都能结束。验证 `/side <首轮问题>` 在新话题先出现明确标注来源的首轮问题，
    Codex 中的首轮来源/发送者仍是原 `/side` 人类消息，reaction 和模型回复则锚定机器人
    seed；Parent 成功时没有文字回复。再由另一名参与者发送 Side 后续和 running steer，
    模型应看到每条实际发送者而完成锚点不迁移；重复投递同一 source 不
    重复 fork。在父 Turn 正在运行时从飞书创建 Side，并让
    Parent 与 Side 的 Turn 重叠；再从同一个 Parent 创建多个 Side，确认它们同时运行且互不
    steer/阻塞。用目标 app 的真实发送链路分别覆盖 direct-root 和 root-plus-seed 两种返回：
    对 root 与 seed 各重放一次相同 UUID，必须返回原消息的 exact message/chat/root/thread
    identity，且只产生一个话题；不同 root/seed UUID 必须互异。这个对账门禁失败时 Side
    必须保持 unavailable，因为 FakeChannel 只能证明本地复用了 UUID，不能证明飞书的响应
    形状。在 Parent 关闭三项反馈后创建 Side，确认无文件终态为富文本/静态文本，
    accepted/steer/终态 Lifecycle Reaction 与 ordinary Turn 相同，且零 `THINKING`/plan
    observation；再创建同时开启三项的 Side，确认 Reaction Pulse 与 ordinary Turn
    相同，Activity/Result/Files 始终更新同一个回复卡 message ID。随后修改 Parent 的
    Model/Effort/Speed 与三项 Task Feedback，既有 Side 后续 Turn 必须继续使用创建时快照；
    新建 Side 才使用新值。Side 内 `/goal` 必须零 mutation 拒绝，根卡 close/expiry 更新仍
    独立于 Turn 回复。Side 根卡另验收原会话名称、人员头像/姓名和客户端本地时间，以及
    默认折叠的“会话详情”原生展开/收起；来源链接在桌面/移动端打开 exact 原话题或聊天，
    不切换当前会话。切换 Parent 后旧 Side 仍显示原会话，删除 Parent 后不猜测新话题或
    提供错误恢复命令。人员组件拒绝、标题读取超时或根卡更新失败不得影响创建、首轮执行
    和关闭；纯文本基础卡仍能创建话题。此处真实客户端显示与点击须单独验收，Fake 不能替代。
    重启服务后旧 Side 明确 expired 且不创建 Binding；再验证 idle 两小时
    过期。若
    P2P 建话题返回 230071，记录为当前飞书 live gate 未通过并保持 Side unavailable，不能
    以单元测试替代。
20. 在普通持久 Turn 中分别用 native Turn diff 和结构化 items 生成 Project 内普通文件、
    Project 外普通文件、Codex 原生 generated-images 目录中的 PNG/JPEG/GIF/WebP 图片和
    至少 18 个文件；native diff 还须覆盖 multi-hunk、非空新增/删除、纯 rename、binary、
    copy/mode-only/空文件 metadata 与异常 hunk，确认完整 hunk 和纯 rename 的整轮、逐文件
    `+N -M`，并确认图片/binary/其他 metadata-only/异常不显示伪造数字。另对 100、400
    个带逐文件 `a/d` 的 synthetic manifest 发真实目标应用卡片，确认平台
    完整 create/update。在源码 checkout、目标部署账号的 login 环境中，以目标应用配置和
    专用测试群执行真实容量 probe；任一 count 非零即不通过：

    ```bash
    set -euo pipefail
    unset FEISHU_APP_SECRET FEISHU_APP_SECRET_FILE NETIZEN_ADMIN_SECRET
    export NETIZEN_ROOT="<目标实例绝对根目录>"
    export NETIZEN_LARK_APP_CONFIG="${NETIZEN_ROOT}/lark-app/config.json"
    export NETIZEN_ADMIN_SECRET_FILE="${NETIZEN_ROOT}/credentials/admin-web-secret"
    test -s "$NETIZEN_LARK_APP_CONFIG"
    test -s "$NETIZEN_ADMIN_SECRET_FILE"
    probe_chat_id=${NETIZEN_FILE_PROBE_CHAT_ID:?set target Feishu chat ID}
    for count in 100 400; do
      .venv/bin/python scripts/probe_feishu_turn_file_card.py \
        --config "$NETIZEN_ROOT/config.yaml" \
        --chat-id "$probe_chat_id" --count "$count"
    done
    ```

    401 个必须在本地门禁中明确拒绝且不截断，并保留目标应用
    96.9 KB 请求返回 230099/200800 的容量证据。Progress Card 关闭时，无文件 Turn 必须仍
    只有富文本/静态文本最终回复；有文件 Turn 必须只有一张同时包含最终回复和本轮文件的
    现有完成卡。Progress Card 开启时，两种结果都更新最初的同一张运行卡，有文件时继续
    包含既有 v4 manifest。再验证 Goal exact 最终物理 Turn completed 的文件进入同一张
    Goal 卡并使用 v5 完整 Reply Card manifest，而更早 rollover Turn 的文件不会被猜测
    聚合；Side exact completed Turn 的 structured items 进入普通 v4 完成/进度卡，并显示
    成功 patch 的累计行数；Side aggregate diff、先前 Side Turn 和未进入受支持 native
    事实的文件不会被补齐。Goal live phase 必须让 resumed exact final physical Turn 创建
    一个临时文件，从成功 `fileChange` 验证累计行数；同一唯一 Goal notification stream
    的 latest aggregate diff 只验证文件发现，更早 physical Turn 的 snapshot 不得泄漏。
    修改 child/fileChange SDK 合同时，在已登录环境另验证真实新建子任务：child 创建文件、
    parent 随后修改，确认累计两者成功 patch、排除继承历史，并保留 child 的原生 parent
    与 Turn/item 归属证据；不能把累计结果当作最终净 diff。子任务运行中或无法读取时只
    省略总计，已知文件数字保留，completion 不等待子任务终态。没有 live 条件时明确报告
    未验证，不能以 synthetic 代替。可在服务账号的已登录环境运行
    `timeout --signal=INT --kill-after=10s 900s .venv/bin/python scripts/probe_child_files.py --live`
    （macOS 用 `gtimeout`；可加 `--model <native-model>`）。该独立探针不包含在常规 SDK
    phases 中：它在唯一临时 Git 目录验证继承历史排除、真实 child→root 消息、父子 patch
    累计 `+4/-1`，再对旧 child 追问，要求总计未知且不导入旧 child 历史。它消耗模型用量，
    只对本探针创建的 exact root Thread 请求一次归档，由 App Server 负责子任务树的
    shutdown/归档，不随后重复归档 child；临时文件移除，归档历史保留。根 Thread 创建
    响应丢失或归档失败时可能留下未归档测试历史，不声称全部清理成功。探针不发送飞书消息，
    原生 Codex 可能自动把临时 Git Project 的 trust 写入用户配置。探针在 finally 中仅
    清理本次新增的 exact fixture trust 条目，保留原有条目及其他配置；无法确认清理则失败。
    JSON 只记录身份、事件类别、计数和验证结果；任一步
    证据不足即失败，原生调用失去响应时仍需外层进程 timeout 兜底。
    强制终止进程可能阻止 finally 执行，不能据此声称配置已恢复。
    Project 内文件显示相对路径，Project 外文件
    显示脱敏逻辑位置；所有条目隐藏大小、按钮统一为“发送”，按 8 个一页完整翻页，可见正文
    不出现绝对路径、文件列表预览、diff 正文或发送全部；v5 callback payload 则必须逐项携带明文
    absolute path，并对已知统计携带成对 `a/d`，翻页后完整保留整轮统计与
    Goal/Activity/Result。
    依次在 P2P 平面消息、
    群主线和已有话题点击普通文件或图片的“发送”：平面卡片必须出现以该卡片为锚点的话题，记录真实
    root/parent/thread 返回；已有话题必须保持原 thread ID，飞书能正常预览/下载实际文件。
    切换到另一 Binding 后旧卡仍能翻页和发送；正常重启 Netizen/App Server 后，再点击
    重启前的 v5 卡，必须只从 callback 内的完整 Reply Card manifest 恢复，且不读取 source
    card、Binding 或 completed Turn。页码表单至少使用三页文件，依次选择第二页、第一页、
    再次选择第二页并提交，最后一次点击必须仍生效；再直接选择末页，确认每次都只更新原卡。
    同一份已渲染 callback 的重复投递仍
    只能处理一次。缺少或带畸形 nonce 的当前版本 payload 仍须只按业务字段解码；正式推广前
    的其他旧测试 schema 不做兼容验收，升级前 v3 卡仍须明确提示已过期且不发送文件、不读取
    history。
    重复点击同一按钮不产生重复文件消息。再分别在点击前删除文件、改成目录、把同一上报
    路径重新绑定到另一个普通文件和删除原卡片：前两类不可用目标应失败，重绑路径发送点击
    时当前内容，删除卡片失败；所有失败均保持原卡且没有文件掉入主聊天。翻页还必须确认
    最终回复区逐字保留、缺失条目在原页显示不可用。另准备 100 和 400 个文件的大卡片，
    确认所有多页卡片都显示页码下拉框和一个“跳转”按钮，单页卡片不显示导航。
    完成真实 create/update；在飞书实际选择末页、中间页和首页并
    点击“跳转”，确认 submit callback 同时保留完整 `value` manifest 与所选页码的
    `form_value`，每次更新后仍能再次跳转，且只在提交按钮中出现完整 manifest。重启前后
    同卡必须保留 Goal/Activity/Result、整轮与逐文件统计；不能只用合成回调证明
    表单点击可用。记录实际序列化容量与平台返回，检查各页都不超过 55,000 bytes；
    任一页无法完整容纳时应明确省略 Files，不截断。没有 live 条件时明确记录这部分
    未验证，不能宣称真实表单兼容或容量验收通过。
    正文图片预览另按 ADR 0065 验收：ordinary、Side 与 Goal 最终正文分别引用本地图片，
    包含无 native 文件证据的已有图片；检查无 Files 时保持原富文本回复、相对 cwd、图片渲染与放大、
    Progress Card 开关和整卡更新，以及 GFM 表格/删除线/任务列表和飞书标签的显示语义。
    Files 不因正文引用增加条目，已有按钮保持可用；代码里的示例不上传。重复引用只上传一次，
    删除本地图片并重启后翻页仍保留正文图，Files 按现有规则标不可用。
    强制图片上传失败只影响该位置；强制结果卡发送失败时，富文本回退保留已上传的正文图与
    完整分段文字，不重新上传或重复完成提醒。长回复仍逐段留在原话题，平铺回复仍仅首段引用，
    完成提及只在首段；定时任务部分发送或未知投递不重发。这一客户端渲染
    live gate 尚须在真实目标应用执行；本地合成测试不声明已经通过。
    P2P 若返回 230071 必须记录为
    本轮文件 live gate 未通过，不得用 FakeChannel 或普通主线发送替代。最后确认这些操作不
    改变当前 schema 表、Binding、Turn settings、Task Feedback、Context Boundary 或 Side
    route 行数。

CLI 中新增的消息不要求回填飞书；验证目标是共享原生后端和可接续性，不是两个 UI
的逐条镜像。
