---
status: accepted
date: 2026-09-07
amends: 0027
related: 0047, 0053, 0055, 0056
---

# 文件卡片统一使用页码选择与跳转

ADR 0027 用一个循环按钮避免重复完整 manifest，但回看上一页需要绕过其余页面。
Files 模块的所有多页卡片统一使用页码选择表单和一个“跳转”按钮，允许直接回看任意页，
同时只在一个提交按钮中携带完整 manifest，保留自包含回调和服务重启后的可用性。
本决定只替换 ADR 0027 的导航取舍，文件来源、当前内容发送、统计和准入边界不变。

多页卡片使用根级 `form` 容器中的 `select_static` 与唯一的“跳转”提交按钮，单页卡片
不显示导航。页码选项只携带从零开始的页码字符串，完整 manifest 只放在提交按钮的
callback value 中；提交时从公开 `CardAction.form_value` 读取所选页码。
固定 `lark-channel-sdk==1.4.0` 不保留独立 select callback 的 `option`，而表单提交的
`value` 与 `form_value` 可由公开接口同时取得，因此不新增 SDK 版本、私有适配或 raw 回调
恢复。该组合依据飞书公开[表单容器文档](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/containers/form-container)。
服务端只接受清单页数范围内的规范页码，缺失或畸形值明确失败，不静默跳转。

保持每页 8 个、最多 400 个完整文件，不截断清单。按当前固定 SDK 实际发送的完整
Card 2.0 JSON 计算 UTF-8 bytes，逐页验证，计入其他回复卡模块、可见行、回调数据及
transport nonce。任一页面超过 55,000 bytes 时，明确提示平台边界并省略整个 Files 模块；
不能只检查首页、文件条数或 manifest 本身，400 是数量上限，不是容量保证。不使用压缩、
进程缓存、数据库清单、下载服务或新的卡片 session，也不维护容量驱动的导航模式选择。

新 v4/v5 PAGE callback 固定携带 `pagination: "select"`，要求从 `form_value` 取得
目标页。旧的无标记 PAGE callback 仍按旧按钮携带的页码和当前 manifest 解码，重绘时
统一生成页码选择表单。该标记仅用于回调解码，不增加领域模型字段、导航状态或 Channel
SQLite 记录；SEND callback 不需要该标记。v4/v5 action version、短字段成对 `a/d`、
完整 Goal/Activity/Result 恢复和 per-render nonce 语义保持不变。

## 验证边界

本地门禁覆盖单页无导航、首尾与中间页跳转、实际 SDK UTF-8 序列化与逐页容量、长路径与
大回复模块、401 拒绝、超限时省略 Files、页码验证、旧 PAGE 解码、重启后完整模块和
统计恢复，以及刷新后可再次跳转。表单 shape 必须经过固定 SDK 公开 CardAction
归一化验证，不能只构造应用层事件。

真实目标应用还须验证 Card 2.0 create/update、不同清单规模的页码选择及提交的真实回调、
任意页跳转和重启后的同卡导航；单元测试与合成回调不能替代真实表单
点击。该 live gate 的步骤与结果按 `docs/deployment.md` 记录；本 ADR 不声明它已经通过。
