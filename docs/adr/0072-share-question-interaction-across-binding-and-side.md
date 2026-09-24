---
status: accepted
date: 2026-09-24
amends: 0071, 0048, 0052
related: 0021, 0029, 0069
---

# 共用问答交互，分别进入 Binding 与 Side 输入入口

结构化问答是会话交互能力，不应绑定持久 Binding 的身份或 current 指针。将 ADR 0071
的展示、表单解码、回答格式、真实回答者、回执和错误处理共享给 Side；只在目标解析、
输入准入和提交处区分 Binding/Side，不复制第二套问答链路，也不统一两者生命周期。

Question Target 只引用现有 Binding ID 或 Side ID。新自包含卡片使用带类型的 v2 target，
旧 v1 Binding 卡片仍可解码；重绘保留原目标并升级格式。不新增身份、配置、卡片状态或
数据库字段，不持久化 Side native Thread ID。回答来源投影使用 v2 target，仍明确区分
真实回答者、原问题卡与机器人发送的新反馈锚点。

点击时先核验原卡片 App/chat/topic 与目标所在位置。Binding 仍须为原 Scope 的 current；
Side 只核验自己的 open route 与 Runtime admission，不重读 Parent。Parent 切换、归档、
删除或恢复不改变存活 Side 的回答归属。Side 关闭、过期、缺少 Runtime 或停止中时拒绝，
绝不改投 Parent、新 current Binding 或创建新会话。

两类目标都在身份核验后、回答者查询和发送回执前捕获 admission。回答分别进入普通
`_consume_prompt()` 或消息与卡片共用的 `_consume_side_prompt()`；running 时 steer
捕获的 exact Turn，idle 时在原 Thread 新开一轮。异步准备期间的关闭、换轮和 revision
变化继续按各自准入拒绝，不排队、不自动重试或改投。Binding 的 catch-up 与 Goal
物理 Turn 规则不变；Side 不增加 catch-up，也不因卡片输入解除冻结配置。

Runtime 共享问题投影和 exact Turn/item 去重，交给 Channel 时携带目标引用。Side 沿用
ADR 0052 的同一 observer cursor；注册问题 handler 后，即使 Progress Card 关闭也观察
结构化问题，不另启 subscriber、consumer 或轮询任务。观察到 completion 仅触发唯一
`handle.run()` drain；observer 不可用或达到原始通知 high-water 后仍立即回退该 drain，
不增加持久 history recovery。进入 drain 或 close 后停止新观察；`run()` 返回后从 SDK
结果的 completed items 补投尚未尝试展示的问题，复用现有 Turn/item 去重，不新增状态，
不重试已失败的发送。补投仍要求原 Side open 且 active 未替换；解析失败仅影响展示。
已发起的有界 best-effort 发送不阻塞关闭，晚到卡片由点击时的目标准入拒绝。

卡片发送与回答回执继续核验原位置，回答后的任务展示不得因原消息消失而回退到聊天主线。
题目/选项、答案编码、Skill 隔离、nonce 去重、错误回执由共同实现及两类目标行为矩阵覆盖；
另测 Binding 切换/Goal 换轮和 Side Parent 独立性/关闭/过期/重启/准备竞态。SDK 版本、
observer 指纹、唯一消费者及 Side 关闭门禁保留。实际飞书 Side 卡片展示、回调、start/steer
仍须实时验收，合成测试不能代替该结论。
