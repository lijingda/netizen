---
status: superseded
superseded_by: 0007
---

# 同一 Project 的 Conversation 共享唯一工作目录

Project 绑定代表一个管理员登记的本地工作目录，而不是用于派生 clone 的代码来源。同一 Project 下的所有 Conversation 都把这个目录作为 Codex working directory；每个 Conversation 仍拥有独立的 Codex Thread、`CODEX_HOME`、权限上限和飞书上下文。`/resume` 恢复对话历史，但不会恢复该对话创建时的文件快照。

不同 Conversation 不设应用层固定并发上限，可以同时运行；同一 Conversation 最多一个活动 Turn，忙时的新输入固定 steer 到该 Turn，不排队下一 Turn。主机资源仍由 systemd 和消息接入背压保护，但它们不参与 Conversation 调度语义。共享目录中的未提交修改、中间状态、Git 锁、覆盖和冲突会相互可见，这是与 Codex App/CLI Local 模式一致的有意语义，Netizen 不自动加 Project 锁、合并 patch 或回滚文件副作用。Project 因而也是文件可见性的安全域，只能把同一信任组放入其 ACL。

未绑定的 Conversation 继续使用私有草稿目录。若以后提供 clone/worktree 隔离，它必须是显式选择的执行模式，不能改变默认 Project 语义。

本决定取代 ADR 0001 中“Conversation 拥有 Project Workspace”的表述，以及 ADR 0002 中“多人模式默认独立 clone”的表述；Project Binding 仍在 Conversation 创建后保持不可变。
