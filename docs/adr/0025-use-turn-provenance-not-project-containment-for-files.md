---
status: accepted
date: 2026-08-19
amended_by: 0027
related: 0015, 0024
---

# 本轮文件使用 Turn provenance，而不是 Project containment

> 修订说明：[ADR 0027](0027-use-turn-diff-and-self-contained-file-cards.md) 保留 Project
> 不是文件授权边界的决定，但新卡 callback 改为携带 canonical absolute path，并发送点击时
> 该路径当前解析到的内容，不再使用 opaque ref 检测路径重绑。下文对应旧卡的该部分语义仅
> 保留为历史背景。

生产实测表明，原生 `imageGeneration.saved_path` 位于标准
`$CODEX_HOME/generated_images/<thread-id>`，而普通任务也可能在 Codex 原生权限允许时
修改 Project cwd 之外的文件。Project 在 Netizen 中是 Turn cwd 和相对路径解析基准，
不是文件 ACL；用 containment 过滤 exact structured item 会把已获授权且真实完成的文件
误判为不可用。

因此，本轮文件准入只要求路径来自 exact completed Turn 的受支持结构化 item，并在渲染
或点击时 canonicalize 为当前存在的普通文件。相对路径仍以 Project 解析，absolute 和
`..` 路径不因离开 Project 而被拒绝；原生 Codex sandbox/approval 继续是文件访问权限
边界。Netizen 仍不扫描目录、不解析最终回复，也不接受 callback 提供路径。

卡片不显示服务器绝对路径：Project 内文件显示相对路径，Project 外原生生成图显示
`生成图片/<文件名>`，账号 home 内其他文件显示 `~/...`，其余位置只显示有界路径尾部。
Project 外 opaque ref 绑定 canonical 目标的摘要；如果软链接或同一路径随后重绑到另一
目标，旧按钮重新提取后无法匹配。这个决定允许用户按需发送 Codex 已报告的跨目录文件，
但不会把未进入 Turn item 的 shell、MCP 或第三方输出纳入卡片。
