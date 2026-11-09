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
| 配置项目、登录 Admin 或维护管理员 | [配置与管理页访问](#配置与管理页访问)。 |
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

程序安装在选定的长期 Python 环境，实例 root 只保存数据。
Adapter 的两类门禁与 Activity/Side 降级见[SDK 边界](design.md#sdk-适配边界)和
[阶段性答案](design.md#阶段性答案)；Activity 降级不能绕过 service-wide cleanup 启动门禁。
按[代码门禁](#代码门禁与按需实时兼容性验证)运行受影响的 capability 与 disposable lifecycle probes。

## 目录

```text
<selected-python-environment>/
  .../site-packages/netizen_cli/               # 程序、Admin 静态资源
    resources/skills/                         # 内置 Skills
    resources/config.example.yaml
  bin/netizen                                 # console entry point
<NETIZEN_ROOT>/                                # 默认有效账号 ~/.netizen
  .netizen-root                               # 私有归属标记
  .netizen-initialized                        # 显式初始化／清理阶段证据
  config.yaml                                # 私有 Channel 配置
  lark-app/config.json                       # 固定 netizen profile
  credentials/admin-user-<sha256>.json        # 每个 App ID + Open ID 一份私有用户凭证
  worktrees/                                 # 默认受管 checkout 根；可在 Admin 改以后新建位置
  attachments/                              # 按需创建的私有输入原件目录；无全局初始化标记
    .staging/                               # 每次输入独立准备；旧残留不在启动时扫描
    retained/                               # 已发布原件；Admin 单文件人工删除
  state/
    admin-users.json                         # 当前应用的管理员名单与展示快照
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
清单，永不递归删除 root／home，不删除未知文件、Project、Worktree 内容或共享 Codex 状态。
purge 删除 Channel 数据库后受管工作区登记也消失；磁盘目录保留不等于可自动恢复登记，
重新初始化不会收编这些目录，清理前应自行保存必要的路径与成果。
输入附件原件也保留，不纳入递归 purge；需要清理时可先使用 Admin“文件附件”。
名单、经校验的新格式用户凭证、旧 `credentials/admin-user-tokens.json` 及遗留
`credentials/admin-web-secret` 均属于精确 purge 清单；普通 setup/start/update 不自动删除
旧凭证或遗留 secret，也不再读取它们。
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
CLI setup 的官方 SDK 浏览器流程会请求下面的应用身份权限、`im.message.receive_v1`
应用身份事件和 `card.action.trigger` 回调，以及 Admin 所需的 user 类型
`component:selector`、`offline_access`。手工准备的应用也必须逐项配置。
安装期权限检查区分 tenant/user 类型；同名的另一类型授权不能代替。应用开通不替代
每位管理员的用户授权，新 Admin 登录统一请求上述两个 user scope。
无论来源，飞书应用版本在发布前都必须确认：

- 单聊事件投递具备 `im:message.p2p_msg:readonly`，普通消息能力具备 `im:message`；
- `/projects` 等卡片回调识别会话类型具备 canonical `im:chat:read`；官方“获取群信息”接口
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
重试耗尽时，普通/Side Turn 与 Goal logical run 都回退到无文件富文本/静态文本、
有文件完成卡的标准路径，不能因为更新失败丢掉权威结果。Goal 管理快照独立于回复。

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
start/steer。另分别发送 locale 正文与顶层 `post.files` 并存的真实普通文件和文件夹：
普通文件应保存原件并携带本地路径提交，保留正文与 @ 语义，按[输入文件附件](design.md#输入文件附件)验收；
文件夹应明确提示不受支持且零 start/steer，不能因 SDK 不生成资源描述符而绕过。
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
4. **审查新增能力的价值与归属。** 对与 Netizen 有关的变化说明用户收益、当前缺口、
   公开能力层级、维护成本、职责边界和所需验证证据，并逐项决定本次接入、原生生效、
   延期或不适用。区分必须适配的契约变化与主动选择的产品扩展；不能因“可选”一律延期，
   也不能只因出现新字段就制造接入需求。延期项写明重新评估条件。核对错误/终态、身份、
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
6. **交付并维护审查结论。** 报告开头和审查结束回复固定包含四部分：**SDK/App Server
   改动简介、改哪些、验证哪些、增加哪些**。分开介绍 Python SDK 的接口/数据变化与
   App Server 的执行行为，用用户操作和实际功能说明影响，不只罗列字段与文件名。
   记录版本基线、官方来源/源码差异、逐项兼容债务决定、新能力取舍及依据、实际检查与
   未覆盖边界；无须改动、不接入或无可删除封装的项也说明原因。明确当前处于审查、实施
   还是验收阶段；讨论改变取舍时更新同一份报告，作为后续实施依据。
   单次版本分析、产品选择与待验收计划放审查报告；长期行为和决策同步设计、ADR 与
   用户手册；只有完成对应验证后，才把通用兼容性摘要写入
   [兼容性结论](#已验证的兼容性结论)。不能将候选计划写成已验证事实。单次日志、exact IDs
   和私有环境信息按该节约定留在验证产物中。升级审查不授权发布或部署。

### 代码门禁与按需实时兼容性验证

所有面向 `main` 的代码先通过 `make check`；PR 和 main push 的 GitHub CI 都在 Linux x64
标准 CPython 3.11-3.14，以及 macOS arm64 标准 CPython 3.14 执行这一个统一本地门禁；
这 5 个 job 都是 `Protect main` ruleset 的 required status checks。
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

Worktree 目录选择、定时工作区交接或原生 cwd 采用边界变化时，从源码 checkout 运行：

```bash
probe_evidence_dir="$PWD/docs/research/compatibility"
mkdir -p "$probe_evidence_dir"
timeout --signal=INT --kill-after=5s 120s .venv/bin/python \
  scripts/probe_scheduled_tasks.py --phase worktree --timeout 100 \
  --output "$probe_evidence_dir/scheduled-worktree.json"
timeout --signal=INT --kill-after=5s 90s .venv/bin/python \
  scripts/probe_worktree_cwd.py --output "$probe_evidence_dir/worktree-cwd.json"
```

macOS 将 `timeout` 换为 `gtimeout`。前者使用生产 Scheduler、Channel dispatch、Runtime
和临时 Git，分别验证 `new/existing` 工作区的首次及后续输入：native cwd、Binding/Run
资源身份、completion cwd、实际请求中的本次输入、首轮交接和来源 current pointer。后续
输入从 Runtime 提交，不覆盖完整 Channel 消息准备；后者验证跨 App Server 的
cwd 冷恢复及 Runtime 采用。定时 Worktree 探针隔离 HOME 和 CODEX_HOME；cwd 探针仅
隔离 CODEX_HOME，HOME 继承调用者环境。两者使用 loopback mock 模型，不使用真实模型
账号，不发送飞书消息，也不修改业务 Project 或用户 Codex 配置。
它们不替代真实飞书表单/回调、Skill/Files 或真实模型 MCP 参数生成的验收。

输出包含源码 commit、dirty 标记和 tracked diff 摘要，只保留固定断言结果及错误位置，
不包含配置、业务路径或消息正文。将 JSON 保留在上述忽略目录或 CI artifact 中，并与
审查/提交关联；dirty 结果只能证明当时工作区，不能冒充 exact commit 的验收，正式更新
兼容性结论前需在对应干净提交复验。失败 JSON 也保留，新增脚本或成功退出之外的文件
存在本身不算验收通过。既有真实模型定时 phases 同样支持 `--output`，仍需显式 `--model`。

过程卡展示设置与时间组织（ADR 0086）调整，需在本地覆盖默认兼容、独立窗口/分段、
配置捕获、旧卡及新 manifest 往返，并实测最大配置组合的中文、转义和 callback 编码容量，
回归既有超限处理。只改展示而未改变 SDK adapter、原生消费/生命周期或租户能力时，
不因此触发整套账号 live phases。Admin 表单的真实浏览器操作、飞书桌面/移动端的两种组织、
终态折叠与服务重启后文件分页仍须单独验收；本地序列化或 API ACK 不证明客户端显示。
真实卡片测试仅使用用户明确授权的测试群/话题，未执行的项明确记录为未验证。

全局过程卡兜底（ADR 0087）调整另覆盖实例 YAML 缺省/保存/冲突、v18 → v19 原值保留、
未配置意图的持久化与各表单往返，以及普通新 Turn、Goal 新执行和 Side 创建时的有效值
捕获。验证全局修改不影响显式配置、运行中执行、Goal 换轮或已有 Side，解析值不得回写
会话。真实 Admin 验收包括“系统管理 → 过程卡”的保存/放弃、未保存提醒，以及既有
会话/默认配置表单仅修改其他选项时仍保留未配置；未触及原生边界时不扩展账号 live phases。

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

归档恢复边界（ADR 0082）变更时，运行
`.venv/bin/python scripts/probe_thread_restore.py --output /tmp/thread-restore.json`
并设置外部 90 秒进程 deadline；可用 `--timeout` 调整默认 60 秒的内部总时限。专项使用
生产 CodexRuntime、临时 HOME/CODEX_HOME/cwd、本地 mock Responses provider 与真实
bundled App Server，验证正常恢复、已恢复、missing、writer 拒绝和原 Turn rejoin。
它不操作既有用户会话，不替代真实模型 Goal 继续或飞书卡片端到端验收；结果须单独记录。

Provider 准入分类（ADR 0093）或 SDK/App Server 升级时，运行隔离生产 Runtime 探针：

```bash
timeout --signal=INT --kill-after=5s 90s .venv/bin/python \
  scripts/probe_model_provider_admission.py --timeout 60 \
  --output /tmp/model-provider-admission.json
```

macOS 使用 `gtimeout`。它使用临时 HOME/CODEX_HOME/cwd、真实 bundled App Server 与
同一 loopback mock URL 的两个 provider，验证旧会话被拒且零模型请求、全局准入保持、
新 Binding 可完成、切回原 provider 后 exact Thread 可继续，并核对用户配置不变。
不使用真实账号、不发送飞书消息；三种拒绝变体及 Side/Goal/compact 等入口另由行为测试
覆盖，真实配置策略和客户端显示仍须独立验收。

阶段性答案、命令大输出持久化、压缩或卸载边界变化时，可运行两个显式原生专项：
`scripts/probe_sdk_partial_answers.py`（外部 150 秒 deadline）和
`scripts/probe_sdk_native_boundaries.py`（外部 180 秒 deadline）。两者均可用 `--case`
选取场景、`--output` 保存证据，使用临时 HOME/CODEX_HOME、回环 mock Responses provider
和真正 bundled App Server，不使用真实模型账号。前者覆盖阶段性答案、命令/MCP 输出冷恢复，
以及通过生产 Goal Tap/CodexRuntime 验证两轮自动 continuation、阶段性片段保留与四证明收尾；
后者覆盖 local/remote-v2 手动压缩成功和 provider 错误、超过 10 秒 idle closing、慢
required MCP 启动断连回收及压缩准备失败。后者直接使用公开 WebSocket wire，是测试 harness，不能
复用于生产私有 gateway；目前依赖 POSIX 文件锁。mock provider 的原生执行通过不代表
真实模型触发频率、压缩自动路径或飞书展示已验收。

历史分页适配（ADR 0083）变化时，运行
`timeout 210 .venv/bin/python scripts/probe_thread_history.py`，保存标准输出证据。它使用
生产 `AppServerThreadHistory`、隔离原生状态与本地 mock provider，覆盖 paginated/legacy
历史、opaque 游标续页、最近 items、非法游标后的连接复用和归档读取；不访问生产账号
或飞书，不替代真实模型自主调用工具的验收。

Goal 管理/回复分离（ADR 0084）的控制变化另运行
`scripts/probe_sdk_partial_answers.py --case goal_controls --output /tmp/goal-controls.json`
（外部 150 秒 deadline）。HTTP response barrier 保持真实物理 Turn 运行，核验 pause、
同流 resume、clear 不 interrupt，清除后当前 Turn 仍交付，以及 `/stop` 的 pause、exact
interrupt 和 cleanup；同时复验 `goal_partial_rollover` 与受影响的取消场景。它们使用
生产 Runtime/Adapter，不将本地 mock 模型结果宣称为真实模型或飞书展示验收。

取消、incomplete response 或冷恢复权限变化时，另运行
`scripts/probe_sdk_cancellation.py`（外部 180 秒 deadline）和
`scripts/probe_sdk_response_recovery.py`（外部 500 秒 deadline）。两者复用上述隔离
mock provider 和真实 bundled App Server，均支持 `--case` / `--output`。取消探针区分
动态工具与 Code Mode 的真实取消、同 Thread 恢复，以及 Goal 自动 interrupted 续跑的
**观测失配防护与局部恢复**；后者通过生产 Runtime 验证仅保留当前 Goal unknown、禁止
旧最终结果、另一 Binding 在故障 Goal 仍运行时可执行，以及确认原生空闲后原 Thread
可继续普通对话；不证明 SDK 完整续流或最终 blocked 交付。响应探针通过生产 Runtime
验证稳定片段、失败释放、上下文用量与
同 Thread 续聊，并在三个独立 App Server 中验证 cold resume 的实际允许/拒绝文件操作和
显式权限覆盖。另覆盖 request/stream retries=2 时不可重试错误仍只发一次请求，以及
`interrupted` 响应在同一普通 Turn 内自动继续。它们不覆盖网络 grants、managed policy
或 Responses WebSocket。

阶段性答案的真实飞书传输另用 `scripts/probe_feishu_partial_answers.py`，仅在用户明确
授权的测试群运行。先用 `--dry-run` 检查生产渲染器生成的载荷；实际投递去掉该参数：

```bash
.venv/bin/python scripts/probe_feishu_partial_answers.py \
  --config "$NETIZEN_ROOT/config.yaml" --chat-id "$probe_chat_id" --dry-run
```

完整探针发送六张明确标注“验收／模拟”的卡、执行二十次更新，并发送五条富文本样例；
不创建真实任务，不提供真实 Goal/文件控制目标。`--only-rich-text` 只发送五条富文本，
不重发卡片；没有自动重试未知投递。它分别记录 API ACK、exact 目的地回读、文件翻页
manifest 的本地往返与可读正文核对，不把合成投影冒充原生端到端或用户真实点击。
若消息读取 API 返回“请升级至最新版本客户端，以查看内容”的明确兼容占位内容，
只能证明消息身份，正文核对必须保持未验证；API ACK 和占位正文均不证明真实客户端
显示或回调可用。实际测试群、消息 ID 与查看者反馈仅保存在私有证据，不写入公共文档。

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
该 phase 不等待也不冒充最后订阅者离开后的卸载验证；何时卸载由 Codex 的
`thread_unload_delay_secs` 决定（0.167 默认 60 秒），Netizen 不设置。当前协议没有
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
随后在同一已命名 Parent 的新上下文上调用生产候选生成入口，验证继承最近项目标识、
无工具输出、临时分支清理与目录不可见，候选返回不改名、显式保存后才更新名称。
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

terminal cleanup/Activity 保留各自精确版本、整包指纹门禁，Activity 另须非消费校验；
其余 capability shape/synthetic/live 门禁按[SDK 适配契约](design.md#sdk-适配边界)分别执行，不得互相替代；
Delete 仍须通过 disposable lifecycle 及原生结果/局部 unknown/零自动重试回归。

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

#### 复验环境与配置

- `include_turns=False` 的 resume 保持 exact Thread 和模型上下文连续性；该选项仅省略
  返回历史。普通持久 Thread 需要终态或 Files 证据时另做公开 read；ephemeral Side
  fork 同样使用此选项，但仍由唯一 `handle.run()` 确认终态。
- 原生 `update_plan` 默认关闭，plan fixture 显式开启；生产 Thread 继承用户工具配置。
  checklist 整体替换与非消费 Activity 观察必须保留同一公开 stream 的原始通知。
- Project 删除的原生兼容性覆盖 mixed-sessions 与 orphan-Side；四视图 absent、
  Side 关闭、跨重启 tombstone 和 cwd 保留是验收要求。真实飞书 topic 与浏览器传输
  不在这项原生探针的覆盖范围内。
- foreground tool process 不属于 background-terminal registry；
  `interrupt` 和 terminal cleanup 成功不证明前台进程已退出。
  `foreground_process_exited_within_5s=false` 是受支持分类，但 native Turn 必须进入
  `interrupted`、same-Thread resume 必须成功，probe 自己不得遗留 marker。
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

使用自定义 `model_catalog_url` 的部署须验证 endpoint 与 discovery 配置；显式目录失败或
对应 discovery 未开启时没有 bundled 目录回退，Netizen 沿用空目录拒绝。

### 定时任务兼容性与验收

定时任务的行为与失败边界见[设计文档](design.md#定时任务)。相关变更先通过 `make check`，
再按触及的边界选择 disposable SDK、真实飞书、跨主机 Admin 和安装回滚验收；普通安装
不自动创建计划或发消息。源码检查、传输替身、API 接受和客户端点击分别记录，不互相
替代，也不把旧候选结果当作后来修改边界的验收结果。

| 记录 / 版本 | 验证层级 | 未覆盖与复验入口 |
| --- | --- | --- |
| [2026-09-23](compatibility-history.md#scheduler-binding-20260923)，`0.155.1` / `gpt-5.6-sol` | 原生 binding start/steer 与 MCP CRUD；飞书替身 | 真实锚点/catch-up/客户端和归档恢复端到端；复验 binding、mcp |
| [2026-09-22](compatibility-history.md#scheduler-phases-20260922)，`0.155.1` | 原生 mcp、mcp-recovery、dispatch、manual；传输替身 | 无新版真实飞书端到端证据；复验相应四阶段 |
| 同记录中的 `0.147.0` 基线 | 真实飞书五类来源默认目标/Project，三类聊天首轮与结果归属 | 不证明 `0.155.1`、任意自然语言无歧义或模型即时发现/生成 |
| [2026-09-21](compatibility-history.md#scheduler-manual-20260921)，`0.154.0` / `gpt-5.6-sol` | 原生 manual 与替身投递 | 飞书点击、移动端、原生权限/网络故障组合、目标机失败回滚及压缩后 MCP 管理仍待验收 |

探针与命令见[代码门禁](#代码门禁与按需实时兼容性验证)，原生探针不发送飞书消息。
新 Thread 空 metadata 或分页暂不可用的读取仍受 [5 秒/3 次 I/O](design.md#普通-turn-终态与观测恢复)约束。
上述证据未直接观察 deferred search；跨会话 catalog cache 只适用于 stdio，不为 HTTP adapter 添加兼容层。

触及对应边界时，候选须完成以下验收：

| 边界 | 验证内容 |
| --- | --- |
| MCP 接入 | 生产传输鉴别、Host/Origin、大小/超时、不依赖 Admin 浏览器登录、同 cwd 身份隔离、缺失/冲突元数据、用户 MCP 和指令继承、冷恢复/fork/服务重启与原生权限组合 |
| 调度与存储 | 四类规则、时区/DST、截止、高水位、宽限/missed、两类目标的交接屏障、原会话未知不阻塞下一到期点、手动不改时间游标、不同计划并发、CAS/幂等、裁剪后原 Run 回执及每个交接断点的重启行为 |
| 原会话输入 | exact Binding/Turn 身份、空闲 start 与运行中 steer、Goal 换轮、配置/上下文竞态、系统来源、原发起人及结束提及保留、catch-up 锚点与接收后游标提交 |
| 飞书与普通生命周期 | 五类来源的自然语言和 /cron；真实 root/seed 或原位置锚点、结果精确归属、卡片完整表单/重试/分页、Activity/Files、停止/归档/删除及极快终态；原会话切换/归档暂停、恢复不补跑及删除联动 |
| Admin 与安装 | 跨主机认证/CSRF、三个入口一致性、Project 删除与在途交接、App 切换、当前库重装、受支持旧库迁移与不受支持库只读拒绝、启动迁移提交前后失败、实例恢复报告、内置 Skills 来自实际安装包 |

Scheduler/MCP 的进程归属、环境继承、ready 前准入、停机排空及重启不补跑，
统一按[定时任务契约](design.md#定时任务)和[调度交接](design.md#调度交接与结果)验收。

启动边界验收包括当前 schema 显式初始化、受支持旧库迁移、不受支持库只读拒绝、
元数据与 Side/Project 墓碑保留。迁移持有 lifetime lock；提交后失败不自动恢复旧数据库。

#### 定时工作区扩展验收

2026-10-20 已确认的方案修订见 [ADR 0061](adr/0061-schedule-ordinary-threads-in-feishu-topics.md#新话题工作区策略)
与 [工作区设计](design.md#新话题工作区)。以下是本次扩展的验证要求，实际完成的范围在本节
末尾分别记录；既有定时任务或默认会话工作区证据不能替代本次验证。本轮实施授权不包含部署或发布。

- 数据与 API：冻结 v16 → v17 与完整 v14 → v17 迁移、新库等价、重复启动、失败回滚；
  旧有效 new_topic 迁移为 `project`，binding/墓碑为 null，历史 Run 的空资源回执合法。
  严格拒绝非法策略与任意路径；create 缺省、update 保留/替换、修改 Project 后 exact
  身份复核、binding 拒绝 workspace 覆盖，未变失效资源允许无关字段修改。
- 共享服务与 Git：options/预览/保存及 busy/missed/blocked 无创建副作用；existing
  选定 cwd/id、不切分支；new 每个实际 Run 仅一份 detached 工作区，默认本次 HEAD、
  显式 ref、项目子目录、无效 ref 与非 Git 失败均准确。不读取 `/defaults` 或来源 Worktree。
- 发布和准入：单次 publication 所有权先于工作区副作用；existing 首发前已知不可用时
  零 root/seed、零 Binding、零 native submit，保留 enabled 和明确原因；自动到期不补跑。
  手动认领前拒绝无 Run/游标变更，认领后失效保留失败 Run。重复请求/dispatch、定时与
  run_now 并发及认领后编辑/暂停/删除不能重复创建或改变冻结策略；claim 到 attach 不插入 await。
- 失败与重启：root/seed 后创建失败、工作区成功但 Binding/首轮失败、Git 取消/超时/未知，
  以及创建中/绑定前 Project 或 Worktree 删除。资源 ID 在 Git 创建前已关联 Run，进程
  中断后能定位保留资源；不盲重试、删目录或重建同一 Run 的工作区。区分 Git、飞书与
  native 未知，确认的消息不能改称未知；未提交原生输入可证明时正确释放交接屏障。
- 时限与生命周期：共享创建 120 秒预算与 dispatch、停机排空和 Project 删除等待相容，
  长 Git/飞书/native I/O 不占 Scope/Binding 锁或 SQLite 事务。Project 删除仍墓碑计划；
  Worktree 删除不删除引用它的新话题计划，显式级联删除 Binding 仍清理其原会话计划。
  暂停/删除计划不回收资源，历史 new Run 工作区删除不影响未来独立创建。
- 三个入口：Admin 列表/详情/编辑、`/cron` 摘要/详情/编辑和 MCP list/view/mutation
  一致展示策略、失效具体原因、`can_run_now=false` 与最近执行资源回执；未知不伪装已删除
  或可用，失效选择保留。原会话只读继承实际目录。状态按需、有界读取并按资源去重，
  不扫描完整仓库或增加持久健康状态；陈旧界面仍由后端复核。
- 共享 UI 与 MCP：Admin 和卡片复用工作区策略组件，加载工作区/查群/失败重绘完整保留
  分组字段、指令、时间、模型与身份/版本/request_id，核对元素数和字节预算及移动端布局。
  模型/聊天/工作区选项错误独立。`list_worktrees` 保持调用者 Project 范围，
  `cron_manage options` 提供计划目标选项，已知 exact ID 可直接写入，不新增 MCP 转调或扩大权限。
- 原生与平台：按受影响边界用一次性资源验证新首轮/后续消息、Skill、Files 的真实 cwd，
  原会话 start/steer/Goal 不回归；真实 MCP 参数生成、飞书 root/seed 和卡片回调、
  目标主机启动迁移分别记录。替身测试和 `make check` 不等于这些 live 验收通过。

2026-10-20，SDK/App Server `0.163.0`：隔离原生目录/后续输入探针（mock provider、飞书替身）
与本地门禁通过。真实 MCP 参数、飞书客户端、目标平台启动迁移及该组合的 Skill/Files/完整生命周期仍待验收；
复验用 `probe_scheduled_tasks.py --phase worktree`。[完整记录](compatibility-history.md#scheduled-worktree-20261020)。

#### 定时新话题自动归档扩展验收

2026-10-22 已确认的方案见 [ADR 0061](adr/0061-schedule-ordinary-threads-in-feishu-topics.md#新话题完成后自动归档)
与[完成后策略](design.md#新话题完成后自动归档)。以下列出验证要求，实际结果另记于本节
末尾；既有手动归档或旧调度探针的结果不能直接证明该策略已验收。本轮不包含部署或发布。

- 数据与三个管理入口：冻结 v19 → v20、完整 v14 → v21 路径、新库等价、重复启动与
  失败回滚；旧计划和墓碑默认关闭。`auto_archive` 为独立严格布尔值，创建省略关闭、
  更新省略保留，原会话显式设置拒绝；Admin、`/cron` 和 MCP 查询、编辑、草稿恢复一致。
- 认领与交接：自动到期和立即运行均从 `Claim.plan` 捕获选项，之后修改、暂停或删除
  计划不影响已认领的一次；普通会话、原会话目标和后续人工 Turn 不获得该策略。
  不在 Run 中保存新的终态、归档回执、结果历史或策略快照。
- 完成与送达：exact initial Turn 为 completed 且无错误，原交付链确认 sent 后才归档；
  覆盖极快终态、卡片与富文本、分段回复和 Files。失败、中断、未知、无有效完成结果、
  分段未全部确认、投递失败或未知均不归档，既有异常与回退路径不变。
- 原生生命周期：验证准确 Binding/Thread 归档，Scope 后来切换的会话不被误归档；
  用户已续聊或启动后续 Turn 时仍直接归档。沿用原生关闭、预约及锁外调用，明确失败与
  未知结果继续既有有界对账，不能倒改成功送达、自动重跑任务或扩大为服务级故障。
- 展示与恢复：不修改结果卡、过程卡、Files、富文本或发送额外归档通知；归档保留
  话题、历史及工作区，恢复通过 `/sessions archived` 的既有能力。重启收敛 Run 不重放
  输入、结果或归档，查历史和刷新最近执行也不触发补归档。
- 实际证据：通过 `make check` 后，按受影响边界用隔离原生资源验证完成送达后的归档及
  归档时已有后续工作；真实 MCP 参数、飞书卡片表单和客户端、目标平台启动迁移分别记录。
  飞书替身回执、Node DOM harness 和本地迁移测试均不等于相应平台验收。

2026-10-22，SDK/App Server `0.163.0`：生产链路 5 组隔离原生场景与本地门禁通过，使用 mock
provider 和飞书替身；真实 MCP 参数、客户端布局/回调、目标平台启动迁移及部署/发布未验收。
[完整记录](compatibility-history.md#scheduled-archive-20261022)。

### 普通会话协作兼容性与验收

2026-10-16，SDK/App Server `0.163.0`：协作预览、原生分页 mock 探针和本地门禁通过；
不覆盖真实模型、飞书/浏览器视觉、动态目录快照一致性或生产规模/部署。
[完整记录](compatibility-history.md#collaboration-pagination-20261016)。


[ADR 0081](adr/0081-coordinate-ordinary-sessions-with-native-external-messages.md) 的六工具、
共享 MCP 传输或 ExternalMessage Runtime 交接变更，需完成 `make check` 及受影响的
原生 probe。可运行以下隔离探针，并以外部进程 deadline 限制整个进程（全部场景建议
240 秒；脚本内部每场景另有 45 秒界限）：

```bash
.venv/bin/python scripts/probe_session_collaboration.py \
  --output /tmp/netizen-session-collaboration-evidence.json
```

`--case sdk_create_idle|sdk_join|runtime_join|runtime_terminal_race|session_mcp|suppress_reply|runtime_external_name|runtime_external_title` 可选择
受影响场景。脚本复用现有本地 Responses mock provider，运行真正 bundled App Server，
只使用临时 Codex 目录与 loopback，不调用真实模型账号或发送飞书消息。Runtime 竞态
只延迟一次真实公开 history read 的返回，不伪造原生 Turn ID 或完成结果。

`suppress_reply` 的工具 metadata 或结束交接调整时，运行对应 case（外部 70 秒 deadline）：
它验证生产 MCP 收到准确 Thread/物理 Turn，工具成功后 Turn 仍在运行，以及后续空模型
响应使原生正常 completed、Thread idle、SDK final 为空。case 使用回调 fixture 和本地模拟
模型，不代表真实模型决策或飞书客户端验收。完成表情、原卡折叠、无卡 @ 及结果未投递，
仍须在明确授权的测试会话单独验收。当前[静默规则](design.md#当前物理-turn-的结果静默)和
[Side 协作来源](design.md#普通会话协作)由设计维护。

| 记录 / SDK | 验证层级 | 未覆盖 |
| --- | --- | --- |
| [2026-10-28](compatibility-history.md#suppress-reply-20261028)，`0.163.0` | MCP 静默及 ordinary/Goal 来源登记的隔离原生探针 | 真实模型决策、飞书投递/客户端 |
| [2026-10-31](compatibility-history.md#suppress-reply-20261031)，`0.163.0` | 本地门禁；ordinary steer / ExternalMessage join 的 mock 原生交接 | Goal/Side/失败路径仅行为测试；真实模型静默改善、飞书及部署未验收 |
| [2026-10-26](compatibility-history.md#side-source-20261026)，`0.163.0` | 本地门禁；真实 App Server + mock provider 的 Side 来源/历史 | 真实模型发送审批、飞书及部署 |
| [2026-10-15](compatibility-history.md#collaboration-20261015)，`0.162.1` | 本地门禁、五组 mock 原生场景与 Goal 预检回归 | 模型自主六工具编排、实际 mutation 审批、完整 145 秒等待、真实飞书/部署及本机 Node 22 |
| [同日命名/回执](compatibility-history.md#collaboration-naming-20261015)，`0.162.1` | mock 原生命名两场景、真实模型 naming runtime-only、本地回执故障回归 | 独立命名中断未复跑；自主编排、create/send 审批、新话题客户端、部署及本机 Node 22 未验收 |

真实飞书单聊/群/话题须按创建→读取→发送→等待及人工干预路径验收；旧工作区记录不能代替当前候选。

### 持久分支、恢复与删除验收

[ADR 0078](adr/0078-fork-persistent-sessions-and-trust-native-lifecycle-results.md) 的三项边界
与 [ADR 0082](adr/0082-restore-archived-sessions-from-exact-native-results.md) 的归档恢复分别
验证，不能用临时 Side 成功代替持久 fork，也不能用目录展示代替恢复或删除的结果。

| 范围 | 自动化与原生验证 | 飞书客户端验证 |
| --- | --- | --- |
| 创建与继承 | exact idle 来源、原生历史/权限、同 cwd、显式设置复制、群目标 catch-up 新边界及单聊 current-only；零新增 Turn 的完整 Binding 与原 handle 订阅/释放 | 当前聊天、其他群与手填其他机器人单聊 ID 各一个新话题；创建中和成功更新同一根卡；名称、链接和来源保持正确 |
| 群选择与提交 | Cron/Fork 共用完整有界查找快照，与 Files 共用页码控件；任意跳页不读取群目录/头像，单页无导航；完整清单、所有页及保留选择均过容量检查，不截断；只用所选目标方式，来源/current/revision 变化时不覆盖；失败保留草稿和新 nonce | 初始不预载目录；查找下方显示带头像的结果框，框下按需显示页码和唯一跳转按钮；首→末→中→首后群名/头像/已选群及名称/任务/模型草稿保持；重查清空旧选择；过多或查询未完成提示细化关键词/填 ID，空群或无效 ID 可修正重试；受理后禁用提交、成功移除按钮 |
| 创建交接 | root/seed 响应阻塞时提前输入；默认配置先创建则 fork 冲突，fork 先提交则输入进入分支；每阶段失败、Project 删除和 shutdown | 提示等待完成后再发消息，失败只报告已知事实，已成功会话不会因回执失败被撤销 |
| 普通恢复 | `activate_exact` 直接 resume，无 list/read 预检；未发新消息且列表缺席分支、Lazy、运行中 rejoin、明确拒绝、未知结果、本地提交失败和旧输入失效 | `/resume`、列表切换、Admin 同一语义；成功后反馈，拒绝保留 current，不卡在目录“未确认”状态 |
| 归档恢复 | 无 list/read 预检；unarchive 同 ID 成功或两条精确移动前拒绝才继续单次 resume；missing/writer/closing/再次归档、部分成功、Binding-local unknown、本地提交前后失败；运行中保留执行 handle，已 current 保留 anchor/revision | 命令、归档卡和 Admin 共用结果分类；已恢复仍可切换，明确拒绝保留关联；部分成功如实提示，卡片失败不重发恢复 |
| 普通删除 | 成功才删 Binding，RPC 拒绝保留并清理失效投影，传输未知局部隔离；零自动 list/read/retry；来源受分支历史引用限制 | 普通/归档/分支相同确认与反馈；不会把源会话删除解释为独立分支级联删除 |

2026-10-16，SDK/App Server `0.162.1`、同步后的 `0.163.0`：六组生产 Runtime 隔离原生恢复
场景与本地回归通过；不含真实模型、Goal 自动续跑、飞书回调延迟或部署。
[完整记录](compatibility-history.md#restore-20261016)。

真实原生持久性验证仅使用 disposable cwd 和自建 Thread：有历史的来源创建持久 fork，
在分支尚无新增 Turn 时同连接 resume、关闭首个 App Server 后冷 resume，核对 exact ID
和继承上下文。目录可能漏列，但不影响直接恢复。随后验证来源的历史引用删除拒绝，
只清理本次自建且身份明确的资源，不能重试结果未知的原生删除或按名称猜认资源。
`/fork` 的五种来源入口（P2P、P2P 话题、普通群主线、普通群话题、话题模式群）和跨群
真实发布需单独验收；不以 FakeChannel 或 native-only 探针宣称通过。

2026-10-09/10（SDK 版本未另记）：本地门禁、禁用动作的真实预览卡 API ACK 和有限人工展示
确认通过；完整查找/分页/提交重试、移动端与部署仍待验收。
[逐日记录与限制](compatibility-history.md#fork-ui-20261009)。
验收仍须覆盖三选一有效输入、两个字段有值时不串目标、失败可填 ID、未触碰的可选字段、
同值重试、固定 Agent 会话只读位置；群到单聊 Fork 的 current-only 续聊与原生历史继承须单独验证。

`/usage` 在无 Project/Binding、普通群话题和有效 Side 分别验收，群聊保留 @ 规则。
由客户端显示同一 epoch 的日期、时间和时区；至少在两个设备时区核对 `local_datetime`。
确认有限额度的剩余条与百分比一致、普通 Codex 额度优先、实际存在的模型/Credits/月度
字段展示正确，缺字段不显示为充足或零；未登录/超时使用现有失败反馈。无需为此创建
任务、购买额度或触发 reset。MCP 指南只核对原生登录与同环境凭证复用说明；文档存在
不表示已验收真实 OAuth。

### 受管 Worktree 专项验收

ADR 0085 的当前实现覆盖 Admin 管理、飞书 `/new`、Agent 两工具与实际 cwd 传递。
本节是专项要求，不代表已部署或真实飞书/native 链路已经通过。`/fork` 不增加 worktree
创建/选择；environment、文件复制、快照恢复、自动回收、Handoff 和 App 私有管理互认均不在本版。

- 运行 `make check`，保留新增 `test_worktree_*`、Admin Worktree Python/JavaScript
  行为测试和 v14 → v15 冻结迁移夹具结果。临时 Git 仓库验证 detached/已有未占用分支/
  新分支、子目录、外部改分支/删目录/替换路径、fetch 失败、无 HEAD、未知创建回执与部分成功；
  不得在用户的真实成果目录上验证删除。
- P2P、群主线、普通话题分别验证 `/new` 只读加载目录步骤、原卡 Scope、Project revision
  和重复回调；默认项目目录、非 Git 无工作区字段、已有工作区不切分支。原生 Thread
  只在首条任务/Goal 物化；工作区创建成功而会话失败必须保留并可再选。公开卡片容量错误
  明确反馈，不能静默截断；卡片更新失败的同 Scope 文本反馈包含已确认部分结果。
- 在一次性 native 测试 Thread 中，用 A/B 两个目录的同名文件与 Skill 核对普通首轮、
  首条 Goal、Side、既有持久 fork、模型 `create_thread`、已有 Binding 定时输入、Files/
  图片与分支状态均使用实际 cwd。A 创建 B 不迁移调用会话；来源 B 新建会话仍在 B。
  不为测试给 `create_thread` 加 cwd 参数，也不借 /fork 创建新工作区。
- Admin 验证配置写入、旧 revision 拒绝、修改根不移动旧目录、默认不 fetch、fetch 失败
  明确停止；列表只按需读取当前页，未知/缺失/身份改变分开展示。Agent 两工具验证
  可信来源、discovery/readSend 范围、分页、默认分支与人工 HEAD 的差异、未知结果不重试。
- Worktree/Project 两个可选级联勾选均默认关闭；预览与执行绑定 exact 清单/选项并二次确认。
  校验保留会话时的活动/Side/来源使用保护、勾选时 native shutdown/delete 的精确结果、
  dirty/untracked/ignored/未保留 detached 提交保护和部分成功不回滚；真实 native 删除
  仍按既有 disposable delete phase 验收。不得用“目录缺失”或目录列表缺项替代成功响应。
- 删除 Project 保留工作区后，Admin 标示孤立、`/new` 不列入、同 alias 重建不重新关联。
  删除目录但保留会话时，保留历史与原 cwd，记录 native 实际返回，不宣称缺目录下所有
  Turn 保证成功或一律禁聊。验证 purge 保留 Worktree 内容但清理数据库中的登记。

真实飞书客户端、目标平台安装/启动迁移和受影响 native 生命周期分别记录证据；本地
fixture、Git 测试或页面 JavaScript harness 均不替代这些验收，不自动发布或重启现有实例。

2026-10-19，SDK/App Server `0.163.0`：本地 Git/行为门禁、隔离原生 cwd 交接及协作 join/换轮
复验通过；未覆盖真实飞书表单/移动端、目标平台迁移、完整缺目录/删除链路、真实模型 Skill
加载或生产延迟，未部署。[完整记录](compatibility-history.md#worktree-20261019)。
当前目录所有权与 Skill 准入分别见[实际目录](design.md#受管-worktree-与实际目录)和
[显式 Skill](design.md#显式-skill-调用与按需历史读取)。持续复验用外部 deadline（macOS 用 `gtimeout`）：

```bash
timeout 90s .venv/bin/python scripts/probe_worktree_cwd.py --output /tmp/worktree-cwd.json
```

输出移入可保留位置并关联提交，要求见[代码门禁](#代码门禁与按需实时兼容性验证)。

### 数据库迁移与中断恢复验收

[ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md) 将迁移移到每次实际
启动，保留 ADR 0075 的 v14 基线与冻结步骤。当前 schema 为 v21，冻结转换为
v14 → v15 Worktree 元数据、v15 → v16 默认工作区策略与 v16 → v17 定时工作区策略/Run
资源回执、v17 → v18 过程卡展示设置、v18 → v19 可空过程卡配置意图，以及 v19 → v20
新话题计划自动归档选项。v18 → v19 不改表结构或已有值，仅允许既有 JSON 中表示未配置；
已有默认值一律保留为显式值。v19 → v20 新增 `schedule_plans.auto_archive`，所有旧计划和
墓碑默认关闭，不增加 Run 归档状态或执行结果历史。v20 → v21 仅创建五个非唯一索引，
覆盖 TTL 到期、Binding-target Run/Plan 和 Project 未决 Run 查询；保留全部行与已有含义。
相邻 v20 → v21、完整 v14 → v21 与已经 v21 的启动均需验收，缺失或错误的必要索引拒绝启动。
v13 及更早 schema／旧布局不因此自动得到支持。
以下为新 CLI 的验收要求，不是通过记录：

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

2026-09-28：旧 release 安装器/schema v14 在 macOS/Linux 隔离服务管理器夹具中通过 10 场景；
不覆盖完整 Runtime、SDK/飞书或新 CLI，不能认证新启动迁移流程。
[完整历史记录](compatibility-history.md#installer-20260928)。

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
- 在 Admin 和 `/defaults` 分别保存项目目录、指定已有受管 Worktree、每个新会话新建
  Worktree 三种策略。确认保存/修改/删除规则都不创建或删除工作区；新建策略可选 ref，
  留空时取实际创建时 HEAD，仅检出提交内容且处于 detached 状态。检查更换 Project、
  旧选项、资源查询失败及已保存资源失效时的展示和提交校验，不能静默改为项目目录。
- 在明确测试聊天中验证无当前会话时普通消息自动创建并执行，主线与两个话题互不串线；
  群聊仍逐条 @，单聊无需 @，`/new` 保留原有表单和手动另建语义。无匹配或默认配置
  失效应给出原有引导，不执行这条任务；失效规则不改为下一条规则。
- catch-up 首轮使用真实触发消息作为初始边界，当前正文／引用／图片正常提交且不补读
  更早讨论；随后在群主线与话题分别验证从该消息之后读取。继续满足既有 chat/thread
  history rollout gate；本地“lower 等于 upper”的测试不能替代真实消息及客户端验收。
- 指定资源经 Admin 删除、目录被外部删除/替换或状态无法确认时，保留规则且本次输入不执行，
  不切目录、不换规则、不自动重建。Project 同名重登记可恢复项目目录策略；已有 Worktree
  的 exact ID/Project 代次校验拒绝旧孤立资源。“每次新建”不引用上一份已删除工作区。
- 同 Scope 并发首消息只创建一个工作区和 Binding，后续消息保持 exact Binding/Turn
  admission，不排队、不保证创建者先执行；Git 操作不得长期持有 Scope 锁。交错提交
  `/new`、Project/Worktree 删除及 shutdown，检查资源保留、已知部分成功反馈和未知结果
  不自动重试；已有 Binding 不因修改默认策略而换目录或重新创建。
- 当前 schema 新库完整初始化、v14 → v15 → v16 默认工作区冻结夹具及继续到 v17 的迁移、当前库重装
  保留默认配置和规则顺序；旧规则使用项目目录，新增策略严格校验持久化形态。
  v13 及更早库只读拒绝。迁移遵循 ADR 0076；启动迁移持续持有 lifetime lock，不提供自动旧库恢复。
  Project/Worktree 删除后默认配置保留，使用时明确提示不可用，不新增联动删除或资源回收。

2026-10-20，schema v16 默认策略扩展（SDK 版本未另记）：本地 Git/行为、迁移、打包与 SDK
synthetic 门禁通过；真实飞书、目标平台启动迁移和交错操作仍待验收，未部署/发布。
[完整记录](compatibility-history.md#defaults-20261020)。

真实群聊消息和客户端操作须在指定的测试环境验收；缺少该条件时分别记录未验证范围，
不能用 synthetic 结果声明飞书卡片、catch-up 或目标机安装回滚已通过。

### Admin 群选择器验证

2026-10-08，平台群接口 v1/v2（SDK 版本未另记）：真实凭据只读群查询与本地 Chromium
模拟管理接口验证通过；真实第二页、权限/退群竞态、部署认证、消息投递与计划执行未验收。
[完整记录](compatibility-history.md#group-picker-20261008)。

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

原生加载可用 `scripts/probe_skill_execution.py --live --timeout 240 --output <evidence.json>`，
外部总 deadline 2400 秒（SIGINT 后另给 15 秒退出）。默认七阶段串行；可用重复的
`--phase new|cold|side|fork|child|goal|isolation` 只跑受影响阶段。使用一次性只读 Skill，
分别断言原生 command 确实读到新随机资源、exact Turn 的最终回答匹配正文和资源标记；
catalog 及请求不含答案。两 App Server 隔离阶段共享 HOME/cwd/Skill 名但 roots 不同。
进程级信任使用顶层 inline TOML `projects={...}`，不能把含引号的路径放进 dotted
override key。失败停止并保留已知自建 Thread ID，不重试未知清理；与其他检查用户配置
不变的探针串行。Goal 阶段验证 pause/resume 后的新物理 Turn，不冒充自动 rollover；
这不替代安装包资源、Lark 实际调用、同名/禁用/歧义规则或平台启动验收。

同时验证双 Admin 登录与 root 标识、端口首次实际分配及固化、端口冲突不漂移、显式
端口、缺管理员的后台启动拒绝、服务 profile 导出别的 root 后仍重申绑定。Project、用户 Codex
状态与外来文件不得因其他实例生命周期而被修改。

### 2026-11-03 输入附件、Admin 会话与通知验收

本次没有升级 SDK、部署服务或发布程序。固定 Codex SDK 0.163.0 / Channel SDK 1.4.0。
原生只读历史用途扩展见 [ADR 0083](adr/0083-page-native-session-history-through-a-narrow-read-adapter.md)，
附件资源边界见 [ADR 0090](adr/0090-retain-input-attachments-independently-of-native-execution.md)。

可重跑的隔离原生探针使用临时 HOME/CODEX_HOME、真实 bundled App Server 和 loopback
mock provider，不读取真实用户历史、不使用真实模型账号、不发飞书消息：

```bash
.venv/bin/python scripts/probe_admin_sessions.py --output /tmp/netizen-admin-sessions.json
.venv/bin/python scripts/probe_input_attachments.py --output /tmp/netizen-input-attachments.json
```

2026-11-03，Codex SDK `0.163.0` / Channel SDK `1.4.0`：隔离原生 history/附件读取、SDK
归一化、本地 Chromium 和代码/打包门禁通过；mock provider 与合成数据不覆盖真实模型、
桌面/手机附件 + @ 入口、问题卡提醒、飞书 WebView 或 macOS 平台效果，未部署/发布。
[完整记录](compatibility-history.md#attachments-admin-20261103)；现役资源与删除规则见
[输入文件附件](design.md#输入文件附件)和[输入附件管理](design.md#输入附件管理)。

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

按能力查找下列实际证据；每行独立限定版本与覆盖层级，17 phases 不替代专项或客户端验收。
完整测试数、源码指纹、首次失败和复验过程保留在[兼容性历史](compatibility-history.md)。

| 能力 / 实际版本与环境 | 验证层级与记录 | 未覆盖 / 复验入口 |
| --- | --- | --- |
| SDK/Runtime、provider 准入；`0.167.0`，2026-11-09 Linux 候选 | [17 个真实账号 phase、命名/fork/child Files/Skills 七阶段/调度五阶段/Project 删除、provider 准入及 mock 原生场景](compatibility-history.md#sdk-0167-20261109) | 冷恢复实际权限本机未验证；真实组织 provider 策略、图片附件形态的真实生成、卸载后接管、解除归档后代恢复、网络 grants、managed policy、Responses WebSocket、飞书/平台 ready 未覆盖；按受影响 phase 复验 |
| 历史分页、Goal 分离；`0.163.0`，2026-10-17/18 本机 | [本地门禁、mock 原生探针](compatibility-history.md#history-goal-20261017) | interrupted 续流缺口仍在；旧混合卡不承诺兼容；真实模型/客户端、十万会话压测与部署未验收；复验 history/Goal 专项 |
| SDK/Runtime；`0.163.0`，2026-10-16 Linux 候选 | [17 个真实账号 phase、命名/fork/child Files/Skills 七阶段/调度五阶段/Project 删除及 mock 28+1 场景](compatibility-history.md#sdk-0163-20261016) | 网络 grants、managed policy、Responses WebSocket、Code Mode 前台独立 wait、远端 Environment 最终 poll、飞书/平台 ready 及完整 CI 矩阵未覆盖；按受影响 phase 复验 |
| 阶段性答案、Goal 两轮、SDK；`0.162.1`，2026-10-10 macOS arm64 | [17 个真实账号 phase、专项、mock 12 场景；飞书 API ACK 与初版有限人工确认](compatibility-history.md#sdk-01621-20261010) | 新版视觉/真实点击、Ultra Fast、压缩组合/revert 竞态、Lark Skill 与平台未验收；原生 Goal 与展示样例不是端到端；复验 partial/Goal/Skills 与客户端 |
| SDK、fork、额度；`0.161.0`，2026-10-08/09 macOS arm64 | [17 phases、命名/Project 删除/定时 binding/六项 Skills 与持久 fork](compatibility-history.md#sdk-0161-20261008) | 该版调度另四阶段、child-files、真实飞书/OAuth/API-key/Bedrock/权限专项和部署未覆盖；按专项复验 |
| 只读额度；`0.160.0` 与候选 `0.161.0`，2026-10-08 | [双版本适配回归、`0.160.0` 登录/未登录原生读取](compatibility-history.md#usage-20261008) | 飞书时间展示、其他账号及候选完整升级不在范围；复验 account-rate-limits |
| SDK；`0.160.0`，2026-10-04/05 本机 | [本地门禁、17 phases、命名/Skills/新增专项](compatibility-history.md#sdk-0160-20261005) | 飞书与目标平台部署未验收；自定义模型目录须验证 endpoint/discovery |
| SDK、零 Turn、观测恢复；`0.159.2`，2026-09-30 本机 | [17 phases、命名/Project 删除、零 Turn 实验、恢复复验与 Skills](compatibility-history.md#sdk-01592-20260930) | 零 Turn 无可靠恢复；Lark 调用/Skill 歧义/Goal 自动 rollover、飞书与目标机未覆盖；复验对应原生及 Skills phases |
| Binding/Side 问答；`0.156.1`，2026-09-24 | [行为测试、Side smoke；两次问题探针未得到 questions](compatibility-history.md#side-questions-20260924) | 不记作 Side 原生问答通过，也不推断不支持；原生问题与客户端仍待验收 |
| 命名；`0.156.1`，2026-09-24 | [真实模型 runtime-only](compatibility-history.md#naming-20260924) | 独立中断未重跑、原介绍面板未验收且已移除；完整命名仍用默认探针 |
| SDK/问题；`0.156.1`，2026-09-23 | [17 phases、命名/Project 删除与原生问题回答](compatibility-history.md#sdk-01561-20260923) | 飞书投递/点击、目标机与发布未覆盖；复验 question 及相应 phases |
| SDK/Goal steer；`0.155.1`，2026-09-22 | [17 phases、命名/Project 删除与 physical-Turn 追加](compatibility-history.md#sdk-01551-20260922) | 飞书、目标机与发布未覆盖；复验 goal |
| 压缩、child Files、Project 删除；`0.154.0` | [历史原生基线及 synthetic trust 清理](compatibility-history.md#sdk-0154-baseline) | 原生自动新增 trust 后清理的 live 路径、飞书 topic/浏览器未覆盖；复验 compact/child-files/Project 删除 |
| Project config；`0.154.0` | [原生分类 hot-reloaded](compatibility-history.md#config-0154-baseline) | 不是后续版本或目标账号的配置/权限结论；复验 config/sandbox |

当前 Goal 观测缺口见[Goal 契约](design.md#goal-物理-turn-与逻辑终态)，Adapter 移除条件与
延期能力见[SDK 边界](design.md#sdk-适配边界)；证据迁出不解除这些限制。
操作、环境和通过判据统一见[代码门禁与按需实时兼容性验证](#代码门禁与按需实时兼容性验证)。

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
`netizen_cli/feishu_app_onboarding.py` 的 tenant/user addons 时，同步更新权限检查、
用户说明及相关行为测试；不新增 CLI 运行依赖。Admin 的窄协议适配遵守
[ADR 0080](adr/0080-authenticate-admins-with-feishu-identities.md)，不扩展为通用 API 转发。
审批、发布、
租户安装与入群须按真实流程确认，SDK 返回凭据不等于全部权限已生效。

### 更换飞书应用与权限修复

setup 对应用和管理员齐全的已有服务绑定报告 already_registered，不静默修改或接管。
已有准确应用只缺管理员时，先 stop，再直接 setup 授权或填写 `--admin-open-id`；服务
绑定与数据库不变。名单格式或文件权限错误会明确失败，不当作空名单重新认领。旧应用
成员不授权当前应用；更换应用后须显式绑定当前应用的首管理员，旧应用成员记录仍保留。
需要修复已有完整绑定的应用权限时，
先用原环境 stop／remove 保留数据，安全备份并按意图调整 profile，再在选定环境 setup、
start。已有有效 App ID 加空 Secret 是 exact-App 修复；删除整个 profile 文件表示
重新选择应用，别在普通更新时删除。新凭据成功保存后即为持久配置意图，后续权限或
启动失败不还原旧凭据。App ID 改变不会迁移旧 Scope 或原生历史。
管理员身份键为 `App ID + Open ID`，不使用 `tenant_key`；换应用时须由本机操作者明确
准备新应用的名单，不能沿用旧应用的 Open ID、名单或用户 token。新版不提供旧 secret、
enabled 配置或旧服务模板的兼容迁移。

### Agent 驱动首次安装

使用选定环境的 `netizen setup --root "<NETIZEN_ROOT>" </dev/null`，在出现验证链接时
逐次转交给用户并保留同一进程，继续读取 stderr 进度。应用注册和首管理员身份确认是
两个阶段；后者通过当前 exact App 的 Device Flow 和本人资料确认，未经同应用证据核对
的注册 Open ID 不直接入名单。stdout 的 helper 凭据通道只由程序私下消费，不能展示到
聊天。每阶段等待最多 660 秒；取消／失败后按输出修复再显式重试，
不要循环申请，不要求用户粘贴 App Secret。setup 成功后另行执行 start 并等 ready。
已有应用也可用 `netizen setup --root "<NETIZEN_ROOT>" --admin-open-id ou_example`
指定首管理员，支持无 TTY；它不覆盖已有名单，也不将手填 ID 当作登录证明。

### 浏览器安装路径验收

在授权的隔离应用／账号完成无 TTY URL 交接、相同进程继续、首次选择／exact-App 修复、
权限不足、取消和超时；验证凭据不进入输出、argv、日志或 YAML。TTY 浏览器失败只
允许一次手工回退，Ctrl-C 不回退，成功后不再次发起浏览器补权。此项须单独记录，
fake helper 和代码门禁不能冒充真实应用验收。

### Admin 飞书登录与管理员维护验收

用户凭证布局现由 [ADR 0091](adr/0091-load-admin-user-credentials-on-demand.md) 改为按用户
独立文件并按需读取，不迁移旧聚合文件。从旧格式升级后需重新完成飞书登录；下述历史
验收记录不代表此变更已发布或部署，新版本仍须分别核对登录、刷新、重启及精确 purge。

2026-10-11 本轮将 [ADR 0080](adr/0080-authenticate-admins-with-feishu-identities.md) 接入
当前源码，未据此发布版本或部署真实实例。临时 demo 的真人登录、选人和本人 Open ID
对应证据不等于正式链路通过；以下 live 项仍需在授权的隔离应用和实例完成：

- 新应用与已有 exact App 的无 TTY setup、手填首管理员、取消/超时及两阶段 URL 交接；
  user 类型权限实际开通、新登录申请及实际授予 `component:selector offline_access`。
- 普通浏览器与飞书客户端的授权返回、非管理员拒绝、搜索选人及 ID 对应、确认增删、
  最后一人保护、自删后所有会话撤销；官方 Selector 作为受信同源依赖加载。
- 真实 UAT/RT 刷新与轮换、失效登出、重启保留凭证但匿名浏览器仍需登录；名单与凭证
  的权限、原子写失败和删除时的在途结果不得恢复已撤销资格。
- 另一台受信内网主机的 Host/Origin/CSRF、登录流程限速与脱敏；Linux systemd 和 macOS
  LaunchAgent 的缺管理员启动拒绝、显式补配置、重启、更新恢复及精确 purge。

本地行为测试和静态检查分别记录，不替代这些真人、跨主机和平台验收；HTTP 的部署
前提仍是受信内网，不新增公网安全或免授权确认承诺。

2026-10-11，Python 3.11 / Selector SDK `1.2.21`：本地门禁、合成后端 Chromium 与官方
Selector 代码复验通过；不含真实授权/搜索、中文字体效果、跨主机/平台或 CI Node 22 验收。
[完整记录](compatibility-history.md#admin-login-20261011)。

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

Admin 使用同一应用的飞书登录，管理员名单与用户凭证分别保存在私有的
`<NETIZEN_ROOT>/state/admin-users.json` 和
`<NETIZEN_ROOT>/credentials/admin-user-<sha256>.json`，后者每个 `App ID + Open ID` 一份，
摘要为 `App ID + "\n" + Open ID` 的 UTF-8 SHA-256，不进入上述 profile 或 Channel SQLite。
文件由当前用户拥有、普通非 symlink、单硬链接，写入权限为 `0600`，读取也允许只读
`0400`；原子替换。在线由服务统一修改，离线 setup 持有维护锁和 lifetime lock。
用户凭证只在实际使用时读取当前用户文件，不启动全量读取或保留 token 缓存；单用户
文件损坏不会阻止服务启动，也不会影响其他用户的凭证读取。不要在服务运行时直接编辑
名单或 token 文件。首次无 TTY 安装可以完成浏览器初始化：

```bash
netizen setup --root "<NETIZEN_ROOT>" </dev/null  # 按输出逐次转交应用绑定/管理员登录链接
# 已绑定当前应用时，也可显式指定首管理员：
netizen setup --root "<NETIZEN_ROOT>" --admin-open-id ou_example
```

从带 Netizen allowlist 的旧版本升级时，必须先从 live `config.yaml` 删除整个
`access:` 段。用户和群的可用范围改由飞书应用后台管理；新版若发现旧段会明确拒绝
启动，避免部署者误以为这些字段仍然生效。

不要把 Secret 内容写入 YAML、仓库、shell 参数、shell profile、systemd `Environment=`
或 LaunchAgent plist。服务只传递应用配置的受保护路径，名单和用户凭证固定从实例 root
派生。旧 Admin secret 不再生成、读取或接受为登录凭据，也不提供自动回退；磁盘遗留
文件不自动删除，只有显式 purge 才按精确清单处理。

`adminWeb` 首次未指定端口时等价于：

```yaml
adminWeb:
  host: 0.0.0.0
  # port 缺失时首次实际绑定 8787–8886 中可用的端口，随后写回此字段
  # accessHost: netizen.internal  # 可选访问 hostname/IP，不含协议、端口或路径
```

可覆盖 host/port/accessHost；Admin 为必备能力，`enabled` 属于不支持的配置，启动会拒绝。
启动须已有当前应用的管理员；缺名单时显式 setup 补配置，后台启动和重启不会等待扫码。
端口字段存在时严格使用，冲突报错；只有缺失才自动分配，`0`、`null`、空字符串无效。
启动进程保留已绑定 listener 后原子更新 YAML，检测人工修改或写入失败即关闭 listener，
不以探测空闲后释放 socket 的方式分配。只有 EADDRINUSE 才尝试下一端口，最多 100 个；
其他错误或耗尽均启动失败。端口一旦写入，即使后续初始化失败也保留；删除 port 再启动
才重新请求分配。停止实例的已保存端口不登记或预留。

在飞书发送 `/admin` 获取成功绑定的管理地址和实例根目录，不要求 Project/会话，也不
调用模型。有效 Side 同样可用，已关闭 Side 保持原路由。命令不返回
credential、session token 或免登录链接。访问 URL 不使用 `0.0.0.0`/`::`；通配监听自动选择
启动时枚举的网卡 IP 中与实际监听地址族及端口匹配的地址，最多展示 4 个；没有匹配项或
枚举失败时回退同族 loopback。网络地址变化后需重启实例刷新。多网卡可设置
`accessHost`，它连同实际端口加入已有 Host/Origin 精确校验，不增加代理信任或 NAT 映射。
只有 loopback 地址时使用服务器本机或自行隧道。未登录时根路径跳转到 `/login`。
页面和登录页均显示实例 root。点击飞书登录后在官方授权页确认，保留原页等待结果；
服务端确认用户身份及当前管理员资格后才保存用户凭证和签发本地 session。匿名访问者
不能认领首管理员。姓名和头像只是本地展示快照，不参与 `App ID + Open ID` 授权判断。

Admin 登录会话不设闲置或绝对时间过期；退出登录、服务重启或管理员被移除会使其失效。
浏览器仍使用会话 Cookie，清除 Cookie 后需重新登录。同一来源最多保留 16 个、全局最多
256 个登录会话；验证成功的新登录在达到上限时替换最早会话，优先在已满的同一来源内
替换，并撤销旧会话的操作凭据。

登录尝试绑定原浏览器、来源和一次性挑战，并按飞书返回期限有界轮询；取消、过期或
限速时按页面提示重试。登录后的操作凭据仍有十分钟有效期。Cookie 名带 rootDigest，
同一 host 不同端口的实例可并行登录。Host/Origin、CSRF、容量与限速校验仍适用。

新格式用户 token 跨服务重启保留，但不会自动恢复匿名浏览器登录。旧聚合文件
`credentials/admin-user-tokens.json` 不读取、不迁移、不自动删除；从旧格式升级后，用户
重新完成飞书登录即可生成新文件，不影响管理员名单、Project 或原生会话数据。显式
purge 继续按精确清单处理旧文件。

仅在实际调用飞书时读取凭证、检查期限并按需刷新，不增加后台续期；刷新失败使当前
请求的浏览器 session 失效，需要重新登录。普通退出只清除当前 session 和关联操作授权，
保留共享用户凭证。每位管理员仅一份当前 UAT/RT；登录或刷新成功原子覆盖，token 不给
前端、不写日志。用户 token、scope、用户凭据文件和飞书认证响应不设置任意长度或大小
接受阈值；类型、期限、身份和私密文件校验保留。上游响应仍完整读入、解析后提取必要
字段，处理后不缓存完整响应；更大的响应仍增加本次调用的临时内存及解析成本。

“系统管理 → 权限管理 → 管理员”使用飞书官方 Selector 搜索选人，管理员确认后由后端
复核当前资格与名单 revision 再添加。管理员同权，可删除自己但不能删除最后一人；
删除立即撤销其所有 session 和未消费操作授权，并清理用户凭证，在途结果不得重新
建立已撤销的授权。名单展示使用本地姓名/头像快照，不额外申请通讯录权限。运行中若
名单不可用，Admin 拒绝管理访问，不终止机器人或已受理任务。仍使用不加密的受信内网
HTTP，不得把端口直接暴露到不受信网络。正式链路的真人和平台验收状态见
[专项验收](#admin-飞书登录与管理员维护验收)。

`instance.projectRoot` 是必填绝对路径，只限制飞书自动创建的空 Project，不是 Binding 默认 cwd。
`worktrees.root` 默认 `<instance-root>/worktrees`，`fetchBeforeCreate` 默认 false；修改只影响
以后创建，不移动已有 checkout 或修改 Codex App 配置。`projects` 只引导尚未登记项；
已删除 alias 须显式重新登记。不要手工编辑表，结构与保存范围见[当前数据契约](design.md#channel-数据库与结构校验)。

setup 才创建新库；最低迁移基线为 v14，缺库不是新实例，低版本/未知/较新/损坏或缺路径均拒绝。
早期试验库须单独停服、备份和校验。启动持有 lifetime lock，提交前回滚、提交后保留新库；
失败按[迁移验收与启动边界](#数据库迁移与中断恢复验收)修复后重试，不自动恢复旧备份。
Project 删除保留原代码目录及聊天默认配置；部分失败/未知后保持停用，刷新剩余清单再确认，
重启不续删。计划墓碑、可选 Worktree 级联及精确清单见[删除契约](design.md#project-删除与交接)。

浏览器初始化会请求 `card.action.trigger`；手工准备应用时，使用卡片前须在飞书开发者后台
打开“事件与回调 → 回调配置”。回调仍走现有 WebSocket 长连接，不需要公网 callback URL；
若未启用，文本命令正常但点击卡片不会产生事件。

## Fail-closed 运维语义

- 先区分本次请求被明确拒绝、目标 Binding 结果未知和实例 admission 已关闭；
  按[失败语义](design.md#失败语义)处理，不因一次请求失败就重启整个实例。
  仅当实例明确要求重启时，检查平台日志后人工执行
  `netizen restart --root "<NETIZEN_ROOT>"`；不要自动重放结果未知的输入。
- 若单个 Binding 提示 Turn 观测不可用，先检查 App Server/平台日志，再使用 `/sessions`
  中该会话的“重新检查”或“停止”。观测与恢复是有界的，其他 Binding 仍可使用；具体状态和
  允许操作以[普通 Turn 终态与观测恢复](design.md#普通-turn-终态与观测恢复)为准，
  不按旧运行卡猜测终态，不手工清理 SQLite 或 `.codex` 状态。
- 若归档、恢复或删除结果未知，保留目标 Binding，按
  [原生归档与删除](design.md#原生归档与删除)核查该操作的结果；不得用目录缺项证明
  删除成功，或在运维层自动重发删除。
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
检查 plist、进程 argv/environment、`netizen.log` 和 `launchd.stderr.log` 均不含 App Secret、
UAT/RT、设备授权码或本地会话凭证。最后在两平台重跑真实 Codex Thread、steer、cleanup 和 exact-ID resume probes，
按上述职责分别记录适配行为与原生能力结果；fake launchctl/systemctl 单测不能替代这些真机门禁。

Admin 矩阵须从另一台受信内网主机，经 `/admin` 给出的实际端口访问
`http://<服务器 IP>:<实际端口>`。真实浏览器、跨主机和端口失败场景是首次上线或相关
边界变更的 live 门禁，loopback 单测不能替代；各行同时核对当前契约。

| 场景／注入 | 预期观察 | 证据类型 | 当前契约 |
| --- | --- | --- | --- |
| A01 未登录访问 `/`、未知 route、受保护 API、公开登录资源与 `/health/ready`；再飞书登录 | `/` 303 到 `/login`，不返回 HTML/状态；未知 route/API 401；仅明确公开的登录页、资源和授权端点进入预认证，仍有浏览器绑定/同源检查；ready 无细节。名单核验后各管理页可访问。 | 跨主机真实浏览器/HTTP | [认证](design.md#admin-http-与认证) |
| A02 打开 Sessions 普通会话、组合筛选并重置 | 默认 Active、最近三个月更新时间；Active/Archived/Lazy 单选，无 Missing/Unknown。Project 可搜索含停用项；Project/Scope/current 及 Side 状态多选，同项或、跨项且；无 ID 输入框但列表 ID 可见。普通重置恢复 Active、三个月、最近更新降序、20 条；Side 恢复默认状态及全部时间。 | 真实浏览器 | [目录][验收2] |
| A03 超过一页匹配会话，切换创建/最近更新时间升降序及 10/20/50/100 页容量 | 两类列表默认最近更新降序，先全局排序再分页、同时间按 ID 稳定排序、缺失更新时间置末；前后页、页码和条数正确。排序回第一页且不提交筛选草稿；目录失败/恢复或会话更新后从第一页重查，不要求跨请求固定快照。 | 真实浏览器/请求记录 | [目录][验收2] |
| A04 P2P、普通群、话题群 Binding 刷新与名称链接 | 真人/群名、类型正确；重复刷新命中缓存；名称打开对应会话，话题行只承诺所在会话。 | 真实浏览器及飞书点击 | [位置链接](design.md#聊天名称与位置链接) |
| A05 100 条首屏；停留两类列表、手动及操作后刷新 | 单次 runtime snapshot ≤50 ID；无周期查询/失败重试。成功更新刷新时间、失败留旧时间；登录/维护轮询不变。 | 浏览器网络记录 | [运行态](design.md#运行态投影与轮询) |
| A06 Project register/create/enable/disable；查旧建会话入口 | 操作成功；Admin 无新建会话入口，已登录请求 `/api/v1/sessions/create-lazy` 返回 404。 | 真实浏览器/API | [管理动作][验收5]、[Project](design.md#project-登记与准入) |
| A07 飞书准备两个 Scope 的 active/inactive、Lazy/materialized/archived Binding 和 open Side | 逐项验收 activate/configure/rename/archive、两种 unarchive、delete-lazy、Stop/Release 及 exact Side Close。 | 飞书准备＋真实浏览器/native 结果 | [管理动作][验收5] |
| A08 临时会话筛选/分页，反复切换两个 Tab | Side 默认 creating/open、20 条，支持项目/群选择器、状态多选、创建时间及 10/20/50/100 前后页，无 Parent/Topic/Root ID 筛选。各 Tab 草稿/已应用查询/排序/分页独立保留，普通操作不变，隐藏 Tab/旧页异步结果不更新当前页。 | 真实浏览器 | [目录][验收2] |
| A09 查询失败及旧查询延迟成功/失败 | 同份列表、排序、页码、已应用条件及草稿保留；旧响应不得覆盖新结果或提示。 | 浏览器故障/迟到响应注入 | [目录][验收2] |
| A10 无首轮问题的 Side 创建中看列表，创建后刷新；同任务追加；删除来源后再查 | 刷新可见可用状态/操作，追加不禁用合法结束；来源名称、话题链接、折叠 ID 正确，来源已删的存活 Side 仍可按项目展示/筛选，已结束记录无“无进程内 Session”提示。 | 飞书＋真实浏览器 | [目录][验收2] |
| A11 disposable Side 执行中停止；保留旧页后开始新 Turn；结束/未确认关闭 | 停止仅作用该 Turn，后续可继续；旧停止拒绝且不误停新 Turn；结束与 `/side close` 一致，未确认不显示已结束；只读运行态接口不重新签发授权。 | 真实浏览器/native；陈旧授权注入 | [管理动作][验收5] |
| A12 active/archived 各一 disposable materialized 行及 Lazy 行：取消/确认 Delete | 仅 capability 可用时 materialized 显示删除；二次确认含会话、Scope、short ID、永久级联后果，取消零 mutation，确认才删 Thread/descendants/Binding；inactive 保留其他 pointer，current 才清空，不先 activate/unarchive/Stop。Lazy 仍确认后只删本地。 | 真实浏览器＋disposable native | [删除][验收7]、[管理动作][验收5] |
| A13 disposable Project 关联 Lazy/active/archived 与 open Side；取消、确认；并发新建/重新启用 | 清单不受 Sessions 筛选影响，取消零 mutation；确认先关 Side 再逐项删会话，保留磁盘标记，Project 消失、Side 墓碑保留；执行期间新关联会话/Side/启用均拒绝。 | 真实浏览器/native＋并发入口 | [Project 删除][验收8] |
| A14 Project 删除部分失败/断线；重启；显式复用 alias | Project 保持停用并展示剩余项，未知不报整体成功；YAML 不复活已删 Project，旧 revision/action 在 alias 复用后失效。 | 故障注入/重启＋浏览器 | [Project 删除][验收8] |
| A15 数据库边界变化的候选 | 受支持版本迁移、不支持库只读拒绝、当前库重装 metadata 保留、失败/中断恢复及候选新数据保留均按[专项](#数据库迁移与中断恢复验收)记录真实结果。 | 受影响平台迁移实测 | [数据契约][验收9] |
| A16 双击同 action；飞书并发操作同目标；重启并查 journal | 双击 stale/consumed，只有符合 exact native identity 的一方提交；旧 Admin session 失效，持久 Binding/设置/Side 墓碑不变；日志无 credential、cookie/action token、cwd、name/preview、body。 | 真实浏览器/飞书并发、重启与日志 | [管理动作][验收5]、[失败](design.md#失败语义) |
| A17 占用 configured port 后启动，释放后显式 start | 明确失败、端口不漂移；已提交迁移保留，释放端口后可显式启动。 | 目标主机真实端口失败 | [部署事务](design.md#部署事务与操作状态) |

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
   `/rename`、`/archive`、`/delete`、`/unarchive`、`/compact`；按[命令注册契约](design.md#命令注册与首次引导)
   核对帮助中的可用集合及不可用命令的拒绝，不包含 `/skills`、`/model`、`/effort`、`/fast` 或当前不可用的
   `/plan`、`/apps`；`/copy`、`/vim`、`/theme`、`/exit` 也不展示。
   另发送 `/new test`、`/new demo`、带引号和坏引号的 `/new ...`，都必须得到同一迁移提示、
   零 Binding mutation；`//new test` 仍作为字面 prompt。
   普通与持久分支 `/status` 的“查看状态／停止／会话配置／会话列表”分别与对应 slash
   命令同样回复；Side 的“查看状态／停止／结束临时会话”同样复用其命令。覆盖单聊、
   群主线及两种聊天的话题，并分别关闭／开启过程卡。查看状态应新发卡，原面板正文
   保留原快照；同一用户重复点击已更新凭据的按钮仍可操作，旧回调重投只处理一次。
   切换会话后旧面板拒绝操作；同会话换轮后停止当前轮，不能因面板生成时 Turn 不同而拒绝。
   Side 的 Parent 切换不影响按钮，Side 关闭／过期后拒绝；状态卡消息 ID 不得当作 Side 根 ID。
   入口重绘失败须提示重新发送 `/status`，不得因此重放刚才的命令。
2. 反馈验收使用 `/new` 卡选择 `test` Project、inherit Codex；按矩阵切换 Reaction Pulse
   与 Progress Card，并分别记录原生执行、实际飞书投递和客户端显示。

   | 场景／注入 | 预期观察 | 证据类型 | 当前契约 |
   | --- | --- | --- | --- |
   | F01 保持 Pulse 关闭、手动关闭 Progress 后发送首条 prompt 并 steer；覆盖三种终态 | accepted 后原消息常驻 `Typing`，零 `THINKING`、进度卡、心跳；steer 成功消息有 `OnIt` 且锚点不迁移。终态先加 completed/failed/interrupted 对应 `DONE`/`ERROR`/`CrossMark` 再移除 `Typing`；无文件用富文本/静态文本，有文件仅现有“最终回复＋本轮文件”卡。手机少量独立生命周期表情消息是接受的取舍。 | 原生结果＋桌面/手机飞书 | [表情][验收13] |
   | F02 使 `OnIt` 失败；`/config` 只开 Pulse 后启动长 Turn | 仅 native steer 成功后有文字 fallback；Lifecycle Reaction 同关闭组，`THINKING` 低频显隐，终态与 `Typing` 一起清理。 | 投递故障注入＋真实长 Turn | [表情][验收13] |
   | F03 只开 Progress，产生 checklist、无 plan、observer unavailable | accepted 后除表情外仅一张运行卡、顶部过程展开；status/checklist `✓/→/○` 更新同一 message ID；无 plan 为“Codex 尚未生成”，不可观察为“暂不可用”。无耗时/百分比/ETA、reasoning、raw command/tool output、arguments、MCP server。 | 原生 Activity＋真实卡；故障注入 | [投影][验收14]、[呈现][验收15] |
   | F04 至少一条最近进展和操作；操作 started→completed | Card 2.0 本地化日期/分钟正确，完成后切 exact completion 时间，checklist 无时间；进展最多四条，正文直接跟 `·`、无额外 `•`。 | 原生时间证据＋客户端渲染 | [投影][验收14] |
   | F05 命令分类已知/未知/复合/空对象、非零退出码、文件/网页、多查询、MCP/dynamic tool 与特殊 Markdown | 显示原生路径/查询，分类不可用时原命令预览、不猜意图、保留非零码；文件/网页显示原生对象，进展保留普通路径/链接/代码而隐藏凭据。操作单行≤120 字符、文件/查询最多前三项、分页详情保留；工具名 exact，动态 Markdown 不注入标签。 | 原生字段＋客户端/分页，合成异常输入 | [投影][验收14] |
   | F06 成功 steer 后等新 plan；完成有/无文件及有/无 partial | 旧 checklist 标过期，新 plan 整体替换；同卡终态折叠过程并显示结果，文件分页/callback 保留：无 partial 用 v4、有 partial 用 v5。 | 原生 steer/plan＋真实卡回调 | [状态 Activity](design.md#状态查询中的-activity)、[呈现][验收15] |
   | F07 initial、中间一次及连续三次 card update 失败；失败期多 revision，随后恢复 | native Turn 始终继续；initial 直接回退；单次中间失败下一轮更新原卡，仅最新快照，成功清零失败计数；连续三次停轮询，恢复后终态仍独立更新原卡且不新增回复。 | 投递故障注入 | [呈现][验收15] |
   | F08 单次/连续终态更新失败，或终态/关闭发生于轮询等待 | 单次重试原卡，成功只发一次结束提醒；连续失败最多三次、间隔 0.5 秒、整组≤5 秒，再标准回退；终态/关闭立即唤醒，不等剩余重试，零重试风暴。 | 投递故障及等待竞态注入 | [呈现][验收15] |
   | F09 两项同时开，在 P2P、群主线、普通话题各一次 | 两套 presenter 并存且最终结果不重复。 | 三类真实飞书入口 | [反馈设置](design.md#反馈设置与结束提及) |
   | F10 普通、持久 fork、Side 分别开/关 Progress，产生多个 completed partial | 初始无空模块，按原生顺序同卡更新或逐条富文本，零完成 @/DONE/提前槽释放；终态 partial 始终展开，保留 final 自身复述、不机械重发片段；partial-only 仅确认交付后相应收尾。 | 分列原生生成、真实发送、客户端显示；未生成不可用合成冒充 | [阶段性答案][验收18]（ADR 0079） |
   | F11 partial 重复事件、终态补读、明确失败/未知发送、观察降级、failed/interrupted；阻塞更新后终态/Side close | 沿 F10 交付判据；Side 降级后 failed `run()` 抛错仍保留已观察片段，不虚构未取得 items 的恢复；容量超限沿既有规则、无 partial 专用兜底；Goal logical reply 同判据，管理卡成功不算答案交付。 | 故障/竞态注入＋原生及投递分层记录 | [阶段性答案][验收18] |
   | F12 running `/status`；可观测完成并排空 usage，再新 Turn、快速 Turn、无新 usage | running 显示完整 native ID、已接受 steer 次数及同一 checklist；完成显示窗口已用 tokens/上限/百分比。下一轮 running 保留标注“上一轮完成时”，完成覆盖；快速完成也读原 handle 保留通知，无新 usage 则清旧值、显示暂不可用。inherit 路径零模型目录读取、零 Model/Effort/Speed override。 | 原生 usage/请求记录＋飞书状态 | [状态用量](design.md#状态查询与用量) |

3. 零参数 `/new` 在存在 enabled Project 时保留一个创建 form，并增加独立的只读目录加载
   form，不显示任务输入或快捷创建按钮。工作区路径与分支选择按 [专项验收](#受管-worktree-专项验收)核对。
   Project 使用现有单个静态下拉框，并展示 Registry 中全部 enabled 项；同 Scope 当前或
   最近使用且仍 enabled 的 Project 可基于现有 Binding 记录预选，不新增 recent 状态；没有
   该记录时下拉保持未选。零 enabled Project 时不显示 form，只引导 `/projects`，且零 Binding
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
   disabled、重名或 stale 引用不得发送 Turn/steer，lazy 会话也不得创建 Thread；已有
   空闲 Thread 可先恢复以取得实际 cwd，校验失败后按正常 idle 策略释放订阅。
   引用消息历史里的 `$skill` 不得激活，当前消息的引用仍正常。
7. Goal 验收先通过 zero-Turn live gate，再执行下表；管理与任务回复分别取证。

   | 场景／注入 | 预期观察 | 证据类型 | 当前契约 |
   | --- | --- | --- | --- |
   | G01 `/goal <objective>`、查询/刷新/控制 | 管理卡静态显示当前状态，仅查询/按钮时更新，无答案/文件；任务回复独立。 | 原生 Goal＋真实飞书卡 | [Goal][验收20] |
   | G02 普通 Prompt、准备期间 rollover，以及 starting/stopping/收尾/unknown/无安全 route | 仅 steer 捕获的 exact 物理 Turn，换轮拒绝、不重投/新建；不可用状态拒绝，`/config`/`/compact` 拒绝；仅 pause/clear 后当前 Turn 仍运行则仍可 exact steer。 | 原生 rollover＋准入竞态注入 | [Goal][验收20] |
   | G03 Progress 开/关，至少两轮 rollover、重复及旧轮迟到事件 | 一个 logical stream：开启同一普通回复卡，关闭富文本/有文件完成卡；rollover 不新发卡，Activity 换轮、partial 累计且去重，Result/Files 仅 exact final physical Turn。 | 原生多轮＋飞书展示/迟到事件注入 | [Goal][验收20]、[回复][验收15] |
   | G04 pause/clear、同流与旧流结束后 resume、`/stop`，含已显式 clear | pause 只暂停续跑、clear 只移除 Goal，当前 Turn 和交付继续；同流 resume 不重复回复/route，旧流结束才新回复；stop 暂停 Goal、interrupt exact 当前 Turn、cleanup，已 clear 则只停止当前执行。 | 原生控制＋回复/route 记录 | [Goal][验收20] |
   | G05 paused/blocked/usage/budget limited 与 complete；已确认显式 clear | 前四类不自动 clear；logical stream、persisted Goal、exact final Turn、Thread idle 四证齐备，complete＋final completed 才自动 clear。已确认显式 clear 可 absent，仍核验当前执行/final Turn，不丢答案/文件。 | 原生四证与 Result/Files | [Goal][验收20] |
   | G06 管理卡更新失败、旧按钮、跨重启、不同 Goal/同秒同 objective-budget 重建 | 确定回执保留，不重做 mutation；未确认终态零完成提及。旧按钮核对原生稳定标记/当前状态，不依赖旧 owner；不同 Goal 拒绝，同秒完全相同重建接受 ADR 0084 有限歧义。 | 故障注入＋重启/真实卡 | [Goal][验收20] |
   | G07 clear false、响应丢失、清理后仍可读，再 get absent | 保留 mutation unknown、关闭 admission、零自动重试；只读 absent 不解除隔离。 | synthetic fault | [Goal][验收20] |
   | G08 阻塞终态交付，handoff 与 shutdown | 交付期间不启动新 Goal；非 unknown slot 有界 handoff 后释放；已终态 Goal shutdown 零 pause/cleanup。 | handoff/shutdown 同步故障注入 | [Goal][验收20] |
   | G09 纯观察失败；consumer handoff 后新消息或 `/goal` 重检 | 只保留当前 Goal，其他 Binding 可执行；有界只读确认非 active、Thread idle、无 inProgress 才可新对话，零自动 clear/pause；active/读取失败/身份错误仍占用，旧 steer token 不改投，mutation unknown 不走此释放。 | 观察故障/身份/并发注入 | [Goal][验收20] |
   | G10 重启后外部 active Goal；外部客户端并发替换 | 不自动接管，提示在原生 Codex 处理；thread-scoped clear 无 generation CAS，不声称原子防替换。 | 重启原生状态核对；能力限制记录 | [Goal][验收20] |

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
11. 同一 Project 两个未选择 Worktree 的 Binding 同时运行，cwd 相同、native ID 不同；
    另选 Worktree 的会话使用所选目录，不因此引入 Project 级执行锁。
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
    并存的真实普通文件、文件夹：普通文件应保存原件、提交路径并保留正文与 @ 语义；
    文件夹必须明确拒绝且零提交。连续发送的多条独立图片不要求自动合并。
16. 在单聊、群聊、话题分别发送 `/projects` 和零参数 `/new`；卡片必须留在原
    Scope。Settings 只显示已实现分区，Projects 使用下拉管理且新增表单留在同一卡片；
    创建/登记/启停或业务错误后仍显示原 Projects 分区。重启服务后 Registry 仍存在；
    停用条目不能新建 Binding，但旧 Binding 仍可继续。`/new` Project 下拉应同步展示全部
    enabled 条目而不分页。
17. 首次上线或 callback operator/Scope 路由边界变化时，由群内另一名真实参与者点击设置
    卡片的刷新操作，确认 callback operator 不受 Netizen allowlist/ACL 限制且响应不转为
    私聊；单账号自动化契约不能替代这项跨账号验收。
18. 生命周期使用自建 disposable 样本；response loss/明确拒绝用 synthetic fault，
    不在真实账号手工制造。目录核查只作独立验收，不加入生产删除前置或失败收尾。

    | 场景／注入 | 预期观察 | 证据类型 | 当前契约 |
    | --- | --- | --- | --- |
    | L01 idle materialized current 的 `/rename` 参数/无参数卡；打开 `/archive` 后切换 current | App/CLI、`/sessions`、`/status` 名称一致；旧 `/archive` 卡提示 active 已切换、零 mutation，新 pointer 不变。 | 原生 App/CLI＋飞书卡 | [生命周期][验收21] |
    | L02 `/sessions` exact 行确认前切 current/替换 Turn，归档非当前/当前行 | 旧行确认仍有效，目标入 archived；非当前保留 pointer，当前才清空；配置保留，普通列表隐藏、archived 列表显示。原卡刷新，删去末页唯一项后夹取页码。 | disposable native＋飞书真实卡 | [列表控制][验收22] |
    | L03 Lazy `/delete` 和 `/sessions` 非当前 Lazy 删除；确认前物化 | 红色不可恢复二次确认只删本地 Binding；第一次点击仅 exact 目标确认卡、零 mutation；物化使旧卡 stale，inactive 保留 pointer、current 清空。 | 本地行为＋飞书卡 | [列表控制][验收22] |
    | L04 materialized `/delete` 与 `/sessions` exact 行；切 current、改变 Turn/Goal/Compaction 或 native identity | 第一次点击仅红色确认卡、零 mutation，完整列明 Thread、spawned descendants、App/CLI 历史及 Binding 永久删除；命令保持 current-only、切换即零 mutation，exact 行不受切换影响。运行状态变化不使确认失效，Binding/native 身份变化必须零 mutation；仅本次原生成功才删 Binding/pointer。 | disposable native＋卡片/身份竞态 | [删除][验收7]、[列表控制][验收22] |
    | L05 `/sessions` 删除非当前/当前 materialized、末页唯一项；`/sessions archived` 独立两阶段 Delete | 非当前/archived 保留 pointer、current 清空，页码夹取；archived 不先 unarchive。active/archived 共用 native-first primitive/返回处理；自建 root/descendants 从四目录视图消失。 | disposable native 四视图＋飞书回调 | [删除][验收7] |
    | L06 `thread/delete` 成功、明确 RPC 拒绝、not-found、传输/响应未知 | 仅成功响应提交删除；明确拒绝保留 Binding、清可能失效的活动投影后可重新确认；未知 Binding-local lifecycle-unknown，not-found 不是成功。任一失败零 list/read 对账/自动 delete，同 Scope 其他 Binding 仍可 start/steer/lifecycle。 | `make check` synthetic fault | [删除][验收7] |
    | L07 archive 响应不确定 | 仅一次 active/archived 目录对账，exact ID 在 archived 才提交、仍 active 则释放 reservation。 | synthetic fault/有界目录记录 | [归档][验收7] |
    | L08 running/stopping/turn-observation-unavailable/Goal/Compaction 上 Archive/Delete | 直接委托 App Server，不先 interrupt/pause/cleanup、恢复/读取 Turn、等 terminal 或证明 idle；归档入 archived、删除从四视图消失；原生成功后取消本地 observer。 | disposable native 各状态＋调用顺序 | [归档与删除][验收7] |
    | L09 ephemeral/未持久化、App Server/存储不可达、compatibility gate 不可用 | 明确失败且保留 Binding，不伪造成新 Thread 状态。 | synthetic capability/故障＋原生拒绝 | [归档与删除][验收7] |
    | L10 卡片或 `/unarchive <短 ID>`，含目标已 current、缺失/外部占用、unarchive 成功后后续失败/未知 | 正常恢复并自动切换；按 ADR 0082 单次 unarchive/resume 原生结果，零额外目录预检。已 current 不回退 catch-up anchor/revision；缺失/占用保留关联及原 current，部分成功如实反馈，未知仅隔离目标 Binding。 | disposable native＋故障/旧卡注入 | [归档恢复][验收21] |
    | L11 `/archive`/`/delete` 带目标参数及列表 exact 删除 | 命令拒绝参数且 current-only；列表删除仅复核 exact Scope/Binding/native 身份并委托 App Server，无额外目录预检。 | 命令/回调行为 | [命令][验收12]、[列表控制][验收22] |

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
    accepted/steer/终态 Lifecycle Reaction 与 ordinary Turn 相同，且零 `THINKING` 与 Activity
    卡；阶段性答案和问题仍从同一观察链提取。再创建同时开启三项的 Side，确认 Reaction
    Pulse 与 ordinary Turn 相同，Activity/Partial Answer/Result/Files 始终更新同一个回复卡
    message ID。随后修改 Parent 的
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
20. Files 按下表分别记录 native 证据、目标应用 API create/update 和真实客户端点击；
    API ACK、合成 manifest 或 callback 不单独证明客户端、原生生成或完整容量验收。

    | 场景／注入 | 预期观察 | 证据类型 | 当前契约 |
    | --- | --- | --- | --- |
    | X01 普通持久 Turn：native diff/structured items 生成 Project 内/外文件、generated-images 的 PNG/JPEG/GIF/WebP，至少 18 个；diff 含 multi-hunk、非空新增/删除、纯 rename、binary、copy/mode-only/空文件 metadata、异常 hunk | 完整 hunk 与纯 rename 的整轮/逐文件 `+N -M` 正确；图片/binary/其他 metadata-only/异常不造数字。 | 真实 native diff/items | [文件证据][验收23] |
    | X02 100/400 个带逐文件 `a/d` 的 synthetic manifest，另造 401 个 | 100/400 真实目标应用完整 create/update，下方容量 probe 任一 count 非零即失败；401 本地明确拒绝、不截断，保留目标应用 96.9 KB 返回 230099/200800 的证据。 | synthetic 边界＋目标应用容量 probe | [文件容量][验收23] |
    | X03 Progress 开/关 × 有/无 Files、partial | 关闭时无文件仅富文本/静态文本，有文件仅一张“最终回复＋本轮文件”完成卡；开启时均更新最初运行卡，Files manifest 无 partial 用 v4、有 partial 用 v5。 | 真实飞书展示/更新 | [回复呈现][验收15] |
    | X04 Goal resumed exact final physical Turn completed 并创建临时文件，前轮也有文件 | 最终文件进同一逻辑回复卡，按 partial 选 v5/v4，不聚合 rollover 旧轮；live Goal phase 用成功 `fileChange` 验证累计行数，唯一 notification stream 的 latest aggregate diff 仅验证文件发现，旧 snapshot 不泄漏。 | 必须原生 Goal live phase＋飞书卡 | [Goal Files][验收23] |
    | X05 Side exact completed Turn | structured items 进对应完成/进度卡并显示成功 patch 累计；不补 Side aggregate diff、旧 Side Turn 或无受支持 native 事实文件。 | 原生 Side＋飞书卡 | [Side Files][验收23] |
    | X06 child/fileChange SDK 合同变更：已登录新建 child 写文件、parent 再修改；child 运行中/不可读 | 累计双方成功 patch、排除继承历史并保留原生 parent 与 Turn/item 归属，不能当净 diff；子任务未终态/不可读只省略总计、保留已知逐文件数字，completion 不等待。下方独立 child probe 无 live 条件须记未验证，不以 synthetic 替代。 | 独立真实 child probe（非常规 phases） | [子任务统计][验收23] |
    | X07 文件列表/分页与 callback payload | Project 内相对路径、外脱敏逻辑位置；隐藏大小、按钮统一“发送”，每页 8 个完整翻页；正文无绝对路径/文件列表预览/diff/发送全部。v5 payload 每项明文 absolute path、已知统计成对 `a/d`；翻页保留整轮统计及 Activity/Partial Answer/Result。 | 卡片正文＋实际 payload | [文件卡][验收23] |
    | X08 P2P 平面、群主线、已有话题发送普通文件/图片 | 平面卡成为锚点话题，记录真实 root/parent/thread；已有话题同 thread ID，能预览/下载实际文件。 | 三入口真实飞书点击/返回 | [发送][验收23] |
    | X09 切另一 Binding；正常重启 Netizen/App Server 后操作旧 v5 卡 | 均可翻页/发送；重启仅从完整 callback manifest 恢复，不读 source card、Binding 或 completed Turn。 | 重启前后真实回调/读取记录 | [自包含文件卡][验收23] |
    | X10 至少三页：第二→第一→第二并提交，再末页；同渲染 callback 重投、nonce 缺失/畸形 | 每次仅更新原卡、最后同值仍生效；重投只处理一次，当前版 nonce 异常仍按业务字段解码。普通 v4 与无 partial 的 v5 可用；v3 明确过期、不发文件/读 history，推广前其他旧 schema 与混合 Goal/答案测试卡不在兼容范围。 | 真实页码表单＋synthetic nonce/旧版 | [分页][验收23] |
    | X11 重复发送；点击前删文件/改目录/同路径重绑其他普通文件/删原卡；翻页缺失条目 | 重复不多发；文件缺失/目录/删卡失败，重绑发点击时内容；失败保原卡、无文件落主聊天。最终回复逐字保留，缺失项留原页标不可用。 | 真实点击＋本地目标变化 | [发送][验收23] |
    | X12 100/400 文件大卡真实 create/update；末页→中间→首页跳转；重启；单页/超容量 | 多页仅页码下拉＋一个“跳转”，单页无导航。真实 submit 同时保留完整 `value` manifest 和页码 `form_value`，可反复跳转，仅提交按钮含完整 manifest；同卡重启前后保留 Activity/Partial Answer/Result 及整轮/逐文件统计。记录实际序列化 bytes/平台返回，各页≤55,000 bytes，任一页容不下即明确省略 Files、不截断；无 live 条件记未验证。 | 必须真实表单点击/完整回调，不能仅合成或 API ACK | [分页容量][验收23] |
    | X13 ordinary/Side/Goal 最终正文引用本地图片，含无 native 文件证据的既有图片；Progress 开/关、整卡更新 | 无 Files 保持原富文本；核对相对 cwd、图片渲染/放大、GFM 表格/删除线/任务列表及飞书标签。正文引用不增 Files、旧按钮可用、代码示例不上传；重复图仅上传一次，删图并重启后翻页保正文图，Files 按规则不可用。 | 三类执行＋真实客户端渲染/重启 | [正文图片][验收24]（ADR 0065） |
    | X14 强制图片上传/结果卡发送失败；长回复、平铺与定时部分/未知投递 | 上传失败仅该位置；卡发送失败的富文本回退保已上传图及完整分段文字，不重传/重复提醒。长回复分段留原话题，平铺仅首段引用、完成提及仅首段；定时部分/未知不重发。此客户端 live gate 仍须真实目标应用验收，合成不算通过。 | 故障注入＋真实客户端/投递记录 | [正文图片][验收24]、[呈现][验收15] |
    | X15 P2P 返回 230071；全部操作后核对持久状态 | 230071 记 Files live gate 未通过，不以 FakeChannel/主线发送替代；当前 schema 表、Binding、Turn settings、Task Feedback、Context Boundary 与 Side route 行数不变。 | 真实平台错误＋数据库前后对照 | [文件发送][验收23]、[数据契约][验收9] |

    X02 容量 probe 在源码 checkout、目标部署账号 login 环境中，以目标应用配置和专用测试群执行：

    ```bash
    set -euo pipefail
    unset FEISHU_APP_SECRET FEISHU_APP_SECRET_FILE NETIZEN_ADMIN_SECRET NETIZEN_ADMIN_SECRET_FILE
    export NETIZEN_ROOT="<目标实例绝对根目录>"
    export NETIZEN_LARK_APP_CONFIG="${NETIZEN_ROOT}/lark-app/config.json"
    test -s "$NETIZEN_LARK_APP_CONFIG"
    probe_chat_id=${NETIZEN_FILE_PROBE_CHAT_ID:?set target Feishu chat ID}
    for count in 100 400; do
      .venv/bin/python scripts/probe_feishu_turn_file_card.py \
        --config "$NETIZEN_ROOT/config.yaml" \
        --chat-id "$probe_chat_id" --count "$count"
    done
    ```

    X06 独立子任务探针可在服务账号的已登录环境运行：
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

CLI 中新增的消息不要求回填飞书；验证目标是共享原生后端和可接续性，不是两个 UI
的逐条镜像。

[验收2]: design.md#会话目录与分页
[验收5]: design.md#管理动作与确认
[验收7]: design.md#原生归档与删除
[验收8]: design.md#project-删除与交接
[验收9]: design.md#channel-数据库与结构校验
[验收12]: design.md#命令注册与首次引导
[验收13]: design.md#生命周期表情与终态投递
[验收14]: design.md#activity-安全投影
[验收15]: design.md#回复卡片呈现
[验收18]: design.md#阶段性答案
[验收20]: design.md#goal-物理-turn-与逻辑终态
[验收21]: design.md#普通会话生命周期
[验收22]: design.md#会话列表与生命周期控制
[验收23]: design.md#本轮文件证据与发送
[验收24]: design.md#最终正文中的本地图片
