---
status: accepted
date: 2026-09-06
amends: 0031, 0034, 0041
related: 0022, 0032, 0035, 0042, 0044, 0050
---

# 通过共享安装器执行管理页手动升级

Admin 需要在管理员点击后完成正式版本安装、重启与结果展示。决定由独立于主服务生命周期的
同用户一次性进程调用 exact 官方安装器，继续复用唯一的安装事务；不检测会话忙闲、不增加
Runtime 维护状态，也不做任务无感续跑，以免为部署入口另造一套执行生命周期。

## 边界与取舍

本 ADR 对 ADR 0031 的“无第二个服务”、ADR 0034 与 ADR 0041 的“无自动更新/后台下载器”
作一个明确、窄的修订：允许实例管理员每次显式提交后，在独立的 service-manager job 中运行
一次下载与安装。它不是长期 Web 服务、第二个 Runtime、定时检查器、任务队列或另一份安装
事务。唯一常驻业务进程、Channel Database、AsyncCodex、管理员 authority 和原生 Codex 状态
保持原边界。此能力不改变 ADR 0050 中由维护者决定正式发布时机的规则。

单次 busy 检查无法排除检查之后的新输入；严格阻止打断任务又需要冻结入口与失败恢复。
本实现选择允许升级打断工作：候选准备期间继续接收消息，安装器切换时才走既有正常停机，
中断普通 Turn、暂停 Goal、结束临时 Side Session。升级不检查 Turn/Goal/Compaction，
不预先关闭 admission、不主动恢复执行，也不新增强杀或绕过 lifetime-lock 退出确认。

## 官方候选与明确提交

Updates 页显示当前运行版本和安装来源。只有从固定产品根实际运行的、metadata/manifest
一致的受管 Published Release，且 `current` 仍指向该 release，才可以提交升级；Source
Install 与非受管运行只提供对应安装说明。第一个具备该入口的版本仍须通过已有安装入口安装。

检查更新只读取固定官方 GitHub latest Release API，接受非 draft、非 prerelease、immutable
的正式版本。服务端绑定版本、Release ID、exact `install.sh` 与项目 tarball 的 SHA-256，
两项资产的名称、下载地址和摘要必须完整匹配；不接受浏览器指定 URL、命令、分支或摘要替换。
已显示候选通过 session-bound 一次性 action/CSRF grant 提交。执行固定所选版本，latest
之后改变不会悄悄换目标；未知提交结果只查询对账，不自动重发。

信任边界是官方 HTTPS/GitHub immutable Release、两项资产 SHA-256，以及安装器原有的
manifest 与严格解压验证。本 ADR 不引入新的制品签名系统，也不把 SHA-256 描述成签名。
未来增强发布签名必须增强 CLI 与 Admin 共用链路。

## 一次性执行者与共同事务

普通子进程不能跨越主服务的 control-group 停止。Linux 因此用同用户 transient systemd
service，macOS 用当前 `gui/<uid>` 下显式 bootstrap 的临时 LaunchAgent。执行程序来自
已安装的物理 release 路径；临时 job 不随主服务停止而退出、不自动重启，也不在下次登录
重放。Linux 由 manager 回收 transient unit；macOS 在终态与安装锁释放之后清理临时 job
和提交文件。macOS 不解析 `launchctl print` 文本，仍不支持 LaunchDaemon 或 root helper。

执行者复用有界账号 shell 环境装载器取得本次环境，不保存环境快照或凭据。它在整个下载、
候选准备、激活、回滚和结果写入期间持有现有 `.install.lock`。Admin 在同一锁内记录并提交
操作，再让执行者取得锁与重读 exact 操作；执行者还核对旧 `current` identity，防止与 CLI
安装交错后按过期来源执行。只允许一个操作，不构造等待队列。

执行者校验所选 exact installer 字节后以零参数 `/bin/sh install.sh`、关闭 stdin 的方式
调用。候选 bootstrap 核对操作绑定的版本和 tarball 摘要；候选安装器验证传入 FD 的 inode、
owner 与已持有的同一锁，并立即恢复 CLOEXEC，避免 Codex 子进程继承。该窄内部 handoff
不成为公共安装参数，也不允许跳过 Host Validation。

安装器仍唯一拥有候选 venv、配置/权限验证、服务原 active/enabled 意图、数据库/Skill
snapshot、激活、ready 与回滚。Admin 或执行者不得提前停止主服务，否则安装器可能误判
原服务本来应保持停止。缺飞书配置/权限时 Admin 操作返回 `requires_action`，不在无交互
worker 中开启授权浏览器；CLI 原有 ADR 0044 exact-App 修复与凭据文件交接继续保留。

## 结果与恢复

`~/.netizen/state/update.json` 仅保留最近一次部署操作的 typed、受限摘要：操作 ID、固定
目标 identity、旧 release identity、阶段、固定错误码与时间。它是 `0600` 的有界原子文件，
不含 token、Secret、命令输出、任务正文或 release notes；不进入 Channel SQLite，不是
history、audit log 或 durable job queue。阶段属于安装流程，不能投影成 Runtime 业务状态。

正常终态区分 `succeeded`、准备失败 `failed`、确认完整回滚 `rolled_back`、需要配置或权限
处理 `requires_action`，以及结果未确认/回滚不完整 `recovery_required`。安装器报告事务
结果，执行者检查退出结果；页面不能根据重新连通、manager active 或 ready 推导成功，因为
就绪的可能是回滚后的旧版本。执行者失联或中断只在共享安装锁与 manager 的有界观察能够
建立相应证据后对账；不凭页面等待超时猜测终态或重复发起操作。

浏览器断开不取消安装；重启后旧 Admin session 失效，用户重新登录读取同一结果。
`recovery_required` 阻止再次从 Admin 提交，管理员通过已有官方安装入口恢复 activation
intent。后续显式 CLI 安装在同一锁内成功完成事务后，把旧未知/非终态记录标为
`recovered/manual_recovery`，保留原 operation 与目标，仅证明后续部署已恢复；实际运行版本
另行显示，不能把原点击改报为 `succeeded`。失败不得清理旧未知结果；Source Install 成功
也可形成恢复结论，但仍不启用源码的 Admin 升级。机器掉电不承诺自动回滚，不能让浏览器
自行修复数据库或删除恢复材料。验证程序与逐平台实机范围由[部署手册](../deployment.md#管理页升级验收)
维护，fake manager 测试不替代真实服务停止与异常恢复证明。
