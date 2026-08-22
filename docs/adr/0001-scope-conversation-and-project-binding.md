---
status: superseded
superseded_by: 0007
---

# 飞书作用域映射当前对话，Project Binding 创建时即不可变

每个 `(tenant, app, chat, scope type, thread)` 飞书作用域可以维护一个当前对话，`/new` 创建新对话，`/resume` 只切换当前指针；存在 `thread_id` 时按话题隔离，普通回复的 `root_id` 不单独切会话。一个 ready 对话在首次 Codex Turn 后拥有一个 Codex Thread。Project Binding 在创建时必须明确选择 Project 或“未绑定”，此后永久固定；切换 Project 总是创建新对话，避免把旧上下文与新代码库混入同一 Thread。Project Workspace 的所有权与共享语义由 ADR 0006 取代。
