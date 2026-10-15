# 原生能力与 Codex App/CLI 差异

[手册目录](user-guide.md) · [命令索引](commands.md)

本页内容：

- [Codex Skills](#codex-skills)
- [MCP 登录](#mcp-登录)
- [与 Codex App/CLI 的差异](#与-codex-appcli-的差异)

## Codex Skills

- 可以直接用自然语言询问 Netizen 的使用方式；本 `netizen-user-guide` Skill 设计为在相关问题上自动匹配。
- 要显式调用其他 Skill，可在普通消息开头写 `$skill-name ...`；同一消息开头可以连续引用多个 Skill。
- Skill 会在 start/steer 前重新发现和校验。名称不存在、已禁用或路径失效时，本条消息不会执行。
- 飞书不提供 `/skills` 浏览命令。想知道当前有哪些 Skill，可以直接用自然语言询问 Codex。
- `$skill` 是普通 prompt 的一部分，不能和飞书 slash control 串成一个消息，也不能放进 Goal objective。
- 本手册随该实例使用的 Python 安装包保存，Netizen 启动时只为自己的 Codex 进程加载内置
  `netizen-user-guide` 与 `netizen-lark` Skills，不写入全局 `$CODEX_HOME/skills`。
  可以显式发送 `$netizen-user-guide <问题>`。升级包后重启实例会加载对应版本；移除实例不删除共享安装的资源，
  包卸载也不删除共享的用户 Skills。各实例仍复用服务账号的原生 Codex 登录和配置。

## MCP 登录

需要 OAuth 的 MCP 提示未登录或授权过期时，可请部署者在运行 Netizen 的主机上，
使用服务的同一系统账号和 Codex 环境，在终端执行 `codex mcp login <服务名>`，
按原生提示完成授权。服务名取自 Codex 的 MCP 配置；飞书当前不提供 `/mcp login`。
远程主机没有浏览器时，可用 `codex mcp login <服务名> --no-browser`，按提示在浏览器
授权后，将回调 URL 粘贴回发起登录的终端。

Netizen 可直接复用原生保存的 MCP 凭证，无须在飞书另行登录；前提是使用相同的
`HOME`、`CODEX_HOME` 和 MCP 配置，且服务能访问该原生凭证存储。在另一台电脑或
另一个系统账号中登录不会自动同步到 Netizen。

登录成功后回到飞书重试。若已有连接仍提示未登录，先核对上述环境；必要时由部署者
在任务结束后，通过 [Admin Web](admin.md#admin-web) 重启目标实例再试。

## 与 Codex App/CLI 的差异

Netizen 复用原生 Codex Thread、历史、配置和工具，但飞书不是 CLI/App 宿主界面。

- 可在 CLI/App 中 resume 飞书创建的原生 Thread；CLI/App 新增的消息不会自动回填飞书。
- 可通过自然语言让 agent [新建、查找、读取和联系普通会话](session-collaboration.md)。
  工具采用 Codex CLI 的原生形态，范围限定为本 Netizen 实例登记的普通会话，并按 Admin
  配置过滤；新任务呈现为当前聊天的新话题。
- Codex App 可直接打开本机工作区文件；飞书对支持的本轮文件改为在终态卡片中按需发送
  到话题。飞书不会自动上传整个工作区或保存 Turn 完成时文件版本。
- `/copy`、`/vim`、`/theme`、`/exit` 和 `/quit` 属于宿主界面或生命周期命令，在飞书中不可用。
- `/plan` 和 `/apps` 当前没有安全、公开的高层 SDK 控制面，在飞书中不可用。
- `$app` 当前不会被包装成原生结构化 attachment。
- `/model`、`/effort`、`/fast` 被统一为 `/new` 和 `/config` 卡片。
- 普通会话、Goal 和 Side 的结构化提问使用飞书选择/填写卡片，回答按各自普通消息消费；它不是原生
  审批界面，也不要求原提问任务一直保持运行。详见[回答 Codex 的问题](messages.md#回答-codex-的问题)。
- 已物化 Thread 必须已持久化且 Delete compatibility gate 可用；删除直接委托 App Server
  处理当前原生活动。不想丢失历史时使用 `/archive`。
- `/release` 只释放飞书服务当前连接的订阅，不删除 Thread；CLI/App 或其他 App Server
  的订阅彼此独立。之后飞书仍可按原 native ID 继续。
- Codex 认证、MCP、Skills、AGENTS、`config.toml`、sandbox 和其他原生配置来自服务用户的标准 Codex 状态，不由每个飞书 Scope 另建一套配置系统。
- Netizen 定时计划独立于 Codex App 的自动化列表；自然语言管理只在 Netizen 的原生执行
  连接中临时提供一个工具，不写用户 MCP 配置，也不改已有会话的指令继承。
- Netizen 服务每次启动都会重新读取服务账号的 interactive login shell 导出环境；写入持久 profile 的 NVM PATH、代理和工具路径在重启服务后生效，不维护飞书专用环境文件。
- 后台服务没有真实 TTY，也不会继承某个已打开终端中的临时 `export`、alias 或未导出的 shell function；这部分无法与宿主终端逐项镜像。
- 全新会话的 approval mode 使用当前 Python SDK 的公开默认 `auto_review`；飞书不提供
  Codex App 的 Ask/Custom 宿主选择器。`/fork` 沿用原生分支继承的权限，不额外覆盖。
