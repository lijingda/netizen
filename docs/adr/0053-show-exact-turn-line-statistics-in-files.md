---
status: accepted
date: 2026-09-02
amends: 0024, 0027, 0047
related: 0014, 0048, 0052
amended-by: 0056
---

# 在 Files 模块展示 exact Turn 行数统计

后续 [ADR 0056](0056-count-successful-patches-in-task-files.md) 替换本文的统计事实源、
累计口径和子任务/Side 范围；本文保留当时决策背景，Files 展示与 manifest 容量约定继续有效。

Codex App Server 的 `turn/diff/updated` 是一个 physical Turn 内所有文件变化的最新
aggregate unified diff，但 SDK 的 `TurnResult` 和持久 `Turn` 都没有独立的 additions /
deletions 字段。Netizen 决定只从这份原生 diff 解析行数，不运行 `git diff`、不扫描工作区，
并让 Ordinary Turn 与 Goal 的 exact 最终 physical Turn 复用同一解析器。

解析器同时产出整轮总计与 current-side path 的逐文件统计，但只计算边界完整、行数可验证的
文本 hunk；metadata header 不计数。它覆盖常见的修改、非空新增/删除、带内容的 rename 与
多 hunk。无 hunk 时只把不含其他 metadata 的 `similarity index 100%` + `rename from/to`
认定为纯 rename `+0 -0`，不继续解释完整 Git diff 语义。

整轮总计包含已删除文件的有效 hunk，即使删除目标不能进入当前可发送文件列表。binary block
保留可解析路径但不伪造 `+0 -0`，也不阻止其他完整文本 hunk 形成总计；当前文件经内容识别
为 PNG/JPEG/GIF/WebP 时同样不显示逐文件数字。copy、mode-only、空文件增删及其他无 hunk
metadata-only block 仅保留可解析的 current-side path，不显示逐文件数字，并使整轮总计未知。
缺失文件不进入 Files 模块。content patch 缺失完整 hunk、出现 hunk 外游离正文或任一 hunk
结构异常，都会让该 aggregate diff 的全部数字 fail closed，但仍可保留独立解析成功的路径；
没有可计数的非 binary block 时也省略整轮总计。
每条 completion 路径只解析一次 snapshot，并把同一份 summary 复用于引用判断、文件提取、
整轮统计和 Progress Card 失败回退，避免对大型 diff 重复做相同工作。

Ordinary Turn 继续使用现有 completion-only `TurnOutcome.turn_diff`。Goal 不增加第二
notification consumer：现有 Goal notification Tap 无论 Activity 是否开启都观察 exact
`turn/started`、`turn/diff/updated` 和 `turn/completed`，在 physical Turn rollover 时清空旧
snapshot，只覆盖当前 physical Turn 的 latest diff，并仅在 SDK 确认的 final physical Turn
身份一致且已完成时放入 `GoalOutcome`。diff 投影 shape/identity 异常只让统计省略，不能中断
Goal 的唯一逻辑通知流。持久 history 不能事后补抓 diff。ADR 0014 的 Goal shape、synthetic
harness 与 live probe 必须覆盖该字段。

Side 本期不获取 aggregate diff，也不修改 observer、`handle.run()`、4096 high-water 或
ADR 0048/0052 的唯一消费者边界；它只复用下面的公共 UI 精简，因此不显示行数。

Files 模块顶部在统计已知时显示 exact physical Turn 的 `+N -M`，当前可发送的非图片文件行
显示自己的 `+N -M`。可见文件大小删除，但 `size` 仍只在内存中用于 availability 判断；所有
文件和图片按钮统一为“发送”，顶部集中说明点击后会把当前路径内容作为图片或文件消息发送
到本卡片话题。

领域模型使用 `additions` / `deletions`。自包含分页 callback 对每个已知条目使用短 wire 字段
`{path, label, a, d}`，整轮总计也使用成对的顶层 `a` / `d`；未知时必须同时省略一对字段。
统计由此可在翻页和服务重启后保留。文件完整分页上限从 500 降为 400，同时保留编码后
55,000 bytes 硬限制；任一超限都不生成整个 Files 模块，绝不截断。400 是数量上限，不保证
超长路径或较大的 Goal/Activity/Result 仍能通过 byte gate。

该 wire schema 在尚未正式推广的 pilot 内原位收敛，不升级 v4/v5 action version，也不提供
升级前测试卡的迁移或兼容承诺。v4 仍表示 Ordinary/Side 自包含 Result/Activity + Files，v5
仍表示 Goal 完整 Reply Card manifest；这两个当前用途不是旧新兼容分支。
