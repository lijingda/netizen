# netizen-herdr（可选扩展）

补充官方 pane 内 skill，说明 Netizen 的 Codex 如何在 Herdr pane 外通过 CLI 调用其他
agent；只补充外部连接与目标定位的差异，不规定任务编排方式。

这是独立的可选 [Skill](SKILL.md)，随正式 Release 包和 Source Install 快照提供，但不会
自动安装到 Codex skills 目录，也不增加路由、服务或依赖。无需把 Netizen 服务或父 Codex
放进 pane。

## 使用前提

- 最新输入的来源包装带有 `execution_host: "netizen"`，且 `HERDR_ENV` 不为 `1`；
  pane 内使用官方 Herdr skill。
- Netizen 服务有效用户能找到 Herdr CLI 和所需 agent CLI，且目标 Herdr server 已运行；
  当前沙箱和 socket 权限允许连接。目标 agent 使用其自身的登录、权限和审批配置。

## 手动安装与移除

以运行 Netizen 的有效用户操作，从已安装版本的
`~/.netizen/current/source/extensions/netizen-herdr/` 复制到其原生 Codex skills 目录；
默认是 `~/.codex/skills/netizen-herdr/`，设置了 `CODEX_HOME` 时以实际生效值为准。
也可直接使用解压后的正式 Release 包或源码仓库，将下面的 `netizen_extension_source`
改为其中 `extensions/netizen-herdr/` 的路径。

下面的命令仅用于首次安装，目标已存在（含符号链接）就停止，不覆盖旧版本：

```bash
netizen_extension_source="$HOME/.netizen/current/source/extensions/netizen-herdr"
netizen_skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
netizen_skill_target="$netizen_skill_root/netizen-herdr"
test ! -e "$netizen_skill_target" && test ! -L "$netizen_skill_target" &&
  mkdir -p "$netizen_skill_root" &&
  cp -R "$netizen_extension_source" "$netizen_skill_target"
```

确认 Codex 能发现 `netizen-herdr`。如需显式指定本扩展，使用 `$netizen-herdr`；
`$herdr` 仍指定官方 pane 内 skill，不会自动重定向。

Netizen 升级只更新版本内的扩展副本，不会更新手动安装的 Skill；卸载 Netizen 也不会
删除它。更新前比较并备份已安装目录；卸载扩展时将该目录移到 skills 目录外。移除 Skill
不会停止已启动的 agent。本文档不代表目标环境已通过端到端验收。
