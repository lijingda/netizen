# 兼容性历史记录

本文件保存从[部署与验收](deployment.md)迁出的公共历史证据，按日期/版本分组。
记录正文原样保留，仅修正迁移后的相对链接；当时的“当前”“本轮”“待验收”指该次记录，
不代表现役版本或后来候选。临时路径仅供追溯，不保证产物仍可用。
当前行为以[工程设计](design.md)为准；命令、触发条件、验收矩阵与通过判据仍在部署文档。
旧版本、mock provider、API ACK、浏览器 fixture 和真实客户端证据不可互相替代。
不在这里记录私有主机坐标；原始私有证据继续留在忽略文件或运维系统。

<a id="attachments-admin-20261103"></a>

## 2026-11-03：附件与 Admin（Codex SDK 0.163.0 / Channel SDK 1.4.0）

Admin 原生探针验证 paginated/legacy 最新完整轮、最新空回答不回溯、已归档和冷进程
归档读取、零输入精确拒绝与缺失 Thread 区分、搜索大小写；105 个原生临时会话中仅一个
属于本实例时，确实跨两个原生页才命中，生产管理查询约 0.038 秒。这是本机合成目录
结果，不是生产长历史、任意规模或最坏延迟承诺。
附件读取验证 read-only/workspace-write 两种预设下新线程、冷恢复及持久分支，六种
场景均从真实 exec_command 结果确认项目外合成原件可读，没有权限覆盖；用户自定义
拒读策略仍按原生权限处理。固定 SDK 归一化样本验证独立 file、普通群/话题中的
`post.files + @`、准确引用及无 @ 拒绝，不能据此声称所有人工客户端均可构造这些事件。

固定 SDK 下载在完整缓冲后才返回 bytes，没有流式/下载前可信大小/字节硬上限。
loopback 16 MiB 合成响应的 Python 分配峰值约 32.3 MiB，两并发约 48.2 MiB；这不代表
RSS 或保证倍数。附件以本条数量、后验大小和有限批次/真实下载额度控制资源，
超时/取消不提前释放仍工作的下载额度。按后续确认的简化方案，不设置累计容量配额，
不在新输入时扫描旧附件；实际写盘失败按整条输入失败处理。输入限制、取消收尾、
历史残留不阻塞新输入和写盘失败时只清理本次文件均有行为测试。

隔离 Chromium 使用实际 Admin 认证、HTTP 和静态资源、合成项目/附件/会话，验证：

- 1440/390/320 像素下共用文件浏览，Project/worktree 仍只读，附件按层展开、文本预览，
  PDF 不支持预览仍可删除，最后原件删除后批次消失，认证失效清空内容；删除结果未知可刷新恢复。
- 1440/320 像素下 Enter 单次搜索、失败保持成功查询/行、共享 Project 默认导航、普通/Side
  本页分组不增加目录请求、异步补名不串位；普通/归档最近回答、无回答、失败和关闭后的迟到响应。
- 上述浏览器路径未发现 JS/CSP 错误或外部请求。保存开关、提及序列化、既有结束提醒资格、
  新问题逐卡提醒与重绘去重由 SDK/运行时/UI 行为测试覆盖。

真实飞书桌面/手机客户端的附件 + @ 发送入口、问题卡实际提醒效果、飞书 WebView 和
macOS 平台效果尚未验收；不得用 API 成功、浏览器窄屏或 mock provider 代替。
按操作隔离简化附件目录后的工作区 `make check` 通过：3,963 项测试，15 项按平台或显式开关跳过；编译、依赖、
SDK 兼容性检查均通过。隔离 wheel/sdist 构建及新 Admin 静态资源验证通过。后续候选版本
仍按改动运行相应门禁；发布和部署由维护者单独安排。

独立 review 发现 Admin 探针曾移除临时 HOME 覆盖，SDK 合并环境后会继承真实 HOME。
已保留临时 HOME/CODEX_HOME 并完整重跑该探针，原生搜索和最近回答断言全部通过；
上面的查询耗时取修正后的结果。未使用真实模型或读取生产会话目录。
review 另补齐无正文自身 @ + 文件、仅顶层 `post.files` 的完整提交回归，以及最后原件
删除后批次清理失败/中断的恢复覆盖；修复未新增原生接收确认或执行期删除锁。
用户随后要求按操作隔离失败，独立设计 review 通过：取消全局附件标记、初始化回滚、
进程级根身份缓存和启动恢复；按每次操作创建所需目录，本批记录保留准确文件证据。
删除只确认目标文件，直接尽力收掉内部记录与空目录，不再搬回 staging 或以清理失败
覆盖删除成功。专项覆盖目录创建半成品、写入与清理同时失败、损坏旧批次、空目录
清理失败后浏览响应，以及附件路径不可用时普通服务正常启动。旧残留不会成为新操作前提。

<a id="suppress-reply-20261031"></a>

## 2026-10-31：静默撤销与交接（SDK 0.163.0）

2026-10-31 静默使用原则与展示调整：收到并确认属于当前任务的新输入后，在原生提交
前立即撤销旧静默；失败、取消或结果未知不恢复旧选择。输入提交期间仍可重新静默，
迟到回执不再次撤销；非空 final 沿正常路径交付。确定性行为测试覆盖 ordinary、Side、
Goal、ExternalMessage 四条输入路径、失败结果和 Goal 换轮隔离。
固定 SDK/bundled App Server `0.163.0` 的生产 MCP → SessionService → ChannelApplication
→ Runtime 隔离探针通过普通 steer 与 ExternalMessage join：原生调用前旧标记已清，
延迟真实接纳回执期间再次静默成功，回执返回后新选择仍保留；空 final 正常 completed、
Thread idle、无结果投递。证据 `/tmp/netizen-suppression-simple-native.json` 对应当时
未提交工作区，使用临时 Codex 目录、loopback mock provider 与 FakeChannel。
本次原生探针未覆盖 Goal/Side 和失败路径；这些由本地行为测试覆盖。以上不代表真实
模型的静默决策改善或飞书客户端已验收，也未部署运行实例。
简化后的 `make check` 通过：3,785 项测试完成（15 项平台或可选环境条件跳过），编译、
依赖一致性及全部 SDK synthetic probes 通过；日志
`/tmp/netizen-suppression-simple-make-check.log`。

<a id="suppress-reply-20261028"></a>

## 2026-10-28：结果静默与来源登记（SDK 0.163.0）

`suppress_reply` 的工具 metadata 或结束交接调整时，运行对应 case（外部 70 秒 deadline）：
它验证生产 MCP 收到准确 Thread/物理 Turn，工具成功后 Turn 仍在运行，以及后续空模型
响应使原生正常 completed、Thread idle、SDK final 为空。2026-10-28 在固定 SDK/bundled
App Server `0.163.0` 上通过；证据保存在忽略目录
`docs/research/compatibility/suppress-reply-native-20261028.json`。该 case 使用回调 fixture 和
本地模拟模型，不代表真实模型决策或飞书客户端展示验收。完成表情、原卡折叠、无卡 @
及结果未投递的真实客户端验收仍须在明确授权的测试会话单独执行。

同日另以生产 MCP → SessionService → ChannelApplication → Runtime 验证普通任务与
Goal 的准确来源登记及工具成功，证据为忽略目录中的
`docs/research/compatibility/suppress-reply-readiness-20261028.json`。人工延迟真实启动响应
交接时，尚未登记的来源被明确拒绝，不误报静默成功；沿用既有归属边界，不新增等待或
准入机制。该证据同样使用本地模拟模型，不包含真实飞书投递。

<a id="side-source-20261026"></a>

## 2026-10-26：Side 协作来源（SDK 0.163.0）

2026-10-26 Side 来源扩展通过 `make check`：3,674 项测试、15 项条件跳过，编译、依赖及
SDK synthetic probes 通过。另用真实 bundled App Server `0.163.0`、临时
HOME/CODEX_HOME/数据库和 loopback mock provider 验证：实际 Runtime Side 经 MCP
`read_thread` 读取 Parent 时，per-call 来源为 Side 自身；既有边界消息中的 Parent/Side
完整 ID 均进入模型输入。实际 SessionService 发送经隔离原生 callback 及生产
Channel envelope 构造后，普通目标的模型输入和持久历史保留工具权限、真实 Side ID
与不可回信说明；外部未登记 ephemeral 和关闭后的 Side 来源被拒绝。
本地证据为 `docs/research/compatibility/netizen-side-session-source-2026-10-26.py`
及同名 `.json`，记录最终生产源码 hash。该探针未覆盖真实模型的发送审批交互或真实
飞书投递；这些不视为已验收，也未执行服务部署。当前执行沙箱的最小 `asyncio.to_thread`
程序会挂起，沙箱外同一程序正常，因此完整代码门禁在沙箱外完成，没有为环境阻塞修改业务代码。

<a id="scheduled-archive-20261022"></a>

## 2026-10-22：定时自动归档（SDK 0.163.0）

2026-10-22 原生隔离验证：`scripts/probe_scheduled_auto_archive.py` 在
`openai-codex` / bundled CLI `0.163.0` 下，通过生产 Scheduler、Channel、Runtime 和
管理服务执行 5 组场景：自动触发开启、立即运行开启、关闭选项、认领后修改选项，
以及首轮结果确认前已开始后续原生 Turn。开启场景均先确认结果送达，再准确归档一次；
后续 Turn 已运行时仍归档。原生归档目录与既有管理恢复路径验证通过，Run 保持
`sent` / `released`，来源当前会话和结果正文保持不变。

探针使用临时 SQLite、HOME/CODEX_HOME、原生 App Server、本地 mock Responses
provider 和内存飞书替身，未调用真实模型账号或飞书，也未改动用户 Codex 配置。
JSON 证据为 `/tmp/netizen-scheduled-auto-archive.json`。真实 MCP 模型参数生成、
飞书客户端布局/回调及目标平台服务启动迁移仍未验收；此证据不代表部署或发布。

同日最终 `make check` 通过：3,634 项测试、15 项条件跳过，Python 编译、依赖检查与
SDK synthetic probes 全部通过，包括 20 次 handle 快速完成、20 次公开 read 恢复、
40 次终态元数据 drain 和进程内 Skill roots 发现。本地 Admin DOM harness 使用
Node.js 24.18.1；wheel/sdist 隔离构建与资源加载包含在门禁中。相关回归覆盖冻结迁移、
三个配置入口、认领后策略保持、投递确认/失败/取消、Run 裁剪及归档异常。
新增控件触发的群聊分页容量回归已修复，保留原 200 元素上限和完整草稿；最终差异复核
未发现遗留问题。门禁日志为 `/tmp/netizen-auto-archive-make-check-final.log`。

<a id="scheduled-worktree-20261020"></a>

## 2026-10-20：定时工作区（SDK 0.163.0）

2026-10-20 隔离原生探针通过：在 SDK/bundled App Server `0.163.0`、临时 Git 仓库与
临时 HOME/CODEX_HOME 下，生产 Channel dispatch 和 Runtime 分别执行 `new`、`existing`
策略，再向各自 Thread 提交一条普通后续消息。共创建 2 个持久原生 Thread、1 个受管
detached Worktree，消耗 4 次 loopback mock Responses 请求。两种策略均确认首轮
native cwd、Binding/Run 的 exact 工作区引用和 completion cwd 一致；后续消息不传 cwd
override，仍复用原 Thread/目录且不新增工作区。初轮 barrier 已释放，结果在内存飞书
替身中确认投递；来源 active Binding 不变，未提交/未跟踪文件未复制，源目录改动保留。
原生 rollout 初建时有两次短暂空记录读取失败，既有有界观测恢复后全部断言通过。

以上为当时一次性临时探针的历史结果，外部进程超时 120 秒，实际耗时 1.637 秒。
该场景现已纳入 `scripts/probe_scheduled_tasks.py --phase worktree`，复验命令与可保留
证据要求见[代码门禁与按需实时兼容性验证](deployment.md#代码门禁与按需实时兼容性验证)；旧临时
脚本和输出不再作为持续复验入口，也不据此宣称新版本已通过。
当时真实模型与飞书调用均为零，用户 Codex 配置未修改，临时状态已清理。
此结果只证明上述原生目录与普通后续输入交接；真实飞书客户端布局/回调、真实 MCP
参数生成、目标平台启动迁移，以及本次工作区组合下的 Skill/Files 与完整生命周期
仍未验收，不能据此声明全部实时兼容性通过或已部署。

同日收敛验证通过 `make check`：3,531 项测试、15 项按条件跳过，Python 编译与依赖检查
通过；SDK Activity、20 次 handle 快速完成、20 次公开 read 恢复、40 次终态元数据 drain
及进程内 Skill roots 探针通过。相关回归覆盖冻结迁移、Service/MCP、Admin HTTP/JS、
卡片草稿与容量，以及真实临时 Git 下的删除竞态、回执原子性、Scheduler 超时/停机和
重启不重放。门禁日志为 `/tmp/netizen-workspace-simplify-make-check.log`。
门禁期间最后调整了 `/cron` 内部错误日志的类型输出及对应断言，随后该模块 18 项回归
全部通过，日志为 `/tmp/netizen-workspace-simplify-cron-logging.log`；其余 398 个源码、
脚本与测试文件摘要一致。此记录不代表发布或目标实例部署。

<a id="defaults-20261020"></a>

## 2026-10-20：默认工作区策略（schema v16，SDK 版本未另记）

2026-10-20 默认工作区策略扩展的上述真实飞书交互、目标平台启动迁移和交错操作仍为
待验收项。本地 `make check` 已通过：3453 项测试、15 项按既有平台或显式启用条件跳过；
包括默认工作区的临时 Git、并发/取消/删除竞态、Admin HTTP/JavaScript、飞书卡片、
v14 → v15 → v16 与 v15 → v16 迁移，以及 wheel/sdist 构建与隔离资源加载。
编译、依赖与 SDK synthetic 门禁均通过，最终 diff 和独立审查完成；未部署或发布，
这些本地结果不替代真实飞书交互或目标平台启动迁移验收。

<a id="worktree-20261019"></a>

## 2026-10-19：受管 Worktree 与 cwd（SDK 0.163.0）

2026-10-19 完成 filter 补齐、Agent 分支工具移除及恢复后 cwd 复核的本地 `make check` 已通过：3391 项测试，
15 项按既有平台或显式启用条件跳过；
包括临时 Git、Worktree 应用层/卡片/Agent 工具/Admin HTTP 与 JavaScript 行为、迁移和
删除并发保护测试。编译、依赖检查与 SDK 门禁均通过，包括 Activity 非消费观测、普通完成/
公开 read 恢复竞态、终态元数据排空及进程级 Skills 注册。最终 diff 检查通过；本轮未部署，
上述真实飞书客户端、目标平台启动迁移及 native 生命周期专项仍待验收。

复审修订收紧创建失败分类：无效 ref/分支、已占用或重名分支等能够证明尚未
尝试 `worktree add` 的失败只撤销精确预留登记，不操作目录；fetch 已更新的引用不回滚。
add 后 reset 未启动、取消/超时和未知结果仍保留登记。独立复审通过；Project 默认目录
缺失继续按不可用处理，不增加已有工作区绕过该检查的兜底。

后续复审按 CLI 策略补齐 Git filter 的命令局部覆盖，新增真实临时仓库验证目标
`includeIf.onbranch`/`includeIf.gitdir` 配置、最终删除内部检查与持久配置不变；未增加
新的隔离系统。Agent `list_worktree_branches` 已移除，仅保留列表与创建工具；Admin
和飞书内部选项查询保留。cwd 交接复核已按下述边界修复；正式 `/new` 的布局调整先发送
了禁用提交的预览卡供确认，随后已在源码中落地分组、并排控件和短灰说明。模型配置、
消息上下文及任务反馈继续复用原控件生成逻辑，选项文本/值、默认值、必填属性及提交
字段不变；与改动前控件的 128 组合独立对比全部一致，340 项相关回归通过。
布局落地后的 `make check` 全部通过：3394 项测试、15 项按既有条件跳过，编译、依赖
一致性和 SDK synthetic probes 均通过；独立呈现复审未发现控件语义变化。
未部署；预览发送成功及本地控件测试不代替真实飞书嵌套表单提交和移动端布局验收。

2026-10-19 的独立原生 cwd 探针已通过。使用 `openai-codex==0.163.0` bundled App Server、
隔离临时原生状态和 loopback mock Responses，验证公开 `Thread.cwd` 的 `AbsolutePathBuf`
返回形态、`thread_start(cwd=...)`、新 App Server 的冷读取，以及不传 cwd 的 resume 保留
原目录。扩展后的同一探针还使用生产 `CodexRuntime` 验证：只读准备取得 A 后，另一
客户端 cold resume 到 B、完成 Turn 并退出；准备于 A 的原输入在恢复后直接采用 B 完成，
仅发送一次、不覆盖 B，Binding 目录同步为 B，`TurnOutcome.cwd=B`，后续准入仍开放。
统一目录所有权后的复验改为已有 Thread 提交完全省略调用方 cwd，仍满足上述断言；
先前的只读目录查询仅作为交接前的观察证据，不参与 Runtime 选择执行目录。
本次共 3 次 loopback mock 请求、0 次真实模型请求，临时状态已清理；未调用飞书或访问真实
业务 Project。该结果不证明完整 Worktree 交互、缺失目录或 native 删除链路通过。
复验用外部 deadline（macOS 对应 `gtimeout`）：

```bash
timeout 90s .venv/bin/python scripts/probe_worktree_cwd.py --output /tmp/worktree-cwd.json
```

持续复验需将输出移入可保留位置并关联提交，具体要求见
[代码门禁与按需实时兼容性验证](deployment.md#代码门禁与按需实时兼容性验证)。

同日额外原生探针确认 idle/unloaded Thread 的跨客户端 cwd 交接窗口：只读取得 A 后，
另一客户端 cold resume 到 B、完成 Turn 并退出，前者不传 cwd 恢复时实际使用 B。
已有订阅的 loaded Thread 忽略不同 cwd 的 resume override；另一 Server 同时恢复则
被 active-writer 保护拒绝。共 3 次 loopback mock 请求、0 次真实模型请求，临时状态已清理。
这不是运行中任意改目录，也不是完整飞书端到端复现。固定 0.163.0 CLI 从 resume 响应
取得 cwd，普通 `turn/start` 显式传 cwd。源码复核及用户确认后，Netizen 已移除之前
“准备目录与恢复目录不同即拒绝输入”的额外策略，改为采用本次恢复后的实际 cwd 继续。
固定 Python 高层 SDK 仅返回 Thread handle，故通过恢复后的公开 read 取得实际 cwd，
不把准备前旧 cwd 写回原生配置。未知、无效或超时只拒绝本条未发送输入，不推进
Context Boundary、不自动重试。已确认目录复用 exact-native 关联同步和 Worktree 删除
准入；成功恢复但未发送的订阅继续按 idle 策略释放。普通输入、ExternalMessage 和 Goal
初始输入均采用实际 cwd，Goal 冷恢复仍沿用该行为；活动 Turn/Goal 保留 exact-Turn steer。

飞书文本、引用和图片准备不依赖 cwd，未为此重做材料准备。已有空闲 Thread 的显式
Skill 输入先恢复并读取实际 cwd，再锁外查找 Skill，提交前复核原 admission、原生
Thread 身份与订阅对象/代次并复用同一 handle，不二次恢复；活动输入用本轮已接纳的
目录，lazy 输入仍先解析 Skill 再创建 Thread。相关行为测试覆盖目录变化、并发输入、
订阅释放、发现期间其他操作物化 Thread、发现失败/取消及读取失败边界；上述原生
探针覆盖目录交接与单次提交，不宣称验证了原生
Skill 加载或完整飞书端到端链路。

本次 cwd 修正后的 `make check` 全部通过：3402 项测试、15 项按既有条件跳过，编译、
依赖一致性及 SDK synthetic probes 均通过；其中目录行为回归为 37 项。独立复审发现的
“Skill 发现期间失败 Goal 创建物化 Thread”竞态已由局部身份校验修复并补充回归，复审
无遗留阻塞。未部署，真实飞书端到端交互与模型执行时的 Skill 加载不在本次验证范围。

随后将执行目录统一收敛到 Runtime：Channel 只提供 lazy 创建意图；已有 Thread 恢复后
读实际目录，复用活动 Thread 同时复用其已确认目录。该规则覆盖 ExternalMessage 原生
返回新 Turn 的窗口，不再把调用方旧目录记入新轮的 Skill/Files 上下文；普通、定时和
Goal 提交均移除目标目录预读，来源继承和状态查询保留。没有新增缓存、持久化字段或
扫描。调用次数回归确认：普通 lazy 创建不新增 read；已有空闲 Thread（含 Skill）为
一次 resume 和一次 cwd read；活动 steer、ExternalMessage join/next Turn 不新增
read/resume，Goal 保留既有 readiness read。这是请求次数证据，不是生产延迟压测。

本轮原生复验通过 cwd 交接及 `probe_session_collaboration.py` 的 `runtime_join`、
`runtime_terminal_race` 两场景：生产 Runtime 分别保持同轮加入和新轮交接，结果各消费
一次。使用 SDK/App Server 0.163.0、临时原生状态和 loopback mock Responses，三个专项
合计 7 次 mock 请求、0 次真实模型请求，不访问业务 Project 或发送飞书消息。

统一目录来源后的完整 `make check` 退出 0：3409 项测试、15 项按既有平台/可选环境
条件跳过；编译、依赖、Activity、20 次即时完成、20 次公开 read 恢复、40 次终态元数据
排空及进程级 Skills 发现门禁全部通过。两处旧定时探针的 Channel 预读断言已改为
验证不预读，同时保留 Runtime 原生 read/目录同步、exact steer 与清理证据。提交前目录
事实缺失复用明确未接受错误，不能误报投递未知。独立复验确认旧目录混入新 Turn 的问题
关闭，最终 diff 检查通过；本轮未部署，真实飞书端到端、真实模型 Skill 加载及生产延迟
压测未执行。

<a id="history-goal-20261017"></a>

## 2026-10-17 至 2026-10-18：历史分页与 Goal 分离（SDK 0.163.0）

2026-10-17 的规模优化与 Goal 分离（ADR 0083/0084）在 SDK/bundled App Server
`0.163.0` 上经六路独立复核后通过本机 `make check`：3,210 项测试、15 项条件跳过，编译、依赖一致性、
Activity/20/20/40 SDK synthetic 与只读 Skill roots 门禁通过。生产历史适配的隔离原生
探针覆盖 paginated/legacy 各三轮、opaque 游标续页、非法游标后复用和归档读取；legacy
items 不支持时只读最新一轮 Full。生产 Goal Runtime/Adapter 的本地 mock provider
探针验证 pause/clear 不 interrupt、同流 resume、清除后的结果交付、stop 精确中断，
以及两轮自动续跑与 complete 自动 clear。既有 interrupted continuation 观测缺口的
局部隔离和 idle 后恢复复验通过，不表示缺口已修复。飞书卡片容量与回调为本地行为测试，
不替代真实目标应用客户端验收；本轮未部署、未验证真实模型自主工具调用或十万会话压测。

2026-10-18 按产品范围移除上一轮为旧混合 Goal 测试卡增加的专用兼容处理，不承诺旧卡
交互保留；当前静态管理卡与普通回复仍按 ADR 0084 验收。删除后完整 `make check`
通过：3,209 项测试、15 项条件跳过，编译、依赖及 SDK 门禁通过。

<a id="collaboration-pagination-20261016"></a>

## 2026-10-16：协作预览与分页（SDK 0.163.0）

2026-10-16 预览适配与 Admin 分页优化：在 `openai-codex` / bundled CLI `0.163.0`
重新通过 `runtime_external_name` 与 `runtime_external_title` 隔离场景。生产出站格式为
`codex_tui` + CLI 委派 envelope，特殊字符正文成为原生 preview，历史仍为工具消息且保留
真实来源，DB-only 目录可发现。后续增强 title 场景验证生产 `thread_catalog_page` 的
创建／更新时间、升／降序、原生时间投影及空归档页；证据
`/tmp/netizen-preview-catalog-page-evidence.json`。所有这些验证只使用本地 mock provider，
未调用真实模型账号、飞书或部署生产服务。Admin 浏览器交互由 Node DOM harness 验证，
未把该测试写作真实浏览器视觉或飞书客户端验收。

本轮四路审查及修复后 `make check` 通过：3,180 项测试完成（15 项条件跳过），Python 编译、依赖
一致性、Activity、20 immediate / 20 history / 40 terminal drains 与只读 Skill roots 门禁
均通过。日志 `/tmp/netizen-native-pagination-review-make-check-final.log`。分页与前端独立复核
发现的稀疏尾部逐条请求、恢复单边日期无法确认问题均已修正；静态多页无漏项及
19 个匹配 + 10,000 个外部候选只需 101 次分页请求由行为测试覆盖，不作为生产压测数据。
后续审查修正真实 SDK／传输读取失败的错误归类，保留取消和超时语义，并清理 Admin
成功响应中已不可达的目录失败标志及提示条；Scope 降级路径保持。另有 1,000 组静态
随机分页验证通过，证据 `/tmp/netizen-review-admin-native-pagination-fuzz.out`，
不代表动态目录快照一致性或生产规模验收。

<a id="restore-20261016"></a>

## 2026-10-16：归档恢复（SDK 0.162.1 与 0.163.0）

2026-10-16，SDK/App Server `0.162.1` 的归档恢复专项通过：生产 Runtime 与临时 SQLite
连接真实 bundled App Server，验证 archived、loaded idle、cold already-unarchived、
running rejoin、missing 和第二 App Server writer 占用六个场景。每次恢复仅调用一次
unarchive 和一次 resume，没有 list/read 预检；running 保留原执行 handle，拒绝保留
Binding/current 且释放本次 lifecycle。两次本地 mock Responses 请求后，原生历史仍
严格只有两条 Turn。该证据不包含真实模型、Goal 自动续跑、飞书回调至卡片可见耗时或部署。
本轮 `make check` 通过（3141 项测试，15 项平台或可选环境条件跳过；编译、依赖及 SDK
探针通过），包含收紧为具体异常类型的恢复保护分支与旧输入准入回归。后者覆盖
idle/running × 恢复成功/明确拒绝四种情况，不启动新 Turn 或误 steer。
独立代码 review 未发现阻断问题；未部署。

同日同步已升级 SDK/App Server `0.163.0` 的 main 后，重新核对 unarchive 的移动前拒绝、
resume 的精确错误与 running Goal 通知路径，六个隔离原生恢复场景再次通过；没有增加
兼容分支，也没有改变 main 已有的 Goal 观测失败恢复规则。上述真实 Goal 与飞书验证边界仍保留。

<a id="sdk-0163-20261016"></a>

## 2026-10-16：SDK 0.163.0 升级

2026-10-16 的 `0.162.1` → `0.163.0` 升级完成精确 Python 包与 bundled App Server
源码复核。高层 API 和私有消费代码未变，生成模型/通知注册表改变；现有窄适配没有
公开等价替代。cleanup 与 Activity 保留原门禁，使用新版源码指纹
`ba6d264e10f7612772473a24a64534e286a8c2badd584675aa1daeccd6086ce9`。

Goal 局部恢复修正后的最终本机 `make check` 通过，执行 3,144 项测试（16 项条件跳过），包括 wheel/sdist
隔离验证、编译、依赖一致性、SDK Activity/20/20/40 synthetic 与只读 Skill roots。
本次本机使用 CPython 3.12、Node.js 24；不代替 Node.js 22 和完整 Python 版本的 CI 矩阵。
Linux 临时候选环境全部 17 个真实账号原生 phase 通过，包括 exact steer、压缩、并发、
Linux marker 中断/清理/续聊、生命周期、Side、订阅释放、配置、Goal 与 sandbox。
同一账号的只读额度、持久 fork、后台命名、child Files、Skills 七阶段、调度五阶段及
Project 删除两场景专项均通过；用户配置不变断言和自有资源清理按各探针执行。

本地 mock provider 加真实 App Server 的 28 个功能场景通过，覆盖普通会话协作、
阶段性答案、大输出历史、local/remote-v2 压缩及错误、卸载与慢 MCP、incomplete response
和冷恢复权限、动态工具与 Code Mode 取消；另有 1 个 Goal 观测失配防护场景通过。
incomplete 验证经生产 Runtime 保留稳定片段、失败后释放槽位并在同 Thread 继续；
context 使用 last 而非累计 token；非零重试与普通 Turn 的 interrupted 自动续请求另有
有界场景。冷恢复跨三个进程实际验证 read-only 读取成功、
写入拒绝和显式 workspace-write 覆盖。没有覆盖网络 grants、managed policy、
Responses WebSocket 和 Code Mode 前台 cell 的独立 wait。独立输出回收探针还确认，
自有子进程仍持有 stdout/stderr pipe 时，生产 non-login 路径和显式开启的 local
exec-server 路径均有界结束并保留输出尾部；远端 Environment 的最终 poll 未独立实测。

Goal 专项发现两版 SDK 都在第一次 interrupted 后收流，新版原生连续无活动保护会
继续到第三次 interrupted 后 blocked，使旧 Runtime 误认过早 final。本次只加强公开
history 身份核验：已有后继 Turn 时只保留当前 Goal unknown，不关闭实例 admission，
禁止旧 Result/Files 和 clear。下一条消息或 `/goal` 查询在原 consumer 交接结束后做
有界只读重检；确认 Goal 非 active、exact Thread idle 且无运行中 Turn 后释放本地占用，
允许同一会话继续对话。原生 mutation 结果未知不走观察恢复路径，旧 steer admission
不能被改投为新 Turn。修正后的真实 App Server 探针确认另一 Binding 在故障 Goal 仍运行
时完成，以及原生 blocked/idle 后原 Thread 新普通 Turn 完成，原生 Goal 保留且零自动
clear；Goal 行为子集 62 项通过。它不修复完整续流，不保证此路径的最终 blocked 交付。
普通 completed 自动换轮的 partial 保留、exact 最终结果和自动 clear 已用生产
Runtime 复验通过；真实账号显式暂停/恢复/steer/最终回复门禁也通过。

以上不包含真实飞书投递、客户端操作或 Linux/macOS 服务部署/ready 验收；本次没有发布、
更新或重启已有实例。

<a id="collaboration-20261015"></a>

## 2026-10-15：协作五场景与 Goal 预检（SDK 0.162.1）

2026-10-15 在 `openai-codex` / bundled CLI `0.162.1` 已通过五个场景：

- ExternalMessage 首次创建、空闲开始新轮以及运行中加入原轮；模型输入保持
  `function_call_output`，持久历史和冷读保持 `functionCallOutput`，没有转成 userMessage。
- 生产 `submit_external` 的 JOINED 只有一个结果消费者，保留原 owner/origin；原生旧轮
  已完成但本地观察延迟时，返回新 Turn，新旧两轮各交付一次且归属正确。
- 生产 SessionMcpRunner 的六工具可被真实 App Server 提供给本地模拟模型；一次
  `list_threads` 调用收到正确的原生来源 Thread metadata，结果进入后续模型请求和历史。
  生产 `approve`/`prompt`/145 秒配置可解析，另一个用户 MCP 仍可发现，配置文件字节不变。

同日最终 `make check` 通过 3,139 项测试（15 项条件跳过），含 wheel/sdist 隔离资源、
Python 编译、依赖一致性、SDK Activity/20/20/40 probes 与只读 Skill roots 检查。
Admin JavaScript 行为测试本机使用 Node.js 24.18.1；CI 规定的 Node.js 22 未在本机复跑。

同日 Goal 预检回执修正后重新执行 `make check`：3,141 项测试通过（15 项条件跳过），
编译、依赖与上述 SDK 检查全部通过。新增回归确认 Goal 只读预检失败/状态未知时零原生
投递并返回明确未接受，真正副作用未知仍保持未知回执；同时补齐配置示例中两组默认
范围的既有测试断言。此修正保留 SDK 对 active Goal 来信的限制，没有修改原生投递方式。

MCP 场景回调是记录参数的 fixture，不代表真实模型会自主正确编排六工具。该证据也不
覆盖 mutation 的实际审批交互、等待完整 145 秒、真实飞书单聊/群/话题的新建与卡片
客户端效果；这些在授权的测试聊天按创建→读取→发送→等待及人工干预路径验收。
本次工作区实现未部署到实例，不将本地检查写作线上验收通过。

<a id="collaboration-naming-20261015"></a>

## 2026-10-15：协作命名及失败回执复核（SDK 0.162.1）

2026-10-15 后续优化追加通过 `runtime_external_name` / `runtime_external_title` 两场景：
真实生产 Runtime 的 ExternalMessage 首条输入被 ephemeral 命名 fork 继承，主任务先于
命名完成、父历史无命名污染；指定标题时真实后台 set_name 被 barrier 延迟，任务接受
与完成仍先返回，零 fork、仅一个任务模型请求。两场景都经生产 SessionService 读取
真实历史，默认 `codexDelegation` 保留来源 Thread ID 与正文，原始 output 仍隐藏。
首次自动命名 probe 暴露 SDK output RootModel 与字符串匹配差异，修正并加入官方类型
回归后复验通过；未改 SDK。证据为 `/tmp/netizen-session-external-name-evidence.json`
和 `/tmp/netizen-session-external-title-evidence.json`，均未调用真实模型账号或飞书。

同轮另执行既有 `probe_thread_naming.py --runtime-only`，真实模型的普通输入命名、
首轮继承、无工具生成、订阅收尾、父会话继续对话与归档通过。该验证限生产命名路径的
局部修改，没有重跑默认模式的独立中断专项。新建协作话题的真实飞书客户端、真实模型
自主编排及 create/send 审批交互仍需按上述范围验收，不能用这两项 native probe 替代。

本轮优化最终 `make check` 退出 0：运行 3,157 项测试，15 项条件跳过，包含包构建与
隔离资源验证；编译、依赖、SDK Activity / 20 immediate / 20 history / 40 terminal
drains 和只读 Skill roots 全部通过。日志 `/tmp/netizen-session-optimization-make-check.log`。
独立正确性复核未发现新的功能缺陷或过度实现，`git diff --check` 通过；本机 Node.js
仍为 24.18.1，未替代 CI 的 Node.js 22 门禁。本轮未部署或发送真实飞书消息。

同日独立复核发现失败卡更新被工具 deadline 取消时仍会覆盖原错误回执，已在共用
失败展示路径按成功回执方式修复。确定未提交保持 false，投递未知保持 null，错误及
目标信息不丢失。新增 MCP→Service→Application 回归在卡片更新开始后确定性触发
期限，两种结果均通过；原 reviewer 复核确认问题关闭。修正后 `make check` 退出 0，
3,158 项测试通过（15 项条件跳过），编译、依赖及上述 SDK probes 全部通过；日志
`/tmp/netizen-session-failure-receipt-make-check.log`。本次仅修改失败展示保护，未重复
真实模型/飞书验收，未部署；前述客户端及 CI Node.js 22 验证边界不变。

<a id="admin-login-20261011"></a>

## 2026-10-11：Admin 登录（Python 3.11 / Selector SDK 1.2.21）

本轮本地证据：独立 Python 3.11 环境按锁定依赖运行 `make check`，全部门禁通过。
其中 review 修复后的 unittest 共 2,939 项、15 项
按平台或原生包管理器探针条件跳过，其余通过；包含 wheel/sdist 构建与隔离资源加载。
编译、依赖一致性、SDK 合成探针及一次性未认证 App Server 的只读 Skills 发现均通过，
不包含模型执行或真实飞书授权。前端行为测试使用 Node 24.18.1，不冒充 CI 的 Node 22
验收；隔离 Chromium 在 390/1365 像素宽度验证登录、取消、管理员增删、最后一人保护、
会话失效和迟到响应，使用合成后端及 Selector 数据，无横向溢出或页面异常。本人资料
仅整页初始化请求一次，切换管理 tab/刷新名单不重复查询。该浏览器环境缺中文字体，
中文内容通过 DOM 校验，截图不作为中文字体效果或真实飞书搜索证据。

本轮实现 review 后另用固定官方 Selector SDK 1.2.21 与合成鉴权响应、候选数据复验：
在 390/1365 像素宽度下，页内选人区域的 body 下拉候选可点击；鉴权请求网络失败导致
SDK Promise 挂起时显示整页重新加载入口，关闭重开不重复鉴权，整页重载后恢复。
行为测试补充 SDK 页外候选、超时和旧请求迟到，以及登录终态容量释放、明确 UAT
过期码穿透至单次刷新。此项使用真实 SDK 代码，但所有网络请求均被合成拦截，不是
真实飞书授权、用户搜索或客户端验收；前述 live 缺口不因此关闭。

<a id="sdk-01621-20261010"></a>

## 2026-10-10：SDK 0.162.1 升级与阶段性答案

2026-10-10 的 `0.161.0` → `0.162.1` 升级在本机 macOS arm64 完成新版源码指纹复核：
`7c50badc428374e91233516562fb971b1727b00157b93e5771221a672a20d215`。
两个 pinned adapter 保留精确版本/指纹与各自的所有权门禁；其他窄适配器没有新增公开
高层替代，继续保留原契约。阶段性答案按 ADR 0079 接入，归因字段不引起 Files 改造，
附件归属查询和 Goal origin 保持设计中记录的延期条件。

视觉修订及阶段性投递回执修复后的最终 `make check` 通过 2,841 项测试、16 项条件跳过，包含 wheel/sdist
隔离验证、编译、依赖一致性、SDK Activity/20/20/40 synthetic 与只读 Skill roots。
此前另测通过的 13 项探针单测（原生/Skill 8 项、飞书阶段性答案 5 项）已纳入此次全量。
定时阶段性富文本复用既有精确投递校验；单条/多分段归属、未知结果不重发及校验异常/
超时的三项新增回归通过，阶段性消息不写入完成回执。
完整命名、持久 fork、只读账号额度、子任务 Files、Scheduler 五阶段及 Project 删除
两场景专项通过；这些原生/Runtime 结果不代表飞书客户端交互已经验收。

全部 17 个真实账号原生 phase 通过，覆盖模型/速度、普通执行、steer、观察、压缩、并发、
中断、Skills、生命周期、Side、订阅释放、配置、Goal 与 sandbox。新确定性原生专项的
12 个场景通过，确认 partial 可继续工具执行或直接 completed；终态摘要不会包含全部
partial，须保留 item 观察/权威完整读取。命令与 MCP 大结果持久化截断在 64 KiB 内，
同轮 72 KiB 的 partial/final 均完整；手动压缩成功与 HTTP 错误后的上下文/工具目录、
压缩准备失败上报且不发起模型请求、slow unload closing 拒绝恢复及最终回收、慢
required MCP 断连回收均有实际证据。

新增 Goal 两轮专项使用真实 App Server、生产 Goal Tap 和 CodexRuntime：一次 start 后
自动生成两轮，每轮一条 partial，第二轮真正调用 `update_goal` complete。最终回答前的
运行快照和最终 Outcome 均保留两片段及 exact 轮次身份；final 只取第二轮，四证明完成
后 clear/get absent。没有伪造 SDK 通知或 Runtime 投影，也不包含飞书投递。

Skill 实际加载的 new、cold、side、fork、child、goal、isolation 七阶段通过，均核对
原生工具读取新随机资源、答案标记和用户配置不变；隔离阶段确认同 HOME/cwd/名称的
两个 App Server 使用各自额外根。首轮 Side 因探针误要求可选 `forked_from_id` 必须存在
而失败；对照生成模型和现有生产契约，修正为允许缺省、拒绝错误实值后，剩余五阶段
复验通过，保留首次失败。没有修改生产 Side 身份守卫。Goal 阶段仅覆盖显式暂停和手动
恢复后的新物理 Turn；自动 rollover 的阶段性答案保留由上述独立原生专项证明。

经用户授权，真实测试群已发送 6 张阶段性卡片与 5 条富文本，共 11 条消息、26 次卡片
更新，均收到成功 API ACK 并核对 exact 消息/聊天身份。原 Files 卡更新后回读的页码标题
为第 2/2 页，但正文 API 返回明确兼容占位，阶段性正文无法从该接口核验；未重发卡片。
用户确认初版能显示，但后续截图指出阶段性与最终正文视觉分界不足；已按反馈调整，
对原六张卡追加六次更新，API ACK 和 exact 身份回读全部通过，没有新增消息。新版尚无
用户视觉复核；原反馈未说明所用客户端，不能推断桌面和移动端均验收。
样例复用生产渲染器/Presenter 和真实传输，运行状态为模拟投影；
原生 Goal 两轮专项是独立证据，不合并宣称完整 native→飞书端到端通过。真实点击回调
尚未验收。

本次目录仅提供 Standard/Fast，Ultra Fast 真实执行未验收。新专项没有覆盖压缩
auto/mid-turn 和压缩与 Goal snapshot/rollover 的组合矩阵、slow unload 与 revert 替换 runtime
的独立竞态；Skill 探针也不替代 Lark Skill 实际调用、同名/禁用/歧义规则或平台启动验收。
上述证据不替代未覆盖客户端组合、真实点击与目标平台服务验收；本次没有发布、
更新或重启已有实例。

<a id="fork-ui-20261009"></a>

## 2026-10-09 至 2026-10-10：Cron/Fork 目标控件（SDK 版本未另记）

2026-10-09，用户在真实飞书话题的独立 Card 2.0 预览卡中输入“研发”，截图确认下拉
菜单将候选过滤为“研发一组”和“研发二组”。该证据仅确认该客户端的本地关键词过滤，
不证明其他客户端、编辑名称保留、真实群头像或新版 `/fork` 创建链路已完成实机验收。

2026-10-09 增加 Cron/Fork 共享目标输入、手填单聊 ID 与 Admin/卡片 ID 帮助链接。
此次实现还需真实客户端验证：单选仅决定有效输入、两个字段都有值时不串目标、群列表
失败可填 ID、校验失败保留草稿且同值可重试，以及固定 Agent 会话准确只读位置。
群到单聊 Fork 须验证后续输入为 current-only 而原生历史仍继承；不能用 Cron/Admin 的
单聊证据代替 Fork 链路。官方指南和本地合成测试不等于上述新交互已经实机通过。
本轮未部署；共享查找/分页与草稿恢复优化通过 `make check`（2754 项测试，15 项条件跳过，
编译、依赖与 SDK synthetic 门禁通过）。已在飞书话题发送使用真实群名和头像的共享组件
预览卡，发送 API 确认成功；查找、翻页按钮禁用，没有创建任务或分支。该证据仅为预览卡
投递，客户端展示确认及真实查询/分页/提交回调仍待验收。

同日进一步将群选择改为完整有界结果快照，Files/Cron/Fork 共享页码选择控件与切片。
本地 `make check` 通过（2775 项测试，15 项条件跳过；编译、依赖与 SDK synthetic 门禁通过）；
收尾头像整批准备和空白群名显示归一化后，相关单元与 Cron/Fork 集成回归、编译及 diff
检查再次通过。独立代码复审未发现阻塞问题。本轮仍未部署；此前禁用按钮的预览卡不能
证明新增页码跳转回调已实机通过，须按上表验证客户端首/末/中页与草稿保持。

2026-10-10 将 Cron/Fork 目标统一为“当前聊天、选择群聊、填写聊天 ID”三选一，
新建默认当前位置，空群/空 ID 不回退；Admin 提示复用既有目标组件，未变更执行或登录机制。
本轮 `make check` 通过（2782 项测试，15 项平台或可选环境条件跳过；包含 Admin
JavaScript 行为测试、编译、依赖与 SDK synthetic 门禁），独立代码复审未发现阻塞问题。
随后在当前飞书话题发送复用生产共享控件的三选一预览卡，含三个真实群名与头像；发送及
话题归属已确认，用户反馈接受当前效果。查找和提交按钮禁用，未创建任务或分支，未部署；
该证据仅覆盖预览展示，真实查找、分页、未触碰的可选字段及提交重试仍待验收。

<a id="sdk-0161-20261008"></a>

## 2026-10-08 至 2026-10-09：SDK 0.161.0 升级

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

<a id="group-picker-20261008"></a>

## 2026-10-08：Admin 群选择器（平台接口 v1/v2）

2026-10-08 在显式选定的当前实例使用同一机器人凭据完成只读验证：群列表返回 8 项，
选中群的成员关系及详情校验通过，按群名搜索返回包含该群的结果。没有发送消息、加入群、
修改应用权限或持久化群目录。实测 v2 搜索的 `chat_mode` 为 `DEFAULT`，与文档示例的
`group/topic` 不同；搜索结果保留未知子类型，选中后仍通过成员检查和 v1 详情确认群聊。

本地 Chromium 配合模拟管理接口验证了关键词分页、单聊手填提示、跨页草稿恢复、从
Sessions 创建精确 Binding 计划及保留筛选状态，并检查桌面和移动宽度下的抽屉。
这不是部署后 Admin 认证链路或真实计划创建的端到端通过记录。当前实例群数不足以触发
真实接口第二页；分页、权限失败、机器人退群和竞态仅有自动化替身覆盖，未在真实群中
制造这些状态。不得由这项只读验证推导出消息投递、计划执行或部署验收已通过。

<a id="usage-20261008"></a>

## 2026-10-08：账号额度（SDK 0.160.0 / 候选 0.161.0）

历史基线：2026-10-08 新增按需账号额度读取（ADR 0077），当时 SDK/CLI 保持 `0.160.0`。`make check`
通过 2,565 项测试（16 项按条件跳过），包括 wheel/sdist 与隔离资源、SDK synthetic 门禁。
额度适配专属的 13 项测试同时通过 `0.160.0` 和候选 `0.161.0`，含真实 SDK client
往返、legacy 严格校验及超时/取消后的连接复用。`0.160.0` bundled App Server 的
只读 live probe 已验证已登录账号成功返回额度窗口，临时未登录环境返回认证错误；
未创建 Thread 或请求模型。真实飞书 `/usage` 展示与其他部署账号尚未验收，以上不表示
已经升级到 `0.161.0` 或完成该候选的全部升级门禁。

<a id="sdk-0160-20261005"></a>

## 2026-10-04 至 2026-10-05：SDK 0.160.0 升级

2026-10-05 SDK/CLI `0.160.0` 通过当前代码的 `make check`（2,505 项测试，16 项按条件
跳过，含包构建及 SDK synthetic 门禁）。10 月 4 日同版本候选已通过全部 17 个原生
phase、完整 Thread naming、额外 Skill roots 实际执行和新增行为专项；对应 SDK
适配器、Runtime 与探针未变。上述本机证据不代表真实飞书或目标主机部署已验收。

与 `0.159.2` 相比，Python SDK 源码及指纹未变，没有新增可替代现有适配器的公开
高层 API。本次仅同步精确版本声明，保留现有适配器与恢复措施，无业务或数据迁移。
原生显式 `model_catalog_url` 失败或对应 discovery 未开启时不再回退 bundled 目录；
Netizen 沿用空目录拒绝，使用自定义目录的部署须验证其 endpoint 和 discovery 配置。

<a id="sdk-01592-20260930"></a>

## 2026-09-30：SDK 0.159.2 与零 Turn 持久性

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

<a id="installer-20260928"></a>

## 2026-09-28：旧 release 安装事务（schema v14）

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

<a id="side-questions-20260924"></a>

## 2026-09-24：Binding/Side 问答（SDK 0.156.1）

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

<a id="naming-20260924"></a>

## 2026-09-24：后台命名 output_schema（SDK 0.156.1）

2026-09-24 SDK/CLI `0.156.1` 的后台命名改用公开 `output_schema` 后，
`probe_thread_naming.py --runtime-only` 通过：生产入口生成有效标题且无工具调用，
临时 fork 在四种目录视图中均不可见、取消订阅完成，父会话历史与后续对话不受污染。
本次局部验证未重跑独立中断场景；当时的模型介绍折叠面板未做真实飞书客户端展示验收，
该面板已于 2026-10-22 按产品决定移除。

<a id="scheduler-binding-20260923"></a>

## 2026-09-23：定时 Binding/MCP（SDK 0.155.1，gpt-5.6-sol）

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

<a id="sdk-01561-20260923"></a>

## 2026-09-23：SDK 0.156.1 升级与原生问题

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

<a id="scheduler-phases-20260922"></a>

## 2026-09-22：定时四阶段（SDK 0.155.1；飞书基线 0.147.0）

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

<a id="sdk-01551-20260922"></a>

## 2026-09-22：SDK 0.155.1 升级与 Goal steer

2026-09-22 SDK/CLI `0.155.1` 已通过 `make check`，以及 `probe_python_sdk.py` 的完整
17 个原生 phase、Thread naming 和 Project delete probes。Goal phase 额外验证当前
物理 Turn 可追加消息、resume 后旧 expected Turn 明确拒绝、新 Turn 的追加改变最终
回复，同时保留原 Goal 文件 diff 与同 Thread 后续对话。两个 pinned adapter 的 SDK
源码 fingerprint 与前一验证版本相同，仅更新 exact version；其他能力仍按 shape 与行为门禁验收。
相关本地回归覆盖追加回执未知时保留已确认的最终回答、模型自行暂停时不误报后台终端清理，
以及 Goal 清单和分页。原生 probes 使用本机登录账号和隔离临时 Git cwd；本次证据不覆盖
飞书客户端投递、目标主机安装或正式发布，也没有部署或重启 Netizen 服务。

<a id="scheduler-manual-20260921"></a>

## 2026-09-21：定时手动运行（SDK 0.154.0，gpt-5.6-sol）

2026-09-21 以 `gpt-5.6-sol`、SDK/CLI `0.154.0` 通过 `--phase manual`：一次自然语言
请求只产生一个手动 Run，计划指令/配置、暂停意图与时间游标保持不变，独立持久 Thread
的首轮完成、屏障释放及测试传输回执均确认。探针所属原生资源已清理，用户 MCP 与
Project trust 配置不变；本次没有真实飞书投递、客户端点击或目标主机安装回滚验收。

尚未完整覆盖客户端全部点击路径、移动端布局、原生权限和网络故障组合，以及目标主机
真实失败回滚。当前证据未直接观察 deferred search；固定版本的跨会话 MCP catalog cache
仅适用于 stdio，不给本 HTTP adapter 添加相应兼容层。`0.154.0` 支持普通会话压缩后
续聊，但压缩后的 MCP 管理组合尚未覆盖，不标为通过。

<a id="sdk-0154-baseline"></a>

## 日期未单列：SDK 0.154.0 能力基线

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

<a id="config-0154-baseline"></a>

## 日期未单列：SDK 0.154.0 Project config 分类

- SDK/CLI `0.154.0` 的 Project config 探针分类为 `hot-reloaded`：同进程及重启后均读取
  新配置，探针不修改用户全局配置。未来版本的 `restart-required` 仍可接受，但须更新
  兼容性结论。
