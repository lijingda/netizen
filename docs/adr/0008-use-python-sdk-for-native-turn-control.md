---
status: accepted
date: 2026-08-03
supersedes: 0007
amended_by: 0009, 0010, 0012, 0013, 0014, 0026
---

# 使用官方 Python SDK 获得原生 Turn 控制

## 背景

飞书中同一会话运行期间的下一条消息，产品上固定映射为 steer，而不是 reject 或
queue；`/stop` 必须映射为 native interrupt。官方 TypeScript SDK 只封装
`codex exec`，没有 active-Turn steer。继续使用它会让核心交互要求永久成为 gap，
或迫使 Netizen 自己解析协议。

官方 `openai-codex` Python SDK 直接管理一个 App Server，并公开
`AsyncTurnHandle.steer()`、`interrupt()`、`run()` 和 `AsyncThread.read()`，同时
复用标准 Codex 本地状态。

## 决定

1. 单个常驻 Python 服务拥有一个 FeishuChannel、一个 SQLite 和一个
   `AsyncCodex()`。
2. 精确锁定 `openai-codex==0.144.4`、`lark-channel-sdk==1.2.0`；生产只调用公开
   高层 Thread/Turn API，不传 custom binary/env/config/model/sandbox。
3. `/new` 只创建 lazy Binding；首条真实 prompt 才 `thread_start(cwd=...)`，随后
   先 write-once 保存 native ID，再调用 `thread.turn()`。后续只做 exact-ID resume。
   若 `thread.turn()` 没有返回 handle，视为启动结果未知并关闭服务 admission；不能
   重试启动第二轮。
4. running Binding 的消息调用同一 handle 的 steer；stopping 拒绝；steer 与完成
   竞态要求用户重发，不转为新 Turn。
5. 不增加全局或 Project 并发上限；多个 Thread 可以共享一个真实 cwd。
6. Channel DB 仅保存 Scope、Binding、active pointer、schema version 和 SDK
   DedupStore TTL key，不保存 prompt、response、Turn 或 queue。
7. 服务以部署所选的 effective user 运行并使用该账号的 Standard CODEX_HOME。不另设
   Netizen 权限 profile；`$CODEX_HOME/config.toml` 尽可能原生生效。
8. `thread_start()` 可省略 approval mode，但省略会使用默认 `auto_review`，公开
   枚举当前也只有 `auto_review` 和 `deny_all`；这是 Ask/Custom 不能完整继承的
   明确 gap，不写私有 shim 或审批卡。
9. pinned `0.144.4` 不用 `handle.run()` 消费终态。运行时通过公开
   `thread.read(include_turns=False)` 轮询轻量 native Thread 状态；`notLoaded` 和
   `active` 继续等待，只有 `idle` 才调用 `thread.read(include_turns=True)`，再按
   exact Turn ID 映射公开的 status、error、items 和 final agent message。
   `systemError` 与未知状态显式失败。若 App Server 在 `idle` 后仍短暂返回当前
   Thread 的精确 `not materialized yet` 错误，则保持 active 并重试；其他
   `InvalidRequestError` 不重试。没有磁盘解析、私有属性或 JSON-RPC。

## Linux M0 结果

目标机以部署账号、该账号的 Standard CODEX_HOME 和无覆盖参数的 `AsyncCodex()` 实测：

- 重新登录后 SDK smoke Turn 完成，返回 `SDK-SMOKE`。
- SDK probe 创建的 exact Thread 出现在原生状态中；同账号全局 Codex CLI 按该
  exact ID resume 后产生第二个 completed Turn，返回
  `CLI-RESUME`。
- live steer 返回同一个 Turn ID，并把最终结果从原要求改成 `M0-STEERED`。
- 两个同 cwd、不同 Thread 的 marker 进程被同时观察到，证明没有 SDK 或产品级
  跨 Thread 并发上限。
- public polling 的真实 Turn 观察到 `active -> completed`，运行中 steer 返回
  同一个 Turn ID，最终回复为 `POLL-STEERED`；全过程未调用 `handle.run()`。
- durable probe 首次复测发现新 Thread 会短暂处于公开 `notLoaded` 状态，此时请求
  `includeTurns` 会被 App Server 拒绝。状态机改为等待 `idle` 后再读取完整 Turns；
  后续 config probe 进一步捕获 `idle` 后仍未 materialize 的窄窗口，因此增加精确
  错误匹配重试及“其他 InvalidRequest 不重试”测试。目标 Linux 52/52 tests 通过。
- config probe 未修改全局配置：同一个常驻 `AsyncCodex` 中，临时受信任 Project
  从 `CONFIG-A` 改为 `CONFIG-B` 后，下一条新 Thread 返回 B，重启后仍返回 B。
  固定 `0.144.4` 当前分类为 `hot-reloaded`；这是版本观测而非永久契约，探针也允许
  新版本明确分类为 `restart-required`，且不代表所有用户级设置都无需重启。
- 初始详细诊断中，`interrupt()` 与 `handle.run()` 都在约 0.01 秒内返回，Turn
  状态为 `interrupted`；但嵌套 sandbox/bwrap 和实际 `/bin/sleep 60` 进程继续
  存在，直到 60 秒后自然退出。仓库当前将可重复门禁缩短为 bounded
  `/bin/sleep 15`：interrupt 后 5 秒 marker 仍存在即判失败，再等待其自然退出，
  避免探针自身留下长时进程。

最后一项符合[官方 App Server API](https://learn.chatgpt.com/docs/app-server#api-overview)
的能力拆分：`turn/interrupt` 取消 Turn，而
`thread/backgroundTerminals/clean` 才负责停止该 Thread 的后台终端；后者是需要
`capabilities.experimentalApi` 的实验方法。当前高层 Python SDK 没有公开该方法。

## 发布门禁与当前风险

### 1. 即时 completion 竞态与公开规避路径

`0.144.4` 在 synthetic App Server 紧邻返回 `turn/start` 响应和
`turn/completed` 时会丢失 completion。官方 main 当前也在收到响应后才注册 Turn
队列。原生 `handle.run()` 诊断仍在第 1 次稳定复现失败。

Netizen 的 Channel 首版不需要 token streaming，因此改用同一高层 SDK 的公开
`AsyncThread.read()` 观察 App Server 原生状态。synthetic
`scripts/probe_sdk_completion_race.py --read-recovery` 已连续 20/20 通过；真实
Linux 也完成 polling + steer + final response。该路径不维护第二份状态，只在内存
持有 active Thread/Handle，并从原生 Turn items 选择 final agent message。

因此 completion race 不再是部署阻断；release gate 改为 public read recovery 必须
通过。保留原生 `handle.run()` 失败探针作为上游回归证据，未来只有在官方修复且新路径
通过同等测试后才考虑移除 polling。本 ADR 仍不授权 patch SDK 内部或复制 JSON-RPC。
ADR 0014 后续只为 Goal/Skills 批准了复用同一 SDK client 的 capability-specific 临时
适配，通用 gateway 仍被禁止。

### 2. interrupt 不清理后台终端

`scripts/probe_python_sdk.py --phase interrupt` 在目标 Linux 稳定证明：公开
`AsyncTurnHandle.interrupt()` 只能保证 native Turn 终止，不能保证正在执行的工具
进程终止。因此当前实现虽然忠实调用 native interrupt，却不能满足用户理解中的
“`/stop` 后命令也停止”。在当时的决策中，以下任一条件满足前，这是唯一硬 release
gate：

- 官方高层 Python SDK 公开稳定的 Thread terminal cleanup，并通过孤儿进程探针；
- 用户明确批准一个精确版本、实验能力门控、隔离 adapter、带移除触发器的窄 ADR；
- 用户明确把 `/stop` 产品语义降级为“只停止 Codex Turn，工具进程可能继续”。

第三项会让有副作用的命令在飞书显示停止后继续运行，风险高，不建议作为默认 Pilot
行为。本 ADR 原本不授权直接调用 experimental App Server JSON-RPC、扫描并 kill
任意子进程，或把 `interrupted` 状态当作工具已停止的证明。用户随后明确选择第二项；
ADR 0009 只授权 exact `thread/backgroundTerminals/clean` 的固定版本/指纹窄 adapter，
不改变这里对其他私有接口和任意进程信号的禁令。

2026-08-07 的精确 `argv[0]` 复验发现 ADR 0009 的方法只覆盖已登记后台 terminal，
无法终止 foreground tool；旧 marker 绿灯是 wrapper 子串匹配造成的竞态性假阳性。
用户随后批准 ADR 0010 的安全修正，明确选择第三项的诚实产品语义：仍请求清理已登记
后台 terminal，但飞书必须警告前台工具进程可能继续运行，不得声称命令已经停止。

## 后果

优点是 steer/interrupt、原生历史、CLI resume、Skills/MCP/config 均留在官方后端，
Channel adapter 很薄。取舍是 App Server 常驻子进程、首建 approval gap、每个 active
Turn 一次 0.5 秒间隔的轻量 native Thread 状态读取，以及 terminal-cleanup adapter
仅清理已登记后台 terminal。foreground tool termination 是 ADR 0010 记录的显式缺口，
由精确 Linux 探针持续分类而不再伪装为已支持能力。

若恢复使用公开 `handle.run()`，丢失终态会让 Binding 永久保持 running，后续 steer
已结束 Turn 也无法自行恢复。当前 polling 只在 exact native Turn 出现终态后清槽；
`InternalRpcError` 会保持 active 并重试（新 rollout 短暂为空是已实测情形），不用
客户端超时猜测终态；SDK 公开 `is_retryable_error()` 判定的 overload 同样保持 active
并重试。当前 Thread 的精确 `not materialized yet` 错误也是实测瞬态；匹配条件固定
到错误类型、code、Thread ID 和完整消息，避免吞掉其他 InvalidRequest。持久错误需要
诊断或重启，不能自动排队或假装完成。

关闭整个 `AsyncCodex` 或 systemd control group 会同时影响其他并发 Thread，不是
per-Binding `/stop`。因此不把“重启整个服务”包装成 stop，也不在无 ADR 时按进程树
发信号。ADR 0009 的 adapter 只封装精确 Thread 的官方清理方法，不得演变成第二套
App Server 客户端。ADR 0014 的 Goal/Skills Adapter 同样复用这一个客户端，但按能力
独立做 shape/harness 门禁，并在公开 facade 支持后逐项删除。
