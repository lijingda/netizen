# 参与 Netizen 开发

感谢你帮助改进 Netizen。产品介绍与首次使用见 [README](../README.md)，完整命令与使用
行为见[用户手册](../skills/netizen-user-guide/references/user-guide.md)。修改代码前阅读
[工程概览](design.md#工程概览)，再按 [AGENTS.md](../AGENTS.md) 直接进入相关契约和 ADR；
精确术语见 [CONTEXT.md](../CONTEXT.md)。

## 准备开发环境

源码开发需要 Python 3.11-3.14 和 `venv`。CI 使用标准 CPython 构建，未单独覆盖
free-threaded 变体。macOS 与 Linux 都可以运行源码、本地门禁及相同的安装/服务命令；
正式服务分别使用 macOS LaunchAgent 与 Linux systemd user manager。生产实现只有
Python 包，不存在 Node.js/TypeScript 运行时、前端构建或 fallback。

在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'setuptools==80.9.0'
.venv/bin/python -m pip install -c requirements.lock -e .
.venv/bin/python -m pip install -c requirements.lock -r requirements-dev.lock
node --version
make check
```

`requirements-dev.lock` 精确固定门禁使用的 ruff 与 mypy；它们只用于开发和 CI，不是运行
依赖，也不进入发行包。

Admin JavaScript 行为测试使用 Node.js 22，请在完整开发门禁前确认版本。Node.js 仅用于
开发与 CI 测试，不是 Python 包生产运行的前置依赖。本地缺少 Node.js 时该项
测试会明确跳过；此时 `make check` 通过不代表 JavaScript 测试已完成。CI 安装 Node.js
并强制执行该项测试。

`make check` 先执行 `make lint`（ruff：语法、pyflakes 与未持有引用的 asyncio 任务）和
`make typecheck`（mypy），再执行 unittest（含 wheel／sdist 构建与隔离资源加载）、Python 编译检查、
依赖一致性检查与 SDK synthetic probes。打包测试要求开发环境安装上述固定构建后端；也可
用 `NETIZEN_TEST_BUILD_PYTHON=/absolute/path/to/builder/python make check` 指定已准备好的
独立构建 Python。测试不自动下载构建依赖，不静默跳过打包门禁。它
不创建真实 Codex Thread，也不要求 Codex 登录。SDK 合同测试主要使用 fake App Server；
内置 Skill discovery 另启动真实 bundled App Server 做只读发现；这不能证明普通
start/resume、Side/fork 或子 agent 的模型执行加载，相关 live 验收见
[多实例与内置 Skills](deployment.md#多实例与内置-skills-验收)。

### 实现导航

代码按职责归属组织，目录拆分不新增运行实例或状态所有者：

| 位置 | 职责 |
| --- | --- |
| `netizen_cli/main.py` | ServiceCore 装配并负责共享管理服务、Scheduler、Runtime、SDK 和 Store 的生命周期；管理服务注入各入口适配器。 |
| `netizen_cli/channel_app.py`、`netizen_cli/channel/` | ChannelApplication 负责准入、Runtime 提交和完成事件编排，并装配、关闭同一表情控制器和回复卡片呈现器；展示会话留在各自对象内。它由 `channel/` 中的单向分层组成（顺序见 `channel_app.py` 模块说明），下层不得调用上层，由类型检查保证；斜杠命令与卡片动作各按名称分发到独立处理函数。 |
| `netizen_cli/channel/input_preparation.py` | MessageInputPreparer 负责当前、引用和 catch-up 材料的读取、校验与图片准备，返回 typed PreparedInput；ChannelApplication 继续捕获 admission、组装 Context Boundary 提交参数并发送可见回执，Runtime 确认接受后才推进边界。 |
| `netizen_cli/message_content.py`、`netizen_cli/message_preparation.py` | 前者统一当前、引用和补充消息的纯内容投影；后者共享公共 SDK 卡片补读，由消息准备组件复用。控制意图仍由 Channel 入口判定。 |
| `netizen_cli/cards/` | `controls.py` 负责管理卡片和表单，`reply.py` 负责回复、Activity、Files，`callbacks.py` 集中共享回调协议及基础组件；包入口显式导出公共接口。 |
| `netizen_cli/admin/` | `web.py` 集中路由、认证、一次性授权与请求任务生命周期；`queries.py` 负责查询和分页游标，`presentation.py` 负责响应转换。 |
| `netizen_cli/admin/port_config.py`、`netizen_cli/chat_access.py` | 前者统一既有 YAML 的原子写入和配置服务快照发布；后者共享群聊与会话权限规则的精确 chat ID 校验和映射解析。 |
| `netizen_cli/management/` | 各管理入口共用的应用边界，包括 `updates.py` 中的安装信息与本实例重启编排。 |
| `netizen_cli/schedules/` | 当前计划、时间规则、最小交接记录、唯一调度器和单工具 MCP 入口；复用同一个 BindingStore writer 与普通 Runtime。 |
| `netizen_cli/deployment/` | 有界 Admin 重启记录、维护锁与独立重启 job。 |
| `netizen_cli/runtime/contracts.py`、`netizen_cli/codex_runtime.py`、`netizen_cli/runtime/` | `contracts.py` 唯一定义公共协议、异常、输入输出和快照；CodexRuntime 继续独占任务、Goal、Side、订阅和锁，并在 `codex_runtime.py` 保留原公共类型导入路径。它由 `runtime/` 中的单向生命周期分层组成（顺序见 `codex_runtime.py` 模块说明），下层不得调用上层，由类型检查保证；共享可变状态只在 `runtime/state.py` 声明一次，超时与预算集中在 `runtime/limits.py`。 |

程序分发与实例数据由 [ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md)
分开：`netizen_cli/cli.py` 提供命令，`cli_services.py` 控制精确系统服务，
`cli_data.py` 负责显式初始化、启动校验和迁移，`cli_packages.py` 限定包维护方式，
`cli_update.py` 与包替换范围外的临时协调器负责环境级更新。程序及依赖由用户 Python
包管理器维护；不会为每实例生成 release 或 venv，也不增加常驻总控服务。

### 静态检查与类型债务

ruff 只检查正确性，不约束格式与风格。mypy 检查整个 `netizen_cli` 包，新模块默认全量检查。
`pyproject.toml` 末尾的类型债务清单按模块列出仍被豁免的具体错误码；修复后从清单删除该
错误码或模块，不为新代码向清单添加模块或错误码。行内 `type: ignore` 必须写明错误码和原因。

`CodexRuntime`（`netizen_cli/runtime/`）与 `ChannelApplication`（`netizen_cli/channel/`）由单向
分层的类链组成，各层顺序记录在 `codex_runtime.py` 与 `channel_app.py` 的模块说明中。层内
方法只能调用本层和下层；调用上层方法会成为 mypy 的 `attr-defined` 错误。新增方法时放入
它实际依赖的最低一层；跨生命周期共享的守卫与状态放在最底层，声明一次并由门面类的
`__init__` 初始化。仅本生命周期及上层使用的状态，在实际使用它的最低所属层声明和
初始化；门面类按层次顺序调用各层的小型显式初始化方法。因此分层模块不得进入
`attr-defined` 债务清单。

### 局部回归与共享测试支持

局部回归使用与完整门禁相同的 discovery 根目录，通过文件名和用例名筛选：

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_schedule_scope_matrix.py'
.venv/bin/python -B -m unittest discover -s tests -p 'test_channel_app.py' -k quote
```

`-p` 选择测试文件，`-k` 进一步匹配用例名称；确认输出中的执行数量，避免筛选出零项。
仓库尚有历史顶层测试导入，不将 `python -m unittest tests.<module>` 作为统一入口。
局部回归用于快速验证受影响行为，提交前仍运行 `make check`。

共享的 Channel 消息/Runtime fake、结果构造和场景装配归属 `tests/support/`，
支持模块不反向导入 `test_*.py`。带资源的 fixture 通过 `async with` 或
`IsolatedAsyncioTestCase.enterAsyncContext()` 交给调用者管理生命周期；测试之间不手工
调用其他 TestCase 的 `asyncSetUp/asyncTearDown`。特定场景的故障注入和断言留在对应测试，
已有支持代码按实际复用需要逐步迁入，不为单个用例建立通用框架。

### 修改数据库结构与持久化语义

Channel 数据库从 schema v14 起维护前向迁移；每次实际服务启动都检查并按需执行，
业务 Runtime 仍只接受当前完整结构。仅 setup 初始化新库，已有实例缺库必须报错。
结构或已保存数据的解释发生变化时，在同一改动中递增 schema，并提供从前一版本出发
的明确迁移；应用版本变化但数据契约未变时不新增迁移。当前 v14 是基线，不补建早期
试验版路径。冻结迁移规则见 [ADR 0075](adr/0075-migrate-channel-databases-during-installation.md)，
当前启动与失败边界由 [ADR 0076](adr/0076-separate-cli-installations-from-instance-data.md) 修订。

迁移使用稳定的 SQL／转换逻辑和对应版本校验，不调用以后会变化的当前建表函数，
不自行 commit、不使用会隐式结束事务的执行方式，也不包含网络、文件等外部副作用。
已发布步骤保持不变，后续修正以新步骤表达。新增版本同时保留独立的旧库夹具；夹具
应包含关联数据、非默认设置及相关墓碑，不能通过当前建表逻辑再改版本号冒充历史库。
每个 schema 版本只有一个冻结校验器：v14 基线单独提供，后续版本由进入该版本的
迁移登记 `validate_target`，同时用于该版本作为升级源时的校验，不重复登记 source 校验器。

测试验证最早受支持版本到当前版本的完整路径、相邻版本、重复运行、迁移后与新库的
结构／约束等价性，以及步骤中途失败后数据和版本一同回滚。补测 startup lifetime lock、
初始化证据、新库与丢库区分、迁移前备份、事务提交前中断，以及提交后服务启动失败。
提交前失败回滚事务；提交后保留新库，不能按 ready 是否出现自动还原旧快照。
执行 `make check`，并按[数据库迁移与中断恢复验收](deployment.md#数据库迁移与中断恢复验收)
记录受影响平台的隔离实机结果；没有运行的项目明确标为未验证。

## 本地运行

启动 Netizen、执行真实 Turn 或运行 live probe 前，先确认运行账号的 Codex 登录有效。
登录可以由 Codex CLI 或 Codex App 建立；已安装 CLI 时可独立验证：

```bash
codex login status
codex exec --skip-git-repo-check "Reply exactly: CLI-AUTH"
```

受管安装不要求全局 CLI；固定 bundled runtime 的登录门禁见
[部署前置条件](deployment.md#前置门禁)。上述 `codex exec` 是真实执行，与无需登录的
本地代码门禁不同。

开发安装也是普通 `netizen-cli` Python 包；`pip install -e .` 使用当前 checkout，
不复制源码到实例，不创建独立 release。选择与真实实例不同的专用 root：

```bash
.venv/bin/python -m netizen_cli setup --root /absolute/path/netizen-dev/.netizen
.venv/bin/python -m netizen_cli start --root /absolute/path/netizen-dev/.netizen
.venv/bin/python -m netizen_cli status --root /absolute/path/netizen-dev/.netizen
```

setup 只准备配置、应用凭据、首管理员、数据库和服务绑定，不自动启动。没有 TTY 时逐次
转交飞书验证 URL 并保留同一进程，不索取 App Secret；也可用 `--admin-open-id ou_...`
指定当前应用下的首位管理员。权限、租户审批、发布、安装、可用范围及入群要求
见[前置门禁](deployment.md#前置门禁)。不要使用旧 `dev-install.sh`／`install.sh`
部署新 CLI；详见[CLI 手册](cli.md)。editable 属于开发安装，不承诺自动 update 支持。

前台调试也必须先有完整准备过的实例，并先停止其受管服务，避免同实例双启动：

```bash
.venv/bin/python -m netizen_cli stop --root /absolute/path/netizen-dev/.netizen
NETIZEN_ROOT=/absolute/path/netizen-dev/.netizen .venv/bin/python -m netizen_cli.main
```

`netizen_cli.main` 是 Runtime 入口，不是空目录初始化器；它仍要求归属标记、初始化
证据、私有配置／凭据、当前应用管理员名单及数据库，启动时在同一 lifetime lock 内检查与迁移。不要通过
手工创建 marker 或删除数据库绕过准入。前台进程不在 update 的系统服务清单保证内，
结束调试后再通过 start 启动受管服务。

配置和凭据布局见[目录](deployment.md#目录)与
[配置与管理页访问](deployment.md#配置与管理页访问)。`config.example.yaml` 只是示例；
`instance.dataDir` 必须是选定 root 的 `state`。`projectRoot` 限制自动创建的空
Project，不是默认 cwd；`projects` 只引导尚未登记项，后续使用飞书／Admin 管理。
服务默认 Admin host 为 0.0.0.0，缺省端口首次实际绑定后写回，已有端口不漂移；
Admin 是必备能力，不支持 `adminWeb.enabled`。管理员通过飞书登录，名单与用户凭证
分别位于实例的 `state/admin-users.json`、`credentials/admin-user-<sha256>.json`；
用户凭证按用户独立保存、使用时读取，不在启动时加载或驻留缓存。旧聚合文件不读取或
迁移，升级后重新登录生成新文件（[ADR 0091](adr/0091-load-admin-user-credentials-on-demand.md)）。
服务重启保留新格式用户凭证，但浏览器 session 失效。安装统一开通 user 类型
`component:selector`、`offline_access`，新登录也请求这两项权限；名单展示不另申请通讯录权限。
相关边界见 [ADR 0080](adr/0080-authenticate-admins-with-feishu-identities.md)，真人和平台验收
见[部署手册](deployment.md#admin-飞书登录与管理员维护验收)，本地测试通过不代表已上线。

实例命令按 root，程序更新按调用环境；另一个 venv 的 start/restart 不自动接管已有
绑定。需要切换时先 remove 保留数据，再在新环境 start，不加 --purge。修改正在被
运行服务使用的 editable 源码也会改变磁盘程序，先停止相关服务、完成修改与检查后再
启动，不视为支持热更新。

内置 Skills 从实际安装包加载；editable 从该 checkout 的 canonical `skills/` 加载，
不写全局 Codex Skills。NETIZEN_ROOT 仍作为当前实例上下文提供给工具；被用户原生
环境过滤策略移除时，内置 Lark Skill 明确失败，不猜另一实例凭据。实际模型加载和
双实例隔离见[对应验收](deployment.md#多实例与内置-skills-验收)。

维护 `netizen-runtime-guidance` 时，遵守[运行环境提示边界](design.md#运行环境提示与使用指南的边界)：
直接生效所需信息放在 description，正文只补充细节；分别保持在 300/600 个 Unicode
字符内，不搬入用户指南内容。新增内置 Skill 时同步运行清单、wheel 构建、sdist manifest
和发布资源校验，并验证包外导入及原生发现；不修改用户全局 Skill 或配置。

## 私有运维记录

仓库不定义默认部署主机或远端账号。维护者可为当前 checkout 保存开发机路径、SSH
目标、远端账号、Admin URL 和私有发布记录：

```bash
cp LOCAL_ENVIRONMENT.example.md LOCAL_ENVIRONMENT.md
chmod 600 LOCAL_ENVIRONMENT.md
```

`LOCAL_ENVIRONMENT.md` 被 Git 忽略且不进入发行包，不得包含 raw Secret。它只是
可选运维档案，不是运行时配置；没有它时仍按本文开发并
[显式选择部署目标](deployment.md#选择部署目标)。运维前若文件存在，应读取其中的目标
约定；不要把私有坐标复制到跟踪文件或公开产物。

## 提交修改与验证

提交 Issue 或 PR 时说明具体场景、当前行为与预期结果；修复问题时附可复现步骤。让改动
保持可审查的范围，按行为补足相关测试，检查最终 diff 是否包含无关改动或遗漏的约束。
产品行为变化时同步更新用户手册与相应工程契约，README 保留准确的产品摘要和入口。
新建 ADR 须同时满足难以撤销、缺少上下文会令人意外、确实经过取舍三条，规则与全部 ADR 的
状态、修订链见[ADR 索引](adr/README.md#何时写-adr)；单个功能的行为直接更新[工程设计](design.md)与用户手册。
用户手册面向使用者：优先使用界面上出现的词和日常说法，不写内部实现细节与重试预算。

升级 Codex SDK 或随附 App Server 时，先完成[SDK 升级审查](deployment.md#codex-sdk-升级审查)，
逐项检查公开 API 替代、既有兼容债务和新增能力的用户价值、接入边界与成本。将讨论取舍
维护在同一份审查报告中，作为实施依据；用 `scripts/diff_codex_sdk.py` 取得原始差异材料，
以 `scripts/run_upgrade_regression.py` 注册表维护已知回归集，并在每次升级时复核这两个
脚本是否仍适用。脚本不判断新增行为的重要性，跳过须留原因，不能替代针对性验证。
报告开头与审查结束回复都按“SDK/App Server
改动简介、改哪些、验证哪些、增加哪些”总结，并把实际结果和未验收项纳入变更说明。

PR 与 main push 统一执行 `make check`。真实账号、App Server 与飞书 live phases 只按
变更触发；完整命令、触发条件、兼容性证据和未开放能力的原因统一维护在
[部署与验收](deployment.md#代码门禁与按需实时兼容性验证)。未触及相关边界的改动无需
重复 live probe。报告所运行的检查、结果以及任何跳过项或验证缺口。

PyPI 发布由维护者显式决定，开发和本地构建不授权上传。必须验收 wheel、sdist 及其
隔离安装，不能用旧 GitHub release archive 的合格结论替代；当前发布状态见
[CLI 手册](cli.md#验证与发布状态)和[发布说明](deployment.md#发布正式-release)。
