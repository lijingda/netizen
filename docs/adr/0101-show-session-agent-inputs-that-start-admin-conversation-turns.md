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
固定 openai-codex 0.169.0 实测：由来信开启的轮次，Summary 只返回最终回答，不含任何
输入项；只有 Full 视图与 `thread/items/list` 返回该 `functionCallOutput`。Admin 因此
只看到回答，看不到谁发来了什么。来信加入运行中轮次时与人工 steer 相同，Summary 都不可见。

不把来信改为 userMessage：那会把工具级的不可信内容提升为用户指令，也会丢失原生
start/join 与委派来源语义（ADR 0081）。本决定只改变 Admin 的只读展示。

## 决定

Summary 页中没有 userMessage 项的轮次，按 Turn ID 调用一次 `thread/items/list`
（升序、`limit=1`）读取该轮第一条 item，按结果分别处理：

- `functionCallOutput` 且整体符合当前委派信封（与 `read_thread` 相同的 `codex_tui`、
  `codex_app` namespace 与 `<codex_delegation>` 外层）：展示为“会话协作来信”，返回
  来源 Codex 会话 ID 和完整正文。正文以 Side 固定来源说明开头时去掉说明，标为来自
  临时 Side。不展示其他工具字段。
- `functionCallOutput` 但格式未识别，包括 2026-10-16 前的 `netizen_sessions` JSON 信封：
  只显示“外部输入，格式未识别”的占位说明，不返回原文，因为 Admin 不展示工具输出
  （ADR 0096）。不为旧信封增加兼容解析。
- 其他类型，例如 Goal 续轮或压缩等由原生开启的轮次：不显示输入，与现状相同。
  不把 reasoning、commentary 或其他 item 当作输入；首项为 userMessage 时也保持现状，
  用户输入仍只以 Summary 为准。返回空列表时同样不显示。
- 原生不支持 items 分页（legacy history 返回 `-32601`）、读取失败或返回的 item 不属于
  该 Turn：该轮显示“未能读取本轮首条输入”的占位说明，并停止本页其余补读，剩余轮次
  显示同一说明。记录不含正文的 warning，不使已成功的 Summary 页失败。

补读沿用本页读取的 exact 身份前后校验、共享在途原生精读名额与十秒外层期限，按顺序
执行，每页最多十次；不并发、不缓存、不预取，也不写 SQLite。运行中轮同样补读，原生
在轮次运行时即可返回该首条 item。来信正文计入既有页面文本预算（16 MiB 逐轮回退与
执行器繁忙时 1 MiB 原文页门槛）；十轮页回退为单轮时复用本次请求已取得的补读结果，
不重复读取。

轮中加入的来信与人工 steer 继续不显示：两者都只能通过 Full 页取得，成本见下文。

ADR 0083 的 `AppServerThreadHistory` 只增加同一固定 `thread/items/list` 端点的升序首项
读取，不新增 RPC 方法、通用 gateway、历史存储或第二客户端。

## 证据与取舍

2026-11-17 使用固定 openai-codex 0.169.0 bundled App Server 与本地 mock provider 实测，
不使用真实模型或飞书。测试会话共 30 轮，每三轮有一轮由来信开启，一半轮次含大量命令
输出（原生在历史中把单条命令输出截至约 64 KiB）。作为对照，开发机本地 Codex 会话历史
文件 p99 约 4.5 MB、最大约 5.3 MB。下表为连续 15 次读取的中位数：

| 读取 | 历史 15.8 MB | 历史 51.7 MB |
| --- | --- | --- |
| Summary 十轮 | 3 ms / 30 KB | 3–4 ms / 30 KB |
| 单次首项读取 | 1.2–1.4 ms / 约 0.8 KB | 约 1.5 ms / 约 0.8 KB |
| Summary 加 3–4 次首项读取 | 7–9 ms | 8–12 ms |
| Full 十轮 | 50 ms / 4.2 MB | 166–213 ms / 13.9 MB |

首项读取的耗时与该轮大小（约 0.8 MB 或 2.6 MB）、轮次新旧和会话总大小无关；读取期间
另一个 metadata 读取仍约 1.1 ms，共享事件循环最大停顿约 1 ms。Full 十轮页使事件循环
最多停顿 15 ms（15.8 MB）或 39 ms（51.7 MB），并随会话增大。运行中的来信轮同样能读到
该 `functionCallOutput`。legacy history 的 `thread/items/list` 返回 `-32601`
（[SDK 0.169.0 升级审查](../research/codex-sdk-0.169.0-upgrade-review.md)），只能显示占位说明。

采用逐轮首项补读：普通用户轮没有额外请求，只有缺少输入的轮次多一次小请求。放弃的方案：

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
覆盖来信开启轮次的首项读取与 `-32601` 降级。原生 Summary 开始保留开启本轮的工具输入，
或公开 facade 提供等价历史分页时，移除补读或按 ADR 0083 迁移；SDK 升级审查需复核
Summary 是否仍缺该输入及首项读取的 shape。
