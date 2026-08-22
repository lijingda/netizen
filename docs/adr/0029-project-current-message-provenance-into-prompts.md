---
status: accepted
date: 2026-08-20
amends: 0011, 0015, 0021
related: 0002, 0004, 0016
---

# 把当前飞书消息来源投影到每次 Prompt

> 后续 [ADR 0030](0030-require-resolved-current-sender-names.md) 取代本文的显示名占位符：
> 当前 Prompt 消息必须由 Channel SDK 的公开成员名单解析出真实姓名，否则整条
> fail closed；当前与历史引用的 sender attribution 都只保留应用内 `open_id`，不投影
> `union_id`/`user_id`，历史引用消息缺名时也不再生成“未知发送者”。

## 背景

Netizen 此前通常只把消息正文交给 Codex；只有逐条引用 envelope 会携带被引用消息的
发送者。群聊中不同参与者依次开始 Turn 或 steer 时，模型因而无法区分当前请求是谁发的。
Side 首轮还有一个更隐蔽的边界：Codex 接收原 `/side <问题>` 的文字，但完成回复和
reaction 锚定机器人在新话题中发送的问题 seed；若从这个完成锚点反推来源，会把机器人
误写成当前请求发送者。

发送者字段一旦作为 native input 提交，就会进入 Codex 原生 Thread 历史，且可被 App/CLI
读取；这比 Channel 内的瞬态展示更难撤回。同时，原生 Thread 的首条用户消息 preview
仍应优先展示真正的问题，而不是一段机器元数据。

## 决定

1. Channel 为每个真实普通或 Side Prompt 构造一个 Current Prompt Message 投影，覆盖
   idle 新 Turn 和 running Steer。它只使用 Channel SDK 公开归一化消息与 `Identity`，
   包含 exact message ID、当前消息类型、内容保真度、发送者公开字段和归一化请求正文；
   不额外查询通讯录或 OpenAPI。显示名缺失时保留可用 ID 并使用明确占位符。
2. 发送者投影只表示 attribution。它不改变 Feishu admission、Scope 共享控制权、Turn
   owner、approval、sandbox、工具权限或指令优先级，也不把某个显示名解释为可信角色。
   来源消息 ID 或 sender ID 与同一次解析得到的 `PromptInput` 冲突时 fail closed。
3. 没有逐条引用时，请求正文保持在 native input 最前，随后附加版本化
   `feishu_current_message` attribution trailer。这样原生首消息 preview 仍以用户请求
   开头，trailer 不重复正文。有逐条引用时，ADR 0011 的 envelope 从 v2 升为 v3：
   `quoted_message` 保持原类型矩阵，最后的 `current_message` 改为包含来源、发送者、
   保真度和完整 `request_text` 的对象。已有 v2 原生历史不迁移。
4. 当前消息仍只支持 ADR 0015 的 `text`、`image`、`post`；被引用消息继续支持 ADR 0011
   更宽的文本、结构化消息、资源元数据和合并转发矩阵。非空 `thread_id` 仍只表示话题
   结构，不产生逐条引用；`/side <问题>` 是 Control，也不因原消息带 reply relation 而
   获取引用内容。
5. Side 首轮显式分开两个对象：Current Prompt Message 是原 `/side <问题>` 入站消息，
   Completion Origin 是新话题中的机器人问题 seed。Codex input、source ID 和 sender
   attribution 来自前者；reaction、错误回复和最终完成投递继续锚定后者。Side 后续消息
   的两个对象通常相同。普通 running Steer 同样投影 steer 消息的实际发送者，但不替换
   当前 Turn 的原 owner 或完成锚点。
6. 图片顺序继续固定为“被引用图片 label/pixels、当前图片 label/pixels、完整最终文本
   prompt”，Runtime 再追加当前消息解析出的 typed `SkillInput`。引用内容和所有 attribution
   元数据中的 `$` 编码为 JSON `\u0024`，只有当前 `request_text` 保留字面 `$skill`，避免
   历史内容或显示名触发 Skill。
7. Channel Database 不增加字段，也不保存投影、发送者、prompt 或回复。普通持久 Thread
   会由 Codex 按原生规则保存这些输入；ephemeral Side 仍随进程失效。Goal objective、
   slash/card Control、Side boundary 和完成卡片不是 Current Prompt Message 投影的适用面。

## 后果与验证

模型能在普通 Turn/Steer、Side 首轮/后续以及逐条引用中一致识别当前请求来源；代价是
飞书公开身份字段会进入 Codex 原生历史。部署和用户文档必须明确这一可见性，并继续把
应用可用范围与聊天成员关系作为准入边界，而不是新增 Netizen ACL。

测试必须覆盖普通 A 发起/B steer、引用双方发送者不同、话题零引用读取、当前
text/image/post 保真度、多模态顺序、Side 首轮 source/origin 分离、Side 后续发送者、
元数据 `$skill` 抑制和当前 `$skill` 保留。发布前还要在目标环境确认首条消息 preview
仍以真实请求开头，并在飞书与 Codex App/CLI 中核对上述身份可见性。
