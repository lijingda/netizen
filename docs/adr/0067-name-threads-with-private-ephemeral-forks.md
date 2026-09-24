---
status: accepted
date: 2026-09-17
amends: 0017, 0031
related: 0021, 0028, 0049
---

# 用内部临时 fork 自动补全原生会话名称

未命名会话只靠首条消息预览很难辨认。每次普通持久会话成功提交新 Turn（包括定时初始
Turn）时，尝试为无名称的 Thread 后台生成名称。steer、恢复 observer、Goal 自动
continuation、Side 和命名分支自身不触发。已有名称保持不变，不判断消息信息量。
本次已结束但未命名时，下一次新 Turn 再尝试；没有定时重试、跨重启恢复或历史回填。

## 后台任务与原生身份

每个 Binding 最多一个命名任务，先同步占位，再在锁外读取原生名称。固定 SDK 的
`turn/start` 返回不证明首条输入已经落入可 fork 的历史：必须有界等待公开 full read
中 exact Turn 的 `userMessage`，再调用同一个 `AsyncCodex` 的公开
`thread_fork(ephemeral=True, include_turns=False)`。缺少本轮输入证据时跳过本次尝试，
不等待父任务完成。`include_turns=False` 只省略 RPC 返回历史，不裁剪模型上下文。

临时分支使用 fork 时的上下文快照及提交时的模型设置，不追随父会话后续 steer。
提示词明确历史只作参考、不得执行原任务或调用任何工具，并通过公开
`turn(output_schema=...)` 要求最终回答为仅含 `title` 字符串的 JSON 对象。标题仍须是
1–120 字符单行文本；格式或值无效时跳过本次命名，不回退接受未受约束的文本。
结构化输出只约束最终回答，不禁用工具；禁工具仍是用户接受的提示词约束，不是权限隔离。
不修改原生工具配置、用户配置或 developer/base instructions。
只接受 exact 命名 Turn completed 的有效标题，通过公开 `set_name()` 写原 Thread。

任务不建立 Binding、Scope、Side route、卡片或数据库记录，不进入普通任务集合、
`wait_idle()`、Activity/Result/Files、主会话用量投影和 Netizen 会话统计。真实模型用量仍由
Codex 原生计量。名称依旧只由原生 Thread 持久化。主会话订阅与临时分支订阅分别拥有，
命名不 resume、interrupt、cleanup 或 unsubscribe 父 Thread。

## 并发与失败范围

自动生成和自动命名的原生 RPC 都不持 Binding/Scope 锁。自动写回在名称锁内重新读取名称，并在 Binding 锁内
复核 exact Thread、任务身份、服务状态、Project 删除意图和生命周期状态；切换当前会话不
改变原任务目标。手动 rename、归档和删除使旧任务失效；旧任务只能移除自己的占位。

所有命名方式共用一个写入入口和每 Binding 一把进程内名称锁。自动写入只尝试拿锁，
已有持锁者或等待者就丢弃结果，不排队；拿锁后仍只补空名称。手动写入按先后顺序等待，
拿锁后沿用原有 exact Binding 生命周期校验，使旧自动任务失效，再覆盖名称。自动先写则
手动随后覆盖；手动先写则旧自动结果退出。不再因自动写入在途而要求用户重试。

这是对 ADR 0017/0031 的 rename 协调范围的窄修订：管理入口只在短 Scope 锁内校验并固定
exact Binding，随后释放 Scope 锁再进入名称入口。等待期间切换会话不会把已接受请求
重定向到新会话；删除原 Binding 后，实际写入前重读会拒绝该目标。名称锁 → Binding 锁
是唯一嵌套方向，没有名称锁 → Scope 的反向依赖。等待名称锁不持 Scope/Binding 锁，
不占用 lifecycle 槽；手动开始执行后保留原有 Binding/lifecycle 协调，自动仅短暂校验。
普通 Turn、steer、停止、归档、删除都不等待名称锁。

锁由实际写入 worker 持有。排队时取消会退出等待且不执行写入；开始执行后取消外层请求
只停止等待结果，不能提前释放锁或撤回 SDK RPC。worker 成功、异常或结束时统一释放锁，
已确认的手动结果不会被较早的迟到自动写入覆盖。名称锁及等待队列不持久化，重启从空状态
开始，不增加恢复任务。外部 RPC 挂起与锁循环死锁不同，不能承诺前者总会在短时间内返回。
自动命名失败不会建立 lifecycle UNKNOWN 或关闭 admission，也不沿用手动 rename 的失败
隔离。外部 Codex 客户端不共享该锁，原生接口没有条件写，因此不能承诺跨客户端的原子
“仅名称为空时写入”。

## 资源收尾

无论成功、失败、超时还是失效，都会在统一收尾路径处理 exact 临时分支：需要时 interrupt
已知命名 Turn、有界等待唯一 `run()` consumer 的终态、请求清理登记的后台 terminal，最后
取消该分支订阅。`run()` 抛错或缺少终态时，在取消订阅前至多用一次有界公开 full read
核对 exact Thread/Turn 终态，仅用于确认结束，不恢复标题；读不到时不推断成功。
各步骤独立有界，前一步失败不能跳过取消订阅；异常只写内部日志，
不向聊天或管理页投递命名错误。unsubscribe 不是立即卸载证明，仍遵循 App Server 的
卸载宽限期；ephemeral Thread 不进入原生持久目录。

fork/turn 请求尚未返回时保留其 worker，迟到拿到身份仍执行收尾，不丢弃或复用在途占位。
请求异常结束但无法确认资源身份，或命名 Turn 收尾仍缺少 exact 终态证据时，保留失效的
命名占位直到进程结束，防止下一轮累计未知分支。这只停止该 Binding 本进程内的自动
命名尝试，手动改名和主线流程保持可用；已确认终态的失败仍可在下一次新 Turn 重试。
命名或清理未收束不会占用主会话。正常服务停止时有界收尾，崩溃/强杀不恢复，也不建立
持久化追踪或启动扫描；最终生命周期由同一 App Server 退出结束。

## 验证

行为测试覆盖新 Turn/steer 区分、占位去重、已有名称跳过、下一轮重试、手动 rename
排序、切换/归档/删除/Project 删除、后台 I/O 悬挂时主会话继续，以及迟到 fork/turn
身份和逐步清理失败。SDK 或此边界变化时运行 `scripts/probe_thread_naming.py`：证明
首轮输入可见后 fork 确实继承本轮内容、无工具标题输出、临时 Thread 在 active/archived
两种目录来源中都不可见、成功/中断收尾及父会话续聊和历史不受污染。
