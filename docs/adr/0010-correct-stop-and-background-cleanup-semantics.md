---
status: accepted
date: 2026-08-07
amends: 0008, 0009
---

# 更正 `/stop` 与后台终端清理语义

## 背景

ADR 0009 把 `thread/backgroundTerminals/clean` 当成了 native Turn 中所有工具进程的
清理能力。旧 Linux probe 通过扫描 `/proc/*/cmdline` 中是否包含 marker 来判断工具
已经启动；sandbox 的 `bwrap` wrapper 会在真实工具进程出现前就在 argv 中携带完整
命令，因此 probe 可能提前 interrupt，并把 wrapper 消失误判为 foreground tool 已被
cleanup。

2026-08-07 改为精确等待 `argv[0] == marker` 后，在目标 Linux 得到以下结果：

- 锁定的 Codex `0.144.4` 与同机 CLI `0.146.0` 行为一致；
- foreground `/bin/sleep` 运行期间，`thread/backgroundTerminals/list` 在 interrupt
  前后均为空；
- `turn/interrupt` 与 `thread/backgroundTerminals/clean` 都快速成功返回，但真实
  foreground process 继续运行；
- native Turn 正确进入 `interrupted`，同一 native Thread 仍可开始下一 Turn；
- 活动 Turn 的公开 Thread read 未提供可用于受支持 terminate API 的 process ID。

[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)也把
`turn/interrupt` 定义为 Turn cancellation，把
`thread/backgroundTerminals/clean`、`list` 和 `terminate` 限定为已登记的后台
terminal。当前高层 Python SDK 与批准的窄 adapter 没有 foreground tool termination
控制面。

## 决定

1. 保留 ADR 0009 的 exact-version adapter，但其唯一语义是为 exact Thread 请求清理
   App Server 已登记的后台 terminal。RPC 空响应不得解释为 foreground process exit
   attestation。
2. `/stop` 继续按 exact Binding 执行 native Turn interrupt，然后执行上述 background
   cleanup 请求。进入 RPC 前的飞书回执改为“正在中断当前 Codex Turn”。
3. native `interrupted` 终态必须明确提示：已请求清理已登记后台 terminal，但前台工具
   进程不受接口保证、可能继续运行。外部 interrupt 没有经过本地 cleanup 时也必须
   明确提示两层事实。
4. background cleanup 请求失败仍保持 Binding `stopping`，允许重复 `/stop` 重试；
   成功后可按 native terminal 释放 Binding，但不能据此声明前台进程停止。
5. Linux marker probe 只接受精确 `argv[0]` 命中。interrupt phase 记录 foreground
   process 是否在 cleanup 后 5 秒内退出；这是一项版本能力分类，不再是产品已经支持
   foreground termination 的硬门禁。若进程仍在运行，probe 有界等待其自然退出，
   避免测试自身留下孤儿，再验证同一 Thread resume。
6. 不增加 `/proc` 生产扫描、任意 PID/process-group signal、第二 App Server、通用
   private RPC 或每 Binding `CODEX_HOME`。关闭共享 App Server 会影响其他并发 Thread，
   也不能包装成 per-Binding `/stop`。

## 验证

- unit test 必须证明 wrapper 仅在 argv 其他位置包含 marker 时不会命中，只有 exact
  `argv[0]` 才算真实工具进程；
- Channel test 必须证明 acknowledgement、local interrupt 和 external interrupt 都不
  声称 foreground process 已停止；
- runtime/fake App Server 继续验证 exact Thread interrupt -> background cleanup 请求、
  失败保持 stopping、重复 stop、shutdown 和 Binding 隔离；
- 目标 Linux interrupt probe 必须输出 foreground 5 秒退出分类、观察 native
  `interrupted`、有界清理自身 marker，并完成 same-Thread resume。

## 后果与移除触发器

Pilot 的 `/stop` 可以停止模型 Turn，并请求清理官方登记的后台 terminal，但用户必须
把前台工具进程继续运行视为已知风险。这比虚假“已停止”安全，但不等价于 Codex App
层面的完整进程控制。

一旦官方高层 SDK 提供 foreground tool cancellation/termination，且目标 Linux 精确
marker、process-tree、same-Thread resume 和跨 Binding 隔离全部通过，可新增 ADR
恢复“命令已停止”产品承诺并移除警告。在此之前不得用 prompt、CLI 输出解析或本地
PID 猜测模拟该能力。
