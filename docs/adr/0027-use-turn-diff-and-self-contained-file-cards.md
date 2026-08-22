---
status: accepted
date: 2026-08-19
amends: 0024, 0025, 0026
related: 0008, 0015, 0021
---

# 以 Turn diff 补全本轮文件，并让新卡片自带文件清单

## 背景

ADR 0024 只从 completed `fileChange` 与 `imageGeneration` item 提取本轮文件，并让
callback 通过 Binding、Turn 和 opaque ref 重读原生 history。生产案例表明，这个来源过
窄：Codex App Server 已公开发送 `turn/diff/updated`，其 `diff` 是 exact Turn 到当前时刻
所有文件变化的最新 aggregate unified diff，但一个成功 Turn 的最终 items 不保证总有
`fileChange`。因此文件真实存在、最终回复也明确链接时，飞书仍可能没有“本轮文件”。

旧回调也把已经发出的卡片错误地依赖于 Netizen 进程和 Codex history。卡片本身已经是飞书
持久消息；把清单写在分页按钮中，就能在服务或 App Server 重启后直接从回调恢复分页，
无需额外 card session、Turn snapshot 或数据库记录。飞书应用可用范围与聊天成员仍是准入
边界，产品决定允许 callback 明文携带服务主机绝对路径；卡片可见正文继续只显示逻辑路径。

## 决定

### 来源和生命周期

1. 普通持久 Turn 在公开 `AsyncTurnHandle.stream()` 上观察 exact
   `turn/diff/updated`。只接受匹配当前 Thread ID 与 Turn ID 的字符串 payload，并反复覆盖
   为最新 aggregate snapshot；不把通知当增量拼接。该值只随内存 completion outcome 交给
   Channel，投递后即丢弃，不写 Channel SQLite 或 Codex history。
2. 持久 `thread.read()` 仍是普通 Turn 终态唯一权威来源。只有曾公开观察到 in-progress 的
   Turn 才在 persisted terminal 后 drain 其已注册 stream；极快完成、无法安全 drain 或
   stream 异常时不猜测 diff，继续使用 completed structured items。
3. 提取顺序为最新 Turn diff 在前，completed `fileChange` add/update/move 与 completed
   `imageGeneration.saved_path` 在后；按当前 canonical path 稳定去重。unified diff 只解析
   file metadata，支持新增、修改、删除排除、rename、binary header、quoted path 和 Git
   UTF-8 octal escaping，从不解释 hunk 正文。
4. 不解析最终回复，不执行 Project/git/worktree 扫描，也不推断没有进入 Turn diff/items 的
   shell、MCP 或第三方产物。Goal、Side、compaction、失败和中断终态保持原行为。

### 自包含 Card 2.0

1. 新终态卡使用本轮文件动作协议 v4。正文仍是一条消息中的最终回复区和本轮文件区；每页
   8 个，不使用 Markdown 表格，不提供预览、diff、发送全部或自动上传。真实目标应用证明
   500 个文件、52,827-byte Card 2.0 能完整 create/update，而 1000 个文件、约 96.9 KB 被
   飞书以 230099/200800 拒绝。因此产品上限为 500 个且编码后最多 55,000 bytes；超过任一
   边界时明确告知，不生成残缺卡片。
2. 每个发送按钮明文携带该条目的 canonical absolute path。每页只显示一个“下一页”，末页
   显示“回到第一页”；这个循环按钮携带完整 `{path, label}` manifest、原最终回答、目标页、
   版本、Scope、Binding ID 和 Turn ID。单按钮避免中间页重复两份 manifest 再次超过平台
   限额，且仍保证所有页面可达。Binding/Turn 只保留 provenance 和幂等 identity。
3. v4 翻页只用 callback 自带的回答和 manifest 重建完整 Card 2.0，再通过公开
   `update_card()` 更新 source message；不读取飞书原卡、Binding、Project、completed Turn，
   也不要求原进程内存仍存在。真实 `fetch_message()` 只返回有损的 `title + elements` 投影，
   不能用于无损恢复回答。每次翻页按 manifest 重新 stat：已不存在、不可访问、目录或特殊
   文件仍占原清单位置并显示“文件当前不可用”，但没有发送按钮。
4. v4 发送直接重新解析按钮中的 absolute path。当前目标必须是普通文件；图片按当前内容
   sniff 为 PNG/JPEG/GIF/WebP 并发送原图，其他内容发送文件。路径可以位于任意 Project
   之外，不加第二层 containment 或凭证。发送的是点击时该路径当前解析到的内容，不是
   Turn 完成时版本，也不检测同一路径是否已被替换或重绑。
5. 文件消息仍以 source card 为 `reply_to`，固定 `reply_in_thread=True` 与
   `reply_target_gone="fail"`，并验证真实 chat/thread/root/parent 关系；失败只在卡片话题
   尽力回复错误，不落入主聊天。重复 v4 发送按 Scope、卡片、sender、Turn 和 path 复用
   deterministic UUID。
6. 本轮文件 callback 只接受 v4。升级前已发送的 v3 opaque-ref 卡片点击时明确提示卡片
   已过期，不再重读 Binding、Project 或 completed Turn history；新卡只发 v4。

## 后果

“本轮文件”与 Codex App 的 changed-files 展示共享更完整的 native diff 事实，同时仍由
structured items 补齐生成图片等路径。已经发送的 v4 卡片把最小恢复信息保存在飞书消息
自身，Netizen 无需维护完成 Turn、文件清单、卡片 session、签名 key 或下载服务；服务重启
不影响翻页和单文件发送。

代价是 absolute path 明文存在于 callback payload，且大 manifest 增加卡片 JSON 体积。
这是飞书应用准入边界内的明确产品取舍，不应被描述为用户可见正文，也不得把它扩展为自动
上传。发布门禁必须覆盖 SDK public stream shape、最新 snapshot、diff parser、items fallback、
v3 expiry、v4 restart pagination、当前文件重检、100/500 passing manifest、1000
rejection、完整卡 update，以及目标飞书应用的真实容量和话题发送行为。
