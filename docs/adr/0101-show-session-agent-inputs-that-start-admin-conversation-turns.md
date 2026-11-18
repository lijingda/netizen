---
status: accepted
date: 2026-11-18
amends: 0083, 0096
related: 0081
---

# Admin 对话记录显示开启本轮的会话协作来信

## 背景

ADR 0096 用 Summary Turn 页展示每轮第一条用户输入。会话协作来信（ADR 0081）以
ExternalMessage 进入原生历史，保持工具来源的 `functionCallOutput`，不是 userMessage。
固定 openai-codex 0.169.0 的 Summary 只保留每轮第一条 userMessage。由来信开启、没有
人工追加消息的完成轮只返回最终回答。若有人随后 steer，Summary 会把这次 steer 作为
用户输入返回。两种情况下，开启本轮的来信都只出现在 Full 视图与 `thread/items/list`。

不把来信改为 userMessage：那会把工具级的不可信内容提升为用户指令，也会丢失原生
start/join 与委派来源语义（ADR 0081）。本决定只改变 Admin 的只读展示。

## 决定

按 Summary 页顺序为每个 Turn（包括运行中的 Turn）读取第一条 item。每轮按 Turn ID
调用一次 `thread/items/list`（升序、`limit=1`），一页串行执行，最多十次。

- 首项为 `functionCallOutput` 时，用该首项替换 Summary 输入。Admin 与 `read_thread`
  共用当前来信识别规则。namespace 须为 `codex_tui` 或 `codex_app`，工具名须为
  `create_thread` 或 `send_message_to_thread`，字符串输出须能按 `<codex_delegation>`
  外层解析，且来源 Thread ID 至少含一个非空白字符。识别成功后显示“会话协作来信”、
  完整来源 ID 和完整正文。即使 Summary 含有后来人工追加的 steer，仍只展示开启本轮的来信。
- 正文以 Side 固定来源说明开头时，才按来源 Thread ID 查询本实例的普通 Binding。
  确认没有对应 Binding 时移除固定说明，标注来自临时 Side。查到 Binding 或查询出错
  时按普通来源展示，完整保留正文中的说明。
- 未识别的 `functionCallOutput`（包括旧的 `netizen_sessions` JSON 信封）用
  “外部输入 · 格式未识别”占位，隐藏工具原文。其他首项类型和空列表保留 Summary
  投影，有 userMessage 就展示该输入，没有则留空。
- 每次发起首项读取前检查同一个绝对期限。到期、SDK 返回 `CodexError`、响应模型抛出
  `ValidationError` 或适配器抛出 `ThreadHistoryUnavailable` 时停止本页后续补读。
  已读轮保留投影，未读轮保留 Summary 中的 userMessage，仅在没有 userMessage 时显示
  “〔未能读取本轮首条输入〕”。legacy 的 `MethodNotFoundError`（`-32601`）属于预期降级，
  不记 warning。其他错误或到期只记一次 warning，字段限 Thread ID、Turn ID 和错误类型。

补读沿用本页读取的 exact 身份前后校验、共享在途原生精读名额与十秒外层期限。外层
超时后，已开始的 SDK 读取继续持有名额直至 worker 结束。读取不缓存、不预取，也不写
SQLite。来信正文计入既有页面文本预算（16 MiB 逐轮回退与执行器繁忙时 1 MiB 原文页
门槛）。十轮页回退为单轮时复用本次请求相同 Turn ID 的输入投影，包括空输入与不可用占位。

轮中加入的来信与人工 steer 均不单独展示。首项不可读时，Summary 无法区分用户首发与
首条人工 steer。

ADR 0083 的 `AppServerThreadHistory` 只增加同一固定 `thread/items/list` 端点的升序首项
读取，不新增 RPC 方法、通用 gateway、历史存储或第二客户端。

## 证据与取舍

2026-11-17 至 11-18 使用固定 openai-codex 0.169.0 bundled App Server 与本地 mock provider
实测，不使用真实模型或飞书。测试会话共 30 轮，每三轮有一轮由来信开启，一半轮次含大量
命令输出（原生在历史中把单条命令输出截至约 64 KiB）。作为对照，开发机本地 Codex 会话
历史文件 p99 约 4.5 MB、最大约 5.3 MB。下表为连续 15 次读取的中位数（两次测量取范围）：

| 读取 | 历史 15.8 MB | 历史 51.7 MB |
| --- | --- | --- |
| Summary 十轮 | 3–4 ms / 30 KB | 3–4 ms / 30 KB |
| 单次首项读取 | 1.2–1.5 ms / 约 0.8 KB | 约 1.5 ms / 约 0.8 KB |
| Summary 加每轮首项读取（十次，串行） | 18–21 ms / 37 KB | 19–21 ms / 37 KB |
| Full 十轮 | 50–64 ms / 4.2 MB | 160–215 ms / 13.9 MB |

首项读取的耗时与该轮大小（约 0.8 MB 或 2.6 MB）、轮次新旧和会话总大小无关。整页补读
期间另一个 metadata 读取约 1.2 ms（单独约 1.0 ms），共享事件循环最大停顿约 1.1 ms。
Full 十轮页使事件循环最多停顿约 16 ms（15.8 MB）或 39–55 ms（51.7 MB），并随会话增大。运行中的来信轮同样能读到
该 `functionCallOutput`。legacy history 的 `thread/items/list` 返回 `-32601`（SDK 0.169.0
升级审查与 `scripts/probe_admin_sessions.py` 的 legacy 场景），该模式退回 Summary 输入。

逐轮首项补读使普通用户轮也增加一次小请求（每页约多 15–18 ms），从而能识别后来收到
人工 steer 的来信轮；只补读缺少 userMessage 的轮次会把这种来信轮的 steer 误显示为开启输入。
还考虑过以下方案。

- 改读 Full 页：成本随会话增大，并拖慢 Admin 与飞书消息处理共用的事件循环。
- 只对来信开启的轮次读 Full：`thread/turns/list` 只能按页和 opaque cursor 读取，无法按
  Turn ID 定位单轮。
- 在 Channel 数据库记录来信：违反不保存消息正文与历史副本的数据边界。
- 把来信改为 userMessage：见背景。

“已接收调整”次数未包含加入运行中轮次的来信，属于统计口径，直接更新 design.md，
不在本 ADR 决定。

## 验证与移除条件

实施需通过 `make check`。synthetic 测试覆盖触发条件、各类首项、legacy 与失败降级、
文本预算和单轮回退复用；`scripts/probe_admin_sessions.py` 的 paginated/legacy 原生探针
覆盖 `create_thread` 开启首轮、运行中来信轮、人工追加后的来信轮、逐页读取次数及
`-32601` 降级。原生 Summary 开始保留开启本轮的工具输入，
或公开 facade 提供等价历史分页时，移除补读或按 ADR 0083 迁移；SDK 升级审查需复核
Summary 是否仍缺该输入及首项读取的 shape。
