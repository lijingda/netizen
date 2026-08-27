---
status: accepted
date: 2026-08-24
amends: 0033
amended_by: 0044, 0045
related: 0032, 0034
---

# 激活前校验飞书租户权限并复用官方已有应用选择

> 2026-08-27 权限契约更正：飞书 reaction create/delete API 接受 `im:message` 或更窄的
> `im:message.reactions:write_only`。当前契约已经独立要求 `im:message`，因此后者不再作为
> 另一项必需权限请求或校验；以下清单保留 2026-08-24 决策形成时的历史记录。

## 背景

ADR 0033 使用官方 `register_app` 配置应用，但全新骨架主动传入
`create_only=true`，隐藏了官方页面的“选择已有应用”入口。安装器声明的 tenant scope
契约还缺少单聊事件投递所需的 `im:message.p2p_msg:readonly`，以及设置卡片识别会话类型
所需的 `im:chat:readonly`。

`register_app(addons=...)` 只把权限带到平台确认流程；已有完整凭据的安装不会进入该流程，
取得 App ID/Secret 或服务进入 ready 也不能证明租户已经授权、应用已经安装并发布。官方
Application v6 `scope.list` 可用当前应用的 tenant access token 查询每个 scope 的租户授权
状态，不需要建立用户 OAuth 或依赖 Lark CLI。

## 决定

唯一的 `REQUIRED_TENANT_SCOPES` 同时驱动注册 addons 和安装期授权校验。契约包含：

- `im:message`
- `im:message.group_msg`
- `im:message.p2p_msg:readonly`
- `im:chat:readonly`
- `im:chat.members:read`
- `im:message.reactions:write_only`
- `im:resource`
- `im:message:send_as_bot`

全新骨架调用 `register_app` 时不传 `create_only` 或 `app_id`，由飞书/Lark 官方页面提供
创建新应用或选择已有应用。配置已经含有 exact App ID，且 Secret 文件存在但内容为空时仍
传 `app_id`，结果必须保持同一应用身份。有效 App ID 对应的 Secret 文件不存在则是用户
显式请求重置 Feishu App Binding：安装器保持文件不存在以跨越无 TTY 重试，交互流程不传
旧 App ID，并允许官方页面返回的同一或不同应用身份以带回滚的配置/凭据更新替换旧绑定。
Netizen 不读取应用列表、不实现选择器，也不保存用户 token。

候选 release 使用固定的官方 SDK 和受保护的 App Secret 文件调用
`GET /open-apis/application/v6/scopes`。所有契约 scope 都必须存在、`scope_type=tenant` 且
`grant_status=1`；缺失、未授权、应用未安装、请求失败或响应不可验证都 fail closed。校验
发生在 service host 准备和 `activate_release()` 之前，因此失败不停止旧服务、不切换
`current`、不发布新服务定义。

已有完整凭据的交互安装发现缺失 scope 时，只调用一次
`register_app(app_id=<exact App ID>)` 官方浏览器修复流程，然后重新查询一次。刚完成首次
凭据初始化的同一轮安装不重复打开浏览器。无 TTY 调用不启动 device flow；它直接列出缺失
scope，要求完成管理员审批、应用发布与租户安装后重新运行 `./install.sh`。二次查询仍未
通过时同样退出，不轮询等待。

安装器不自动调用 `scope.apply`：该接口存在按应用版本计数的申请上限和重复申请语义，自动
重试会制造审批噪声。可用范围、机器人入群以及真实消息/卡片验收仍是外部完成项；scope
门禁不冒充完整的飞书侧部署证明。

## 后果

新用户可以在同一个官方页面创建或复用应用，升级也不会因完整凭据而跳过权限契约验收。
Agent/CI 始终有界且不阻塞；交互用户只有在确有缺失权限且本轮没有刚做初始化时才看到一次
修复流程。新增权限或运行时能力时必须先更新唯一契约和聚焦测试，安装器才能在激活前发现
未发布的权限变化。App ID 改变会进入新的 Binding Scope namespace；旧 Channel 记录和
原生 Codex 历史保留，但不自动迁移。

## 否决的方案

- 自建已有应用列表和选择器：重复飞书官方能力，并引入用户身份与应用枚举边界。
- 只删除 `create_only` 而不校验授权：改善首次 UX，但无法保护手工凭据和升级。
- 以真实消息、群或卡片做权限探针：依赖现场资源，且无法可靠证明单聊事件投递权限。
- 自动轮询审批或反复调用 `scope.apply`：增加等待、状态和审批噪声，收益不足。
