---
status: accepted
date: 2026-08-26
amends: 0042
---

# 移除冗余的正式发布人工审批

ADR 0042 把代码资格固定到 exact main push CI，并把正式发布流水线收窄为制品身份、完整性和
不可变发布。初始配置仍要求仓库所有者在 `published-release` environment 中审批。该审批不
展示 main 门禁之外的新技术证据，而且审批人与创建 tag、触发 workflow 的维护者是同一账号，
因此不能发现代码或制品缺陷，也不能形成实际的职责分离。

## 决定

1. `published-release` environment 继续用于 GitHub deployment history，但不配置 required
   reviewer、wait timer 或自我审批规则。
2. 正式发布意图由两个显式动作共同表达：维护者创建与项目版本一致的受保护 exact `vX.Y.Z`
   tag，再手工 dispatch release workflow 并传入该 tag。项目不因 main push 或 tag push 自动
   发布。
3. 发布仍必须核对 tag 指向的 exact commit 已有成功的 main push CI，只构建一次 deterministic
   candidate，验证 manifest、tag、commit 和 SHA-256，并发布 Immutable Release。只有最终
   publish job 拥有 `contents: write`。
4. 若以后出现独立发布角色、限定发布时间窗、environment Secret 或合规上的职责分离，再通过
   新决策恢复与该风险相匹配的 environment protection；不得把 reviewer 点击描述为代码质检。

## 后果

正式发布不再因同一维护者的重复确认而停顿，deployment history 和全部机器可验证门禁保持
不变。误发布仍需要先创建一个新受保护 tag，再单独手工 dispatch；同名 Release 不可覆盖，
tag 不可移动或删除。账号失陷风险不因本次变化实质增加，因为原 required reviewer 正是同一
账号；这类风险应通过账号安全、最小 workflow 权限和不可变 release 处理。
