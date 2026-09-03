---
status: accepted
date: 2026-08-28
amends: 0014, 0024, 0027, 0046
amended_by: 0048, 0052, 0053
---

# 组合类型化回复卡并只自动收尾完成的 Goal

> [ADR 0048](0048-integrate-side-turns-with-task-feedback-reply-cards.md) 让 Side Turn 也复用
> Activity、Result 与 Files 模块，但不允许 Goal Module；本 ADR 的单 Presenter 与封闭模块
> 集合保持不变。
>
> [ADR 0053](0053-show-exact-turn-line-statistics-in-files.md) 让现有 Goal notification Tap
> 捕获 exact 最终 physical Turn 的 aggregate diff，并把 Files 完整分页上限调整为 400。

普通 Turn 已经分别拥有进度卡、完成文件卡与富文本终态，Goal 又有独立状态卡和终态回复，
导致相同结果被多条消息投递，新增展示项还会继续复制 card shell、分页与失败回退。Netizen
改为由一个 **Reply Card Projection / 回复卡投影** 按固定顺序组合四种封闭 typed module：
Goal、Activity、Result、Files。模块是纯展示投影，唯一的进程内 Presenter 持有 exact
message identity、合并 revision 并在每次变化时重绘整卡；模块不得各自更新飞书消息，也不
扩展成 provider registry、动态插件或任意 SDK event renderer。

## 组合与既有行为

Goal Module 存在、Progress Card 设置为开而产生 Activity Module，或存在 Files Module 时
使用一张 Reply Card；终态 Result Module 随卡片一起呈现。三者都不存在时，Result 继续走
既有富文本/静态文本路径。因此普通 Turn 的既有契约保持不变：Progress Card 关闭且无文件
时只发终态文本，有文件时仍是一张 Result + Files 完成卡；开启时从 Activity 运行卡更新成
Activity + Result + 可选 Files。Task Reaction 仍只属于 ordinary Turn；Progress Card
设置则决定后续 ordinary Turn 和 Goal operation 是否加入 Activity Module。Goal Module
承担 Goal 状态与控制面，始终存在，不因 Activity 关闭而消失。

每次更新都渲染完整 Projection。Files 分页 callback 继续跨重启自包含，但其 manifest 扩展
为有界的完整 Reply Card manifest，携带有界冻结的 Goal/Activity/Result 模块；翻页不得
丢失其他模块。普通 Result + Files 与 Activity + Result + Files 继续生成 v4 callback；
只有 Goal 与 Files 同卡时使用 v5 完整组合 manifest。两版都按 500 项、每页 8 项和
55,000-byte 上限校验。展示失败停止 Presenter，原生执行不受影响；Goal 初始卡失败必须
提供可见文字回执，终态在有界 handoff 内回退到新的自包含卡或既有文本。

Goal 活动卡从 start 到 pause/resume/terminal 复用同一条机器人消息。按钮携带 exact Scope、
Binding、SDK 已公开不可变字段组成的最强 Goal fingerprint 与预期状态；Presenter 还要求
当前进程中 exact message source、logical run 和 fingerprint 同时归属该路由，回调再在 Scope
coordinator 与 Binding lock 内重读原生 Goal。`createdAt` 只有秒级精度，因此 fingerprint
本身并非不可复用 identity；同秒且 objective/token budget 相同的 Goal 可以碰撞，进程内
exact source ownership 才防止旧卡控制新 Goal。切换 active Binding 不会让当前受控卡失去
exact Binding 控制权；进程重启后旧控制按钮一律视为 stale，`/goal` 生成并注册新的快照卡，
不扫描飞书或猜测旧 message ID。

Runtime 在终态证据冻结后仍保留 exact Goal slot，直到 Channel 的有界终态 handoff 返回，
因此显式 clear、同秒新 start、裸 `/goal` recovery 与 shutdown 不能越过 Result/Files 投递。
非 cleared Goal 的终态控制 Projection 仅在当前进程有界保留；cleared 卡不保留本地 session，
其 v5 文件翻页仍完全依赖 callback manifest。所有 identity/session 都不进入 Channel Database。

受支持的 Netizen 路径只允许 Runtime 在 Binding lock 内写 Goal。外部 CLI/App Server 在同一
Goal 生命周期并发替换同一 Thread Goal 明确不受支持；当前 SDK 的 `goal/clear` 是
thread-scoped mutation，没有 expected-generation CAS。Runtime 的 clear 前重读可以关闭所有
Netizen 内部竞态，但无法把“外部替换发生在最后一次 get 与 clear 之间”变成原子操作。

## completed-only Goal Finalization

Goal 的逻辑通知流、persisted Goal、exact 最终物理 Turn 与公开 Thread idle 四项证据全部
确认后，Runtime 先冻结 clear 前的 GoalSnapshot、最终回复、exact final Turn status/items，
再且仅在 Goal status 为 `complete`、final Turn status 为 `completed` 时执行一次原生 clear，
并以第二次 `goal/get == None` 确认。Channel 即使在 clear 后仍可用冻结 outcome 组装
Goal + Result + 可选 Files；有界终态 handoff 返回后才释放进程内 slot，
随后 `/sessions` 恢复 idle 的归档/删除操作。展示 handoff 超时只结束展示等待，不改变已经
确认的原生 Goal 结果；finalization unknown 则仍保留 slot。

paused、blocked、usage-limited、budget-limited、external-active 以及任何终态证据 unknown
都不自动 clear。前四种保留原生 Goal，由 Goal Module 按状态提供 paused resume 或显式
“结束 Goal”；
external/unknown 只读隔离。complete clear 的响应丢失、取消、false 或 clear 后仍能读到 Goal
都属于 finalization unknown：保留 unknown slot、关闭 admission、绝不自动重试，同时仍投递
已经权威取得的最终回答并在 Goal Module 标明“执行完成但收尾未确认”。

Goal Files 首期只来自四项证据中 exact 最终物理 Turn 的公开 items；当前 Goal adapter 不暴露
全部 rollover Turn 的可靠集合，因此不能扫描 Project 或把更早 continuation 的文件冒充为
完整 Goal 产物。扩展为全 Goal 文件集合前必须为 exact rollover provenance 增加独立能力契约
和 live probe。

## 验证

门禁覆盖所有 Goal/Activity/Files 组合、普通 Turn 零行为变化、分页后完整模块重建、单卡
start/pause/resume/terminal、切换 Binding 与 stale/multi-user callback、初始/中间/终态展示
失败回退，以及 complete clear 成功和所有 unknown 分支。Runtime 测试还必须证明非 complete
状态零 clear、四证据任一缺失零 clear、finalization unknown 保留 slot 并关闭 admission，
clear 后 outcome 仍含最终回复和 exact final Turn items、terminal handoff 完成前 clear/new
Goal 都不能越过投递、handoff 超时释放非 unknown slot，且 shutdown 不再 mutation 已终态 Goal。
