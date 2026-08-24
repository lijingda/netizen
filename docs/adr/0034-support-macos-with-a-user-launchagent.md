---
status: accepted
date: 2026-08-23
related: 0022, 0023, 0032, 0033
---

# 使用用户级 LaunchAgent 支持 macOS

## 背景

Netizen 的源码开发已可在 macOS 进行，但正式安装事务只认识 Linux systemd user manager。
简单地在 macOS 写入一个 plist 虽可启动进程，却无法证明 `bootout` 后旧进程已经退出；候选
启动失败时若立即恢复 Channel SQLite 或受管 Skill，仍存活的进程可能继续写入被恢复的状态。
依赖 `launchctl print` 的文本字段也不可接受，因为 Apple 明确不把该输出声明为稳定 API。

## 决定

Netizen 正式支持 macOS 14+ 当前登录用户的 LaunchAgent，Apple Silicon 与 Intel Mac 均在
支持范围内，保持零参数
`install.sh` / `uninstall.sh` 与 `service.sh start|stop|restart|status`，不使用 sudo。Linux 与
macOS 共用 release、配置、凭据、数据库、Skill、activation intent 和回滚事务；窄
Service Backend 只拥有服务定义、状态、停止确认、发布、启动和 ready 等待。macOS 使用
`~/Library/LaunchAgents/io.github.lijingda.netizen.plist`，不提供 LaunchDaemon；注销时停止，
下次登录自动启动。两类芯片均已完成服务运行真机验证，实现不按 CPU 架构分叉。

LaunchAgent 只通过 `launchctl print gui/<uid>/<label>` 的退出码判断 loaded，不解析输出文本；
每次 bootstrap 前先 `enable` 以清除 sticky disabled 状态。受管 plist 必须同时满足当前 UID
拥有、普通非 symlink、禁止 group/world write、Label、指向 `~/.netizen/current` 的 exact
`ProgramArguments` 与受管环境 sentinel 全部匹配，否则安装器拒绝覆盖或卸载。

服务 launcher 在 `~/.netizen/state` 的稳定 inode 上持有 lifetime `flock`。锁 FD 默认
CLOEXEC，只在 launcher 最终 `execve` 前设为 inheritable；`netizen.main` 接管同一 FD 后
立即恢复 CLOEXEC，防止 Codex 工具或后台 terminal 继承。停止只有在 launchd target 已卸载
且安装器能够非阻塞取得该锁时才确认完成，未确认前禁止恢复数据库或 Skill。installer 在
每次启动前删除旧 ready 文件，launcher 再次清理，主进程只在 Feishu background、唯一
Runtime 和 admission 全部打开后原子写入 `0600` marker；loaded 不等同于 ready。

macOS 服务入口使用精确锁定的 `truststore` 将应用 TLS 委托给 Security.framework 和系统
钥匙串；Linux TLS 路径不变。该注入只发生在 Netizen 应用启动时，不生成 CA bundle、不保存
安装时证书或新增环境文件，因而由系统管理员维护的企业根证书也能被 Feishu WebSocket 使用。

## 后果

macOS 获得与 Linux 同等级的 active/stopped upgrade、失败回滚和卸载保留边界，同时仍只有
一个用户服务和一套状态。LaunchAgent 不承诺注销后常驻；V1 不增加 LaunchDaemon、root
helper、Swift ServiceManagement、GUI/pkg/签名公证、自动更新、第二套环境文件或跨平台 home
迁移，也不通过解析 launchctl 文本推测进程状态。
