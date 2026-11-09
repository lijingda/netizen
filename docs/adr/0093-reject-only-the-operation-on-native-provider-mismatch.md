---
status: accepted
date: 2026-11-08
amends: 0078, 0088
related: 0014, 0021, 0067, 0082, 0090
---

# 原生模型提供方拒绝只拒绝本次操作

App Server 0.167 在 resume 和输入提交前重新检查会话保存的模型提供方与当前配置、组织
策略是否相容。原先将此拒绝解释为结果未知，会让用户切换 provider 后的一条旧会话输入
关闭整个实例准入。固定版本源码中，[ConfigManager](https://github.com/openai/codex/blob/rust-v0.167.0/codex-rs/app-server/src/config_manager.rs)
的 `check_thread_model_provider_for_input` / `load_for_resume` 在
[turn 处理器](https://github.com/openai/codex/blob/rust-v0.167.0/codex-rs/app-server/src/request_processors/turn_processor.rs)
与 Thread 处理器提交之前执行；`scripts/probe_model_provider_admission.py` 的隔离
mock provider 实测确认被拒请求的模型请求数为零。
因此接受新增的已验证未投递分类，修订 ADR 0078/0088；其他未知副作用边界不变。

只识别公开 `InvalidRequestError`、code `-32600` 且 `data` 为字典、能按 SDK 生成的
`ModelProviderError` 校验的三种变体：`modelProviderMismatch`、
`modelProviderNotConfigured`、`modelProviderPolicyConflict`。不匹配文案或异常链；
provider ID 去控制字符并截断后用于中文说明，不暴露原始配置。

普通输入、ExternalMessage、Skill 准备、切换、Side 父会话、Goal 打开、压缩和 rename
的 resume，以及普通／Side／定时首轮 turn/start、steer 与 compact 请求适用此分类。
只拒绝本次操作，不关闭其他 Binding 准入、不重试、不新建替代 Thread。保留 Binding、
原执行及 Goal 状态，不提交上下文游标或成功回执；未启动 Turn 的订阅排期释放，Side
保持 open/idle，compact 释放本次预留。定时首轮交接以 `input_rejected` 释放，不能标记
`initial_start_unknown`。归档恢复释放 lifecycle 预留，但必须如实报告本次 unarchive
是否已成功，不把后续拒绝当作撤销解除归档。已验证未投递的输入附件沿 ADR 0090 清理。

Goal set／`start_goal_operation` 仍由 SDK helper 管理路由与取消（ADR 0014），本次不改
适配器；其中的 provider 拒绝仍落入原 mutation unknown 规则。未结构化的配置读取失败
也仍按原操作的未知范围处理。`thread/fork` 不执行这项 provider 检查；后台命名 fork
后续 Turn 失败继续仅按 ADR 0067 的命名任务边界收尾，不影响主会话。

行为测试覆盖三种变体、分类边界、各入口资源释放和状态保留、定时首轮与协作工具回执；
隔离探针通过生产 Runtime、真实 bundled App Server 和临时双 provider 配置验证旧会话
零模型请求拒绝、新 Binding 可运行、切回后 exact Thread 可继续。真实账号、飞书客户端
与平台验收单独记录，不由本地测试代替。
