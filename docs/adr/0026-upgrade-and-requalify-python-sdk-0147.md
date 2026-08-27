---
status: accepted
date: 2026-08-19
amends: 0008, 0009, 0020
amended_by: 0027
related: 0014, 0021, 0022, 0024, 0025
---

# 升级并重新认证 Python SDK 0.147.0

> 修订说明：[ADR 0027](0027-use-turn-diff-and-self-contained-file-cards.md) 在本版本上增加并
> 认证了公开 Turn diff stream 作为文件来源；persisted Thread read 仍是普通 Turn 终态权威。
> 下文关于本轮文件只沿用既有 item shape 的结论仅保留为升级当时的历史背景。

## 背景

一次初始对照中，用户交互终端里的全局 `codex-cli 0.147.0` 能完成 Turn，而通过
non-interactive SSH 启动的 Python SDK/bundled CLI 超时。后续控制变量确认该对照混入了
ADR 0022 的账号环境差异：non-interactive SSH 缺少 profile 导出的代理变量；在与服务
一致的 interactive login 环境中，当前生产 `0.144.4` 和候选 `0.147.0` 的 smoke Turn
都约十秒完成，当前生产进程继承的代理值也与新鲜 profile 一致。因此本次升级不得被描述
为此前无文本/无文件表现的根因修复；候选 release 此前从未激活，本轮文件行为仍分别由
ADR 0024、ADR 0025 定义。

PyPI 已发布配套的 `openai-codex==0.147.0` 与其精确依赖
`openai-codex-cli-bin==0.147.0`。用户选择将 bundled runtime 与当前发行线同步；让
Python SDK 调用账号 PATH 中的全局 CLI，或绕过 live gate，都会破坏 ADR 0008 的 SDK
ownership 和可复现发布边界。

## 决定

1. 同步精确锁定 `openai-codex==0.147.0` 与
   `openai-codex-cli-bin==0.147.0`；生产仍不传 custom binary、env 或第二套 Codex
   state。
2. 保留公开 persisted Thread polling 作为普通 Turn 终态权威路径。升级本身不授权改用
   stream、解析 CLI 输出、读取磁盘 history 或复制协议；completion race synthetic 与
   Linux polling/steer gate 必须重新通过。
3. 高层 facade 只新增与当前产品无关的 `thread_list(section_id=...)`；Thread/Turn、文件
   item、compaction、lifecycle 和 model settings 依赖的公开 shape 保持兼容。Goal、Skills、
   Side 和 materialized Thread Delete 的既有 migration sentinel 结果不变。
4. 高层 SDK 仍没有 background-terminal cleanup。ADR 0009 的 exact method、参数、空响应、
   `experimentalApi` 与私有持有关系保持不变；整个 Python 包 fingerprint 更新为经过
   synthetic harness 认证的 `0.147.0` 值，任何后续源码变化继续拒绝启动。
5. 高层 SDK 仍没有非消费 plan snapshot。ADR 0020 的 router、lock、Queue 与 generated
   plan step shape 保持不变；`TurnPlanUpdatedNotification` 新增可选
   `explanation: str | None`，observer 明确校验但不展示该解释，并继续只投影 exact
   Thread/Turn 的 `plan`。
6. 升级不修改或删除共享缓存、用户 `config.toml`、认证、MCP、Skills、原生 Thread、
   Channel schema 或产品语义。目标 Linux live phases 必须从 ADR 0022 定义的账号
   interactive login 环境运行；自动化 non-interactive SSH 不能冒充服务环境。候选只有在
   全部 synthetic、live phases、安装回滚门禁和服务 ready 检查通过后才能激活。

## 后果

Netizen 的 bundled runtime 与账号当前 CLI 处于同一发行线，同时保留两个私有 adapter
的窄边界和移除触发器。该对齐降低版本漂移，但不构成此前现象由 `0.144.4` 引起的证据。
代价是每次 SDK 升级仍须重新计算整包 fingerprint、检查新增公开 facade，并跑完整真实
Thread 门禁；不得把本次认证外推到其他 `0.147.x` 版本。
