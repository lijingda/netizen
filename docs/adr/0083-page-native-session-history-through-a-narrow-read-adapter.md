---
status: accepted
date: 2026-10-17
amends: 0014, 0081
amended_by: 0096
related: 0077
---

# 原生会话历史采用只读分页适配

协作 `read_thread` 和 `wait_threads` 通过完整 Thread 历史获取少量结果，会使长会话的
每次查询成本持续增长。固定 Python SDK 0.163.0 的公开 facade 尚未提供历史分页，
但同版本 App Server 与安装包 generated models 已支持 `thread/turns/list` 和
`thread/items/list`。采用这两个固定方法的薄适配，不建立本地历史或第二客户端。

> [ADR 0096](0096-read-admin-conversations-through-summary-turn-pages.md) 将下文 Admin 最近回答扩展为 Summary Turn 分页对话记录，
> 保留最新轮图片规则与本适配的迁移条件。下文保留原始决定。

## 决定

`ThreadHistory` 只暴露上述两类只读分页，使用同一 initialized `AsyncCodex`、安装 SDK
的请求/响应类型与 opaque cursor。调用与取消沿用现有 SDK 的 request 生命周期，
不补通用 RPC gateway、不修改 SDK 内部、不消费通知，也不把任意失败转为完整历史读取。

`read_thread` 先读无历史的 metadata，再读原生 Full Turn 页；`wait_threads` 只读
metadata、最新一个 Summary Turn 和至多二十个最近 item。原生 legacy history 不支持
items 分页时，根据 metadata 的 history mode 选择最新一个 Full Turn；仍不读取全 Thread。
输出预算可以裁剪单页内容，不能丢弃整轮后返回已越过这些轮次的原生游标。
普通执行、Goal 终态与 Files 的既有精确证据读取不在本决定范围内。

2026-11-03 扩展同一只读适配的用途：Admin 的“最近回答”读取最新一个 Full Turn
（`limit=1, items_view="full"`），仅投影该轮的公开最终回答、状态及实际存在的时间。
运行中只显示状态；无最终回答不回溯更早轮次，不用 wait 的尾部二十个 item 推断无回答。
归档会话直接读取，不 resume/unarchive，不在列表逐条预取。精确身份检查、请求期限与
在途 worker 限制沿用管理边界；不增加历史缓存、完整聊天浏览器或新 RPC 方法。

2026-11-04 扩展 Admin 最近回答的图片投影：只展示 completed Turn 最终 Markdown
图片节点明确引用、且与该轮唯一 completed `imageGeneration.savedPath` 匹配的原生
`result` PNG 字节。固定 SDK/原生生成工具将同一份 PNG Base64 写入 `result` 和保存文件；
分页历史保留该结果。路径仅作词法匹配，不读取文件、不要求文件仍存在，也不将继承历史
路径中的 Thread ID 误认为当前分支身份。重复路径候选视为歧义；其他工具、未引用的图片、
本地/远程资源、飞书 image key 均不成为图片来源，不新增图片列表或历史查询。

同一受限预览执行器完成校验、Markdown 渲染及 JSON 编码。按实际输出节点限制十六张、
单图 10 MiB、累计解码字节 20 MiB、累计 3200 万像素（重复引用也计入像素预算）、
单边 16384 像素，含图片响应限 32 MiB；超限或不可用保留文字。压缩字节与解码像素
分别约束传输和展示成本；这些参数只用于最近回答，不改变 Project 文件预览预算。
原生 Full Turn 读取成本不受这些输出预算限制。
浏览器仅为所用图片创建 Blob URL，替换、关闭和认证失效时释放；刷新失败保留旧快照，
迟到响应不创建资源，快照不保留 Base64。没有后台图片预取、定时刷新、持久缓存或数据库
图片记录；本地文件预览的授权边界保持独立。

模型的未归档、归档发现均使用 `cursor`/`nextCursor`，按原生更新时间降序逐页查询。
它们与 Admin、飞书列表共享按需分页核心：每批原生 ID 仅查询本实例匹配 Binding，
过滤后填满当前页即停止，保留原生页内 offset，避免跳过批次尾部。模型决定是否继续。
Scope、Project、权限和搜索等过滤不通过预加载全部 Binding 或两份目录完成；稀疏过滤
仍可能读取多页，受已有请求时限约束。Lazy Binding 独立使用本地分页，不因列表查询物化。

跨会话发送取消归档全目录预检，直接使用既有 exact resume/提交链的原生结果。
已验证 `ThreadArchived` 返回已归档；泛化的 `thread not found` 明确拒绝不能被猜成归档。
不自动恢复、不重发、不增加目录对账。单会话标题按已知 ID 精读；本地完整 ID 精确查询、
短前缀至多取两条判歧义。Admin Projects 只展示本地统计，不再计算归档数和待确认数。

## 验证与移除条件

适配必须通过安装包 generated types/client shape、synthetic 分页/错误/游标测试与真实
bundled App Server 的 paginated/legacy 原生探针。公开 facade 出现等价能力时，migration
sentinel 要求迁移到公开 provider 并删除该适配；SDK 升级时重新核验 history mode 和两个
固定端点，不把新版本的协议模型复制到旧 SDK。飞书卡片展示另行验收，原生 mock provider
探针不代表真实模型自主工具调用或飞书客户端已通过。
