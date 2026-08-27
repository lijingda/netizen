---
status: accepted
date: 2026-08-24
amends: 0017, 0031
amended_by: 0038
supersedes: 0019
related: 0014, 0026
---

# 用薄 SDK Gap Adapter 与原生目录对账开放 Thread Delete

> 修订说明：ADR 0038 只为普通 `/sessions` 增加 exact idle 会话的两阶段删除入口；本 ADR
> 定义的原生删除与四视图对账保持不变，`/delete` 文本命令仍为 current-only。

## 背景

[ADR 0019](0019-keep-native-thread-delete-unavailable.md) 记录了
`openai-codex==0.144.4` 的 `thread/delete` 会报错但仍让目标从目录消失，因此暂停了
materialized Thread Delete。仓库现已锁定 `openai-codex==0.147.0` 及其 bundled
App Server/CLI `0.147.0`；Python 高层 facade 仍没有 Thread Delete，但 App Server
继续提供固定的 `thread/delete`。

2026-08-24 对该 exact 版本重新执行了 disposable live probes：

- active persisted Thread 删除成功；archived Thread 删除成功；
- 真实 root → subagent → grandchild 树由 App Server 一次删除，四个目录视图均不再出现；
- 请求写出后取消调用方时，服务端仍可能完成删除；故意丢失响应时也观察到服务端成功；
- ephemeral Thread 明确拒绝且仍可读取；Lazy、尚无 rollout 的 Thread 不能 archive，
  但同一 App Server 进程可 delete；
- 对已确认删除的 ID 再次调用，在 `0.147.0` 仍返回 `no rollout found`，不能把 delete
  当作客户端可安全重发的幂等操作。

这说明原生能力本身已经可用于产品，但调用方必须处理“报错或取消并不证明没有副作用”。
官方 [App Server 文档](https://developers.openai.com/codex/app-server/#delete-a-thread)
定义了 root 与 spawned descendants 的级联删除；Netizen 不应复制这棵树或模拟删除。

## 决定

### 只增加一个固定方法的生产 Adapter

生产 `ServiceCore` 可以构造 `AppServerThreadDeleteControl`。它复用唯一、已初始化的
`AsyncCodex` client，只暴露 `delete(thread_id)`，并固定调用：

```text
thread/delete { threadId }
```

Adapter 直接使用安装 SDK 的 generated `ThreadDeleteParams/Response` 做 shape 校验；不
暴露 generic request、字符串 method、任意 response model、第二个 App Server、CLI 输出
解析或本地原生状态修改。若 ownership edge、generated model 或 synthetic contract 不
匹配，该 capability 单独 unavailable，服务仍可运行。

`facade_migration_requirements` 继续监测 `AsyncCodex.thread_delete` 或
`AsyncThread.delete`。公开 facade 出现后，升级门禁必须先失败，推动调用点切回公开 API
并删除本 Adapter；不能让临时边界永久化。

### `/delete` 固定当前会话与 exact native identity

Lazy Binding 的既有行为不变：红色二次确认后只删除本地 Binding。materialized
Binding 仅在 Delete capability 可用时生成红色确认卡；卡片明确说明原生 Thread、spawned
descendants、Codex App/CLI 历史和本地 Binding 都会永久消失，并携带：

- exact Binding ID；
- 打开卡片时的 exact native Thread ID；
- exact Feishu Scope envelope。

提交时在 Scope 锁内重新确认当前 Binding 与 native ID 均未变化。旧 Lazy 卡不能删除后来
物化的 Thread，旧 materialized 卡也不能删除后来换绑的 Thread。入口仍只作用于当前
Binding，不新增 `/delete <id>`。共享 Management Runtime Port 增加 exact delete primitive
供同一个 current-Binding application service 使用，但 Admin Web controller 与 HTTP route
仍只提供 `delete-lazy`；materialized Admin route 若要开放需另行决定其确认与授权界面。

原生删除开始前，Runtime 必须通过公开 read 证明目标 persisted、non-ephemeral 且 idle，
并与 ordinary Turn、Goal、compaction 和其他 lifecycle mutation 互斥。running、stopping、
ephemeral、未持久化或状态不可读都不能直接发 delete。

### native-first，并对失败做一次有界四视图对账

删除顺序固定为原生 Thread 在前、本地 Binding 在后：

1. App Server 正常返回空成功响应时，认为原生删除成功，随后在 Channel Database 删除
   exact Binding；不增加一次成功后查询。
2. delete 抛出非取消异常时，不自动重发。Runtime 在同一 Binding 生命周期槽内只做一次
   有界只读对账，分别查询 rollout scan 与 state DB 的 active/archived catalog，共四个
   exact-ID 视图。
3. 任一视图仍存在目标时，原生删除按“未完成”处理：保留 Binding，释放本次 lifecycle
   槽并保持 admission 开放，让用户重新确认后重试。
4. 四个完整视图都不存在目标时，按“原生已删除”处理，提交本地 Binding Delete。这样
   覆盖 App Server 报错或响应丢失但副作用已经完成的情况。
5. 视图冲突、分页/shape 错误、超时、查询取消或其他无法得到完整四视图结论时，状态为
   unknown：保留 Binding 与 lifecycle-unknown，关闭进程级 admission，绝不猜测或重发。
6. 调用前的公开 resume/read 若失败，但同一次四视图对账明确目标均不存在，则允许只提交
   本地 Binding Delete。这是服务在“原生成功、本地提交前退出”后的恢复路径；若任一视图
   仍存在或查询不完整，则不进入原生 mutation，也不删 Binding。
7. 原生成功或确认不存在后，本地 Binding 提交若失败，同样保留 unknown 并关闭 admission。
   Channel SQLite 不记录中间态；重启后由上一步的 absent 对账收尾。

取消发生在 delete request 可能已经写出之后，当前 task 不再尝试屏蔽取消或继续查询；它
直接保留 unknown 并关闭 admission。正常服务重启清除进程内槽位，用户再用同一确认流程
对账。这里的“可重试”只发生在目录明确仍存在时，不等于盲目再次发送同一个 RPC。

### 级联语义归 App Server，候选发布必须实测

Netizen 只提交 root ID，不解析 descendant IDs，不建立 native metadata index，也不在
Channel Database 复制父子关系。成功语义采用 App Server 的 root + spawned descendants
契约。

候选 `lifecycle` live gate 必须在目标账号、目标 exact SDK/App Server 上创建 disposable
persisted Thread，完成 seed、公开 rename/archive/unarchive，再经薄 Adapter delete；最后
确认 rollout scan 与 state DB 的 active/archived 四视图全部 absent。shape/synthetic
harness 继续覆盖 exact method/params、空响应、response loss 和 facade migration sentinel。
任一门禁失败只关闭 materialized Delete capability，不得改走 CLI、数据库或第二客户端。

## 非目标

- 不把 `thread/delete` 扩张为通用私有 RPC gateway。
- 不在 delete 失败后自动调用第二次 delete。
- 不把 Binding Delete 与原生删除伪装成跨数据库事务。
- 不扫描、缓存或逐个删除 spawned descendants。
- 不改变 archive、release、uninstall 或 Side ephemeral cleanup 的语义。
- 不借本决定开放 Admin materialized delete、批量删除或 `/delete <id>`。

## 结果

materialized `/delete` 恢复可用，同时把 `0.144.4` 已暴露的“错误但已产生副作用”纳入
显式三态：present、absent、unknown。正常成功路径保持最薄；失败路径只读对账一次，既
不会因盲目重试扩大副作用，也不会因丢失成功响应永远遗留本地 Binding。

代价是失败对账要完整读取四套分页目录，最坏受统一 deadline/page/item limits 约束；目录
不可判定时会关闭整个 Runtime admission，需要重启恢复。这是不可逆 native mutation 的
有意 fail-closed 边界。
