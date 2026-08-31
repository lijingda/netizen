---
status: accepted
date: 2026-08-31
amends: 0046, 0048
related: 0049
---

# 始终反馈任务生命周期，只让执行中表情闪烁可选

ADR 0046 将任务接收、执行中脉冲、成功 steer 与终态表情合并成一个默认关闭的
Task Reaction 选项。这能避免部分移动端把 reaction 变成额外消息，但也同时拿掉了用户
判断“任务已正常接收”、“调整已接受”和“本轮已结束”的稀疏关键反馈。真正会周期性产生
展示噪音的是运行中 `THINKING` 的反复添加和删除，两者应当分开建模。

## 决定

普通 Turn 和 Side Turn 始终使用尽力而为的 **Lifecycle Reaction / 任务生命周期
表情**：native Turn 确认 accepted 后在 completion origin 添加 `Typing`；只在 native
steer 确认成功后在 steer 消息添加 `OnIt`；确认终态后先添加
`DONE`/`ERROR`/`CrossMark`，再清理原消息上的运行表情。被拒绝或未被 native
接受的输入不制造成功表情。`OnIt` 投递失败时，已成功的 native steer 始终回退一条
简短的“已接收调整”文字。所有 reaction 仍是尽力展示，不改变 native outcome。Goal 仍不使用
Lifecycle Reaction。

Binding Task Feedback 中历史 Task Reaction 选项收窄为 **Reaction Pulse / 执行中表情
闪烁**，默认关闭且只控制 `THINKING` 的低频 pulse。关闭时不添加、删除或周期调度
`THINKING`，但照常管理 `Typing` 的 exact reaction ID 以便终态和正常 shutdown
清理。普通 Turn 在 admission 时捕获该选择；Side 创建时冻结 Parent 当时的选择并供容器内
所有 Side Turn 沿用。Progress Card 的默认值、Activity 语义与 Goal 组合边界全部不变。

Channel schema 仍为 v7。SQLite 历史列 `task_reactions_enabled` 和表单字段名保持不变，
其值改为表示 Reaction Pulse 选择。领域属性使用 `reaction_pulse_enabled`，不让
历史存储名继续污染运行语义。已有值无需改写：原来开启的 Binding 继续显示 pulse，
原来关闭的 Binding 仅获得始终存在的 Lifecycle Reaction。由于升级前已发出卡片的文案
承诺了不同语义，`task-feedback` option reference 升为 v2；旧卡提交会 fail closed 并要求重新
打开，不会静默改释。

## 取舍

部分移动端仍可能把 accepted、成功 steer 和终态 reaction 显示成少量独立消息。这是有意
接受的取舍：这些稀疏节点能让用户确认消息正在被处理，而可配置的 `THINKING` pulse
才是应当可以关闭的周期性噪音。

## 验证

聚焦门禁覆盖普通与 Side Turn 的 pulse 开/关两组路径：两组都有 accepted、成功 steer 与
completed/failed/interrupted 生命周期表情；只有开启组调度 `THINKING`；关闭组在终态与
shutdown 仍使用 exact ID 清理 `Typing`。还要覆盖被拒绝的 steer 零成功反馈、`OnIt` 失败的
文字回退、reaction 失败不改变 native execution，以及 Progress Card 四种组合的无回归。
