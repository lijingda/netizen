---
status: accepted
date: 2026-10-11
amends: 0031, 0062, 0066, 0074, 0076
---

# 用飞书身份登录并维护实例管理员

Admin 不再以共享 secret 代表一个运维身份。复用当前实例的飞书应用，通过 Device Flow
确认用户，以 `App ID + Open ID` 匹配私有管理员名单；管理员权力相同，不引入角色、
Project ACL 或 Channel 准入规则。选择 Device Flow 是为了不要求每个内网/动态地址实例
配置 OAuth 回调。删除旧 secret 的生成、读取和登录依赖，不保留认证提供者框架或回退。

## 身份、凭证与会话

名单保存到实例私有 `state/admin-users.json`，当前用户凭证保存到独立私有
`credentials/admin-user-tokens.json`，均不进入 Channel SQLite 或应用 profile。在线只有
同一服务 writer；离线 setup 仍受实例维护/lifetime 锁保护。文件原子替换，坏文件不视为空。
名单 revision 只随授权增删变化；姓名头像只是展示快照，不参与身份或授权版本判断。

每位当前应用管理员共享一份当前 UAT/RT；只有可信登录身份通过名单复核才允许保存。
按用户串行刷新和提交，不并发消费同一 RT；独立且仍有效的登录按实际提交顺序覆盖，
不按浏览器或登录发起时间排序。已取消的登录、删除或重添前的结果不得恢复授权，刷新
写回仍须对应当前凭证和成员代次。仅在调用飞书时检查和刷新，不设置后台续期，也不区分
用户操作与轮询。刷新失败按凭证过期撤销当前
请求的浏览器会话；普通接口异常按普通异常处理。本人资料在整页加载时查询，名单展示
本地快照，不为名单申请通讯录查询权限。

浏览器继续使用内存 opaque session、来源/容量限制、同源校验与一次性操作授权。
无额外 session TTL；重启丢失 session，保留用户凭证。磁盘 token 不证明匿名浏览器身份。
普通退出只撤销本浏览器，不删除共享凭证；删除管理员先撤销资格及所有相关 session/
grants，再清理凭证，清理失败不恢复资格。重新添加不复活旧会话或在途结果。已经受理
的管理操作不因此取消。不新增浏览器协调、撤销推送或历史恢复状态机。

## 绑定、维护与受信依赖

Admin 为必备能力，不再有 enabled 配置。显式 setup 在原有应用绑定流程中补齐当前应用
首管理员，支持 Agent 转交有界浏览器授权 URL，也支持本机显式填写 Open ID。已有名单
不因安装/升级操作者改变。注册流程返回的身份未经同应用命名空间验证不得直接授予权限；
必要时在同一 setup 中对已绑定的准确应用完成用户身份确认。后台启动、自动恢复和 Admin
重启只检查，不等待扫码。无管理员时提示运行 setup；不为未正式推广的旧认证/配置/服务
模板建立兼容层。旧 secret 文件不自动删除，精确 purge 清单可保留它。

系统管理 → 权限管理中使用飞书官方 Selector。安装统一开通 user 类型
`component:selector`、`offline_access`，登录统一申请相应 scope；应用开通不替代用户
授权，不承诺免确认或刷新绝不通知。SDK 固定官方版本，作为受信同源前端依赖按需加载，
只放开实际需要的 CSP 能力和来源。用户明确接受该依赖具有同源脚本权限；不再要求严格
iframe 隔离，不增加部署入口、通用代理或替代搜索权限。候选结果不是身份认证，必须由
当前管理员确认、后端复核资格和 revision 后添加；可删除自己但不能删最后一人。
HTTP 仍仅用于受信内网，不宣称第三方脚本零风险或适合公开不受信网络。

## 飞书协议适配边界与移除条件

固定 `lark-oapi==1.7.2` 的 DeviceFlowClient 当前端点与请求编码不能完成已验证的飞书
设备授权；不补丁 SDK、不增加 CLI 运行时依赖。允许一个仅服务上述用途的 typed HTTP
适配器，采用官方 CLI 已公开且 demo 验证的设备授权/token 协议及公开 user_info、刷新、
组件 ticket 接口。固定飞书 HTTPS 端点，禁止重定向与任意 URL 请求，有界超时及轮询，
`slow_down` 至少增加五秒；不记录原始 token、device code、Secret 或上游错误正文。
当固定官方 SDK 提供经验证等价的公开能力时，用 SDK 替换对应 HTTP 方法并删除例外。
此边界不授权内部 selector gateway、通用 Feishu API 转发或另一长驻服务。

协议来源：[官方 CLI Device Flow](https://github.com/larksuite/cli/blob/main/internal/auth/device_flow.go)、
[官方 CLI 刷新](https://github.com/larksuite/cli/blob/main/internal/auth/uat_client.go)、
[组件鉴权](https://open.feishu.cn/document/common-capabilities/web-components/component-sdk-authentication-process)。

验收包括浏览器绑定/取消/过期、非管理员不落 token、刷新/重新登录/删除竞态、重启、
最后一人保护、安装与无人值守边界，以及真实飞书账号/客户端回归。合成测试不能替代
平台实测；已通过的临时 demo 也不等于正式版本全部验收通过。
