---
status: accepted
date: 2026-10-17
amends: 0014, 0081
related: 0077
---

# 原生会话历史采用只读分页适配

协作 `read_thread` 和 `wait_threads` 通过完整 Thread 历史获取少量结果，会使长会话的
每次查询成本持续增长。固定 Python SDK 0.163.0 的公开 facade 尚未提供历史分页，
但同版本 App Server 与安装包 generated models 已支持 `thread/turns/list` 和
`thread/items/list`。采用这两个固定方法的薄适配，不建立本地历史或第二客户端。

## 决定

`ThreadHistory` 只暴露上述两类只读分页，使用同一 initialized `AsyncCodex`、安装 SDK
的请求/响应类型与 opaque cursor。调用与取消沿用现有 SDK 的 request 生命周期，
不补通用 RPC gateway、不修改 SDK 内部、不消费通知，也不把任意失败转为完整历史读取。

`read_thread` 先读无历史的 metadata，再读原生 Full Turn 页；`wait_threads` 只读
metadata、最新一个 Summary Turn 和至多二十个最近 item。原生 legacy history 不支持
items 分页时，根据 metadata 的 history mode 选择最新一个 Full Turn；仍不读取全 Thread。
输出预算可以裁剪单页内容，不能丢弃整轮后返回已越过这些轮次的原生游标。
普通执行、Goal 终态与 Files 的既有精确证据读取不在本决定范围内。

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
