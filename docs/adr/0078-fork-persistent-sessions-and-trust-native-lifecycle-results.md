---
status: accepted
date: 2026-10-08
amends: 0028, 0031, 0037, 0049, 0060
related: 0021, 0067, 0073, 0077
---

# 持久分支使用普通会话，恢复与删除以原生返回为准

用户需要保留当前讨论的上下文，在另一个飞书话题中继续使用。`/fork` 使用公开 SDK
创建持久 Thread，并保存为普通 Binding；创建后没有分支专属生命周期。原生目录可能
漏列尚未开始自己第一轮对话的持久分支，因此目录不再作为切换的存在性证明，删除也不再
用目录缺项推断成功。两者直接使用 exact native ID，由 Codex 决定是否能够执行。

## 持久分支与交接

`/fork` 只从当前已物化、持久且确认空闲的普通 Binding 创建。运行中、停止中、压缩中、
生命周期或观测未知时拒绝；暂停且空闲的 Goal 可以作为来源，分支不接管来源的 Goal。
它在当前聊天、机器人已加入的另一个群或可访问的机器人单聊中新建普通话题，不接入已有话题，也不改变来源
Scope 的 current pointer。选群查询复用 Management 的共享群目录，提交复核共用群/单聊目标校验，不调用
Admin HTTP，不增加客户端、缓存、权限层或全局聊天目录。

来源历史快照由 native `thread_fork(ephemeral=False, include_turns=False)` 管理；不复制
历史到 Channel Database。分支使用同一 Project/cwd，文件修改相互可见，不创建 worktree。
复制来源显式保存的 Model/Effort/Speed、Task Feedback 和 Mention Context Mode 意图，
之后各自独立；单聊目标固定 current-only，群目标的 catch-up 边界使用目标话题的根消息
或 seed，不复制来源聊天的消息游标。单聊适配只影响后续飞书补读，不清空继承的原生历史。
原生权限与配置由 fork 继承，不额外覆盖 approval/sandbox 或伪造 effective settings。

创建顺序为：原生 fork → 发布唯一的“创建中”根卡片 → 取得真实 topic ID → 原子保存
完整 Scope/Binding/native ID/设置/current → 将原返回 handle 登记到普通订阅管理 →
原地更新根卡片为成功并给出入口。发送响应未返回 topic ID 时，可在同一根消息下发送
seed 取得身份；仍只有一个话题。不能先保存 native ID 为空的可运行占位 Binding。
创建不发送首轮 Prompt，后续普通输入才按普通会话规则执行。

提交时复核 exact 来源及配置，完整绑定时复核 Project revision 和目标身份；目标已有 Binding 时
明确冲突，不能覆盖。根卡片提示“请等待创建完成后再发送消息”，不新增创建期输入
拦截或持久标记。用户提前输入时，若目标默认配置先建立普通会话，该输入可能在没有
继承上下文的新会话执行，随后 fork 绑定失败；若 fork 先完成，则使用分支。无可用默认
配置时按现有未建会话提示处理，不排队或自动重发。此成本是为保持普通输入路径统一而
接受的边界，提示本身不保证提前输入不执行。

创建调用在第一个外部副作用前登记进程内在途所有权，覆盖原生创建、root/seed 发布和
完整绑定；服务停止按既有有界交接等待整个调用。Project 删除遇到在途持久分支时，
本次不能完成删除，保留停用 Project，待创建收尾后重新预览和确认；最终绑定仍复核
Project 可用性，不能晚到绑定已停用或删除的 Project。这不是 Project 执行锁，也不
承诺重启后自动找回未知资源。

失败只反馈已知事实，不盲目重试 fork、按名称或时间猜认 native ID、自动删除残留，或
新增恢复队列。已知 handle 交还既有订阅释放边界；完整绑定已成功后，命名或卡片回执
失败不撤销会话。创建中断可能留下未绑定的原生 Thread 或话题，允许按 exact 证据人工
处理。已经绑定的分支使用普通会话的设置、恢复、订阅、归档、删除及 Project 清单。

卡片沿用工程现有的 SDK safety 去重、FIFO、公共 callback 编码及 exact/revision
校验；可重复导航使用公共 nonce，最终提交使用同类一次性动作规则。受理后显示创建中，
成功后移除提交按钮。不为 fork 写 SQLite 去重键、引入 claim 或保存卡片 session，也不
声称现有机制保证多个操作者对整张卡全局只执行一次。

2026-10-09 调整创建交互：名称和位置放在同一表单，“新话题”放在字段说明，群选项
展示名称和头像。最终选群方案与 Cron 共用显式“关键词查找 → 本页结果选择 → 页码跳转”，
不预载群目录，也不把原生下拉的本页过滤当作全群搜索。首次查找在时间、请求次数和数量
限制内收齐结果，完整快照随唯一跳转按钮携带；超限或未完成则提示细化关键词，不展示
不完整清单。翻页按快照直接切片，不查询群目录或重传头像。新查找清空旧结果选择，
跳页保留已选目标及待提交草稿，并对所有页及保留选择检查整卡容量。
普通选择不重绘卡片；查找和翻页通过本次提交带回表单，不保存卡片 session。沿用
[ADR 0058](0058-use-page-selection-for-file-cards.md) 已接受的公开 value + form_value：
提交按钮携带一次 exact 来源，选项只保存目标 chat ID，创建和查找通过按钮 callback
value 区分，不依赖 SDK 未透传的按钮名称。

同日增加聊天 ID 输入：复用问题卡的“单选输入方式＋独立输入框”交互，提交只读取所选
方式，未选中的字段残留不覆盖目标。当前聊天/群选择与手填 ID 均在原生副作用前复核
真实聊天类型和机器人访问资格；单聊仅通过 ID 或当前聊天指定，不新增单聊目录。
校验失败保留草稿，创建/查找/翻批的公共 nonce 均在重绘时更新，允许同值重试；一旦原生
创建已开始，仍按既有已知/未知结果交接，不能恢复可提交表单诱导重建。
ID 帮助链接只指向官方指南，不引入 OAuth 或发送探测消息。

2026-10-10 收敛为与 Cron 共享的三个显式选项：当前聊天、群聊选择、聊天 ID。
新建默认当前聊天，含单聊、群主聊天和话题所属聊天；群结果不再承担“当前聊天”特殊项。
选 ID 必须填写，不能以空值回退到当前聊天；未选方式的残留值不参与目标解析。
Fork 表单使用 v5，只解析当前协议，不新增旧卡字段转换或兼容分支。

## 普通会话恢复与切换

`/resume`、会话列表的“设为当前”和 Admin 的同类操作共用 `activate_exact`。保留本地
Scope/Binding、current、lifecycle 与 Context Boundary 检查；有 native ID 时直接调用
一次公开 `thread_resume(exact_id, include_turns=False)`，不先 list/read 探测存在性，
不传 cwd、model、config、approval 或 sandbox override。原生返回同一 ID 后登记普通
订阅，再提交 current/context boundary；Lazy 会话只做本地切换。

原生明确返回归档、不存在、占用或正在关闭时保留 Binding 与原 current，并给出对应
提示；分类限定为已验证的 SDK 异常类型、code、完整 message 和 exact ID，不用模糊
关键词推断。其余无法确认的 resume 结果沿用进程级关闭新 admission、要求重启的边界，
不自动重试、恢复归档或清除本地关联。原生成功而本地提交失败需报告部分结果，并按已有
机制交还已知订阅；只有已确认回滚时才能声称 current 没变。

切换不停止旧 Binding 的任务，也不替换本进程已持有的活动 Turn/Goal handle；原生对
同一运行中 Thread 的 rejoin 继续可用，不为切换增加 idle-only 条件。Netizen 不为选择
会话显式提交新 Turn，但原生 cold resume 后的 persisted active Goal 自动继续在
0.160.0 已存在，本次不修复或增加保护流程。Goal `origin` 缺口及重新评估条件仍按
[SDK 适配边界](../design.md#sdk-适配边界)记录，不能把此旧行为称为升级引入的问题。

## 删除只信任本次原生响应

所有普通 materialized Binding 删除继续复用同一 `thread/delete` 窄适配：预约 exact
lifecycle intent 后释放 Scope/Binding 锁，App Server 负责 shutdown 和 spawned
descendants 的级联；不先 interrupt、pause、cleanup、resume 或等待 idle。

- 原生成功响应后才删除 exact 本地 Binding，并按既有规则清空它的 current pointer。
- 明确 RPC 错误保留 Binding，反馈原生拒绝并允许重新确认。原生可能已先 shutdown，
  因此丢弃可能失效的本地活动和订阅投影，不恢复旧 Activity 或伪造 Turn 终态。
- 超时、断连、取消或响应不可验证时保留 Binding-local `lifecycle-unknown`；其他
  Binding 仍可使用。无论哪条路径都不自动重发 delete 或追加目录对账。

`not found` 也是失败，不能当作成功清除 Binding。成功响应丢失、部分原生删除或本地
提交失败都可能留下需要人工核查的关联；接受这一边界，换取单一的原生判定来源。目录
只供展示和独立操作使用，不能把未列出分支等同删除完成。archive 的原有只读对账保持不变。

原生历史引用限制同样适用：持久分支不是 spawned descendant，删除来源不会自动删除
分支；仍有分支引用来源历史时，Codex 可以拒绝删除来源。Netizen 不级联其他独立分支、
复制或脱离历史，不扩大 Project 删除清单绕过拒绝。用户按原生提示处理已知引用后重新确认。

## 验证

行为测试覆盖同聊天/跨群的 exact 身份与设置继承、一个根话题、完整绑定的原子性、
提前输入的两种提交顺序、Project 删除/停止交接、各阶段明确和未知失败、公共卡片策略，
以及零新增 Turn 的 fork 订阅与释放。恢复测试覆盖目录不可见的分支、Lazy、运行中
rejoin、明确拒绝、响应未知、本地提交失败和旧输入失效；删除测试覆盖成功、明确 RPC
拒绝、响应丢失、局部隔离和零自动 read/list/retry。真实持久 fork、冷恢复、引用删除
限制与飞书 topic/卡片分别验收，合成通过不能替代客户端或原生兼容性证据。
