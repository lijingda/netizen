---
status: accepted
date: 2026-09-16
amends: 0011, 0015, 0029, 0039, 0055
---

# 统一卡片与转发材料的输入投影

卡片和合并转发已有引用文本支持，但当前消息只接受 text/image/post；直接放开类型会把
原作者的 `/stop` 或 `$skill` 误当成当前用户指令，并放大混合子消息的遗漏风险。新增
interactive/merge_forward 直接输入，同时将当前、引用和补充历史的内容解释收敛到同一
纯投影；来源校验、请求意图与原生提交仍由原入口负责。

## 决定

- `message_content` 统一消息类型、文本、资源元数据和读取状态；只消费 Channel SDK
  公共 typed content、`flatten_content` 和公开 `normalize` 提及转换，不复制转换器
  或协议模型。合并转发统一从校验、裁剪后的 typed 树调用 SDK 渲染，再按同一消息的
  公开 mentions 恢复提及映射，不回用入站 `content_text`。转发富文本的正文和资源
  描述来自同一个首选 locale、非空 `content_v2` 优先的可见 AST；只在其副本中把真实
  媒体 target 改成未读标记，Markdown 图片复用既有图片标记处理，正文与代码中的
  字面值保持。内部 exact 资源身份不变。共享的
  `message_preparation` 只通过公共 `fetch_quoted_context` 补读顶层卡片占位符，继续使用
  ADR 0011/0055 已批准的精确版本 CardKit 2.0 fallback；来源入口先验证 exact identity，
  每次补读独立使用 10 秒预算。不增加其他 SDK gap adapter 或私有 API。
- 卡片输入只支持飞书 Card 2.0；当前、引用、补充历史和转发子项中的 Card 1.0 均明确
  拒绝，不新增旧格式 renderer，也不把只有标题的部分读取当作成功。CardKit 2.0 的
  fallback 是当前固定 SDK 的能力缺口适配，不是旧格式兼容，继续保留原移除条件。
  初次读取未确认版本时，补读结果也须在采用 SDK 文本前检查公开 raw 卡片版本。
- 直接提交的卡片与合并转发是用户提供的材料，不解析其中的 slash control 或显式 Skill。
  当前请求要求结合已有明确任务处理，没有明确任务时询问用途；材料的 `$` 编码为
  `\u0024`，发送者仅表示本次提供材料的人。原作者文字、内嵌卡片按钮和历史命令均不
  获得操作权限。text/post/image 的请求正文、命令、Skill、图片与附件拒绝语义保持原样。
- 外层 Current Prompt v1、Quoted Prompt v4、Context Prompt v2 的版本、字段和顺序不变；
  直接材料放在现有 `current_message.request_text` 内的独立材料表示中。引用和补充历史
  仍使用 compact Historical Message，材料投影不取代 source validation、context selection
  或 envelope renderer。不会迁移既有 Codex 历史。
- 合并转发从 SDK 公共 `MergeForwardContent.items` 树投影，整个转发包合计最多保留
  50 个子消息节点、最终文本最多 16,000 字符；条数或文本截断传播到外层。最大嵌套
  深度为 3，最外层容器计为 depth=0，允许其内再嵌套三层；depth>3 或 SDK 返回
  `max_depth_exceeded` 时整条拒绝，不将超深内容作为正常截断提交。对 SDK 返回树的
  深度检查先于条数裁剪，避免条数预算遮蔽深处的超限。
  这是额外的全包限制，不能把 SDK 每个容器的 50 项上限当成总预算。保留的子项只使用
  公共类型和 SDK 渲染，未知类型、不可渲染项、循环结构、loading 或 error 明确失败；
  不把读取失败冒充正常截断，也不沿子项继续递归追踪引用。
  SDK 在生成公共树前已静默省略的条目没有可恢复的完整性证据；提示明确内容仅限
  SDK 返回的展开结果，不代表源话题完整历史。`truncated` 表示已知截断，不是完整性证明。
- 混合转发中的图片、文件、文件夹、音视频和表情只提供文字或附件描述，并明确未读
  像素/正文。本次不下载这些资源，也不为内嵌卡片逐条补读；无法得到其可见文字时整条
  失败。独立 image/post 仍按原路径准备原生图片。这个产品范围不是对飞书资源 API
  当前能力的断言；以后扩大下载范围须验证 exact message ID/key 与真实资源访问。
- 固定 SDK 1.4.0 的卡片 converter 会遍历 header/body 下任意字典值，可能把 `value`、
  `confirm`、`options`、`behaviors`、`events` 及提示/初始值字段内的文本节点混入可见文本。公共内容层
  对可确认的这种结构有界检查并拒绝整条，不自行重写卡片 renderer。正常可见按钮
  label 和普通 scalar value 允许。固定 SDK 契约测试保留此差异；上游排除这些隐藏
  payload 并通过同一测试及真实卡片验收后，删除此限制。

飞书官方[转发话题接口](https://open.feishu.cn/document/im-v1/message/forward-2)
返回 `merge_forward`，因此共用上述容器，不新增话题分享类型。这里读取的是平台返回的
转发材料；不会把当前消息的 `thread_id` 当作待读取源话题，也不会扫描源话题全部历史。
普通话题的 Scope 身份、逐条引用选择与权限仍按 ADR 0011/0039。

## 后果与验证

这项扩展只增加输入边界的材料策略与有界容器投影。Runtime、SQLite、Scope/Binding、
exact-Turn admission、catch-up 选取/权限和原有图片预算不变；没有第二套历史、资源缓存
或任务队列。代价是共享投影必须持续覆盖混合内容、读取不完整和 SDK 归一化差异。

验证要求覆盖直接/引用/补充来源的同内容一致性、普通与 Side 的材料提交、卡片补读失败
零提交、Card 1.0 明确拒绝、slash/Skill 惰性、混合子项未下载、全包条数/文本截断
传播、真实 SDK 的超深错误整条拒绝、富文本可见正文与资源一致、未知/失败子项拒绝，
以及 text/post/image 与现有外层 wire 回归，并运行 `make check`。入口测试应断言确定的
成功或失败结果，不能同时接受两种结果。
上述边界收紧后的本地 `make check` 已通过（1,843 项测试、编译、依赖及固定 SDK
合成门禁）。真实 SDK 合成用例覆盖初读失败后补读旧卡拒绝，以及条数截断不能遮蔽
超深错误；独立复核未发现这两条组合路径的遗留问题。
尚未进行真实飞书客户端与权限验收；发布前还须按
[部署验收](../deployment.md#前置门禁) 使用真实飞书客户端验证卡片、合并转发和转发话题，
包括混合子消息、当前群/话题 @ 准入、目标应用权限与不可读失败；本地 fixture 或官方
示例不能代替客户端及权限验收。
