---
status: accepted
date: 2026-08-19
amended_by: 0027, 0047, 0053
related: 0008, 0015, 0016, 0021, 0025
---

# 从普通 Turn 终态卡片按需发送结构化本轮文件

> 修订说明：[ADR 0027](0027-use-turn-diff-and-self-contained-file-cards.md) 将文件来源改为
> Turn diff 优先、结构化 items 补充，并让新卡片通过 callback 自带 manifest 独立于
> Binding、Project 和 Turn history。下文依赖 history 重读与 opaque file ref 的 v3
> 回调语义仅保留为历史背景。
>
> [ADR 0047](0047-compose-typed-reply-cards-and-finalize-complete-goals.md) 允许 Goal exact
> 最终物理 Turn 的 completed 文件进入 Files 模块；Goal + Files 使用 v5 完整 Reply Card
> manifest，普通完成/进度文件卡继续使用 v4。

## 背景

Codex App 可以直接打开本地工作区文件；飞书 Channel 运行在另一台服务主机时，回复一个
绝对路径既不能预览也不能下载。把每轮工作区整体上传到聊天同样不可接受：一次代码修改
可能涉及数十或数百个文件，源码、配置和密钥也不应被自动复制进聊天历史。

公开 Codex Python SDK 的普通持久 Turn history 已保留结构化 `fileChange` 和
`imageGeneration` item。它们能够提供“这个 exact Turn 报告过哪些路径”的原生事实，
但不是内容快照；文件在 Turn 完成后仍可能被其他会话修改或删除。飞书 Card 2.0 支持
callback 和更新原卡片，公开消息 API 支持以 `reply_in_thread=True` 回复指定消息并固定
`reply_target_gone="fail"`。

## 决定

### 来源与范围

1. V1 只为成功完成的普通持久 Binding Turn 展示本轮文件。Goal、Side、compaction、失败
   和中断终态保持原行为。
2. 文件只来自该 Turn 的公开结构化 items：completed `fileChange` 的 add/update（move 使用
   新路径）以及 completed `imageGeneration.saved_path`。delete、未完成 item 和重复路径
   被忽略。
3. 不解析最终回复中的 Markdown 链接或服务器路径，不扫描 Project，不补抓 shell、MCP 或
   第三方工具未写入上述 item 的输出。固定 SDK 的公开 item shape 和 exact history 重读
   都是候选门禁；门禁失效时能力关闭，不能退回文本解析或私有 RPC。

### 终态卡与无状态分页

1. 若没有当前可用的本轮文件，继续发送现有纯文本最终回复。若存在文件，只发送一张完整
   Card 2.0：上方为原最终回复，下方为“本轮文件”，两部分属于同一条消息。卡片发送或
   构建失败时回退纯文本，不能丢失最终答案。
2. 每页固定显示 8 个文件、总数和页码；使用卡片列和按钮，不使用 Markdown 表格、不静默
   截断，也不提供“发送全部”。翻页只更新原卡片。
3. 卡片的本轮文件区只显示脱敏逻辑位置和点击当下读取到的大小：Project 内文件使用相对
   路径，Project 外原生生成图使用 `生成图片/<文件名>`，账号 home 内其他文件使用
   `~/...`，其余位置只显示有界路径尾部。callback payload 只携带版本、Scope、Binding
   ID、Turn ID、页码或 opaque file ref；不携带绝对路径。
4. 不新增 card session、Turn 副本、文件清单、内容、摘要或快照。翻页和发送回调都按卡片
   中的 exact `binding_id + turn_id`，通过公开 `thread_resume()` 与
   `thread.read(include_turns=True)` 重新读取唯一 completed Turn，再重新提取文件。它不
   要求该 Binding 仍是 Scope 的 active pointer，也不写 Channel SQLite。

### 文件边界与话题发送

1. 每次渲染或点击都重新 canonicalize Project 与目标路径。Project 只作为相对 item 路径
   的解析基准，不是文件授权边界；exact structured item 报告的 absolute 或 `..` 路径也
   可用。目标必须存在且是普通文件；缺失、目录和设备文件不可展示或发送。同一 canonical
   路径稳定去重，Project 外 opaque ref 绑定 canonical 目标摘要，路径重绑后旧按钮失效。
2. PNG、JPEG、GIF、WebP 使用 `OutboundImage` 并显示“发送原图到话题”；其他文件使用
   `OutboundFile` 并显示“发送文件到话题”。V1 不做图片、Markdown、文本或 diff 预览。
3. 点击发送时只信任重新匹配得到的服务端路径，并对 callback source card 调用公开
   `send()`：`reply_to=card_message_id`、`reply_in_thread=True`、
   `reply_target_gone="fail"`。平面卡片由此成为话题锚点；卡片已经在话题中时，文件必须
   留在原 topic。响应必须确认 exact chat/thread 和非空 root/parent 关系；普通回复树与
   既有话题可能按飞书规则保留更早的 root，不能把 `root_id` 等同于 callback source。
   目标消失或关系异常时不能落到主聊天。
4. 每个 sender、卡片、Binding、Turn、动作和文件引用产生确定性发送 UUID；重复 callback
   重用同一 UUID。Netizen 不为此写去重记录。文件发送失败时尽力在同一卡片话题回复简短
   错误，原终态卡保持不变，不替换成通用错误卡。

## 后果

飞书用户可以从最终回复直接选择所需文件，并使用飞书的图片/文件预览与下载能力；大量
文件仍有完整分页，但只有明确点击的文件进入聊天历史。源码、配置和密钥不会因“属于本轮
文件”而自动上传，所有点击者仍受飞书应用可用范围与聊天成员边界控制。

“本轮文件”只表示 exact Turn 报告且点击时当前仍可访问的文件。Project containment 不
是另一层 ACL，访问权限继续由 Codex 原生 sandbox/approval 决定。Netizen 不检测 Turn 后
修改，因此发送内容不能被描述为“Turn 完成时版本”。如果未来需要 shell/MCP 产物、不可变
快照、批量发送或预览，必须另做来源、权限、存储成本和泄密风险设计；不得悄悄扩展本 ADR
的无持久化边界。

发布门禁包括结构化 item shape、exact completed Turn 重读、Project 外文件与原生
`generated_images` 路径、canonical 重绑、分页和 payload 边界、inactive Binding、重复
callback、Card 2.0 限额、图片/文件消息类型以及飞书真实
平面消息转话题、既有话题、删除卡片、P2P 230071 行为。FakeChannel 只能验证本地参数和
响应校验，不能替代目标应用的真实飞书关系验收。
