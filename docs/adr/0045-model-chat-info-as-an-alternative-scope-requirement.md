---
status: accepted
date: 2026-08-26
amends: 0033, 0035
related: 0044
---

# 将群信息权限建模为等价 scope 能力

## 背景

ADR 0035 把 `im:chat:readonly` 作为设置卡片识别会话类型所需的精确 tenant scope。
飞书官方“获取群信息”接口实际允许
[`im:chat`、`im:chat:read`、`im:chat:readonly` 三者任一](https://open.feishu.cn/document/server-docs/group/chat/get-2.md)。
`card.action.trigger` 回调本身[不要求 scope](https://open.feishu.cn/document/feishu-cards/card-callback-communication.md)；
Netizen 只是在回调没有携带会话类型时，使用 Channel SDK 的公开 `get_chat_info()` 调用该
接口区分群聊和单聊。

生产应用的官方 `GET /open-apis/application/v6/scopes` 结果显示 tenant
`im:chat:read` 已授权，而 `im:chat` 与 `im:chat:readonly` 不存在。安装器仍要求 exact
`im:chat:readonly`，把一个已经满足的接口能力误报为缺失；官方 exact-App 配置页完成后也
无法让这个错误的精确门禁通过。

## 决定

1. 受管注册 addons 以当前可申请的最小读权限 `im:chat:read` 作为群信息能力的 canonical
   scope，不再主动请求 `im:chat:readonly` 或包含写能力的 `im:chat`。
2. 有效权限校验把 canonical `im:chat:read` 映射为
   `{im:chat, im:chat:read, im:chat:readonly}` 等价集合。只要其中任一项以 tenant scope 且
   `grant_status=1` 返回，群信息能力即通过；三项都未授权时，机器输出仍只报告 canonical
   `im:chat:read`，保持缺失列表有序且无重复。
3. 其余权限继续逐项精确校验。平台响应失败、字段无效、等价项只有 user scope、或所有等价
   项都未授权时仍 fail closed，并保持 ADR 0035/0044 的 activation 与回滚边界。
4. 不移除群信息能力。设置卡片需要从 `open_chat_id` 恢复公开 ScopeKind，而官方回调没有
   提供可替代的群聊/单聊类型字段。

## 后果

已经具有 `im:chat:read` 的应用可以直接通过安装门禁，不再看到无意义的修复链接；仍持有
旧 `im:chat:readonly` 或较宽 `im:chat` 的应用也无需降权后才能升级。新建或修复应用只请求
canonical 最小权限。以后遇到一个 API 的 N 选一权限时，安装器必须建模接口能力与等价 scope，
不得把文档列出的任意一个别名提升为唯一必选项。

## 否决的方案

- 完全移除群信息权限：卡片回调缺少会话类型，无法安全区分私聊与群聊设置。
- 只把字符串替换为 `im:chat:read`：会错误拒绝已经持有官方另外两个等价权限的应用。
- 接受任意 `im:chat*` scope：会把无关或 user-only 权限误判为 tenant 接口能力。
