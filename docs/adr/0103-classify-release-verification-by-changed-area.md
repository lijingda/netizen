---
status: accepted
date: 2026-11-19
amends: 0050
related: 0014, 0029, 0039, 0042, 0055, 0064, 0075, 0076, 0080, 0099
---

# 按发布范围内的变更确定验收要求

## 背景

以往每次发布都要人工判断该补哪些实机证据，普通修改也会等候确认。PR 和 main push 的 CI
现已覆盖五个 `make check` job、Linux/macOS 原生隔离回归和 Linux Admin 浏览器冒烟。
仍需维护者判断的，是本次变更触及了哪些 CI 无法观察的行为，以及未验证部分能否接受。

## 决定

正式发布前，维护者可运行 `python scripts/release.py --plan`。脚本只读本地上一版本 tag
与 HEAD 的已提交内容，列出命中区域、路径、验收要求和缺少的参数。同一文件可以触发多个
实机边界。精确路径及内容比较规则集中在[发布脚本的区域表](../../scripts/release.py)。

| 类别 | 区域 | 变更边界与验收依据 |
| --- | --- | --- |
| manual | `codex-sdk`、`feishu-sdk` | 比较清单和锁文件中的实际 SDK／随附 CLI 依赖。前者逐项核对升级审查处置，后者验证真实投递、回调、引用和租户历史。 |
| manual | `platform-service` | 服务管理、安装／更新／移除、实例数据控制、受管环境、lifetime lock 或 ready 标记变化，须验收两平台及多实例。制包脚本逻辑、构建后端、分发名、入口与动态元数据变化也触发此项。 |
| manual | `python-matrix` | Python 版本要求、Python 版本分类或跨 workflow 的 CI 版本集合变化，须在 macOS arm64 当前 GUI 用户下安装、启动 LaunchAgent 并取得 ready。新 job 复用已有 Python 版本可直接复用 CI。 |
| manual | `feishu-app-setup` | 浏览器 setup／补权、应用凭据格式或权限契约变化，需验证真实浏览器安装、审批、发布、租户安装及入群。 |
| manual | `admin-login` | 飞书 OAuth、用户凭据或登录页脚本变化（登录页标记由浏览器冒烟覆盖），需在浏览器和飞书客户端验证登录与凭据刷新。 |
| manual | `skills-loading` | 内置 Skill 装载、根注册／目录适配、构建钩子或 sdist 清单中的 Skill 收录规则，以及 setuptools 对 Skill 资源的显式排除规则变化，须保留受影响阶段实际读到资源的执行证据。 |
| manual | `release-publisher` | 分发名或发布 workflow 变化须重新核对外部发布身份、环境和仓库保护。每次发布仍须确认既有外部准备。 |
| automated | `runtime`、`admin`、`cards`、`migrations`、`docs`、`other` | 其余路径由 exact main push CI 提供代码资格，包括行为测试、迁移夹具和浏览器冒烟。普通 Skill 正文修改归入此类。 |

制包校验脚本中 wheel／sdist 必备文件清单的条目、setuptools 的 `package-data` 与包发现清单、
随包示例 `config.example.yaml` 均由 CI 的发行包资源测试及发布工作流的隔离安装验证。

维护者请求正式发布时，对每个命中的 manual 区域提交一条 `--verified AREA="验了什么、在哪里验"`
或 `--accept-risk AREA="残留风险、接受原因及重新评估条件"`。可引用本候选已有的 PR 或审查证据。
缺项、重复或过期参数均在发布写操作前拒绝。脚本在 Release notes 的 Verification 节记录
exact main CI 及各项处置。常规发布仅需该提交的 CI 和既有的 wheel／sdist 隔离验证。
维护者仍决定发布时间，发布后仍须独立核对实际索引安装。

## 理由与后果

纯手工清单可能漏项，也无法在发布页追溯取舍。把每次 Runtime 修改都列为门禁会重复 PR 的工作。
以下验收由触及边界的 PR 记录证据或具体缺口，发布时复用。维护者可在发布前选择复验。

- 材料、引用、转发投影及历史 catch-up 的真实客户端检查，包括发送者归属（ADR 0029/0039/0064/0099）。
- 迁移框架本身变化时，Linux 和 macOS 的真实启动迁移、中断恢复和 ready 检查（ADR 0075/0076）。
- Admin 从另一台受信主机访问，以及按开发期变更触发的原生、模型、adapter 与生命周期 live probes。

飞书客户端渲染、真实模型加载资源、跨主机浏览器和 systemd／LaunchAgent ready 各自需要实测。
合成测试或 API ACK 不能证明这些结果。实机和 live 结果仍分别记录。

新增或移动发布边界时，在同一 PR 中更新脚本区域表，注明部署条款和触发原因，补命中与常规
修改的分类用例。混合模块须标明具体触发定义，例如服务入口内发布及清除 ready 标记的函数。
