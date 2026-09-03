---
status: accepted
date: 2026-08-10
amended_by: 0055
---

# 支持普通消息与逐条引用中的原生图片输入

> [ADR 0055](0055-upgrade-and-requalify-lark-channel-sdk-140.md) 将 Channel SDK
> 升级到 1.4.0；`content_v2` 图片资源差异仍在，同时新增的顶层 `post.files`
> 附件区必须独立于 locale 图片 AST 失败关闭。下文的 1.2.0 叙述保留为历史背景。

> [ADR 0029](0029-project-current-message-provenance-into-prompts.md) 为最终文本 prompt
> 增加当前消息来源；本文的图片支持矩阵、原生输入顺序和原子失败边界不变。
> ADR 0039 加入 supplemental 来源后，2026-09-01 的 compact wire 继续复用本文的
> exact 下载身份，但模型可见图片关联统一改为 prompt-local `imgN`。

## 背景

[ADR 0011](0011-support-feishu-quoted-message-context.md) 首次接入逐条引用时只投影
Channel SDK 公开资源 key，不读取二进制。产品现要求当前消息和被引用消息只要是普通
`image` 或富文本 `post`，就把其中全部普通图片交给 Codex 原生视觉输入；文字、图片
以及当前/引用两个来源可以同时存在。

飞书“获取消息中的资源文件”接口允许按精确 `message_id + file_key` 下载普通图片和
富文本图片，单资源服务端上限为 100 MB；不支持卡片、合并转发子消息和表情包。
固定的 `lark-channel-sdk==1.2.0` 公开 `FeishuChannel.download_resource()` 方法覆盖该
接口，但会先把完整响应读为 `bytes`，再由调用方检查大小，无法在下载前按
Content-Length 或流式字节数截断。

同一版本的 `post` converter 还有一个公开归一化差异：`content_v2` 的图片会出现在
公开 `content_text` 的 Markdown 图片标记中，却可能不进入 `resources`；当
`content` 与 `content_v2` 同时存在时，`resources` 还可能来自未被渲染的旧版本。
同时，普通 text 节点和 fenced code 中也可能包含字面 Markdown 图片语法，不能扫描
整个 `content_text` 猜资源。可见图片必须从公开 typed `PostContent.post` 中按 SDK
相同的“首个 locale、非空 content_v2 优先”规则提取真实 `img` 节点；`md` 节点只在
成对代码围栏外提取。

固定的 `openai-codex==0.144.4` 公开支持同一次 start/steer 传入有序
`TextInput | ImageInput` 列表，其中 `ImageInput` 接受 data URL。现有
`CodexRuntime` 已透明传递 native input，不需要第二套附件协议或运行时。

## 决定

### 支持矩阵与准入

1. 当前消息为 `text` 时继续只允许无附件文本；为 `image` 时读取唯一普通图片；为
   `post` 时读取公开 typed AST 当前可见版本中的全部 `image`。当前消息的可见版本
   包含文件、音频、视频、表情包或其他资源时显式拒绝，不静默忽略。
2. 被引用消息继续使用 ADR 0011 的类型矩阵；只有引用类型为 `image` 或 `post` 时
   增加图片读取。其他引用类型保持原文本/结构化文本/资源元数据语义。
3. 当前消息和被引用消息可以同时带图；catch-up 还可带 supplemental 图片。组合顺序
   固定为“supplemental、引用、当前”。全部图片按最终 native input 顺序分配同一个
   prompt-local `img1..imgN` 空间；每张图前的 `TextInput` label 只携带 `ref`、`source`、
   source 内序号/总数、MIME 和大小，不暴露 exact `message_id` 或 `file_key`。随后放对应
   `ImageInput`，最后一项携带完整 prompt，避免历史内容替换当前请求。
4. 富文本仍保留 SDK 生成的 Markdown 图片原位置标记，但只有已验证并成功准备的真实
   图片 target 会在 fenced code 外改写成对应 `imgN`；Historical Message 的
   `attachments` 与图片 label 使用同一个 `imgN` 建立文字、消息与像素的确定映射。
   资源按 SDK 渲染顺序处理，同一消息内
   相同 key 只下载和提交一次。若 SDK 没有提供可验证的 typed post AST、却声称存在
   图片资源，则 fail closed；不会把字面 Markdown 或可能属于其他 locale/旧内容
   版本的图片交给 Codex。
5. 不改变飞书准入：单聊普通图片可直接触发；群聊/话题仍要求每条消息 @机器人。
   不启用 Channel SDK 的连续媒体批处理，因此多条独立图片消息仍是多次 prompt。

### 有界下载与原子失败

所有资源只通过 Channel SDK 公开 `download_resource(file_key, "image",
message_id=exact_message_id)` 读取，不访问 `_client`、不手写 OpenAPI，也不持有
Binding 锁等待网络：

- 当前消息与引用消息合计最多 20 张；
- 单图下载后最多 20 MB；全部图片原始字节合计最多 50 MB；
- 每条 prompt 内串行下载，单图 10 秒、整批 60 秒；
- 若等待 native steer 时 handler 被取消，官方异步 SDK 的底层同步 RPC 可能仍在执行；
  Runtime 关闭全局 submission admission 后再返回取消，未知副作用不会与后续 prompt
  并发。正常服务关闭本来也先关闭 admission；
- 以 magic bytes 只接受 PNG、JPEG、GIF、WebP；
- 下载完成且全部验证成功后才构造 native input；任意图片超时、无权、删除、保密、
  空响应、格式不符或越界时整条 prompt 不 start/steer，不部分提交；
- 图片编码为内存中的 data URL，不写 Channel SQLite、不落文件或长期 media cache。

这是公开高层 API 的有界使用，不是 SDK Gap Adapter。仍有一个明确残余风险：SDK
在调用方校验前可能把飞书允许的 100 MB 单资源完整读入内存，而且 asyncio timeout
不能真正停止已经进入 worker thread 的阻塞读取；50 MB 已接受原图还会产生约 67 MB
data URL，native RPC JSON 序列化会产生额外大字符串和编码缓冲。Python 分配器、HTTP
SDK 与 pipe 缓冲使峰值不能表述为硬性的字节上限。不同 Binding 的图片 prompt 按既有
并发语义独立准备，不增加全局 gate、semaphore 或队列，因此并发图片会线性放大内存
风险。Pilot 的用户范围受控、图片低频且通常较小，接受该风险并在发布后观测 RSS；当
Channel SDK 提供下载前可验证长度或流式硬上限的公开 API 时，应改用该能力。

### exact-Turn 与内容保真度

普通图片准备和逐条引用读取都属于 native submission 前的异步准备。只要当前或引用
存在待下载图片，就先捕获 ADR 0011 的 `SubmissionAdmission`；下载结束后在 Binding
锁内兑换。期间发生其他 prompt、stop、completion 或 idle ABA 时，本条显式失败，
绝不改投另一 Turn。

成功提供的引用图片仍在内部 rich projection 将资源 `content_read` 设为 true；普通图片
和仅含已读图片的富文本仍计算 `content_fidelity = full_multimodal`。这些读取状态不进入
compact Historical Message，模型只通过 `attachments[].ref -> image label.ref` 看到已经
实际提交的像素；未成功准备的图片绝不分配本地 ref，也不能只凭 key 声称已读像素。

## 非目标

- 卡片图片、合并转发子消息图片、文件预览、视频封面、音频和表情包；
- OCR 服务、图片转文字缓存或自建视觉模型；Codex 直接消费原生像素；
- 连续多条独立图片的自动合并；
- 放宽群聊/话题逐条 @机器人规则；
- 将图片或 data URL 持久化到 Channel 数据库。

## 验证

- SDK 契约测试固定普通 `image`、多图 `post` 的公开资源顺序、公开下载方法以及
  Codex `TextInput/ImageInput` 输入形状。
- 纯函数测试覆盖资源缺失、去重、prompt-local ref、来源 label 不暴露 exact ID、PNG/JPEG/GIF/WebP、数量/单图/总量、
  单次/整批超时、字面 Markdown/代码围栏误判和无图片字符串零变化。
- Channel 测试覆盖当前普通图片、引用普通图片、引用多图富文本、当前富文本与引用
  图片组合、下载失败零提交、图片命令拒绝、admission 捕获和不同 Binding 图片并发。
- Runtime 测试固定同一个多模态列表可原样 start，并在 active Turn 上原样 steer。
- 发布手工验收单聊图片、群聊 @机器人富文本多图、文字引用图片、图文引用图文，
  并确认任一不可读图片不会产生 Codex Turn。

## 后果

本 ADR 取代 ADR 0011 中“本版本不下载资源”的决定，但不改变其引用关系、卡片文本
fallback、权限和 exact-Turn 语义。代价是每个含图 prompt 增加一到多次串行飞书
读取以及 data URL 内存；收益是 Codex 看到真实像素，而不是仅看到资源 key。
