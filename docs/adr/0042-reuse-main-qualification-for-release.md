---
status: accepted
date: 2026-08-25
amends: 0041
---

# 正式发布复用 main 代码资格

ADR 0041 把 Published Release 与 Source Install 分开后，仍让正式发布流水线对同一份代码
重复执行跨平台本地测试矩阵，并把依赖真实账号和环境的 live probe 作为每次发布的外部
证据。项目现在把这三个责任进一步拆开：PR 和每次 main push 的 required CI 负责代码资格，
发布流水线只负责复用 exact main commit 的成功结论并验证制品完整性，live probe 则属于
相关开发迭代的兼容性验证。

## 决定

1. `make check` 是统一代码门禁。正式发布的 exact tag commit 必须存在一次成功完成的
   `main` push CI；发布流水线读取并核对这个 exact SHA 的结论，不重新运行测试。
2. 正式发布流水线只构建一次 deterministic archive/bootstrap，验证 tag、commit、manifest
   和 SHA-256，在受保护 environment 审批后发布不可变 Release。它不安装测试矩阵、不运行
   `make check`，也不接收 live-probe URL、摘要或独立的 `confirm_publish` 作为资格输入。
3. `scripts/probe_python_sdk.py` 及飞书/服务 live cases 保留为按变更触发的开发工具。修改
   pinned SDK/App Server、SDK Gap Adapter、相关原生生命周期路径、模型提供方或租户能力时，
   开发者运行受影响的 phase 并更新兼容性结论；普通 merge 和正式发布不重复运行。既有 ADR
   中的 release/live gate 表述相应解释为该能力首次上线或相关边界变更时的开发/rollout
   资格，不再向每次正式 Release 增加 workflow job 或外部 evidence 输入。
4. `make check` 中的 fake-server、Runtime 和 Channel 测试继续覆盖 Netizen 自己对 SDK shape、
   状态和失败语义的处理，但不声称证明真实账号环境或上游 App Server 的运行行为。
5. Source Install 仍对当前工作区运行完整本地门禁；Published Release 安装仍执行每台主机
   必需的包、配置、登录、权限、服务 ready 与回滚验证。这两者都不把 live probe 重新引入
   正式发布流水线。

## 后果

正式发布的速度和失败面不再随账号环境或重复测试矩阵变化，且 Release 仍被绑定到已经通过
required CI 的 exact main commit。相应代价是上游 SDK、模型提供方或租户环境的回归不会被
每次发布重新发现；项目接受这一边界，并在相关依赖或集成开发时主动执行 live requalification。
已经发现但不影响本次策略落地的 `0.147.0` compact 后续 Turn 问题不新增临时产品 workaround，
待匹配的 `0.149` Python SDK/App Server 组合发布后随 SDK 升级重新验证。
