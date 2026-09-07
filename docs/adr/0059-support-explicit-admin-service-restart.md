---
status: accepted
date: 2026-09-07
amends: 0057
related: 0022, 0031, 0034, 0041
---

# 通过同一部署执行者支持管理页独立重启

修改需要重启的 Codex 配置后，管理员需要保持当前版本重新启动服务。系统维护页增加独立的
“重启服务”，复用 ADR 0057 的一次性执行者、安装锁、授权与结果对账，直接调用当前物理
release 的 `source/service.sh restart`，避免为此重新安装或另造服务生命周期。

重启无需检查更新，对通过现有安装身份校验且 `current` 仍精确指向运行 release 的受管
Published Release 和 Source Install 开放；action grant 绑定该 release digest。执行者
持锁直到服务脚本退出和结果写入，脚本不继承锁 FD；未恢复 activation intent 阻止重启。
停机影响、重新登录与不自动续跑沿用升级契约；重启不改配置，也不保证配置作用于已有 Thread。

两类操作共享 `state/update.json`；升级保留 schema 1，重启使用 schema 2、`kind=restart`
和 `{version, releaseDigest}` 目标。成功要求 exact 服务脚本完成 stop-confirm/start-ready
并零退出；进入重启后失败报告 `recovery_required/restart_failed`，不推断旧服务仍运行或
已经回滚。pending/未知结果阻止两类提交，迟到 accepted 执行者不得重新认领已过期操作；
manager 观察未知不触发重派。恢复沿用显式 CLI 安装，保留原操作并只标记 `recovered`。
[部署手册](../deployment.md#管理页重启验收)维护重启的增量验收及两平台实机门禁。
