---
status: accepted
date: 2026-08-22
related: 0022, 0023, 0032
amended_by: 0035, 0044, 0045
---

# 使用官方 SDK 完成飞书应用初始化

## 背景

Netizen 的运行身份是飞书/Lark 自建应用机器人，但首次安装过去要求用户先在开发者后台
自行创建应用、逐项配置权限/事件/卡片回调，再把 App ID 与 App Secret 手工填给安装器。
这既是安装流程中最容易漏配的一步，也让“能启动服务”和“机器人具备 Netizen 的完整能力”
成为两个没有机器可读契约的状态。

Lark CLI 提供了方便的一键创建体验，但把它作为 Netizen 的安装依赖会额外引入另一套 CLI
安装、版本、配置和认证生命周期。复制它背后的私有 HTTP/device-flow 协议又会让 Netizen
承担协议兼容与凭据安全责任。官方 Python OpenAPI SDK 已公开 `register_app`，可以通过
device flow 创建或更新应用，并在确认页预填应用身份权限、应用身份事件和卡片回调。

## 决定

首次有 TTY 且飞书凭据不完整时，零参数 `./install.sh` 默认提供官方浏览器/二维码初始化；
用户也可显式选择继续手工输入。已有标准 `instance.appId` 但 Secret 缺失时，流程更新该
exact App，而不是静默创建另一应用；全新骨架只允许创建新应用。浏览器流程失败后回退到
手工输入，Ctrl-C 仍中止安装。已有完整凭据和无 TTY 安装不启动 device flow；后者继续只
生成骨架和受保护凭据文件，然后给出精确补全步骤。

应用初始化固定使用官方 `lark-oapi==1.7.2`，终端二维码固定使用 `qrcode==8.2`，都随候选
release 安装。安装器先构建并验证候选 release，再用其中的独立 helper 调用
`lark_oapi.register_app`；不安装、调用或读取 Lark CLI。该 device flow 只用于授权创建者
创建/更新 Bot 应用，不建立 Netizen 的用户身份运行态：配置使用最小模板，只声明 tenant
scopes、tenant event `im.message.receive_v1` 和 callback `card.action.trigger`，不申请 user
scope/event，不保存 user token 或 SDK 返回的 user info。

显式 tenant scopes 是当前产品能力的部署契约：`im:message`、
`im:message.group_msg`、`im:chat.members:read`、
`im:message.reactions:write_only`、`im:resource` 与
`im:message:send_as_bot`。SDK 只负责把这些公开配置带到平台确认页；租户管理员审批、按租户
策略发布应用版本、配置可用范围以及把机器人加入目标群仍由用户在飞书/Lark 完成。

helper 的 stderr 只显示一次性验证 URL、二维码和非敏感进度；成功前 stdout 保持为空，
成功后只向父安装器的捕获 pipe 写一份版本化 JSON 凭据。父安装器不把 stdout 转发到终端，
也不把 Secret 放入 argv、环境、YAML、unit 或日志；它验证 exact shape/App ID 后，用原子
文件替换把 App ID 写入 `~/.netizen/config.yaml`，把 raw Secret 写入现有 `0600`
`~/.netizen/credentials/feishu-app-secret`。整个 helper 有 660 秒父进程 deadline，覆盖 SDK
默认 600 秒确认窗口并限制卡死请求。异常信息和 dataclass repr 均不得包含 Secret。

## 后果

新用户从一个安装入口即可得到与 Netizen 能力相符的 Bot 应用配置，同时仍可在受限网络、
已有应用或自动流程不可用时走手工路径。运行服务不依赖 Lark CLI、device flow 或用户
OAuth 状态；新增 SDK 只增加候选 release 的安装体积。

交互式首次安装需要先完成候选 venv 和 release gates，之后才能展示二维码；因此 Codex
登录、Python 包下载或候选检查失败会早于飞书初始化。应用侧审批、发布、可用范围和群成员
关系仍是明确的外部完成项，安装器不能把取得凭据误报为这些步骤已经完成。

## 否决的方案

- 直接依赖 Lark CLI：安装体验依赖另一 CLI 的版本、配置目录和认证生命周期。
- 在安装器复制 registration HTTP 协议：会绑定非公开 wire contract，并扩大 Secret 处理面。
- 保持只允许手工 App ID/Secret：无法消除最常见的权限、事件和 callback 漏配。
- 为 Netizen 建立用户 OAuth：改变 Bot-only 身份与权限边界，且运行时并不需要用户 token。
