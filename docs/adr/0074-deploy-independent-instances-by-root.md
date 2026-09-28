---
status: accepted
date: 2026-09-28
amends: 0014, 0031, 0032, 0034, 0057, 0059, 0066
related: 0022, 0023, 0041, 0061
---

# 按根目录部署独立实例，继续共享 Codex 用户状态

> 已在当前源码实现；双平台实机与 Skills 实际模型执行仍须独立验收，既有 Release 不受影响。
> 运行契约见 [工程设计](../design.md#部署事务与操作状态)，操作与验收见
> [部署手册](../deployment.md#多实例与内置-skills-验收)。

同一账号需要按用途部署不同飞书机器人，又希望继续复用 CLI 的登录、配置和工具环境。
选择多个独立部署实例，不把单一运行时扩展为多机器人，不要求固定的 instances 父目录。
用 `--root` / `NETIZEN_ROOT` 选择安装位置，默认 `~/.netizen`；canonical root 是唯一部署
定位依据，平台服务名、临时维护 job 和 Cookie 后缀由它派生，不另设用户维护的实例 ID。
每实例仍只有一个长期服务、Channel、Channel Database、AsyncCodex、Admin 和 Scheduler。

受管和手工启动都在入口确定同一个 root；未指定时使用账号默认目录，不从 YAML 或状态
目录反推。启动 Codex 前总将 canonical root 写入当前进程的 `NETIZEN_ROOT`，并校验配置、
应用凭据和状态均属于该 root，冲突则拒绝启动。Skill 直接使用此运行上下文，不在安装时
生成替换路径的副本，也不改变用户原生工具环境过滤策略。

Admin 各自监听。明确配置的端口严格使用；缺失时首次启动实际绑定 8787 起的有界候选，
绑定成功后原子固化，后续不自动漂移。新增 `/admin` 返回当前实例管理 URL 和根目录。
不采用共享端口的路径网关，不登记停止实例的端口，也不增加跨实例总控或依赖。

保持原生共享 `CODEX_HOME`，Netizen 内置 Skills 改为随实例 release 保存。允许在同一个
已初始化 SDK client 上增加固定 `skills/extraRoots/set` 薄适配口，只设置当前 App Server
进程的额外 Skill 根，不写用户配置、不启动第二个 App Server。沿用 ADR 0014 的 typed
模型、能力形状、synthetic/live 和公开 facade 替换门禁；未证明实际执行加载前不能宣布
兼容通过。普通用户 Skills/MCP/历史仍由 Codex 拥有；内置 Lark Skill 用当前 root 定位
固定 `netizen` profile。

固定 SDK `openai-codex==0.156.1` 已提供 `SkillsExtraRootsSetParams/Response`，但缺少
高层 facade，故使用同一已初始化 client 的固定 typed 适配口；公开 facade 通过 parity
验证后移除。该 API 设置完整额外根集合，不替换原生默认发现根，也不等于只给
`skills/list` 附加目录；每个 App Server 启动时重新注册，不修改用户持久偏好。
参见[官方 App Server 协议](https://learn.chatgpt.com/docs/app-server#api-overview)。

安装、Admin 升级/重启及其一次性 worker 必须显式传递同一 root；停止确认、锁 FD、ready、
数据库回滚和不可变 Release 校验保持既有门禁。取消全局内置 Skill 的安装/快照/回滚/
卸载步骤，由物理 release 决定加载版本。任何实例都不回滚共享 Codex 状态或删除整个 root。

项目尚未推广，不实现旧固定服务名、旧安装布局、旧全局 Skills 的迁移兼容，也不保证
跨此次部署格式变化的自动降级。实例间权限、资源隔离、容器/VM、根目录搬迁、统一版本
协调和自动发布均不在本次范围。实现及验收状态见部署手册，不以本 ADR 代替 live 证据。
不要求所有实例运行相同 Netizen 版本，但也不承诺任意 Codex 版本共享原生状态均兼容。
