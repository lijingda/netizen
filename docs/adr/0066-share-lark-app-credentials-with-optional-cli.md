---
status: accepted
date: 2026-09-16
amends: 0032, 0033, 0035, 0062
related: 0034, 0039
---

# 用单一应用凭据文件与可选 Lark CLI 共享机器人身份

Netizen 原先分别在 YAML 保存 App ID、独立文件保存 Secret，再由 `netizen-feishu` 脚本
换取临时令牌供 Agent 调用可选的 Lark CLI。现在将 App ID 与 raw Secret 统一保存为
`~/.netizen/lark-app/config.json` 的固定 `netizen` profile，采用官方 CLI 配置格式，
让服务和 CLI 直接复用同一份应用凭据，删除 Skill 的取令牌脚本。文件路径表达应用凭据
归属；安装和运行 Netizen 都不增加 CLI 依赖，也不新增 CLI 调用包装器。

文件包含 `apps` 数组，受管条目使用 `name: netizen`、`appId`、`appSecret`、
`brand: feishu`、`defaultAs: bot` 与空 `users`；默认 `currentApp` 为 `netizen`。
Netizen 使用标准库解析并固定选择唯一的 `netizen` 条目，不跟随 `currentApp`，不解析
Secret 引用、不建立用户 OAuth。目录权限为 `0700`，文件由当前用户拥有、普通非 symlink、
权限为 `0600` 或更严格；重复 JSON 字段、重复受管 profile 和无效凭据明确失败，错误不带
原始 Secret。服务只传绝对路径 `NETIZEN_LARK_APP_CONFIG`；手工运行默认读取 YAML
旁的 `lark-app/config.json`，旧 Secret 环境来源不再支持。

`netizen-feishu` 更名为 `netizen-lark`，只说明可选 CLI 如何通过
`LARKSUITE_CLI_CONFIG_DIR` 选择此目录并显式使用 `--profile netizen --as bot`。
CLI 负责按本地应用凭据获取令牌，上游 lark Skills 负责查询和分页。Skill 不读取或展示
Secret，不修改用户默认 CLI 配置；其文档说明已有环境认证覆盖和 workspace 选择的影响。
需要话题历史时仍从当前 `message_id` 查询位置，不向每条 Prompt 增加聊天／话题字段，
不改 Scope、Side、catch-up 或消息 provenance。官方 CLI `1.0.95` 是此次格式兼容基线，
隔离假 profile 的格式及认证路径检查不代表真实应用认证或权限验收通过。

## 初始化与安装事务

安装器只初始化、读取并原子写入该 profile。项目尚未推广，不实现旧凭据格式迁移、
旧 Skill 名清理或旧格式版本回滚兼容。

已有有效 App ID、`appSecret` 为空表示 exact-App 修复；删除整个 profile 文件表示
重新创建或选择应用。
浏览器失败、取消或超时保留原修复／重绑定意图；成功写入的新凭据继续遵守 ADR 0062
的两阶段语义，不因后续权限失败或候选回滚恢复旧凭据。人工放弃重绑定时恢复此前安全
备份的整个 profile，再运行安装入口。Admin 后台升级仍不能发起浏览器授权。

`netizen-user-guide` 与 `netizen-lark` 两个受管 Skill 共同参与既有安装事务，失败时恢复
每个目录安装前的存在性与完整内容；其他用户 Skill 不受影响。
数据库与 Skill 恢复仍须满足 manager target 已卸载且 lifetime lock 已释放。
卸载保留应用凭据目录，和其他用户配置一样不递归删除。

此选择承担一小块官方 CLI 文件格式兼容责任，换取唯一凭据来源与无脚本的 Skill 接入。
不选择生成第二份长期 CLI 凭据副本，也不要求 Netizen 安装、调用 CLI 或管理它的 token
缓存。初始化、重绑定及安装失败恢复需要行为测试；浏览器及真实飞书边界按
[部署门禁](../deployment.md#浏览器安装路径验收) 验证，本 ADR 不声明这些 live 门禁已通过。
