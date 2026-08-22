---
status: superseded
superseded_by: 0007
---

# 共享 Conversation 的执行上限不可提升

Codex Thread 会把所有历史 prompt 带入后续 Turn，因此一次 Turn 的调用者权限不能消除低权限成员先前植入的指令。Conversation 创建时固定可提交 prompt 的 actor 安全域与 execution ceiling；只要允许 `run_inspect` 成员输入，该 Conversation 就永久保持 `inspect`。Operator 需要写文件或开网络时必须新建只允许相应授权成员输入的新 Conversation/Thread，不能给共享历史原地提权。ACL 收紧可立即降权或停止 Runner，但不能把既有 Conversation 提升到更高能力。
