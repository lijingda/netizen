---
status: accepted
date: 2026-08-25
amends: 0033, 0034
related: 0032, 0035
---

# 分离已发布 Release 安装与源码安装

## 背景

零参数 `install.sh` 当前同时安装正式源码、当前 checkout 和未提交修改。安装器因而无法
引用发布者对 exact 最终制品的资格结论，只能在每个新内容摘要的目标主机上重新运行完整
测试。这个默认值适合直接开发后部署，却让只消费正式版本的用户重复执行与主机无关的
回归测试，并把“发布认证”和“主机激活”混在一条入口中。

## 决定

1. Netizen 使用两个显式、零参数入口。正式 Release 提供的 `install.sh` 安装该 exact
   Published Release；仓库根 `install.sh` 解析到最新稳定正式 bootstrap，仓库根
   `dev-install.sh` 安装当前工作区形成的 Source Install。
   入口表达候选来源，不通过 Git 状态、目录名、下载扩展属性或可由用户切换的
   `skip-tests` 参数猜测。
2. Published Release 是项目自行构建并上传的 exact tarball，不使用 GitHub 按请求生成的
   source archive。制品包含版本、commit、受管源码摘要和依赖锁摘要；发布流水线先构建
   一次制品，再在支持矩阵中对解压后的同一份字节运行完整本地门禁和发布者 live probes。
   只有全部通过才与一个内嵌 exact tag、资产名和 SHA-256 的 bootstrap 一起发布。
3. 正式 bootstrap 只从自己的 exact tag 下载资产，校验 SHA-256 后安全解压，并调用内部
   `install-release`。`releases/latest/download/install.sh` 可以选择最新稳定 Release，
   但重定向后得到的 bootstrap 仍固定下载自己的 exact 版本。GitHub Release 必须启用
   immutable policy；客户端不新增 GitHub CLI 依赖。
4. `dev-install.sh` 调用内部 `install-source`，沿用当前内容寻址快照、新 venv、固定依赖和
   完整测试。`install-release` 要求合法 Published Release manifest，不运行完整单元测试，
   但仍执行 package/resource 完整性、`compileall`、`pip check`、固定 SDK synthetic probes、
   配置解析、Codex 登录、飞书权限和 Host Validation。
   Source 与 Published 资格进入各自 domain-separated 的本地 release identity，内部 metadata
   同时核对 gate schema、qualification、源码摘要及 Published commit，禁止跨模式复用资格。
5. 两条候选准备路径必须汇入同一个配置、凭据、Service Backend、数据库/Skill snapshot、
   `current`/`previous`、activation intent、lifetime lock、ready 和 rollback 事务。不得复制
   安装器、增加第二套服务或让 release 来源改变状态生命周期语义。
6. `curl ... | sh` 的 stdin 不是 TTY。正式 bootstrap 只在存在真实 controlling terminal
   且调用是人类交互安装时把内部安装器连接到该 terminal；Agent/CI 仍使用下载到文件后
   `</dev/null` 的非交互流程，绝不因主动分配的伪终端意外进入浏览器初始化。

## 后果与迁移

正式用户只承担发布制品身份校验和每台主机必需的验证；开发者仍可部署任意当前源码，代价
是通过 `dev-install.sh` 完成全量门禁。迁移把原 `install.sh` 的源码语义显式移到
`dev-install.sh`，并把 `install.sh` 改为正式入口；旧安装的配置、凭据、数据库、Codex 状态
和 service manager 意图均不迁移。

本 ADR 不增加自动更新、后台下载器、第二个产品根、可移植 venv/container、离线分发保证，
也不把 Host Validation 降格成 release 正确性的替代证据。
