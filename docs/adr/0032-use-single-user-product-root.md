---
status: accepted
date: 2026-08-21
related: 0022, 0023, 0031
---

# 使用单一用户产品根

## 背景

Netizen 每个 Unix 用户只运行一个实例，却把 release、配置、状态和缓存分布在四个固定的
XDG 风格目录中。同时，安装身份又有意忽略所有 `XDG_*` override，以防同一个 user service
在不同 shell、Agent 或 sudo 环境中漂移。目录分散因此没有提供可选 profile 或多实例能力，
反而增加了查找配置、备份状态和解释部署布局的成本。

## 决定

Netizen 的唯一产品根固定为 effective user 账号 home 下的 `~/.netizen`。它包含根级
`config.yaml`、`credentials/`、`state/`、`releases/`、`cache/` 以及 `current`/`previous`
release 指针。配置、凭据和 Channel 状态仍位于 release 之外；Project 仍只是 canonical cwd，
不会被复制到产品根。安装器继续忽略 `XDG_DATA_HOME`、`XDG_CONFIG_HOME`、
`XDG_STATE_HOME` 和 `XDG_CACHE_HOME`，也不把产品根解释为 profile 或第二实例选择器。

systemd user unit 继续位于 systemd 发现的 `~/.config/systemd/user/netizen.service`；受管用户
指南 Skill 继续位于 `${CODEX_HOME:-~/.codex}/skills`。它们是各自宿主系统的集成资源，不属于
Netizen 产品根。

`~/.netizen` 是跨卸载保留的容器，不是递归删除目标。安装器只把 `releases/` 和 `cache/`
视为带精确 ownership marker 的可删除目录；卸载还会删除经过校验的 `current`/`previous`
指针，但保留 `config.yaml`、`credentials/`、`state/` 以及其中必须跨卸载存续的安装锁。
任何持久配置、Project 或 `CODEX_HOME` 路径位于 `releases/` 或 `cache/` 内都必须 fail closed。

## 后果

用户只需定位和备份一个 Netizen 根，同时仍可按子目录区分配置、状态、缓存和 release 的
不同生命周期。安装、候选校验、回滚和卸载必须以真实的可删除子树为安全边界，不能再把
整个程序根传给持久路径检查或递归删除。
