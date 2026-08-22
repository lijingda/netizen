---
status: accepted
date: 2026-08-19
related: 0008, 0009, 0014, 0022
---

# 让服务启动环境成为工具子进程的基线

## 背景

ADR 0022 让 launcher 在每次服务启动时运行账号的 interactive login shell，并把完整导出
环境交给 Netizen 和唯一的 App Server。固定 Codex 0.144.4 默认还会为每个 Thread 用
non-interactive login shell 生成 shell snapshot，并把该快照恢复到后续工具命令。

这两次启动的语义并不等价。Ubuntu 默认 `.bashrc` 会在非交互 shell 中提前 `return`；若
NVM 等初始化位于其后，`bash -lic` 捕获的 PATH 包含 NVM，而 Codex 的 `bash -lc` 快照会
重新写入不含 NVM 的 PATH。此时 Netizen 主进程和 App Server 都能找到工具，真实 Turn
却只能命中系统 PATH 中的旧入口或失效符号链接。把 PATH、NVM 版本或每个工具逐项复制
到另一份环境文件，只会重新引入 ADR 0022 已移除的双配置问题。

Codex 的公开 `allow_login_shell=false` 配置会让 shell 工具在省略 `login` 时使用非登录
shell，并拒绝显式 `login=true`。工具环境仍由同一 App Server 进程和用户原生
`shell_environment_policy` 产生。

## 决定

1. 生产仍只创建一个 `AsyncCodex` 和一个 App Server，但通过公开 `CodexConfig` 固定唯一
   override：`allow_login_shell=false`。这条决定只取代 ADR 0008、0009 和 0014 中“无参数
   `AsyncCodex()` / 不传 config”的表述，不改变它们的单客户端和公开 SDK 边界。
2. ADR 0022 的 launcher 继续是环境事实源：每次 start/restart 重新取得账号 interactive
   login shell 的导出环境。Netizen 不生成 PATH 快照，不识别 NVM，不修改用户 dotfile，
   也不安装或链接具体工具。
3. 用户级 `~/.codex/config.toml` 仍控制模型、工具、权限、sandbox、MCP、Skills 以及
   `shell_environment_policy` 的继承、过滤和显式 set。Netizen 不复制或覆盖这些字段；若
   用户主动过滤 PATH，结果仍按原生配置生效。
4. 不关闭或修改用户的全局 shell snapshot feature。固定版本的普通工具调用改为非登录
   shell后，不再让第二次 login profile 覆盖 launcher 已捕获的环境。升级 Codex 时必须用
   一个只存在于继承 PATH 中的临时可执行文件重跑真实 Turn，不能只检查父进程环境。

## 后果

修改持久 shell profile 后重启 Netizen，已有和新建飞书会话的普通工具调用都会继承新
环境；不需要修改 `service.env`、Codex config 或安装系统级副本。工具 schema 不再允许
`login=true`，因此某条命令若确实需要独立 login-shell 语义，应在命令本身显式启动所需
shell，并接受它可能重新解释 PATH 的结果。

当上游 Codex 能在默认工具路径中可靠保留父进程的 interactive 环境，或
`allow_login_shell` 的公开语义变化时，先更新固定版本的黑盒验证，再删除这个单一
override；不得演变成按工具或变量维护的兼容层。
