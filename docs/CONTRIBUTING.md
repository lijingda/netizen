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
.venv/bin/python -m pip install -c requirements.lock -e .
node --version
make check
```

Admin JavaScript 行为测试使用 Node.js 22，请在完整开发门禁前确认版本。Node.js 仅用于
开发与 CI 测试，不是生产运行或 Source Install 的前置依赖。本地缺少 Node.js 时该项
测试会明确跳过；此时 `make check` 通过不代表 JavaScript 测试已完成。CI 安装 Node.js
并强制执行该项测试。

`make check` 执行 unittest、Python 编译检查、依赖一致性检查与 SDK synthetic probes，
不创建真实 Codex Thread，也不要求 Codex 登录。SDK 合同测试主要使用 fake App Server；
受管 Skill discovery 另启动真实 bundled App Server 做只读发现。

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

复制 [config.example.yaml](../config.example.yaml) 到本地配置文件，填写 App ID，并将
`dataDir`、`projectRoot` 与 Project 目录示例替换为实际绝对路径。`projectRoot` 用来限制
自动创建的空 Project，不是默认工作目录。`projects` mapping 启动时导入尚未登记的项；
后续在飞书 `/settings` 管理 Project，已停用、动态登记或删除的记录不被配置文件覆盖。
没有登记并启用的 Project 时，`/new` 会引导打开 `/settings`，不会回退到服务工作目录。
持久化规则见[数据与配置](design.md#数据与配置)。

本地手工准备的飞书应用需按[权限、事件与回调契约](deployment.md#前置门禁)逐项配置。
受管安装器会自动请求该契约，但两种方式都需要完成租户审批、应用发布与安装，设置可用
用户和群，并把机器人加入目标群；权限变更必须随应用版本发布。

本地开发支持 `FEISHU_APP_SECRET`，也可像受管服务一样用 `FEISHU_APP_SECRET_FILE`
指向权限为 `0600` 的 raw Secret 文件。不要将 Secret 写进仓库或命令历史。Admin Web
仅支持凭据文件：启用时必须设置绝对的 `NETIZEN_ADMIN_SECRET_FILE`，不接受 raw secret
环境变量。以下使用已安全准备的 Feishu Secret 文件，并生成独立 Admin credential；
所有 `/absolute/path/` 都需替换为本地路径：

```bash
umask 077
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > /absolute/path/admin-web-secret
export NETIZEN_CONFIG_PATH=/absolute/path/config.yaml
export FEISHU_APP_SECRET_FILE=/absolute/path/feishu-app-secret
export NETIZEN_ADMIN_SECRET_FILE=/absolute/path/admin-web-secret
.venv/bin/python -m netizen.main
```

Admin 默认监听 `0.0.0.0:8787`，面向受信内网中的单一实例管理员；登录与凭据轮换见
[配置与管理页访问](deployment.md#配置与管理页访问)。本地只调试飞书入口时，可在 YAML
中显式设置 `adminWeb.enabled: false`。

要把当前工作区安装为服务，使用 `./dev-install.sh`，包括未提交修改；`./install.sh`
安装的是最新正式 Release。两者使用相同激活和回滚事务，不执行 `git pull`。首次安装、
服务环境与启停流程见[部署文档](deployment.md#安装)。Agent 安装当前工作区可直接执行
`./dev-install.sh </dev/null` 并按输出继续；其他交互方式与排障见
[Agent 安装说明](deployment.md#agent-驱动首次安装)。

## 私有运维记录

仓库不定义默认部署主机或远端账号。维护者可为当前 checkout 保存开发机路径、SSH
目标、远端账号、Admin URL 和私有发布记录：

```bash
cp LOCAL_ENVIRONMENT.example.md LOCAL_ENVIRONMENT.md
chmod 600 LOCAL_ENVIRONMENT.md
```

`LOCAL_ENVIRONMENT.md` 被 Git 忽略且不进入安装 release，不得包含 raw Secret。它只是
可选运维档案，不是运行时配置；没有它时仍按本文开发并
[显式选择部署目标](deployment.md#选择部署目标)。运维前若文件存在，应读取其中的目标
约定；不要把私有坐标复制到跟踪文件或公开产物。

## 提交修改与验证

提交 Issue 或 PR 时说明具体场景、当前行为与预期结果；修复问题时附可复现步骤。让改动
保持可审查的范围，按行为补足相关测试，检查最终 diff 是否包含无关改动或遗漏的约束。
产品行为变化时同步更新用户手册与相应工程契约，README 保留准确的产品摘要和入口。

PR 与 main push 统一执行 `make check`。真实账号、App Server 与飞书 live phases 只按
变更触发；完整命令、触发条件、兼容性证据和未开放能力的原因统一维护在
[部署与验收](deployment.md#代码门禁与按需实时兼容性验证)。未触及相关边界的改动无需
重复 live probe。报告所运行的检查、结果以及任何跳过项或验证缺口。

正式 Release 由维护者决定时机，复用 exact main commit 的成功 CI，不重复代码测试或
账号级 live probe；执行流程见[正式发布](deployment.md#发布正式-release)。
