---
status: accepted
date: 2026-11-12
amends: 0031, 0083
---

# Admin 按轮分页查看只读对话记录

管理员排查会话需要看到请求与结果的对应关系，只有最近回答不足以提供上下文。
Admin 仍是管理入口，不是第二个 Codex 客户端；本决定将最近回答扩展为只读对话记录，
通过 ADR 0083 的同一历史适配读取 Summary Turn 页，不新增 RPC、历史存储或客户端。

## 决定

物化持久普通会话（包括可读归档）可按轮查看第一条用户输入、最终回答、状态和原生
实际存在的时间。每页固定十轮，最新轮在下方，打开定位到最新；只有点击“加载更早”
才继续原生 opaque cursor。不展示推理、commentary、工具参数/输出或输出文件。
summary 只保留本轮第一条输入，因此不显示 steer 追加输入。运行中轮展示状态与 summary 实际返回的首条用户输入，不展示尚未终态的回答。
没有发送、重试执行、从某轮继续、全量加载、历史搜索、导出、自动翻页、轮询、预取或缓存；
刷新丢弃已加载的更早页，重新读取最新页。归档读取不 resume/unarchive。

正文完整返回，每轮独立使用既有有界 Markdown 渲染；超过 256 KiB / 20k token 的排版
限制时保留完整原文。预览执行器繁忙时，本页输入、回答及展示元数据文本不超过 1 MiB
则在事件循环直接编码不含 Markdown HTML/图片的原文页，有回答时在页面级提示一次排版
暂不可用；超出才返回 503。执行器关闭返回 503；排版执行器期限耗尽或 worker 结果未知
改为返回 504，此前最近回答在这些情况下降级为原文。
一页非图片部分（输入、回答、Markdown HTML 和元数据）超过 16 MiB 时从同一 cursor 改用 limit=1，提示
“本页内容较大，已改为逐轮加载”；此回退仅限当次请求，下次“加载更早”仍尝试十轮。
单轮非图片部分仍过大时保留身份、状态、时间和原生游标，
正文标注“该轮内容过大，Admin 不显示”。不丢整轮后返回已越过的 cursor。
第一页仅在 summary 最新轮 completed 且回答包含 `![` 时额外读取 Full Turn；读取失败
（CodexError/ThreadHistoryUnavailable）记录不含正文的 warning 并降级为无图，仍受外层
期限约束。以既有规则和全部图片预算展示 completed 最终回答
引用且唯一匹配 completed imageGeneration.savedPath 的原生 PNG；两次读取的最新
Turn ID 不同则本次不附图。十轮页回退到单轮时复用同 Turn 已取得的图片，不重复读取 full。
图片字节不计入 16 MiB，只受既有图片预算（含 32 MiB
MAX_IMAGE_RESPONSE_BYTES）约束，不因图片大而隐藏已读取的文字。更早轮次只给图片
文字说明，不读取 full 或传输图片字节。

用户输入只识别当前版本的 Netizen 外层结构，整体解析成功才展示请求、来源、发送者、
附件文件名及引用/补充消息条数，不返回引用与补充上下文的其他人正文。带标记但解析
失败时原文回退并标记；App/CLI 的纯文本保持原样，不算失败。非文本输入仅作占位说明。
实例管理员可看到任意会话每轮的用户输入正文，包括单聊；这符合少数受信管理员的定位，
必须在用户手册明确告知，不引入 Project ACL 或多角色权限。

## 证据、取舍与迁移

固定 openai-codex 0.167.0 bundled App Server 与 mock provider 的实测表明：paginated
summary 十轮约 2.8 ms / 46 KB，与会话长度及翻页深度无关；full 十轮约 23 ms / 2 MB。
legacy 每页约 4.9 ms × rollout MiB，与视图、limit、深度无关，与已有最近回答一次读取
成本相同；summary 翻页未观察到事件循环延迟。两种模式的 summary 均只有第一条
userMessage 和 final_answer，steer 仅 full 可见。failed 保留 error.message；legacy
interrupted 可能带 commentary，必须按 phase 过滤。非法 cursor 返回 JsonRpcError 后
连接仍可用；legacy items 分页不支持，本功能不使用它。

采用 summary 限定展示与成本，接受 steer 不可见和 legacy 固有读取成本；不通过新历史
索引或解析原生文件弥补。exact Binding/Thread 前后校验、共享在途限额、超时和认证撤销
契约保持不变。迁移与移除条件沿用 ADR 0083：公开等价 facade 可用时迁移并删除适配，
升级时复核 shape/synthetic 和隔离原生 paginated/legacy 探针。
