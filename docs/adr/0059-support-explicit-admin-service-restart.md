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
manager 观察未知不触发重派。一般未知结果仍沿用显式 CLI 安装恢复，保留原操作并只标记 `recovered`。

单纯 `recovery_required/restart_failed` 增加窄范围复核：管理入口在构造时只读一次已有
`restarting/recovery_required` 重启的 operation ID；只有本进程随后完成受管 ready
发布且尚未开始关闭，该内存证明才有效。状态读取（含刷新维护状态、检查更新）在现有安装锁内
重读操作并成功清理执行者后，重新核对 exact operation、运行 release/`current`/目标版本与
digest 一致、无 activation intent，才写入 `recovered/service_ready`。这仅确认当前服务
已恢复就绪，不把原重启改报成功。缺失、不可读或 `accepted` 的启动快照不能由后续刷新补齐；
旧进程、未释放锁或无法确认执行者收尾时保持原结果。关闭开始即撤销该内存证明。

该复核不增加状态文件、后台轮询或组件健康探测；升级、回滚、worker 失联与其他未知原因
仍由安装器恢复，不能用服务重新连通替代事务证据。

新版本兼容读取旧记录。`service_ready` 是新增固定结果码；手动降级到尚不识别该码的旧版并
保留此记录时，旧版维护页会拒绝读取。正常 Admin 升级在提交时已写入新的操作，不受此限制。
[部署手册](../deployment.md#管理页重启验收)维护重启的增量验收及两平台实机门禁。
