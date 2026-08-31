---
status: accepted
date: 2026-08-31
amends: 0043
---

# 由脚本在维护者指令后执行发布全链

ADR 0043 把发布意图表达为"维护者创建 exact tag + 手工 dispatch"两个手势。v0.4.0 的实际
发布表明，当执行者是 agent 时这两个手势也由 agent 代执行：维护者的真实参与只有"何时发布"
的指令。流水线本身从不信任触发者——资格来自 exact main CI，制品身份来自单次构建与哈希
校验，发布不可变——因此把机械执行链交给脚本不降低安全性：误触发的最坏结果是多发布一个
完全合格的版本。

## 决定

1. 发布时机始终由维护者决定；项目不因 main push、tag push 或任何累计阈值自动发布，也不
   维护自动触发策略。
2. 维护者指令后，`scripts/release.py` 一次执行全链：从 conventional commits 推导版本
   （feat→minor，其余→patch）、生成确定性 release notes、提交版本 bump PR、等待 required
   checks 并合入、等待 merge commit 的 main CI、在 merge commit 上创建并推送 annotated
   exact tag、dispatch release workflow 并跟踪至发布完成。
3. 需要人工判断的情形 fail closed 并升级给维护者：范围内出现 breaking change（subject 的
   `!` 或 `BREAKING CHANGE` footer）、维护者显式指定的 `--version`、任一门禁失败。脚本不
   绕过或弱化 release workflow 的任何校验。
4. release notes 由脚本从 git log 确定性生成（按 conventional 类型分组、标注 PR 号），经
   release workflow 新增的可选 `notes` dispatch 输入前置到发布页正文；未传入时发布页保持
   原有纯完整性元数据。
5. ADR 0042/0043 的完整性契约全部保持：exact-tag checkout、main CI 资格复用、单次构建、
   manifest 与 SHA-256 校验、tag 双重核对、Immutable Release，以及 `Protect main` 的
   PR + required checks 对版本 bump 的约束。

## 后果

发布不再依赖维护者逐步手工执行；维护者的参与收敛为发布时机决策与异常处理。发布意图的表达
从"两个手工手势"改为"维护者指令 + 脚本执行"，这与手势实际常由 agent 代执行的现状一致，
是显式化而非放松。版本推导规则固定后，普通迭代不再出现主观版本选择；breaking change 与
跨里程碑版本始终由人决定。
