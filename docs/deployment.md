# 单实例 Linux 与 macOS 部署

Netizen 以执行正式 `install.sh` 或源码 `dev-install.sh` 的当前用户运行，与该用户的 CLI 共用标准 `$CODEX_HOME`
（默认 `~/.codex`）；不创建专用 Agent 用户、第二套 Codex 状态或 system-level daemon。
正式支持 Linux 的 systemd user manager，以及 macOS 14+ 当前 GUI 登录用户的 LaunchAgent；
Apple Silicon 与 Intel Mac 均在支持范围内。macOS 注销后停止、下次登录自动启动；不提供
LaunchDaemon。

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

每个 Python release 与自己的 venv 一起安装到当前用户的标准 data 目录。ADR 0014 的
Goal/Skills、ADR 0021 的 Side 与 ADR 0037 的 Thread Delete Adapter 不做运行时版本
allowlist；修改 pinned SDK/App Server 或这些 Adapter 时，开发迭代必须对实际 resolved
组合运行受影响的 capability harness。Delete 能力变更还必须覆盖 disposable lifecycle
live probe 与 Runtime 四视图对账测试。ADR 0020 的 active-Turn plan
observer 另行精确锁定 SDK 版本、源码指纹和非消费 queue contract；门禁失败只关闭
checklist 展示，不关闭普通 Turn。这个降级以 ADR 0009 独立的 service-wide SDK/cleanup
启动门禁通过为前提；不能用 plan 的展示降级绕过该门禁。

## 目录

```text
~/.netizen/
  config.yaml                                   # Channel 配置，0600
  credentials/
    feishu-app-secret                           # raw Secret，0600
    admin-web-secret                            # 独立 32-byte base64url credential，0600
  state/
    channel.sqlite3[-wal|-shm]                  # Binding/Turn settings/Registry/Dedup
    .install.lock / .activation-intent.json     # 跨卸载锁与异常中断恢复意图
    service.lifetime.lock / service.ready       # 精确退出与 readiness 契约
    netizen.log / launchd.stderr.log            # macOS 有界服务日志/launcher 错误
    rollback-recovery-*                         # 回滚恢复材料
  releases/.netizen-managed                     # 删除前必须匹配的 ownership marker
  releases/<sha256>/source/                     # Published Release 或当前工作区快照
  releases/<sha256>/venv/                       # 与源码成对的 runtime
  current -> releases/<sha256>                  # 现役 release
  previous -> releases/<sha256>                 # 一个回滚点
  cache/.netizen-managed                        # 可删除安装缓存的 ownership marker
~/.config/systemd/user/
  netizen.service                               # Linux 渲染后的 user unit
~/Library/LaunchAgents/
  io.github.lijingda.netizen.plist              # macOS 渲染后的 LaunchAgent
${CODEX_HOME:-~/.codex}/
  skills/netizen-user-guide/                    # release 全量管理的用户咨询 Skill
```

配置、Channel 状态和 Codex 原生状态都在 release 之外。ProjectRegistry 仍会解析并保存
canonical cwd；安装器不复制 Project，也不会创建 per-Binding workspace。

Netizen 产品根固定为 effective user 的账号 home 下的 `~/.netizen`，安装器有意忽略
`XDG_DATA_HOME`、`XDG_CONFIG_HOME`、`XDG_STATE_HOME` 和 `XDG_CACHE_HOME`。这是单一
user service、单一全局用户指南 Skill 的固定安装身份，不是可用来运行多实例的 profile。
`CODEX_HOME` 仍遵循 Codex 原生覆盖。开发源码位置与安装位置解耦：任意 checkout、文件
同步或云上直接修改仍可部署到同一套 release；需要隔离安装器黑盒测试时使用临时 Unix
用户、容器或 VM，而不是改 XDG 变量。

安装器只会新建空的 `releases`/`cache`，或复用带精确 `.netizen-managed` marker 的目录。
若它们已经非空且无标记，安装和卸载都会 fail closed，避免仅凭目录名认领并递归删除。
Linux 安装器还会读取 systemd user manager 的环境；若它用 `XDG_CONFIG_HOME` 或
`SYSTEMD_UNIT_PATH` 排除了固定的 `~/.config/systemd/user`，安装会在切换前明确失败。
macOS 则在任何配置、release 或服务文件 mutation 前检查当前 GUI launchd domain，并用
`plutil -lint` 与结构化回读验证 plist。

## 前置门禁

先确认 CLI 登录有效：

```bash
codex login status
codex exec --skip-git-repo-check "Reply exactly: CLI-AUTH"
```

不要把一次登录成功当作持久前提。若命令出现认证错误，应先独立验证登录，不要用服务
失败掩盖认证问题。

逐条引用会调用“获取指定消息”，普通/富文本图片还会调用“获取消息中的资源文件”。群聊
Binding 的 catch-up 模式会在收到有效 @ 后，通过同一应用身份调用“获取会话历史消息”和
exact “获取指定消息”；群主线使用 chat container，普通话题使用 thread container。
受管安装器的官方 SDK 浏览器流程会用最小模板请求下面的应用身份权限、
`im.message.receive_v1` 应用身份事件和 `card.action.trigger` 回调，不申请用户身份权限或
token；手工准备的应用必须逐项配置。无论来源，飞书应用版本在发布前都必须确认：

- 单聊事件投递具备 `im:message.p2p_msg:readonly`，普通消息能力具备 `im:message`；
- `/settings` 等卡片回调识别会话类型具备 canonical `im:chat:read`；官方“获取群信息”接口
  支持的 `im:chat`、`im:chat:read`、`im:chat:readonly` 三者任一 tenant 授权都满足门禁；
- 群聊回查额外具备 `im:message.group_msg`，不能只有接收 @ 消息的
  `im:message.group_at_msg`/readonly；
- 当前 Prompt 发送者姓名解析具备 `im:chat.members:read`；权限不足时 Channel SDK
  无法从 chat member roster 补全真实显示名，Netizen 会零 start/steer；
- Prompt pulse、steer 确认和终态表情具备
  `im:message.reactions:write_only`，或已具备覆盖该能力的 `im:message`；
- 本轮文件具备 `im:resource` 与 `im:message:send_as_bot`，允许机器人上传图片/文件并
  回复消息；“事件与回调 → 回调配置”已开启，按钮 callback 能到达当前 WebSocket；
- 机器人仍在目标群中，新权限已随应用版本发布而非只保存在开发者后台。

权限不足时不降级为忽略引用或图片的普通 prompt；当前消息必须显式失败且
不调用 Codex。任务表情是展示层的尽力操作：reaction 权限或单次请求失败只记录日志，
不得阻断已经启动的 Turn 或最终文本回复；`OnIt` 失败会在 native steer 已成功时回退
一条简短确认，避免把成功操作显示成无响应。

引用功能发布后要用真实消息手工验收普通文本与 CardKit 2.0 应用卡片。卡片用例应
确认 header/body 可见文本进入 Codex，而按钮 value、确认弹窗和隐藏 option 不进入；
确认两次 SDK 读取分别使用 10 秒预算，不得退回共享总预算而产生假超时。
当前消息来源还要由两名真实参与者交叉验收：A 启动长 Turn，B 发送 steer，Codex 应分别
看到两条消息的实际发送者，但完成回复仍锚定 A 的原任务消息。随后从 Codex App/CLI 查看
同一 native Thread，确认公开身份字段进入原生历史，并确认 `/sessions`、`/status` 的首条
preview 仍以 A 的真实请求开头，而不是 attribution 元数据。此验收只确认归属可见性，
不得把显示名或 ID 当作额外权限。临时撤销 `im:chat.members:read` 或使用未发布该权限的
应用版本重试时，消息必须明确提示开通权限且零 start/steer，不能出现“未知发送者”
Prompt；恢复并发布权限后同一成员应重新解析出真实姓名。
图片用例还要覆盖：单聊普通图片、群聊 @机器人富文本多图、文字引用图片、当前图文
引用另一条图文。确认图片以原生视觉输入提交；任一资源删除/保密/无权时零
start/steer。观察进程 RSS：固定 Channel SDK 在 20 MB 应用层门禁前可能完整读取
飞书允许的最大 100 MB 单资源，data URL 和 native RPC JSON 还会产生额外副本。
不同 Binding 的图片准备保持并发，没有全局容量 gate；asyncio 超时也不能停止已经进入
worker thread 的阻塞读取。Pilot 依赖受控用户范围、低频小图，并需持续观察并发图片时
的 RSS；若实际使用模式变化，应先推动 Channel SDK 的有界流式下载能力。

catch-up 上线前必须使用目标租户和目标应用做群主线 chat container、普通话题 thread
container 两类 live probe。probe 要同时证明 lower/upper exact endpoint 可见、同秒消息、
倒序分页、sender name、机器人加入前已创建/历史受限的话题，以及早于 upper 但首个快照
暂不可见的消息能在一次有界重读中收敛或被明确标成不完整。若平台表现为静默遗漏，必须
保持 catch-up unavailable，不能用 generated SDK shape 或本地 Fake 代替这个 rollout gate。

### 代码门禁与按需实时兼容性验证

所有面向 `main` 的代码先通过 `make check`；PR 和 main push 的 GitHub CI 都在
Python 3.11/3.12 执行这一个统一本地门禁。正式 Release 复用 exact main commit 的成功 CI
结论，不重新执行本节测试。

真实账号 live probes 是开发阶段按变更触发的兼容性工具，不是普通 merge 或正式 Release
门禁。升级 pinned SDK/App Server 时运行完整集合；修改 SDK Gap Adapter、相关原生生命周期、
模型提供方、飞书租户能力或服务环境时，只运行受影响的 phase。没有触及这些边界的迭代无需
运行 live probe。

macOS 系统不自带 GNU `timeout`；只在执行 live probes 时先用
`brew install coreutils` 提供 `gtimeout`。最终用户安装和日常服务运行不依赖 Homebrew
coreutils。下面的块会按平台选择命令，并在缺失时明确失败：

```bash
case "$(uname -s)" in
  Darwin) deadline=gtimeout ;;
  Linux) deadline=timeout ;;
  *) echo "unsupported live-probe platform" >&2; exit 1 ;;
esac
command -v "$deadline" >/dev/null || {
  echo "missing $deadline (macOS: brew install coreutils)" >&2
  exit 1
}

for phase in models turn-settings smoke usage steer plan polling compact concurrency interrupt skills lifecycle side; do
  "$deadline" --signal=INT --kill-after=10s 420s \
    .venv/bin/python scripts/probe_python_sdk.py \
    --cwd "$HOME/projects/test" --phase "$phase" || exit
done
"$deadline" --signal=INT --kill-after=10s 420s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$HOME/projects/test" --phase release
"$deadline" --signal=INT --kill-after=10s 660s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$HOME/projects/test" --phase config
"$deadline" --signal=INT --kill-after=10s 660s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$HOME/projects/test" --phase goal
"$deadline" --signal=INT --kill-after=10s 300s \
  .venv/bin/python scripts/probe_python_sdk.py \
  --cwd "$HOME/projects/test" --phase sandbox
```

这些 live commands 只在需要更新兼容性结论时执行，并且必须位于与服务相同的账号
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
`turn/diff/updated` latest aggregate snapshot、公开 `ThreadItem.root` 的 completed
`fileChange` / `imageGeneration.saved_path` fallback、unified diff metadata parser、v3
过期拒绝、v4 自包含循环分页、100/500 完整 manifest、1000 明确拒绝，以及 Lark
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
override；两轮都必须完成。它是 SDK/App Server 升级时对持久 Binding 配置重复应用的
端到端 shape/连续性验证，不声称能读取或证明 Thread 内部当前值。SQLite 持久化、每轮
live revalidation、admission revision 和 steer 不应用由 `make check` 的 synthetic
Runtime/SQLite 测试负责。

`usage` phase 先通过公开 `thread/read(include_turns=True)` 观察 exact Turn 为
`inProgress`，再用公开 read 确认持久化终态，最后排空该 handle 的公开 stream。它必须
收到 identity 匹配的 `thread/tokenUsage/updated`，且 `last.total_tokens` 非负、
`model_context_window` 为正数。这个 probe 验证 `/status` 的生产时序；外层进程 `timeout`
负责 SDK/App Server 违约时的最终隔离，不在进程内取消阻塞 stream。

所有依赖普通 Turn final response 的 live phase 都必须在公开 full-history 中同时看到
terminal status 与 final agent message；若 App Server 短暂先暴露 completed 状态，探针
继续有界重读，不能把部分 materialized Turn 当成最终结果。生产 Runtime 对同一窗口最多
重读 4 次，并保留无文本 Turn 的既有显式兜底。

`make check` 会运行 `probe_sdk_turn_plan.py`：真实安装 SDK 连接 fake App Server，observer
先快照 exact active Turn 的 plan，再证明队列长度、顺序和对象身份未变，最后由公开
stream 收到同一 plan 并排空 completion。`plan` live phase 会要求模型先生成 checklist，
在有界延迟 Turn 中接受一次 steer，再观察包含 steer 新步骤的完整 plan replacement、
最终 steered 回复和终态后公开 stream 中仍存在这些通知。相关 SDK/Plan 迭代必须在合入前
解释并处理任一步失败。

每次 probe 都输出实际 `openai_codex_version` 并先运行 facade inventory。若 Goal、
Skills、Side boundary inject、Thread unsubscribe、Apps 或 Thread Delete 出现候选高层 API，
`sdk_gap_facade_migrations` 必须使候选
失败，直到对应 port 切回公开 provider 并删除 shim。`make check` 还会让真实安装 SDK client 连接 fake
stdio App Server，按能力验证 fixed method/params/generated model、Goal 的即时通知与
多 Turn logical stream、resume route-before-mutation、Thread Delete 空响应与 response-loss
unknown、Side 固定 boundary、三种 unsubscribe status 与 response-loss 不重试，以及无
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
全部 absent。探针不触碰任何既有 Thread；delete 响应失败时也不得自动重发。

`release` phase 是普通持久 Thread 空闲订阅释放的原生兼容性探针。它先在 App Server A 创建
并完成一个 Thread，确认 `thread/backgroundTerminals/list(limit=1)` 为空后取消当前连接
订阅，再在同一连接按 exact ID resume 并完成后续 Turn。关闭 A 后，App Server B 必须按
同一 ID 接管、继续 Turn 并再次取消订阅；这证明 Binding 可以保留 ID/历史而无需常驻订阅。
该 phase 不等待也不冒充 App Server 最后订阅者离开后的 30 分钟卸载宽限期。当前协议没有
稳定、无副作用的 live registered-terminal fixture，因此 list 非空、检查错误与 unsubscribe
响应未知的阻断/重试由真实 SDK fake-server harness 和 Runtime 测试作为本地代码门禁。

`side` phase 是 Side 上线的原生硬门禁：先创建并物化 Parent，再启动一个可观察的普通
Parent Turn；在该 Turn 仍 running 时用公开 `thread_fork(..., ephemeral=True)` 验证 exact
ID、ephemeral 与 parent shape，通过固定 Adapter 注入 boundary，并在 Parent marker 仍存活
时启动 Side Turn，证明 Parent/Side 真并发。随后在同一 Side Thread 连续完成至少两轮，
请求 terminal cleanup 和 unsubscribe，最后证明 Parent 仍能继续并将 Parent 归档。Parent
的 seed、并发 Turn 和 after Turn 都使用公开 full-history 终态恢复；只有 ephemeral Side
使用 `handle.run()`。它不增加 Side 专项 completion-race gate；普通持久 Thread 的
read-recovery 门禁仍由 `make check` 保留。
飞书 topic 能力另做下文五入口 live 验收，尤其不能用 FakeChannel 宣称 P2P Topic 已支持。

`goal` phase 是 Goal 上线的硬门禁。它首先创建零 Turn Thread，并用公开 read 证明该
Thread 已是 idle、非 ephemeral 且有持久化 path；这一项失败时 Goal 必须保持 unavailable，
不能用 dummy Turn 或 synthetic 结果替代。随后用有界、无破坏 objective 验证 start ->
pause -> exact physical Turn interrupt -> resume rollover -> terminal -> same-Thread normal
Turn，并由第二个无本地 route 的 SDK client 只读确认 persisted active Goal。灰度前还
必须记录目标环境实际 sandbox/approval 姿态，确认原生自动 continuation
适合无人值守执行。进程重启后 external-active Goal 的隔离仍需手工验证；当前版本不会
安全重挂或替用户暂停它。

`compact` phase 创建一条短原生会话，要求公开 `compact()` 的立即 acknowledgement
之后，公开 `thread.read(include_turns=True)` 能观察到 baseline 之后新增的 completed
`contextCompaction` Turn，并在同一 Thread 完成 `COMPACT-AFTER`。只看到空响应或
Thread idle 不算通过；phase 会记录实际状态序列和 compact Turn/item 类型。生产命令
还要求 baseline 后候选唯一，多个候选或 10 分钟无终态均 fail closed。

ADR 0009 的 fail-closed 门禁会校验整个 pinned `openai_codex` Python 源码树的
确定性聚合指纹；部署包必须保留 `.py` 源文件。只有 `.pyc`、无法读取源码或任一源码
文件不匹配的安装都会按设计拒绝启动，不能绕过该门禁。

上述版本/指纹规则只属于 terminal inspection/cleanup。Goal/Skills SDK Gap Adapter 按
ADR 0014、Side boundary 与 Thread subscription Adapter 按 ADR 0021/0028、Thread Delete
Adapter 按 ADR 0037 使用 capability shape + synthetic + live harness，不得新增另一套
运行时版本白名单。Delete 的生产调用还必须固定为一个 method，并由 Runtime 承担
present/absent/unknown 对账；同样不得删除 ADR 0009 的既有门禁来“统一”两类 Adapter。

原生 `handle.run()` completion 探针在 `openai-codex==0.147.0` 第 1 次复现失败，
但它不是 production 路径；带 `--read-recovery` 的公开 polling 门禁必须通过。
interrupt phase 按 ADR 0010 精确等待 `argv[0] == marker`，执行 exact Turn
interrupt，并为 exact Thread 请求清理 App Server 已登记的后台 terminal。它记录
`foreground_process_exited_within_5s`，但该值是版本能力分类，不是 cleanup 成功
证明，也不是硬门禁。phase 必须观察 native `interrupted`、有界等待自己的 marker
自然退出而不留孤儿，并在同一 native Thread 上完成 `AFTER-CLEANUP` 新 Turn。

### 已验证的兼容性结论

实例专属的主机、账号、PID、native ID、release/备份路径、数据库行数和私网访问结果不属于
公共部署契约；维护者应把这类记录保存在被忽略的 `LOCAL_ENVIRONMENT.md` 或自己的运维
系统中。下列记录用于判断何时需要重新运行相关 live probe，不属于每个正式 Release 的资格
输入，也不能拿某次实例验收替代目标主机自己的 Host Validation：

- 固定 `openai-codex==0.147.0` 与 bundled CLI `0.147.0` 已保留 models、Turn settings、
  smoke、usage、steer、plan、polling、concurrency、interrupt、Skills、lifecycle、Side、
  release、config、Goal 和 sandbox 的开发验证记录；普通新候选不重跑完整集合。
- `0.147.0` 的同连接 `COMPACT-AFTER` 在 2026-08-25 重新验证时失败；隔离使用
  App Server `0.149.0` 的同一探针已通过。当前不增加临时 workaround，待匹配的 Python
  SDK/App Server `0.149` 组合发布后，在依赖升级迭代中重跑 compact 及相关 phase。
- 精确 SDK 源码指纹为
  `35ec9419cb9f42577080f9bf410e81cb5a97ae64e5297c4302878c73749d39eb`。
  该值属于 ADR 0009/0020 的版本兼容门禁，不是某台主机的环境配置。
- foreground tool process 不属于 background-terminal registry；
  `interrupt` 和 terminal cleanup 成功不证明前台进程已退出。
  `foreground_process_exited_within_5s=false` 是受支持分类，但 native Turn 必须进入
  `interrupted`、same-Thread resume 必须成功，probe 自己不得遗留 marker。
- 固定 SDK 的 Project config 观测分类为 `hot-reloaded`；升级后
  `restart-required` 仍可接受，但必须按新版本实际结果更新兼容性判断。
- sandbox probe 只报告 `workspace-write-or-full` 或 `read-only-or-denied` 的端到端
  体感分类，不识别配置来源，也不能替代目标账号的真实权限验收。
- Admin Web 首次上线或相关边界变更时，必须从另一台受信内网主机直接验证 readiness、
  login、CSRF/Origin/Host、
  inventory、mutation、restart session invalidation、平台日志脱敏和数据库完整性；
  使用 `http://<server-ip>:<port>`，不得把真实实例地址写回本文。
- non-interactive SSH 可能缺少账号 profile 中的代理、CA 和 PATH。所有 live probe 必须在与
  服务相同的账号 login 环境运行，只比较必要变量名或摘要，不记录环境值。
- migration、rollback、installed-source equality、database integrity、service ready、
  Linux `NRestarts` 或 macOS failure restart，以及遗留进程都必须针对本次候选重新验证，
  历史绿灯不能作为当前证据。

`--phase config` 只在给定测试 cwd 下创建临时 Project，验证同一 App Server 的
Project config 重载后自动清理；它不会读写全局 `config.toml`。实测结果为
`CONFIG-A -> CONFIG-B`，当前固定 `0.147.0` 分类为 `hot-reloaded`。升级后若输出
`restart-required` 仍是受支持结果，但必须更新兼容性判断并按重启语义验收。用户级
`~/.codex/config.toml` 不由 Netizen 监听；修改官方要求重启的键后先执行：

```bash
"$HOME/.netizen/current/source/service.sh" restart
```

然后重跑对应 probe，不能拿重启前的分类替代新配置的实际效果。

`--phase sandbox` 同样只使用临时 Project：它不向 SDK 传 sandbox/approval override，
也不写任何 Codex 配置，只通过一次 cwd marker 写入报告当前实际权限为
`workspace-write-or-full` 或 `read-only-or-denied`。这是端到端体感分类，不声称识别了
具体配置层；修改用户级 profile 后可重跑比较。

不要把某次 sandbox 分类固化成现役状态，也不能拿全局 CLI 的显示替代 SDK 探针。
需要改变飞书侧权限时，修改云端用户级 Codex 配置、按配置语义重启服务，并重跑此
phase，以新结果为准。

## 发布正式 Release

`.github/workflows/ci.yml` 在面向 `main` 的 pull request 和每次 push 到 `main` 时，分别用
Python 3.11、3.12 安装固定版本依赖并运行 `make check`。公开仓库的 `Protect main` ruleset
禁止删除和 force-push、要求所有变更经过 pull request，并把这两个 job 配置为 required
status checks；仓库所有者也不在绕过名单中。

仓库使用 `.github/workflows/release.yml` 的手工 dispatch 发布正式版本。GitHub 仓库必须启用
Immutable Releases；active 的 `Protect version tags` ruleset 禁止任何人更新或删除已创建的
`v*` tag，且无绕过者。`published-release` environment 只保留 deployment history，不配置
required reviewer 或 wait timer；创建受保护的 exact tag 并手工 dispatch 已共同表达明确的
发布意图。tag 采用与 `pyproject.toml` 完全一致的 `vX.Y.Z`。workflow 校验 clean exact-tag
checkout 和 full commit，并通过 GitHub Actions API 要求该 exact SHA 已有一次成功完成的
`main` push `Main quality gate`；只有 PR 上别的 SHA 成功，或 main run 尚未完成，
都不能发布。随后在
创建 draft 前和正式发布前再次解析 lightweight/annotated tag、核对它仍指向同一 commit；
不一致时在 Release 变为 immutable 前失败关闭。构建阶段只运行一次
`scripts/build_release_artifact.py`，生成项目自建的
deterministic archive 与 exact `install.sh` bootstrap；单个完整性 job 校验 archive SHA-256、
安全解压、manifest 的 version/full commit/qualification，再把同一份字节交给发布 job。

Release workflow 不安装依赖、不运行 `make check`，也不接收或执行账号级 Codex、飞书与
真实 service lifecycle live probes。创建 exact tag 与手工 dispatch 构成人工发布边界；
`published-release` environment 不增加一次无法提供新技术证据的重复审批。最终 job 通过
GitHub Release API 创建 draft、上传已经验证摘要的 archive 与 bootstrap、再次核对 tag 后
发布。它不依赖 GitHub CLI，也不允许覆盖已有 tag 或 asset。
发布失败时 draft 保留供维护者检查，不得用重新构建的同名文件替换原候选。

archive 内的 `.netizen-release.json` 不参与自身记录的 `sourceDigest`，但会作为独立成员被
installer 再校验。当前 `requirements.lock` 固定版本但尚未锁定各平台 wheel hash，因此
Release Integrity 证明的是 exact Netizen source archive、requirements lock 摘要和 main
代码资格的绑定，不声称目标机从 package index 下载的 wheel 与 CI 字节完全相同；目标机的
`pip check` 和 SDK synthetic probes 仍是强制 Host
Validation。若要升级为离线或 wheel 字节级认证，应另行引入 hash-locked wheelhouse。

## 安装

需要 Python 3.11 或 3.12、`venv` 和有效的当前用户 Codex 登录。Linux 还需要
systemd/logind；macOS 需要系统自带 `launchctl`、`plutil` 和当前用户的 GUI 登录会话。
服务内的 HTTPS/WebSocket 在 macOS 上直接使用系统钥匙串信任；企业根证书应由管理员安装并
标记为受信任。Netizen 不生成或维护单独的 CA bundle，Linux 的证书路径不受此行为影响。

普通用户使用 GitHub Published Release。latest URL 返回最新稳定 Release 的 bootstrap；
重定向后的脚本已经固定自己的 exact tag、项目构建 archive 名和 SHA-256，不会在安装中途
再次追随 latest：

```bash
# 最新稳定版本
curl -fsSL https://github.com/lijingda/netizen/releases/latest/download/install.sh | sh

# 指定版本（示例）
curl -fsSL https://github.com/lijingda/netizen/releases/download/v0.3.1/install.sh | sh
```

仓库根的零参数 `./install.sh` 只是同一 latest 正式入口。它不会安装当前 checkout；开发、
本机调试或云上直接编辑后部署当前工作区必须显式运行零参数 `./dev-install.sh`。两类脚本都
不接收 App ID、Secret、路径、branch、`skip-tests` 或 upgrade 参数，也不执行 `git pull`。
不要用 `sudo`“提升权限”：脚本总是为执行它的 effective user 安装；若明确以 root 执行，
得到的就是 root 自己的安装。

正式 bootstrap 只下载该 exact tag 的 `netizen-v<version>.tar.gz`，先校验 bootstrap 内嵌的
SHA-256，再用拒绝绝对路径、`..`、重复成员、链接、特殊文件和超限展开的 extractor 解包，
并要求 `.netizen-release.json` 的 version、full commit、`sourceDigest`、
`requirementsDigest` 和 qualification 全部合法。随后内部 `install-release <source-root>`
安装固定依赖，执行 package/resource 完整性、`compileall`、`pip check` 和固定 SDK probes；
它不重复运行该 exact source commit 已在 Main Qualification 中通过的全量 unittest。

`dev-install.sh` 调用内部 `install-source`。当前工作区里的受管源码、文档、Skill 和测试
（包括未提交的新文件与改动）一起计算 SHA-256 并复制到独立 release，`.git`、venv、cache
和 pyc 不进入快照；候选随后运行完整 unittest 及全部 Host Validation。Source 与 Published
使用隔离的本地 release identity，不能以相同源码摘要跨模式复用资格。两条路径在候选准备
后汇入同一个配置、凭据、Service Backend、数据库/Skill snapshot、activation intent、
`current`/`previous`、ready 和 rollback 事务。

首次有 TTY 且飞书凭据不完整时，安装器先构建、验证候选 release，再提供两种方式：默认
使用候选 venv 中固定的官方 `lark-oapi` device flow，显示 URL 与终端二维码；或手工输入
`cli_...` App ID 和隐藏 App Secret。全新骨架由官方页面选择创建新 Bot 应用或复用已有
应用；已有 `appId` 且 Secret 文件存在但内容为空时只更新该 exact 应用。已有有效 App ID
但 Secret 文件不存在时则视为显式飞书应用绑定重置：官方页面重新创建或选择应用，并允许
结果替换旧 App ID。它使用
`addons.preset=false`，只声明前置门禁
列出的 tenant scopes、`im.message.receive_v1` tenant event 和 `card.action.trigger`
callback；不安装/调用 Lark CLI，不申请 user scope/event，不保存 user token/info。确认成功
后 App ID 与 Secret 直接写入现有受保护文件；失败会显示手工回退，Ctrl-C 中止安装。

部署后更换应用不要求卸载程序。如需保留人工回退能力，先成对备份固定路径
`~/.netizen/config.yaml` 与 `~/.netizen/credentials/feishu-app-secret`，再删除 Secret 文件并
执行原来的正式或源码安装入口即可进入上述绑定重置；正常升级不要删除该文件，只需直接
再次安装。选择不同 App ID 后，旧应用的 Scope/Binding 和 Codex 原生历史仍保留，但
不会迁移到新应用的飞书 Scope。

应用重绑定和 release 激活采用两阶段语义。官方或手工流程一旦成功写入新 App ID/Secret，
这对凭据就是持久的用户配置意图；随后 tenant 权限门禁失败时不会进入 activation，候选启动
失败时则回滚旧 `current`、服务定义、数据库和 Skill，但两种失败都不自动恢复旧应用凭据。
完成管理员审批、应用发布和租户安装后重跑同一入口，会复用新绑定继续验证与激活，不重复
打开应用选择流程。若用户决定放弃重绑定，必须成对恢复事先备份的 `config.yaml` 与
`feishu-app-secret`；只恢复其中一个会形成不匹配凭据。权限门禁失败且旧进程未停止时，它
继续使用启动时已加载的旧凭据；候选启动失败回滚或任何后续服务启动，都使用磁盘上的新绑定，
因此不能把权限未就绪状态长期搁置。

取得完整凭据后，安装器使用候选 release 的官方 SDK 查询租户授权状态；tenant 权限能力
契约中的精确 scope 或官方等价 scope 组必须满足，才能准备 host 或进入 release activation。
已有完整凭据的安装发现缺失项时，无论是否有 TTY 都只对 exact App 运行一次官方浏览器修复
并重新查询；
该流程通过 stderr 输出验证 URL/二维码并有界等待最多约 660 秒，不读取 stdin。本轮刚完成
首次初始化时不重复打开修复流程；二次查询仍缺失或查询不可验证时直接退出。旧 `current`、
运行中服务和服务定义此时均未改变。device flow 只完成公开应用配置：租户管理员审批、按
租户策略发布应用版本、完成租户安装、配置可用范围、把机器人加入目标群仍是人工完成项；
完成后重新执行同一个安装入口，安装器不会轮询审批或自动重复申请。安装器还会用
`secrets.token_urlsafe(32)` 自动生成不带
换行的独立 Admin Web credential；已存在的合法文件只验证、永不覆盖。两个 Secret 都不会
进入命令参数、环境、YAML、unit 文本或日志；Feishu App Secret 只从 helper stdout 的父进程
捕获 pipe 落到 `0600` 文件，stdout 不转发到终端。

### Agent 驱动首次安装

Agent 不得使用 `curl | sh`，因为脚本内容和安装器输入会争用同一 stdin。先把 latest 或
exact-tag 正式 `install.sh` 下载到文件，再运行 `sh install.sh </dev/null`；源码安装则运行
`./dev-install.sh </dev/null`。无 TTY 时安装器绝不 prompt：若飞书凭据缺失，脚本创建带
`cli_REPLACE_ME` 的配置骨架、空的 `0600` Feishu Secret 和已经可用的 `0600` Admin
credential 后退出。Agent 按错误中打印的精确路径完成 App ID/Feishu Secret，再重新运行
同一个文件或开发入口。已有完整凭据但 tenant scope 未全部授权时是唯一的浏览器例外：候选
release 自动对 exact App 启动一次官方 device flow，通过 stderr 输出 URL/二维码并等待确认，
不读取 stdin；确认后重新查询有效权限，通过才继续，不通过则在切换 release 前退出。已有
有效配置、Secret 和完整授权的升级仍天然非交互。
若有效 App ID 对应的 Secret 文件被显式删除，无 TTY 安装会保留“文件不存在”这一重置
信号，立即退出并提示改用交互安装，或由 Agent 同时写入目标 App ID 与 Secret 后重跑；它
不会创建空文件、启动浏览器或等待输入。
不要让用户把 App Secret 粘贴到聊天、命令参数、仓库或 YAML 中。

Agent 代用户承载浏览器流程时，命令工具必须能保持同一个长运行进程跨越对话轮次，并在进程
退出前读取中间 stderr。已有完整凭据的 exact-App 权限修复不需要 PTY 或可写 stdin；首次
凭据初始化若要使用浏览器而不是 credential-file handoff，才额外需要持久 PTY 和 stdin 来
选择安装方式。满足对应能力时：

1. 首次凭据初始化在持久 PTY 中运行下载到文件的正式 installer 或 `./dev-install.sh`，等
   安装方式菜单出现后选择 `1`（直接回车也会选择默认项）；已有应用权限修复则照常使用
   `</dev/null`，安装器发现缺失项后会自动进入 exact-App flow。
2. 等待 helper 在继承的 stderr 中打印验证 URL、终端二维码和进度；将 URL 原样及可用的
   二维码交给用户，明确请用户在浏览器完成确认后回复。helper 的 credential stdout 由
   安装器私下捕获，Agent 不应尝试读取或展示凭据。
3. 把对话控制权交还用户，同时保留该进程；用户确认后继续读取同一个会话，直至安装完成或
   有效权限复查明确失败。父进程最多等待约 660 秒，过期后应重新发起，不保存或复用旧链接。

不能保留进程或转交中间输出时，不要让 Agent 承载浏览器确认：首次安装继续使用凭据文件交接；
已有应用可由管理员在飞书后台完成权限申请、审批、发布和安装后重跑。首次凭据浏览器流程失败后，
安装器只在有 TTY 时提供手工凭据回退；Agent 不要通过聊天收集 Secret。

systemd 与 launchd 都不会替服务读取完整的 `.bashrc`、`.profile` 等账号工具环境。渲染的
service definition 只给 profile loader 一条固定基础 `PATH`；macOS 额外包含标准
`/opt/homebrew/bin` 与 `/opt/homebrew/sbin`，但两种平台都不保存执行安装的 SSH、Agent、
venv 或 NVM PATH。每次 start/restart 和服务管理器自动重启都会先运行 release 内的短生命
周期 launcher；它
根据 effective uid 的账号数据库取得 home 与 login shell，执行一次无 TTY interactive
login profile，取得完整导出环境后直接 `exec` release Python。服务管理器因而继续监督
真实 Netizen PID、信号和退出状态，不把进程长期包在交互 shell、tmux 或伪终端中。

Bash/Zsh/POSIX shell 使用 `-lic`，Fish 使用等价 login + interactive 模式。profile 的
stdout 和环境快照由 launcher 增量、有界读取，stderr 直接丢弃，都不进入 journal、launchd
stderr 或文件；随机 NUL 边界定位快照，声明长度和 SHA-256 摘要会拒绝后台 writer 在边界内并发
插入的内容。探针用 `exec` 替换 login shell，因此
不会在完成环境捕获时执行 `.bash_logout`、`.zlogout` 等会话结束 hook。探针超过 10 秒、
输出超过 4 MiB、shell/profile 退出
非零或未返回完整快照时会 fail closed，错误只报告 shell 和状态，不回显可能含 Secret 的
profile 输出。用户应先在终端验证相同模式，例如 Bash 使用：

```bash
/bin/bash -lic 'command -v codex; command -v bytedcli; command -v node'
```

Bash 的 interactive login shell 按原生规则读取 `.bash_profile` / `.bash_login` / `.profile`，
不会额外自动读取 `.bashrc`。若 NVM 等初始化只在 `.bashrc`，应由 login profile 正常 source
它；launcher 不替用户强制再 source 一遍，避免常见 profile 已经引用时产生双重副作用。
Zsh、Fish 和其他支持的 shell 同样遵循各自原生 startup 顺序。

捕获结果保留 PATH、NVM、代理/CA、语言、XDG 和普通导出变量；随后重新覆盖账号身份、
`HOME`、安装时选择的 `CODEX_HOME`、Netizen 配置/两个 Secret 路径，并清除 profile 中
的 direct Feishu/Admin Secret、两条可能漂移的 Secret path，以及会污染 release Python
的 venv/Python 变量；随后写回安装器固定路径。service definition 中的 launcher、环境探针和最终 Netizen
解释器都显式使用 `-E -B -u`（非交互校验省略 `-u`），因此其他 `PYTHON*` 变量可以继续
作为工具环境存在，却不能改变受管 release Python 的 import、优化、pyc 或缓冲行为。
Codex 工具子进程的继承、过滤和显式 set 仍由同一份用户级
`~/.codex/config.toml` 的原生 `shell_environment_policy` 决定。Netizen 只通过公开
`CodexConfig` 固定 `allow_login_shell=false`：工具默认直接继承 launcher 已取得的环境，
不会再由 Codex 的 non-interactive login shell/snapshot 把 NVM PATH 覆盖回系统 PATH；
不写死 PATH，也不维护第二份变量策略。修改持久 profile 后执行 `./service.sh restart`
即可；某个已有终端里的临时 `export`、alias、未导出的 shell function 和真实 TTY 状态
不会被后台服务继承。

systemd user service 在用户注销后继续运行依赖 linger。若未启用，交互安装会执行一次
`sudo loginctl enable-linger <当前用户>`；无 TTY 安装会先退出并打印这条命令，Agent 可
经主机授权独立执行后重试。安装器不会关闭 linger，因为它可能同时服务该用户的其他
user units。

macOS 不使用 linger，也不会请求 sudo。`~/Library/LaunchAgents/io.github.lijingda.netizen.plist`
只属于当前用户登录会话；退出登录会停止服务，下次登录由 `RunAtLoad` 自动启动。若用户执行
`service.sh stop`，当前会话保持停止，但 plist 仍为下一次登录启用。每次显式 bootstrap 前
安装器都会执行 `launchctl enable gui/<uid>/io.github.lijingda.netizen`，清除 launchd 保存的
sticky disabled 状态。

### 候选验证与切换

安装器先在新 release 创建全新 venv，并以 `requirements.lock` 约束安装依赖。
Source Install 额外执行完整 unittest；两路都在目标机运行 `compileall`、`pip check`、固定
SDK synthetic probes 和 `scripts/verify_installed_release.py`（逐一比较 Python 与 Admin
HTML/CSS/JS 等所有普通 package files，并做 `importlib.resources` smoke）、配置解析和候选
venv 中固定 Codex CLI 的 `login status`。这些安全检查使用安装调用者经清理的环境；安装器不会在 service cgroup
之外执行任意账号 profile，因为 profile 可以产生不可逆副作用或自行 daemonize。真实
profile 只在候选 service 启动时加载：首次安装和原本 active 的升级会等待 ready，失败时
回滚；原本停止的升级保持停止，之后 `service.sh start/restart` 会等待最多 45 秒确认 ready
并直接暴露 shell/profile 启动错误；对已经 loaded 且已有有效 ready marker 的服务，
`start` 幂等返回，loaded 但未 ready 则只做有界等待。任意具体 MCP/工具是否可用仍取决于其自身配置，不能
由安装器枚举。真实 Thread capability phases 只在相关 SDK/Adapter/环境开发变更时按前文
运行，不在正式 Release workflow 或每个最终用户安装中重复创建探针 Thread。

激活阶段停止现役 user service（如果原本在运行），随后对 configured Admin Web address
执行 best-effort bind preflight。所有成功 socket 会持有到本次地址枚举结束，IPv6 明确
设置 `IPV6_V6ONLY`；`EADDRNOTAVAIL` 只有在同一配置至少一个地址成功时才可忽略，端口占用
则在数据库快照、service definition 和 `current` 切换之前失败并进入既有 activation rollback。runtime
bind 仍是最终事实。预检通过后才渲染并验证平台 service definition，原子切换
`current`，再用候选 release 完整替换
`${CODEX_HOME:-~/.codex}/skills/netizen-user-guide`。这一个 Skill 内的人工修改会在升级
时丢失；其他 Skill 不会被读取或修改。首次安装会 enable 并启动服务；升级前若服务在
运行，新版本会启动并等待主进程发布 `0600` ready marker；若原本停止则保持当前会话停止。
Linux 延续原 enabled/disabled 意图；macOS 保证 plist 已安装/enabled，供下次登录自动启动。

只有 service definition、release、Skill 和服务 ready 全部成功，旧 `current` 才记录为 `previous`，
并只保留这两个 release。候选启动前会在旧进程停止且端口预检通过后复制
`channel.sqlite3` 及现有
sidecar；任一步失败都会停止候选并恢复旧 release 指针、service definition、数据库、Skill
和 enable/active 状态。launcher 启动时在稳定的 `state/service.lifetime.lock` inode 上持有
独占锁；主进程接管同一 FD 后立即恢复 CLOEXEC，Codex 工具与后台 terminal 不会继承。
安装器只有在服务管理器目标已卸载且该锁可取得时才恢复数据库或 Skill；若无法确认，跳过
两项恢复并保留 recovery snapshot，避免仍存活的候选写入旧状态。即使回滚点来自该 Skill 上线前，也会按安装前快照恢复“原本不存在”
的状态，不要求旧 release 提供新脚本。若数据库或 Skill 恢复本身失败，受保留的 state
目录会保存 recovery snapshot，并在错误里打印精确路径。

任何可能停止旧服务的 mutation 之前，安装器都会原子写入 activation intent，记录本次
操作完成后应保持的 active/enabled 状态。正常成功或完整回滚会清除它；若进程在切换中被
`SIGKILL` 或异常退出，下次执行原安装入口会优先恢复该意图，再执行候选切换。因此
发布后的半成品状态不会被误认成用户主动停止/禁用服务；`uninstall.sh` 会清除遗留意图。

从早期 `/etc/systemd/system/netizen.service` 升级时，安装器只自动迁移带 Netizen 标识的
旧 unit。若它仍 active/enabled，交互安装会请求一次 `sudo systemctl disable --now
netizen.service`；无 TTY 时打印同一条预备命令。候选失败会尽力恢复旧 system service。
不认识的同名 system unit 一律 fail closed，不代替用户停用。

### 升级、启停和卸载

正式升级重新运行 latest 或选定 exact-tag installer；更新任意开发目录后运行源码入口：

```bash
./dev-install.sh
```

日常服务控制只走当前用户的平台 service manager；`service.sh` 内外都不使用 sudo：

```bash
./service.sh start
./service.sh stop
./service.sh restart
./service.sh status
```

若原 checkout 已删除，从安装目录使用完全相同的入口：

```bash
"$HOME/.netizen/current/source/service.sh" status
"$HOME/.netizen/current/source/uninstall.sh"
```

`uninstall.sh` 无参数，停止/disable user service，并删除渲染的 unit/plist、程序 releases、
安装 cache 和精确的受管用户指南 Skill。它明确保留 `config.yaml`、`credentials`、含 Channel
SQLite 的 `state`、Project 目录、其他 Codex Skills 及原生 Thread/Turn 历史。若用户也
要删除这些数据，应在确认备份和影响后另行处理，不能扩张卸载器的默认删除范围。

安装或升级后核对 ready 日志，并在飞书发送“运行中的任务再发一条消息会怎样？”确认
自然语言回答包含 steer 且明确不排队；再用 `$netizen-user-guide 如何切换会话？` 验证
显式调用能说明 `/sessions` 和 `/resume`。

`config.yaml` 采用仓库示例的 mapping 形态。Feishu Secret 文件只含 raw value，权限必须是
`0600` 或更严格；Admin credential 必须是 `token_urlsafe(32)` 的 canonical base64url 单行、
无尾换行，且 mode 必须精确为 `0600`。若需要由 Agent 预配置，可安全地以文件写入 API/
受控 stdin 写入；不要把 Secret 放在 CLI 参数或 shell history 中。示意路径可由安装器首次
无 TTY 运行生成：

```bash
./dev-install.sh </dev/null  # 缺配置时生成骨架后明确退出
```

从带 Netizen allowlist 的旧版本升级时，必须先从 live `config.yaml` 删除整个
`access:` 段。用户和群的可用范围改由飞书应用后台管理；新版若发现旧段会明确拒绝
启动，避免部署者误以为这些字段仍然生效。

不要把 Secret 内容写入 YAML、仓库、shell 参数、shell profile、systemd `Environment=`
或 LaunchAgent plist。service definition 只注入两个受保护文件的固定路径。

`adminWeb` 默认等价于：

```yaml
adminWeb:
  enabled: true
  host: 0.0.0.0
  port: 8787
```

可覆盖 host/port 或显式关闭；启用时 `NETIZEN_ADMIN_SECRET_FILE` 必须是绝对路径。直接在
受信内网打开 `http://<服务器 IP>:8787`，未登录时根路径会跳转到 `/login`。登录凭据可由
实例管理员在受控终端读取：

```bash
cat "$HOME/.netizen/credentials/admin-web-secret"
```

轮换时用安全的原子文件写入替换同一路径并保持 0600，然后刷新页面；运行中 auth 会在下一
认证边界检测到合法 identity/content 变化并立即注销全部旧 session。非法替换会锁闭 Admin
admission，修复文件后仍需 `./service.sh restart`，不会自动重新开放。V1 使用不加密的内网
HTTP；不得把该端口直接暴露到不受信网络。

`instance.projectRoot` 用于限制从飞书自动创建的空 Project；未配置时回退为
`defaultCwd.parent`。Channel Database 只接受当前 schema v6，不在服务启动时自动迁移旧
schema。schema v6 为 Binding 增加 Mention Context Mode、exact Context Boundary 和独立
revision；不保存任何补充消息正文。v5 -> v6 cutover 必须在 release transaction 中完成，
并保留现有 Scope/Binding/Project、去重记录和 `side_topics` 永久路由墓碑；迁移失败时
恢复旧数据库与旧 release。不得归档后创建空数据库，否则旧 Side 话题可能重新落入普通
Binding 路由。配置文件中的 `projects` mapping 会在启动时以 `INSERT OR IGNORE` 导入，数据库
里已经停用或由飞书创建的条目始终优先。不要手工编辑 `projects` 或 `bindings` 表。

浏览器初始化会请求 `card.action.trigger`；手工准备应用时，使用卡片前须在飞书开发者后台
打开“事件与回调 → 回调配置”。回调仍走现有 WebSocket 长连接，不需要公网 callback URL；
若未启用，文本命令正常但点击卡片不会产生事件。

## Fail-closed 运维语义

- 若 Netizen 提示 admission 已关闭或要求重启，表示一次 native start/turn/terminal
  结果无法安全判定。Pilot 有意不自动重试，也不自动修改 Binding；检查平台日志后
  人工执行当前 release 的 `service.sh restart`。
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
  v4 callback payload 明文包含路径和分页 manifest，这是已接受的飞书应用边界，不是
  下载凭证或快照；不要手工写 SQLite，也不要把失败文件补发到主聊天。
- 若 Side 显示 `creating`、清理未确认或要求再次结束，不要删除 SQLite route 或把该话题
  当普通 Binding 使用；在原 Side 话题重试 `/side close`，或正常重启让遗留 open route
  转为 expired。Side 卡在 active close 时先检查 App Server/平台日志；禁止猜测 native ID。
- 正常停机会停止所有 pulse，并用已记录的 exact reaction ID 清理常驻的 `Typing` 与
  当时可见的 `THINKING`。若进程被 `SIGKILL`、主机掉电或崩溃，运行态表情可能留在原
  消息；在“不持久化飞书展示状态”的边界下无法安全恢复，禁止为此扫描/猜测 reaction
  或修改 SQLite。

## 平台服务管理器

### Linux systemd

仓库 `deploy/netizen.service` 是安装器渲染的 user-unit 模板；它不包含 `User=`、固定
home 或 release 路径，也有意不增加外层 filesystem/network sandbox。用户仍由共享
`.codex/config.toml` 的原生 permission mode 决定 Codex 行为。不要手工把模板复制到
`/etc/systemd/system`，也不要用 system-level `systemctl` 控制它。

```bash
./service.sh status
journalctl --user -u netizen.service -n 100 --no-pager
loginctl show-user "$USER" --property=Linger
```

正式与源码安装器共同负责 unit 写入、`systemctl --user daemon-reload/enable`、首次启动或按旧状态
切换；`uninstall.sh` 负责 stop/disable 和删除。`service.sh` 只接受
`start|stop|restart|status`，不会安装、升级、enable、改配置或请求 sudo。

### macOS LaunchAgent

安装器用 `plistlib` 生成并回读
`~/Library/LaunchAgents/io.github.lijingda.netizen.plist`，随后执行 `plutil -lint`。受管判断
同时要求：当前 UID 拥有的普通非 symlink 文件、无 group/world write、exact Label、指向
`~/.netizen/current` 的 exact `ProgramArguments`，以及受管 environment sentinel；任一不符
都拒绝覆盖或卸载。不要手工用 `launchctl load/unload` 或编辑 plist；使用相同的
`service.sh` 命令。

```bash
./service.sh status
tail -n 100 "$HOME/.netizen/state/netizen.log"
tail -n 100 "$HOME/.netizen/state/launchd.stderr.log"
```

状态只把 `launchctl print gui/<uid>/io.github.lijingda.netizen` 的退出码作为 loaded 判断，
不解析其文本；Apple 不把该文本声明为稳定 API。`start` 使用
`enable + bootstrap gui/<uid> <plist>`，`stop` 使用 exact service target 的 `bootout` 并等待
lifetime lock，`restart` 是完整 stop-confirm 后再 bootstrap，不使用 `kickstart`。应用 INFO
日志由标准库 rotating handler 保留为最多 5 MiB × 3 个文件；launcher/exec 失败单独进入
`launchd.stderr.log`，两者都不得含 Secret。

ready marker 只有在 Admin credential/closed bind、唯一 Codex Runtime、Store、Channel
application、Feishu ingress 和 Admin admission 全部成功后才以原子 `0600` 文件发布。
installer 每次启动前权威删除旧 marker，launcher 每次进程启动再次清理，正常退出也尽力
删除；service manager 的 loaded/active 不能替代 ready。正常 shutdown 在 Channel
background loop 使用一个 60 秒 monotonic absolute budget：先关闭 Admin listener、Feishu
policy 和 Runtime admission，再排空 Admin/Feishu handlers 与 blocking I/O，最后清理 native
Turns/reactions、Codex transport 和 Store。systemd `TimeoutStopSec=75s` 与 LaunchAgent
`ExitTimeOut=75` 给 Python finally 留出完整内部预算；安装器的停止确认窗口为 90 秒，且未
取得 lifetime lock 就不会执行状态回滚。
唯一兼容例外是候选失败后恢复本机制上线前的旧 Linux unit：旧 release 的重启仍按其原有
journal ready 日志确认；候选和所有新 service definition 只接受私有 marker。

## 验收顺序

每次改动安装器、launcher、主进程 ready 时，先完成两套平台门禁：Linux 重跑 systemd
fresh/active/stopped upgrade、失败回滚、linger 和卸载；真实 macOS 14+ 受支持架构真机在
实际 GUI 登录用户下依次验证首次安装、`start|stop|restart|status`、active/stopped upgrade、
故意启动失败后恢复旧版本、失败自动重启、sleep/wake、logout/login 后自动启动，以及卸载
保留边界。macOS 还必须在 Codex 启动一个后台 terminal 后停止 Netizen，确认 terminal 可继续
存活但不会持有 `service.lifetime.lock`；检查 plist、进程 argv/environment、`netizen.log` 和
`launchd.stderr.log` 均不含 App/Admin Secret。最后在两平台重跑真实 Codex Thread、steer、
cleanup 和 exact-ID resume probes；fake launchctl/systemctl 单测不能替代这些真机门禁。

先从另一台受信内网主机直接访问 `http://<服务器 IP>:8787`：未登录的 `/` 返回 303
重定向到 `/login`（不返回 HTML 或状态），未知 route 和 API 必须返回 401，只有登录页复用的
无状态 CSS 可匿名读取，`/health/ready` 只返回无细节状态；
使用独立 credential 登录后，检查三个一级
页面、筛选、分页和五秒 runtime polling。Sessions 分别选择 10/20/50/100，确认前后翻页、
页码与当前页条数正确；在 P2P、普通群和话题群 Binding 上确认显示真人/群名称与正确类型，
重复刷新命中缓存，名称链接能打开对应飞书会话，话题行只承诺打开所在会话。100 条页面的
首屏和 polling 都不得向单次 runtime snapshot 请求发送超过 50 个 ID。依次验收 Project
register/create/enable/disable，
两个 Scope 中 active/inactive、Lazy/materialized/archived Binding 的 create/activate/
configure/rename/archive/两种 unarchive/delete-lazy/Stop/Release，以及一个 open Side 的 exact
Close。双击同一 action 应返回 stale/consumed；与飞书并发操作同一目标时只允许符合 exact
precondition 的一方提交。重启服务后旧 Admin session 必须失效，持久 Binding/设置/Side
墓碑不变；journal 不得出现 credential、cookie/action token、cwd、name/preview 或 body。
最后占用 configured port 再执行一次候选激活，确认它在数据库快照/current 切换前失败且旧
release 恢复；释放端口后再部署。以上真实浏览器、跨主机与端口回滚属于 Admin Web 首次
上线或相关边界变更的 live 验收，本地 loopback 单测不能替代。

1. P2P `/help`、exact `/new`、`/sessions`、`/status`（native=pending）；`/sessions`
   返回分页卡片，将 active Binding 置顶、将 lazy Binding 显示为“新会话”，有原生
   Thread 时优先显示 `name`、否则显示 `preview`。创建第二个会话后点击第一项的
   “设为当前”，原卡必须刷新 active 标记，且不得停止另一会话仍在运行的 Turn；归档项
   不得混入普通卡片。idle materialized 行显示带确认的“归档”，Lazy、running、Goal、
   compacting 和 lifecycle-unknown 行不显示。idle Lazy 行与 Delete capability 可用的 idle
   materialized 行显示“删除”；第一次点击只打开独立红色确认卡且不得 mutation，running、
   Goal、compacting 和 lifecycle-unknown 行不显示。`/status` 分行显示 `name`、`preview`
   与“上下文窗口：暂无（首条消息后生成）”。帮助包含 `/config`、`/compact`、`/goal`、
   `/rename`、`/archive`、`/delete`、
   `/unarchive`，不包含
   `/skills`、`/model`、`/effort`、`/fast` 或当前不可用的
   `/plan`、`/apps`；`/copy`、`/vim`、`/theme`、`/exit` 也不展示。
   另发送 `/new test`、`/new none`、带引号和坏引号的 `/new ...`，都必须得到同一迁移提示、
   零 Binding mutation；`//new test` 仍作为字面 prompt。
2. 通过 `/new` 卡片选择 `test` Project 和 inherit Codex 后发送首条 prompt：原消息先出现
   并一直保留敲键盘（`Typing`）表情，
   “思考中”（`THINKING`）按低频节奏显示/隐藏，不收到“已接收”或心跳回复。running 时
   `/status` 出现完整 native ID、
   已接受 steer 次数，以及原生 checklist（`✓/→/○`）；无 plan 时明确显示“Codex 尚未
   生成”，observer unavailable 时显示“暂不可用”。发送一条 steer 后，原消息 pulse
   保持原锚点，steer 消息只添加
   `OnIt` 且正常不回复文字；`/status` 在新 plan 到达前标记旧 checklist 可能过期，之后
   整体替换并清除标记。Turn 到达终态后先添加 completed/failed/interrupted 对应的
   `DONE`/`ERROR`/`CrossMark`，再移除 `THINKING` 与 `Typing`，模型结果照常回复；
   在可观测 Turn 完成、公开 usage 通知已排空后 `/status` 显示当前
   窗口已用 tokens、窗口上限和百分比。再启动一个普通 Turn 时，running `/status` 保留
   并标注“上一轮完成时”的快照；本轮完成后覆盖为新值。固定 SDK 若丢失即时完成通知，
   则终态后明确显示暂不可用并在下一次可观测 Turn 完成后更新。该继承路径不得读取模型
   目录或向 SDK 传 Model/Effort/Speed override。
3. 零参数 `/new` 只显示一个创建 form，不显示任务输入或快速按钮。Project 使用现有单个
   静态下拉框，并展示 Registry 中全部 enabled 项和 `none`；准备 13 个以上 enabled
   Projects 验证没有 12 项截断、分页控件或命令兜底，disabled 项不出现。P2P 表单不显示
   @ 上下文模式；群主线和普通群话题显示“仅当前 @ 消息（默认）”与“补充上次请求后的
   消息”。选择 inherit Codex 时不保存 Model/Effort/Speed override；选择实际 Model 时
   三项必须与本机 `models` phase 一致并全部保存。模型目录不可用时仍显示可提交的
   Project + inherit 表单。成功卡片显示 Project、会话短 ID、Model 来源与 @ 上下文模式；
   即使原卡更新失败，同一 Scope 也应收到等价兜底回复。再用足够大的 Registry 触发真实
   平台容量错误，必须明确说明没有截断、分页或快捷创建，且零 Binding mutation。
4. 在 idle active Binding 上发送 `/config`，选择三项和群聊 @ 上下文模式后原子保存；
   不得要求任务或立即启动 Turn，也不得显示目标会话；配置其他会话必须先 `/resume`。
   后续每条需要启动新 Turn 的普通消息都在 exact native Thread 重新校验并显式应用，
   配置不会在首轮后清除。对目录中支持加速 Tier 的模型依次验收
   `Fast/priority -> Fast/priority -> /config Standard/default -> Standard/default`，
   四轮必须在同一 native Thread 连续成功；卡片只显示动态名称，不显示协议 ID，也不
   出现费用提示。打开卡片后先
   `/resume` 另一个 Binding，再提交旧卡片，必须零 Codex mutation 并提示重开；两张
   `/config` 卡也必须分别由 settings/context revision 拒绝后提交的旧卡。启用 catch-up 时
   exact card anchor 读取失败必须同时保持旧 Model 与旧 mode；running Turn 上 `/config`
   必须拒绝，running steer 不得解析或应用 Binding 配置。
5. 在已有历史的 idle Binding 发送 `/compact`：先收到开始提示，`/status` 显示
   `compacting`，并使压缩前的上下文用量快照失效；普通 Prompt、`/config`、再次
   `/compact` 必须拒绝，`/stop` 必须说明只控制普通 Turn。lazy/running Binding 上
   `/compact` 必须拒绝。固定 `0.147.0` 当前存在 compact 完成后同连接后续 Turn 失败的已知
   上游兼容性问题；该项不作为正式 Release 门禁，也不增加临时 workaround，待匹配的
   Python SDK/App Server `0.149` 发布后再验收同一 Thread 继续和用量恢复。压缩期间不要从
   CLI/其他 App Server 并发写同一 Thread。
6. `/skills` 必须作为未知命令拒绝且零 Codex mutation；用自然语言询问当前可用 Skill
   必须按普通 Prompt 启动或 steer。在消息开头连续输入两个 `$skill-name`，只启动一个
   原生 Turn；running 时同样只 steer exact Turn 一次。未知、
   disabled、重名或 stale 引用必须零 Codex mutation；引用消息历史里的 `$skill` 不得
   激活，当前消息的引用仍正常。
7. 在已通过 zero-Turn live gate 的环境发送 `/goal <objective>`，卡片与 `/status` /
   `/sessions` 显示 Goal 状态；同一 Binding 的普通 Prompt、`/config`、`/compact` 被
   拒绝。验证自动 rollover 后只有一个逻辑终态；`/goal pause` 与 `/stop` 都先暂停 Goal、
   中断 exact 物理 Turn 并请求 terminal cleanup，随后 `/goal resume` 可继续，paused/
   terminal 时 `/goal clear` 可清除。重启期间保留的 active Goal 必须显示为外部活跃并
   拒绝 mutation，提示在原生 Codex 暂停，不能自动重挂。
8. 第二轮验证 exact-ID 上下文。
9. 长 Turn 中发第二条消息，结果必须被 steer 改变；native steer 失败时不得出现
   `OnIt` 或 steer count，必须明确提示本条未执行。故意使 `OnIt` 投递失败时，只在 native
   steer 已成功后收到“已接收调整”兜底，原 Turn pulse 不受影响。
10. 长 Turn `/stop`，确认先收到“正在中断当前 Codex Turn”，再收到明确警告前台工具
   进程可能继续运行的唯一终态；native Turn 为 `interrupted`，之后同一 Thread 可
   继续。不得把 cleanup 空响应当作前台进程退出证明。
11. 在空闲 active 普通会话查看 `/status` 的“Netizen 订阅”行；等待十五分钟后应显示当前
    连接已取消订阅，但不得声称 writer 已立即释放。再次发送消息必须 resume 同一 native
    ID 并保留上下文。再用 `/new` 或 `/resume` 切走一个 idle 会话，确认旧订阅立即释放；
    `/release` 应得到相同的保留 Binding/历史语义，running、后台 terminal 或状态未知时
    必须拒绝。重启服务后不扫描旧 Binding 或重建 timer。最后验证全局
    `codex exec resume <native-id> "..."` 能接续飞书 Thread；必要时要等 App Server 的
    最后订阅宽限期释放 writer，不能把 unsubscribe 返回当成 writer 已释放证明。
12. 同一 Project 两个 Binding 同时运行，cwd 相同、native ID 不同。
13. 运行 exact-`argv[0]` interrupt probe，记录 foreground 5 秒退出分类；probe 有界
   等待其测试 marker 自然退出后，检查无遗留 exact marker/App Server probe 进程。
14. 加一个测试群：未 @ 不触发，每条 @ 可用。在话题群中分别用纯文本根消息
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
15. 验收逐条引用：在 P2P 和普通群主线分别验证首层与嵌套文本/富文本；
    由 A 发送被引用消息、B 发送当前提问，模型输入必须把两名发送者分别归到
    `quoted_message` 与 `current_message`；群内当前提问仍要 `@机器人`。再验证
    Card 1.0/default 和 Card 2.0 的可见文本、
    图片/文件的“公开资源 key 与元数据，而非正文”提示、撤回目标与临时去掉
    `im:message.group_msg`
    后的零 Codex 提交。在真实话题内回复根消息时不应混入“被引用消息”
    上下文；话题 Scope 和原有上下文仍正常。
16. 在单聊发送普通图片；在群聊发送 `@机器人` 的多图富文本；再分别验证文字引用
    图片、当前图文引用另一条图文和 catch-up 补充消息中的图片。模型必须能描述真实像素
    且正确区分 supplemental/当前/引用来源；
    删除其中一张资源后重试必须零提交。连续发送的多条独立图片不要求自动合并。
17. 在单聊、群聊、话题分别发送 `/settings` 和零参数 `/new`；卡片必须留在原
    Scope。Settings 只显示已实现分区，Projects 使用下拉管理且新增表单留在同一卡片；
    创建/登记/启停或业务错误后仍显示原 Projects 分区。重启服务后 Registry 仍存在；
    停用条目不能新建 Binding，但旧 Binding 仍可继续。`/new` Project 下拉应同步展示全部
    enabled 条目而不分页。
18. 由群内另一名真实参与者点击设置卡片的刷新操作，确认 callback operator 不受
    Netizen allowlist/ACL 限制且响应不转为私聊。自动化契约已通过；该跨账号手工项
    尚未复验，是当前小范围 Pilot 的非阻塞待办。
19. 在一个 idle materialized 当前会话上验收 `/rename` 直接参数和无参数卡片，Codex
    App/CLI 与 `/sessions`、`/status` 都应看到同一原生名称。打开 `/archive` 卡后先切换
    会话，再点旧卡必须零 mutation；重新归档后 active pointer 为空，Binding 配置保留，
    普通 `/sessions` 不显示它而 `/sessions archived` 显示。用卡片或
    `/unarchive <短 ID>` 恢复并自动切换。Lazy 会话的 `/delete` 必须显示红色不可恢复
    二次确认、只删 Binding，并验证 stale current 防护。idle materialized `/delete` 必须
    显示原生 Thread、spawned descendants、Codex App/CLI 历史与 Binding 均永久删除的红色
    确认卡；打开卡后切换 active、令 Lazy 物化或改变 exact native ID，再点击都必须零
    mutation。正常确认后 root 与 descendants 从原生四视图消失，Binding/pointer 再删除。
    用 synthetic response loss 分别覆盖：四视图 absent 时提交 Binding、任一 present 时
    保留 Binding 并允许重新确认、查询冲突/失败时保留 lifecycle-unknown 并关闭 admission；
    任何路径都不能盲目再次调用 delete。running、Goal、compacting、ephemeral、未持久化
    和 compatibility gate 不可用时必须拒绝且保持原生 Thread 与 Binding。
    另在 `/sessions` 中直接归档一个 idle materialized 非当前行，确认真实 active pointer
    不变、目标移入 archived catalog、原卡刷新并在删除末页唯一项时夹取页码；再归档当前
    行，确认 pointer 为空。打开行内确认后改变 active pointer 或令目标开始 running，再点
    旧按钮必须零 mutation。再从 `/sessions` 分别删除 idle Lazy 与 idle materialized 非当前
    行：第一次点击只出现带 exact 目标的红色确认卡，最终确认后 active pointer 保持不变；
    删除当前行时 pointer 清空。确认期间切换 active、令 Lazy 物化、改变 exact native ID 或
    令目标开始 running，旧按钮必须零 mutation；删除末页唯一项后页码夹取。materialized
    路径必须复用上述一次四视图对账与 admission fail-closed。`/archive` 与 `/delete` 仍不
    接受目标参数且保持 current-only，`/sessions archived` 不提供删除。
20. 在已物化 Parent 上分别从 P2P、P2P 话题、普通群主线、普通群话题和话题模式群触发
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
    形状。重启服务后旧 Side 明确 expired 且不创建 Binding；再验证 idle 两小时过期。若
    P2P 建话题返回 230071，记录为当前飞书 live gate 未通过并保持 Side unavailable，不能
    以单元测试替代。
21. 在普通持久 Turn 中分别用 native Turn diff 和结构化 items 生成 Project 内普通文件、
    Project 外普通文件、Codex 原生 generated-images 目录中的 PNG/JPEG/GIF/WebP 图片和
    至少 18 个文件；另对 100、500 个 synthetic manifest 发真实目标应用卡片，确认平台
    完整 create/update；1000 个必须在本地门禁中明确拒绝且不截断，并保留目标应用
    96.9 KB 请求返回 230099/200800 的容量证据。无文件
    Turn 必须仍只有纯文本最终回复；有文件 Turn 必须只有一张同时包含最终回复和本轮文件的
    卡片。Project 内文件显示相对路径，Project 外文件显示脱敏逻辑位置；所有条目显示大小，
    按 8 个一页完整翻页，可见正文不出现绝对路径、预览、diff 或发送全部；v4 callback
    payload 则必须逐项携带明文 absolute path。依次在 P2P 平面消息、
    群主线和已有话题点击普通文件与“发送原图到话题”：平面卡片必须出现以该卡片为锚点的话题，记录真实
    root/parent/thread 返回；已有话题必须保持原 thread ID，飞书能正常预览/下载实际文件。
    切换到另一 Binding 后旧卡仍能翻页和发送；正常重启 Netizen/App Server 后，再点击
    重启前的 v4 卡，必须只从 callback 内的原回答 + manifest 恢复，且不读取 source card、completed
    Turn。抽样一张升级前 v3 卡，必须明确提示已过期且不发送文件、不读取 history。
    重复点击同一按钮不产生重复文件消息。再分别在点击前删除文件、改成目录、把同一上报
    路径重新绑定到另一个普通文件和删除原卡片：前两类不可用目标应失败，重绑路径发送点击
    时当前内容，删除卡片失败；所有失败均保持原卡且没有文件掉入主聊天。翻页还必须确认
    最终回复区逐字保留、缺失条目在原页显示不可用，并按单个“下一页/回到第一页”按钮循环
    覆盖全部页面。
    P2P 若返回 230071 必须记录为
    本轮文件 live gate 未通过，不得用 FakeChannel 或普通主线发送替代。最后确认这些操作不
    改变 schema v6 表、Binding、Turn settings、Context Boundary 或 Side route 行数。

CLI 中新增的消息不要求回填飞书；验证目标是共享原生后端和可接续性，不是两个 UI
的逐条镜像。
