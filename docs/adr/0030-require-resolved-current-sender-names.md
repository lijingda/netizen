---
status: accepted
date: 2026-08-20
amends: 0029
related: 0011, 0021
---

# 要求当前 Prompt 消息有可解析的发送者姓名

## 背景

ADR 0029 直接投影 Channel SDK 的公开 `Identity`，并在 `display_name` 缺失时写入
“未知发送者”。真实环境证明这不是可靠语义：飞书入站事件稳定提供发送者 ID，但固定
`lark-channel-sdk==1.2.0` 的通用 identity lookup 可能只返回 ID；同一个 SDK 的公开群成员
接口在应用具备成员读取权限后可以从 exact `open_id` 得到姓名。占位符让 Codex 看见一个
貌似真实、实际由 Netizen 编造的显示名，也掩盖了应用权限或发布状态问题。

## 决定

1. `FeishuChannel` 开启公开的 `resolve_sender_names` 配置。SDK 在消息通过自身安全准入后，
   使用有缓存的 chat member roster 补全当前 `InboundMessage.sender.display_name`；Netizen
   不自写 OpenAPI client、不读取 SDK 私有状态，也不增加第二份姓名缓存。
2. 每个 Current Prompt Message 必须同时具备 exact app-scoped `open_id` 和非空真实
   `display_name`。sender attribution 只投影 `display_name`、`open_id`、`is_bot` 与
   `sender_type`；不投影用于跨应用关联的 `union_id` 或租户级 `user_id`。普通 idle Turn、
   running Steer、Side 首轮和 Side 后续使用同一检查；`text`、`image`、`post` 以及是否带
   逐条引用都不改变要求。
3. 当前姓名缺失时在引用回查和图片下载前 fail closed，不调用 Codex start/steer，也不写
   “未知发送者”。用户收到明确错误，要求为飞书应用开通最小权限
   `im:chat.members:read`、发布应用版本后重试。`/side <问题>` 此时可能已经创建了新话题，
   但首轮 Prompt 不执行，错误同时显示在 Side 卡片和问题 seed 下；用户可在该 Side 重发。
4. Slash/card Control 不进入 Current Prompt Message，仍不要求解析显示名。发送者姓名只作
   attribution，不改变 Feishu admission、Scope 共享控制权、Turn owner、approval、sandbox、
   工具权限或指令优先级。
5. 逐条引用的历史消息继续按 ADR 0011 使用 SDK 回查时的公开身份字段，但 sender 使用与
   Current Prompt Message 相同的最小字段。当前 chat roster
   不能证明已离群用户在历史消息发送时的显示名，因此 Netizen 不用当前 roster 反向补写；
   回查缺名时保留真实 ID，但不再生成“未知发送者”字段。这不放宽本 ADR 对当前消息的严格
   要求。引用中的 mention、`share_user` 等消息内容字段不属于 sender attribution，不因本
   决定被机械删除。

## 后果与验证

部署前必须在飞书开发者后台授予 `im:chat.members:read`，并把权限随应用版本发布。权限
缺失、成员接口不可用或 SDK 仍无法给当前 sender 补名时，用户请求会明确失败；这是选择的
可用性代价，不得降级为匿名 Prompt。

自动化测试固定 Channel 配置、缺名投影错误，以及普通 Turn、running Steer、Side 首轮和
Side 后续的零提交语义。真实发布验收要由至少两名成员分别发送普通消息和 steer，确认
Codex 原生输入显示各自姓名；临时撤销成员读取权限时，同样的消息必须只收到权限提示且不
产生 native Turn/steer。
