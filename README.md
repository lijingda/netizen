<p align="center">
  <img src="docs/assets/netizen.svg" width="76" height="76" alt="Netizen">
</p>

<a id="netizen"></a>
<h1 align="center">Netizen</h1>

<p align="center"><strong>在飞书里使用 Codex。</strong></p>

<p align="center">
  在单聊、群聊和话题中发起任务、补充要求，接收结果与文件。<br>
  运行在自己的 Linux 或 macOS 主机上，复用 Codex 登录、原生会话、Skills 与 MCP。
</p>

<p align="center">
  <a href="https://github.com/lijingda/netizen/releases"><img src="https://img.shields.io/github/v/release/lijingda/netizen" alt="Latest release"></a>
  <a href="https://github.com/lijingda/netizen/actions/workflows/ci.yml"><img src="https://github.com/lijingda/netizen/actions/workflows/ci.yml/badge.svg" alt="Main quality gate"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="skills/netizen-user-guide/references/user-guide.md">使用指南</a> ·
  <a href="docs/cli.md">CLI 安装与维护</a> ·
  <a href="docs/CONTRIBUTING.md">参与贡献</a>
</p>

<picture>
  <source media="(max-width: 600px)" srcset="docs/assets/workflow-mobile.svg">
  <img src="docs/assets/workflow.svg" alt="交互示意：在飞书描述任务，执行中补充要求，完成后接收回答并按需发送文件。">
</picture>

<a id="pilot-形态"></a>

## 你可以用它做什么

- **把任务交给 Codex。** 在飞书中阅读代码、修改项目、整理资料；任务由官方 Codex SDK
  管理的原生会话执行，沿用运行主机上的工具和工作目录。
- **边做边沟通。** 普通任务执行时继续发消息，可以补充条件或调整方向；也可以开启
  [过程卡](skills/netizen-user-guide/references/user-guide.md#飞书中的运行反馈)查看进展。
- **带上图片与讨论背景。** 支持图片、逐条引用，以及可选的
  [群聊上下文](skills/netizen-user-guide/references/user-guide.md#发送消息引用与图片)。
- **取回本轮文件。** 完成后从回复卡片中按需发送有原生执行记录的文件；发送的是点击时
  的当前内容，详见[文件说明](skills/netizen-user-guide/references/user-guide.md#查看和发送本轮文件)。
- **保留上下文，另开一条讨论。** 用
  [持久分支](skills/netizen-user-guide/references/user-guide.md#fork-持久分支)在当前聊天或其他群
  创建普通话题，之后独立继续。
- **展开临时讨论，推进持续目标。** 用
  [Side](skills/netizen-user-guide/references/user-guide.md#side-临时话题)另开临时话题，或用
  [Goal](skills/netizen-user-guide/references/user-guide.md#goal)让 Codex 围绕目标持续推进。
- **让任务按时开始。** 用自然语言或 `/cron` 创建
  [定时任务](skills/netizen-user-guide/references/user-guide.md#定时任务)，可每次在独立话题执行，也可在原会话定时继续。

普通会话的历史由 Codex 保存，可在 Codex App/CLI 中继续使用；App/CLI 中新增的消息不会
自动回填飞书。多个会话可以并行，同一项目的文件目录共享，修改会相互可见。

<a id="安装升级与启停"></a>

## 快速开始

### 1. 准备运行主机

| 需要 | 说明 |
| --- | --- |
| Linux 或 macOS | Linux 使用 systemd user；macOS 14+ 支持 Apple Silicon 与 Intel，需已登录桌面 |
| Python 3.11–3.14 | 使用用户选定的环境，不捆绑 Python；生产运行不依赖 Node.js |
| 有效的 Codex 登录 | 先在运行服务的同一账号下通过 Codex CLI 或 App 登录；不要求全局安装 CLI |
| 飞书应用 | setup 引导创建或选择机器人应用；按租户要求完成授权、审批、发布和可用范围设置 |

Netizen 面向一台主机上的受信用户，使用服务账号的 Codex 状态与工具权限。
Linux 注销后常驻需要 linger；macOS 服务随桌面登录启动、注销停止。
[完整前置条件](docs/deployment.md#前置门禁)

### 2. 安装 CLI 并创建实例

在选定 Python 环境从 PyPI 安装 `netizen-cli`。源码开发安装见
[贡献指南](docs/CONTRIBUTING.md)。

```sh
python -m pip install netizen-cli
# 也可使用 uv tool install netizen-cli
netizen setup
netizen start
netizen status
```

setup 准备配置、飞书授权和实例数据并注册服务，不默认启动。首次缺凭据会输出浏览器
验证链接；Agent 可运行 `netizen setup </dev/null`，转交链接并保留同一进程，确认后自动
保存凭据。不要把 App Secret 发到聊天里。start 等待服务真正就绪，每次实际启动均检查
数据格式并按需迁移。

默认实例数据集中在 `~/.netizen`；程序位于选定 Python 环境。用 `--root` 或 NETIZEN_ROOT
选择其他实例，例如 `netizen setup --root "$HOME/work/.netizen" --admin-port 8890`。
各实例共享原生 Codex 状态；服务固定注册时的 Python，终端换 venv 不改变已有绑定。

日常维护用 `netizen update` 更新当前环境并恢复原来运行的实例；它不接受 --root。
`netizen remove --root ...` 默认保留数据，--purge 才清理精确范围，-y 只省略确认。
完整流程、失败边界、跨环境切换和卸载见 [CLI 安装与维护](docs/cli.md)。

### 3. 在飞书开始第一次对话

1. 打开机器人单聊；如需在群里使用，先将机器人加入目标群。
2. 发送 `/settings`，创建或登记一个项目并启用。项目就是 Codex 实际工作的目录。
3. 发送 `/new`，在卡片中选择项目。模型配置可选择“继承 Codex”；过程卡默认开启，可按需关闭。
4. 直接发送任务，例如：**“梳理这个项目的结构，并生成一份入门说明。”**
5. 收到回复后继续交流；如果出现“本轮文件”，点击“发送”取回需要的文件。

不确定下一步时发送 `/help` 查看快速开始。通过 `/defaults` 或 Admin 配好
[会话默认配置](skills/netizen-user-guide/references/user-guide.md#默认会话配置与自动创建)后，
没有当前会话时也可直接发送任务：机器人会在消息所在主线或话题创建会话并执行。
没有可用默认配置时，机器人仍会提示准备步骤；被拦下的任务需在准备好后重新发送。

群聊和群话题中的每条请求都需要重新 **@机器人**，包括命令。`/new` 只通过卡片创建会话，
不接受参数。普通任务执行中发来的新消息用于调整当前任务，不会排队成下一个任务。

安装遇到问题可从[部署文档](docs/deployment.md)继续；已能对话时，常见疑问见
[使用 FAQ](skills/netizen-user-guide/references/user-guide.md#常见问题)。

## 命令

日常使用可以从这些入口开始：

| 想做什么 | 入口 |
| --- | --- |
| 创建或登记项目工作目录 | `/settings` |
| 新建会话，选择项目 | `/new` |
| 查看、保存或删除当前聊天的会话默认配置 | `/defaults` |
| 查看并切换已有会话 | `/sessions` |
| 从当前空闲会话另建持久分支话题 | `/fork` |
| 调整模型、思考强度和过程卡 | `/config`，当前会话空闲时使用 |
| 查看当前任务和上下文用量 | `/status` |
| 查看共享 Codex 账号额度和重置时间 | `/usage` |
| 中断当前任务 | `/stop`；不保证前台工具进程退出，见[停止说明](skills/netizen-user-guide/references/user-guide.md#stop) |
| 查找当前机器人的管理入口 | `/admin`；返回实际管理 URL 和实例根目录，不返回登录凭据 |
| 管理定时任务 | `/cron`，也可以直接用自然语言描述计划 |
| 查看当前实例提供的命令 | `/help` |

重命名、归档、恢复、删除等完整用法见[命令索引](skills/netizen-user-guide/references/user-guide.md#完整命令索引)。
当前实例的命令开放情况以 `/help` 为准，任务状态以 `/status` 和卡片提示为准。

### 两个常见场景

**另开一个临时讨论。** 当前会话已有对话后，发送：

```text
/side 这个方案还有哪些边界情况需要考虑？
```

Side 会在同一聊天中新建话题，可继续多轮讨论。它与原会话共享项目目录，空闲两小时或
服务重启后过期；`/side close` 主动结束话题会话。Side 中不支持 Goal。

**每天回顾项目进展。** 在已经选择项目的普通会话中发送：

```text
每天北京时间九点，在当前群总结这个项目的进展。
```

计划每次创建独立执行话题；暂停计划不会停止已经触发的任务。
[定时任务用法](skills/netizen-user-guide/references/user-guide.md#定时任务)

<a id="admin-web"></a>

## 管理与维护

实例管理员可以通过 **Admin Web** 集中管理项目、会话、Side 话题、会话默认配置和定时任务，
并在“系统维护”中检查正式更新或重启服务。发送 `/admin` 可获取当前实例的管理 URL
和根目录，无需创建会话。未指定端口时首次从 `8787` 起分配并保存，各实例独立监听；
管理页仍使用安装器生成的独立管理员凭据，命令不会返回凭据或免登录链接。
[访问方式](docs/deployment.md#配置与管理页访问)见部署文档。

升级与重启都会影响正在执行的任务，重启后不会自动续跑。
[正式升级、启停与卸载](docs/deployment.md#升级启停和卸载) ·
[管理页升级](docs/deployment.md#从-admin-升级) · [管理页重启](docs/deployment.md#从-admin-重启)

## 使用前了解

- **共享环境。** 飞书应用的可用范围和群成员关系控制谁能访问；同一实例复用服务账号，
  不提供按用户或项目隔离的权限体系。Admin Web 面向受信内网的单管理员。
- **输入与输出。** 支持文本、普通图片和富文本图片，也可提供飞书 2.0 卡片、合并转发与转发话题作为
  [背景材料](skills/netizen-user-guide/references/user-guide.md#卡片合并转发与转发话题)。材料内部的媒体只提供未读附件描述；直接文件和音视频仍不支持。
  飞书 1.0 卡片明确不支持。
  输出文件按本轮原生记录发现，不扫描工作区补齐，也不自动上传。
- **Codex 能力有宿主差异。** 飞书复用原生会话和工具，但并非每个 App/CLI 控件都可用；
  全新会话采用 `auto_review`，持久分支沿用原生继承权限；不提供 App 的 Ask/Custom 选择器。
  [完整差异与当前限制](skills/netizen-user-guide/references/user-guide.md#与-codex-appcli-的差异)

<a id="用户指南-skill"></a>

也可以直接在飞书问 **“Netizen 怎么切换会话？”**。Python 安装包随版本提供
[用户指南 Skill](skills/netizen-user-guide/SKILL.md)，支持自然语言咨询；
需要显式调用时，发送 `$netizen-user-guide 你的问题`。内置 Skills 属于实例所绑定的
Python 安装，只为该实例的 Codex 进程加载，不覆盖用户的全局 Skills。

### 按需读取飞书历史

Python 安装包同时提供 [netizen-lark Skill](skills/netizen-lark/SKILL.md)。Agent 可以从当前消息的
`message_id` 定位聊天／话题，再以 **本机 Netizen 的机器人身份**按需读取相关历史。
例如：“看看这个话题之前的讨论，再回答我的问题。”已有逐条引用和 catch-up 不依赖此能力。

要使用这项能力，推荐在运行 Netizen 的同一主机、同一账号下，按
[飞书 CLI 与 Skills 的官方安装说明](https://github.com/larksuite/cli)安装
`lark-cli` 及 `lark-im` / `lark-shared` Skills；已有安装可以复用。

`netizen-lark` 说明如何选择当前 `NETIZEN_ROOT/lark-app/config.json` 中的 `netizen` profile，
由可选的 CLI 直接复用 Netizen 的应用凭据；查询与结果处理由 lark Skills 指导。
Skill 不带脚本，不需将凭据读入模型上下文，也不修改用户默认的 CLI 配置。
运行上下文缺少 `NETIZEN_ROOT` 时明确报错，不猜测默认实例的机器人身份。
机器人仍受已有权限和聊天可见性约束；历史只在任务需要时读取。
其他场景（包括读取消息中的文档、妙记链接）沿用原有 lark Skills 与用户的 CLI 配置；
失败后由 Agent 酌情判断是否尝试 Netizen 机器人凭证，具体见 Skill 的身份使用范围。

<a id="本地开发"></a>
<a id="开发与兼容性验证"></a>

## 文档与贡献

| 你的任务 | 从这里开始 |
| --- | --- |
| 查命令、理解操作后果或排查使用问题 | [用户手册](skills/netizen-user-guide/references/user-guide.md) |
| 安装、配置应用权限、升级或恢复服务 | [部署文档](docs/deployment.md) |
| 准备开发环境、运行测试、提交改动 | [贡献指南](docs/CONTRIBUTING.md) |
| 理解系统与修改实现 | [工程设计](docs/design.md) · [领域词汇](CONTEXT.md) · [架构决策](docs/adr/) |

欢迎通过 [Issues](https://github.com/lijingda/netizen/issues)反馈问题或提出建议，也欢迎
提交代码与文档改进。开发当前工作区请按贡献指南安装 editable Python 包；
正式发行使用上面的 CLI 安装流程。旧 shell 安装入口已退役，不会自动转换现有实例。
