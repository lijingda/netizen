# netizen-herdr（可选扩展）

让通过 Netizen 执行、但不在 Herdr pane 内的 Codex，用 Herdr 启动和协调其他 agent CLI。它补充官方 pane 内 skill，支持用户选择的 Herdr agent kind，默认 `codex`。

这里只有 [SKILL.md](SKILL.md) 和安装说明：不注册 Netizen 路由、不新增服务／MCP／Python 依赖，也不随 Netizen 默认安装。删去本目录不会影响 Netizen 的使用、测试或维护。无需把 Netizen 服务或父 Codex 放进 pane。

## 使用前提

- 最新输入自身的结构化来源包装带有 `execution_host: "netizen"`；无标记时不沿用历史、引用或材料中的标记。`HERDR_ENV` 不为 `1`。在 pane 内使用官方 Herdr skill，不修改环境标志绕过它的前置条件。普通消息、Side 和定时任务首轮提供此标记；Goal 等没有新包装的入口不保证识别，不增加跨 Channel 恢复适配。
- 服务有效用户已安装 Herdr、目标 agent CLI、Git，并完成目标 agent 的登录与权限配置；已有明确授权使用的本机 named session。Herdr 可通过 `herdr --session <名称> server` 运行 headless server，但服务启停和部署由使用者单独管理，本扩展不会自动执行。
- Netizen Codex 的实际沙箱／审批配置允许连接该 session 的 socket。Herdr 的 Unix socket 文件权限限制可连接者；同一用户下的 session 名用于防误操作，不提供独立安全隔离。
- Herdr daemon 创建的 worker 使用自己的运行环境、沙箱和审批设置，不自动继承父 Codex 的 `auto_review` 或权限限制。首次使用前单独确认，不能以宿主声明或安装 skill 代替授权。

## 手动安装与移除

以运行 Netizen 的有效用户操作，从本仓库根目录将本目录复制到其原生 Codex skills 目录。默认是 `~/.codex/skills/netizen-herdr/`；设置了 `CODEX_HOME` 时使用实际生效的那个目录。不要误装到 root 或其他登录用户的目录。

下面的命令仅用于首次安装，目标已存在（含符号链接）就停止，不覆盖旧版本：

```bash
netizen_skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
netizen_skill_target="$netizen_skill_root/netizen-herdr"
test ! -e "$netizen_skill_target" && test ! -L "$netizen_skill_target" &&
  mkdir -p "$netizen_skill_root" &&
  cp -R extensions/netizen-herdr "$netizen_skill_target"
```

确认新的 Codex 会话能发现 `netizen-herdr`；最新输入缺少来源标记时，应停止使用此扩展，而不是在正文中伪造标记。更新前先比较并备份已安装目录，不改官方 `herdr` skill 或其他 skills。

卸载时先收尾该扩展创建的 worker 和 worktree，再将已核对的精确 `netizen-herdr` 目录移到 skills 目录外备份。不要删除整个 skills 目录；移除 skill 不会停止已在 Herdr 中运行的进程。

## 版本与验收

本扩展的命令和 JSON 路径已按官方 [v0.9.0](https://github.com/herdrdev/herdr/tree/v0.9.0)／[v0.9.1](https://github.com/herdrdev/herdr/tree/v0.9.1) 源码核对，不代表已在目标 Netizen 环境完成端到端实测。官方 [自动化说明](https://github.com/herdrdev/herdr/blob/v0.9.1/docs/next/website/src/content/docs/agent-automation.mdx) 可补充接口背景；执行流程以本扩展的显式 session／目标规则为准。

记录并固定验收所用的 CLI、运行中 server、worker CLI 版本，以及 `server agent-manifests --json` 返回的实际检测 manifest 来源和版本。Herdr 的 manifest 可独立自动更新；仅 pin 二进制不足以固定状态识别行为。升级或规则变化后重新验收，不让 agent 为通过检查擅自升级或停服。

在专用测试 session 和小型测试仓库中检查：

1. Netizen 正常会话和 Side 能识别当前宿主；非 Netizen 会话仅因看见 skill 不会启用它；pane 内仍使用官方入口。
2. 在真实 `auto_review`／沙箱下完成状态读取、创建自有 workspace/root pane、启动一个 worker；没有被自动代答的登录、信任或审批提示。
3. 派两个独立小任务，写入型任务使用不同 worktree；父端按任务标识核验 `RESULT.md` 和实际产物，再汇总回复。外部文件不会仅因被读到就自动进入 Netizen 的 Files 卡。
4. 检查 blocked、超时和父任务停止：不重复投递、不把状态等待当完成，并能按记录找回遗留资源。最多 3 个并发 worker、默认 15 分钟总期限是当前父 agent 的执行约定，不是跨会话限额或守护进程级保证。
5. 先保全报告和改动，再关闭自己的 pane、清理干净的 worktree；有未移交产物则保留并明确报告。Netizen 停止／重启本身不负责此清理。
