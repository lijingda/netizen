---
status: accepted
date: 2026-08-19
related: 0008
---

# 每次服务启动时加载账号 Shell 环境

## 背景

Netizen 与同一用户在终端启动的 Codex CLI 共用 Codex 状态、配置、权限和工具。早期
systemd user unit 固定安装时的 `PATH`，并从安装进程一次性捕获代理和 CA 变量到
独立的持久环境副本。这种副本不会随 `.profile`、`.bashrc`、NVM 或其他工具初始化变化，
最终形成第二套需要持续维护的环境配置，违背 Channel 与 CLI 尽量一致的产品边界。

systemd 服务本身没有真实 TTY，也不会自动执行 shell 启动文件。把主进程长期包在
`bash -lic`、tmux 或伪终端中会让交互逻辑、信号、日志和失败状态混在一起，同样不能作为
可靠 daemon 边界。

## 决定

1. user unit 继续直接由 systemd 监督，但 `ExecStart` 先运行 release 内的专用 Python
   launcher。unit 只提供加载 profile 所需的固定基础 `PATH` 和 Netizen 自己拥有的配置、
   Secret 文件及 `CODEX_HOME` 路径，不保存安装调用者的 PATH。
2. launcher 每次启动（包括 systemd 自动重启）都根据 effective uid 的账号数据库读取
   home、用户名和 login shell。它运行一次无 TTY 的 interactive login shell：
   Bash/Zsh/POSIX shell 使用 `-lic`，Fish 使用对应 login + interactive flags。
3. profile shell 只执行一个带随机前后边界的环境探针。launcher 以 NUL 分隔解析导出的
   环境；profile stdout 经 pipe 增量、有界读取，stderr 直接丢弃，都不进入 journal，也
   不把环境写入文件或命令日志。快照携带声明长度和 SHA-256 摘要，拒绝 profile 后台
   writer 在随机边界之间并发插入的内容。探针有 10 秒超时、4 MiB 输出上限和独立进程组；
   超时会终止整组并以不含输出内容的错误 fail closed。探针用 `exec` 替换 login shell，
   避免一次服务启动被误当成终端会话结束而执行 Bash/Zsh logout hook。
4. 捕获后保留 PATH、代理、CA、语言、XDG 及其他普通导出值，但重新覆盖账号身份、HOME、
   `CODEX_HOME`、Netizen 配置和 Secret 文件路径，并删除直接 Secret 与可能污染 release
   Python 的 venv/Python 覆盖。unit launcher、探针和最终解释器显式使用 `-E -B -u`，
   防止其余 `PYTHON*` 环境改变受管 runtime；随后用 `execve` 原位替换为 release 的
   Python，让 systemd 继续直接观察真实服务 PID 和退出状态。
5. Netizen 不提供独立的服务环境文件或旧环境迁移层。安装器和新 unit 都不读取、写入或
   复制历史环境文件；账号的持久 shell profile 是唯一的用户环境配置来源。
6. Netizen 不修改或复制 Codex 的 `shell_environment_policy`。按 ADR 0023，唯一的公开
   Codex 启动 override 是 `allow_login_shell=false`，防止固定版本为工具再次运行
   non-interactive login shell 并覆盖本 launcher 已捕获的环境；继承、过滤和显式 set
   仍完全来自同一个用户级 `config.toml`。
7. 安装器不在 user service cgroup 之外预执行 profile。任意启动文件可以产生副作用，且
   自行 `setsid`/daemonize 的子进程无法由安装器进程组可靠回收。首次安装、active 升级和
   日常 start/restart 都以 service ready 作为成功边界；停止状态的升级仍保持停止。

## 后果

用户修改持久 shell profile 后重启 Netizen，即可像重新启动一次 Codex CLI 一样获得新
的导出环境；NVM 当前 Node 版本产生的 PATH、代理和新增工具不再需要安装器逐项适配。
已运行的服务不会热更新环境，这与已运行的 CLI 一致。

工具子进程默认直接使用该环境，不再额外执行 login profile。固定 Codex 0.144.4 仍可在
Thread 启动时生成 shell snapshot，但普通工具不会用它覆盖服务环境；升级 SDK/App Server
时必须按 ADR 0023 重跑继承 PATH 的真实 Turn 门禁。

某个现有终端里临时执行但未写入 profile 的 `export`、alias、未导出的 shell function 和
真实 TTY 状态不在继承范围。profile 如果退出非零、提前退出、输出过大或等待交互，服务
会明确启动失败；用户应先在终端修复对应 login shell 的启动文件，再 restart。新增 shell
类型必须先定义其无 TTY profile 语义并补成功、失败和超时测试，不能退回持久环境副本或
让 daemon 长期运行在交互 shell 中。
