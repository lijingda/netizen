---
status: superseded
superseded_by: 0007
---

# 飞书控制面与 Codex 执行面分离

飞书凭据、命令路由、权限判断和状态库属于控制面；Worker Supervisor 属于受信任执行编排面；每个 Turn 的 Codex CLI 在独立 Runner 中执行。Runner 只挂载目标 Workspace、对话专属 Codex 状态和最小只读工具链，不挂载 Workspace 父目录、其他 Project、共享 Codex home、控制面 socket、宿主凭据或容器运行时 socket。Workspace 在同一 Project 的 Conversation 间共享，详见 ADR 0006。

控制面和 Supervisor 使用 mTLS 或同机 Unix socket 上的窄作业协议通信。Codex permission profile 与 shell 环境过滤是纵深防御；跨对话的首要边界是 Linux mount namespace/容器。Runner 启动时若无法确认目标内核 sandbox、只读根文件系统、网络策略、cgroup 和 mount 隔离有效，必须 fail closed。代价是每个 Turn 都要启动隔离环境，并为每个对话持久化独立 Codex state。
