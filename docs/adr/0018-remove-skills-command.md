---
status: accepted
date: 2026-08-12
amends: 0012, 0014
---

# 删除 `/skills` 浏览命令，保留原生 Skill 调用

## 背景

ADR 0014 为飞书增加了 `/skills` control，用 live `skills/list` 展示当前 Project 的
Skill 目录。这个入口只负责浏览，不执行 Skill；真正的显式调用一直是普通消息开头的
`$skill-name`，普通自然语言消息也可以直接询问 Codex 当前有哪些 Skill 可用。

专用浏览 control 因此重复了自然语言交互，同时扩大了命令注册、Channel handler、
Runtime summary DTO、帮助文案和验收面。删除它不应连带删除提交前的可信目录校验。

## 决定

1. 从统一命令注册表、`ControlName`、Channel 路由和 `/help` 中删除 `/skills`。
   输入 `/skills` 按既有未知 slash command 规则 fail closed，不转换成 Prompt，也不产生
   Codex mutation。
2. 查询当前可用 Skill 使用普通自然语言消息，保持与其他开放式问题相同的
   start/steer 语义；Netizen 不为这类问题增加特殊解析或本地响应。
3. 显式调用继续使用一条普通消息开头的一个或多个 `$skill-name`。`SkillCatalog`、
   `skills/list` SDK Gap Adapter、typed `SkillInput` 和提交前 live revalidation 全部保留。
4. 删除只服务于目录展示的 `SkillSummary` DTO 与 Runtime summary 方法。Goal objective
   中的 `$skill`、Apps 和 Plan 边界不变。

## 验证

- parser 与 Channel 测试证明：即使 Skills capability 可用，`/skills` 仍是未知命令、
  不进入帮助且零 native submission；
- 既有多个 `$skill-name` 的 Turn/steer、未知/disabled/重名/stale fail-closed、引用消息
  不激活 Skill 以及 SDK Gap Adapter contract 测试保持通过；
- `make check` 继续验证完整仓库回归面。

## 后果

飞书命令面减少一个只读入口，用户通过自然语言发现 Skill、通过 `$skill-name` 显式调用。
Netizen 仍需要原生 Skills discovery 作为安全校验边界，因此本决定不会减少 ADR 0014
Adapter 的升级与移除门禁。
