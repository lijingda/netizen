# netizen-herdr（可选扩展）

补充官方 pane 内 skill，说明 Netizen 的 Codex 如何在 Herdr pane 外通过 CLI 调用其他
agent；只补充外部连接与目标定位的差异，不规定任务编排方式。

这是源码仓库中独立的可选 [Skill](SKILL.md)，不包含在 netizen-cli wheel 中，也不会
自动安装到 Codex skills 目录。它不增加 Netizen 路由、服务或依赖，不修改用户 Herdr
配置，不替用户启动 Herdr server。无需把 Netizen 服务或父 Codex 放进 pane。

## 使用前提

- 最新输入的来源包装带有 `execution_host: "netizen"`，且 `HERDR_ENV` 不为 `1`；
  pane 内使用官方 Herdr skill。
- Netizen 服务有效用户能找到 Herdr CLI 和所需 agent CLI，且目标 Herdr server 已运行；
  当前沙箱和 socket 权限允许连接。目标 agent 使用其自身的登录、权限和审批配置。

## 手动安装与移除

以运行 Netizen 的有效用户操作，从可信的源码 checkout 中复制
`extensions/netizen-herdr/` 到该用户的原生 Codex skills 目录；默认是
`~/.codex/skills/netizen-herdr/`，设置了 `CODEX_HOME` 时以实际生效值为准。
新 CLI 的实例目录没有 `current/source`，不要从实例目录寻找本扩展。将下面的
`netizen_extension_source` 改为实际 checkout 中的绝对路径。

下面的命令仅用于首次安装，目标已存在（含符号链接）就停止，不覆盖旧版本：

```bash
netizen_extension_source="/absolute/path/to/netizen/extensions/netizen-herdr"
netizen_skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
netizen_skill_target="$netizen_skill_root/netizen-herdr"
test ! -e "$netizen_skill_target" && test ! -L "$netizen_skill_target" &&
  mkdir -p "$netizen_skill_root" &&
  cp -R "$netizen_extension_source" "$netizen_skill_target"
```

确认 Codex 能发现 `netizen-herdr`。如需显式指定本扩展，使用 `$netizen-herdr`；
`$herdr` 仍指定官方 pane 内 skill，不会自动重定向。

Netizen 包更新不维护这份可选扩展，也不会更新手动安装的 Skill；卸载 Netizen 不会
删除它。更新扩展前从所选源码版本比较并备份已安装目录；卸载扩展时将该目录移到 skills 目录外。移除 Skill
不会停止已启动的 agent。本文档不代表目标环境已通过端到端验收。
