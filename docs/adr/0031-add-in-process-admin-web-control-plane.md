---
status: accepted
date: 2026-08-21
amends: 0017, 0021, 0028
amended_by: 0037, 0049
related: 0016, 0019
---

# 引入进程内 Admin Web 管理控制面

> **修订：** [ADR 0049](0049-bound-turn-observation-and-delegate-thread-removal.md)
> 删除 Admin exact archive 的 idle 与 Runtime activity 前置条件；它与飞书入口一样只占用
> exact Binding 后直接委托 App Server。Admin materialized Delete 仍不开放。

## 背景

飞书卡片适合当前 Scope 的日常控制，但不适合在 Project、普通 Binding 和 Side Topic
数量增长后承担全实例检索与跨 Scope 管理。继续扩展 `/settings` 会让实例级管理受卡片
容量、当前消息位置和 Scope 参与者权限约束；另起一个 Web 服务则会产生第二个
`AsyncCodex`、第二个 SQLite writer 或必须自建的内部 RPC，无法共享 Runtime 的 Turn、
Goal、compaction、subscription 和 lifecycle-unknown 状态。

Netizen 因此需要一个仅用于管理、由实例管理员操作的 Web 控制面。它不是新的 Prompt
Channel，也不是另一个 agent runtime。飞书 `/settings` 的 Project 能力继续保留给现有
使用者；Admin Web 提供同一 Project Registry 的全局视图，并补充普通会话和 Side Topic
的集中管理。

## 决定

### 单进程、单 Runtime、两个控制面

Admin Web 默认启用，默认监听 `0.0.0.0:8787`，允许在现有 Netizen YAML 中覆盖 host、
port 或显式关闭。服务只有在 Feishu Channel、Admin Web、Channel Database 和唯一
`AsyncCodex` 全部准备完成后才报告 ready；默认端口绑定失败属于启动失败，不能静默降级。
这是把管理面视为默认产品能力后的有意强耦合：升级可能因端口冲突而拒绝启动，但不能让
管理员误以为已启用的全实例控制面实际不可用。

HTTP socket 可以在启动中先绑定以保留端口，但在 shared application service、Runtime 和
所有 stores 全部 ready 前，login、query 和 mutation 一律返回 unavailable，不能在半初始化
对象上执行。正常 shutdown 先关闭 Admin admission 和 Feishu admission，拒绝新请求并有界
排空已进入 application service 的 Admin handlers，之后才开始 Turn/Side cleanup 和关闭
Codex transport；在途原生 mutation 仍使用相同的 unknown-side-effect 语义，不能靠取消
HTTP request 假装回滚。readiness endpoint 在启动和关闭期间都返回 not ready。

Admin Web 与 `FeishuChannel` 运行在同一个长驻 Python 服务和同一个异步事件循环上，共享
唯一的 `BindingStore`、`ProjectRegistry`、`CodexRuntime` 和 `AsyncCodex`。不得为 Web
创建第二个服务进程、数据库连接所有者、Codex client、App Server、状态副本或内部通用
RPC。HTTP handler 不能直接写 SQLite 或调用 Codex SDK；飞书 controls 和 Web actions
必须进入同一个 typed application service，再由它统一取得 Scope coordinator、Binding
Runtime lock 和原生 lifecycle slot。

这里的“同一个异步事件循环”特指现有 `FeishuChannel` background loop：`ServiceCore`、
Channel handlers、Admin runner/handlers、application service 和 Runtime locks 都归属该 loop。
`main()` 的 `asyncio.run()` orchestration loop 只处理 signal，并通过 `channel.schedule(...)`
等待 background-loop lifecycle；它不得执行 Web handler、切换 Admin admission 或取得共享锁。

Web 不接受 Prompt、图片、Skill 或 Goal objective，不调用 `thread.turn()` / `steer()`，
也不提供原生历史浏览器。它可以停止已有操作和执行下文明确列出的管理 mutation，但不能
成为第二个 Codex 交互客户端。前端资源与 JSON endpoints 由同一 origin 提供，不增加独立
前端部署、WebSocket 或 durable Web job queue；运行态刷新使用有界 HTTP polling。

### 单管理员认证与直接 URL 访问

`Instance Administrator` 是持有 Admin Web credential 的 Netizen 服务运维者，拥有全实例、
跨 Scope 的管理权；它不是 Channel Participant、Binding creator 或 Project owner。
V1 不增加多用户、角色、RBAC、Feishu 身份登录、OIDC 或 Project ACL。

安装器生成至少 256 bit 随机的独立 Admin secret，固定保存在 release 之外的
`~/.netizen/credentials/admin-web-secret`，与 `config.yaml` 和 `feishu-app-secret` 同属受管配置，
跨升级、回滚和默认卸载保留。文件必须为 `0600` 普通文件；缺失、权限过宽、是 symlink 或
不是普通文件时，默认启用的服务必须启动失败。它不得复用 Feishu App Secret、Codex
credential，不得进入 YAML、URL、cookie、页面源码、命令参数或日志。secret 轮换必须使
已有登录 session 全部失效，比较 credential 时使用 constant-time 校验。

`GET /login` 先签发一次性 pre-auth CSRF nonce，登录 POST 必须同时验证该 nonce、`Origin`
和 `Host`，成功后用 secret 换取进程内、至少 256 bit 随机的 opaque session。session 使用
两小时 idle TTL 和十二小时 absolute TTL；cookie 必须 `HttpOnly`、`SameSite=Strict`。因为
已决定直接使用 HTTP，它有意不设置只适用于 HTTPS 的 `Secure`。登录以后所有 mutation
必须校验 authenticated session、一次性 CSRF token、`Origin` 和 `Host`，且只接受 POST。
Host 必须是启动时发现的本机 interface IP 或 localhost，Origin 必须与 exact scheme、Host
和 port 相同；V1 不信任 forwarded headers 或 reverse proxy identity。响应使用
`Referrer-Policy: same-origin`：它不向跨 Origin 请求发送 referrer，同时保留同源普通 HTML
form POST 的真实 `Origin`；不能改成会把该 `Origin` 序列化为 `null` 的 `no-referrer`。

登录失败按 source IP 每五分钟最多五次、全进程每五分钟最多二十次，超限只返回统一错误，
不泄漏 credential 是否接近或存在。所有 HTTP header、URL、form body、JSON body、并发连接
和处理时间都有硬上限，Web 不接受文件上传。服务重启使全部 Admin session 失效。实现不得
把登录 session、CSRF token 或 audit record 写入 Channel Database。

管理员直接访问 `http://<server-ip>:<port>`，不要求 SSH tunnel、TLS、reverse proxy 或
外部 IdP。部署明确接受 Admin secret 和 session cookie 在受信内网 HTTP 上不加密传输的
风险；`0.0.0.0` 只是监听范围，独立 Admin credential 仍是授权边界。未来若公开到不受信
网络或引入多个管理员，必须另立 ADR 选择 TLS termination、外部身份与审计模型，不能在
本边界上逐步长出自建 RBAC。

### 页面与查询模型

Admin Web 提供三个一级页面：

1. **Projects**：显示 alias、canonical cwd、enabled、revision、普通/Lazy/归档会话数量和
   最近 Binding 活跃时间；支持登记已有目录、在 `projectRoot` 内创建空目录和启停。它与
   飞书 `/settings` 调用同一个 Project Registry，使用同一个 revision/CAS 约束。
2. **Sessions**：分页显示普通 Binding 的 native title/preview、Binding/native Thread
   ID、Project、Scope、active/lazy/materialized/archived 状态、Turn Settings、创建/激活
   时间和当前进程可观察的运行态；按 Project、Scope kind、chat/topic ID、持久状态和时间
   过滤，并支持 Binding/native Thread/chat/topic 等 Channel-owned ID 查询。
3. **Side Topics**：单独显示 Side route、Parent Binding、派生 Project、chat/topic/root
   identity、状态和时间。Side 不伪装成可 resume、rename 或 archive 的普通会话。

SQLite 只提供 Channel-owned Scope、Binding、Project 和 Side route 数据。Thread name、
preview 和 archived membership 每次通过公开、分页的 Codex catalog 读取；当前 Turn、Goal、
compaction、subscription 和 lifecycle 状态只读自本进程 Runtime snapshot。不得为了 Web
搜索、排序或展示把这些 Codex-owned 或瞬态值复制进 SQLite。飞书 chat/topic 显示名只做
尽力 live enrichment，失败时回退稳定 ID，不作为 mutation 的身份或前置条件。

结果列表必须 server-side 有界分页，不能把全量 Binding 或 native catalog 编进一个页面。
Project/Scope/ID/时间查询先作用于 Channel-owned 索引，只为当前结果页 live hydrate native
title/preview；V1 不提供需要每次扫描完整 Codex catalog 的全局 title/preview 搜索或排序。
archived 过滤确实需要完成公开 archived catalog 分页，读取失败时整次查询明确失败，不能把
不完整结果冒充完整命中。普通 topic 的持久分类仍只有 `topic`；在没有可靠 live metadata
时，界面不得虚构“P2P 话题、群话题、话题群”等更细类别。

### 管理 action 的共同契约

每个 mutation 使用服务器生成的一次性 action token 防止双击或浏览器重发，用独立的一次性
CSRF token 防止跨站请求，并携带 exact Project revision，或 exact Scope key、完整
Binding/Side ID 及该操作要求的 settings revision、active pointer 和 native catalog 前置
条件来拒绝陈旧页面。提交时必须在共享 Scope coordinator 内重新读取目标，随后由 Runtime
在 exact Binding/Side lock 内再次验证原生活动；页面快照从来不是事实源。action token
只在进程内有界保留，服务重启后依赖 live 前置条件对账；原生 mutation 不做隐式重试。

所有涉及 ordinary Binding 的入口统一按 `Scope coordinator -> exact Binding Runtime lock`
取锁，持有 Binding lock 时绝不反向获取 Scope coordinator。Side action 只取得 exact Side
lock，不与 Scope lock 嵌套；Project action 只执行自身的短 Registry transaction。原生
mutation 和随后 active pointer 提交继续位于同一协调临界区，任一步结果未知都按下文保留
占用，而不是释放锁后猜测或补偿。

Admin action 不通过先激活目标 Binding 来获得操作资格。只有显式“设为当前会话”会修改
Scope active pointer；它不停止原 active Binding 的 Turn。归档 active Binding 成功后清空
该 pointer，归档 inactive Binding 不改变它。恢复归档与“恢复并设为当前”是两个明确动作。
即使原 active Binding 正在运行，管理员也可以显式切换 pointer；页面必须提示原 Turn 仍在
运行。之后管理员仍可按 exact Binding Stop，飞书参与者也可先 `/resume` 回原 Binding 再
steer/stop，这与现有 `/new`、`/resume` 不停止其他 Binding Turn 的语义一致。

一旦原生 mutation 已发出而响应、取消或本地提交结果未知，继续沿用 Runtime 的
`lifecycle-unknown` 和 service-wide admission close 语义；页面必须报告“结果未知、需要重启
对账”，不能显示成功、自动重试或允许另一个入口绕过。所有 mutation 记录结构化 journald
日志，只包含 action kind、opaque Admin session ID、目标 ID、开始/终态和错误类型，不记录
credential、CSRF、name、preview、cwd 或 Prompt 内容。

### 操作矩阵

| 对象 | Admin 操作 | 前置条件与确定结果 |
| --- | --- | --- |
| Project | 登记已有目录 | alias 尚不存在；路径符合现有 Project Registry 的可用 canonical absolute directory 规则；创建一条 enabled Registry 记录。 |
| Project | 创建空目录 | alias 尚不存在；消除 symlink/`..` 后的 canonical 目标严格位于 `projectRoot` 且尚不存在；创建目录和 enabled Registry 记录。 |
| Project | 启用 / 停用 | exact revision 匹配；停用只阻止新 Binding，不影响已有 Binding 或删除 cwd。 |
| Project | 删除 / 改路径 | 不提供。Project cwd 与已有 Binding 的解释保持稳定，Netizen 从不删除 Project 目录。 |
| Binding | 创建 Lazy 会话 | 只允许已存在的 Scope 和 enabled Project；显式选择“仅创建”或“创建并设为当前”，不启动 native Thread。 |
| Binding | 设为当前会话 | exact Binding 属于目标 Scope、未归档；原 active Binding 的 Turn 不停止、不迁移。 |
| Binding | 修改 Turn Settings | exact settings revision；与 running/stopping Turn、Goal、compaction 和 lifecycle mutation 互斥；只保存 Binding-scoped intent。 |
| Binding | 重命名 | exact materialized Binding；可 active 或 inactive；使用公开 `set_name()`，不复制名称到 Channel Database。 |
| Binding | 归档 | exact materialized、persisted、idle、未归档 Binding；与 Turn、Goal、compaction 和 lifecycle mutation 互斥；仅当它当前 active 时清空 Scope pointer。 |
| Binding | 恢复归档 | exact Binding 必须在 live archived catalog；公开 unarchive 后 exact resume 验证同一 native ID，保留原 Binding/Turn Settings 和 Scope pointer，并立即按 inactive 策略尝试释放新订阅。 |
| Binding | 恢复并设为当前 | 满足恢复条件；公开 unarchive 和 exact resume 成功后在同一 Scope coordinator 中设置 exact active pointer，并按 current warm-window 策略保留订阅。 |
| Binding | 删除 Lazy 会话 | exact Binding 仍没有 native Thread 且没有 Runtime 活动；原子删除 Binding，并仅在它 active 时清空 pointer。 |
| Binding | 删除 materialized 会话 | 不提供。ADR 0037 只恢复飞书 current `/delete`；Admin 不调用该 Adapter，也不只删本地 Binding。 |
| Binding | Stop | exact Binding 当前存在本进程可控的普通 Turn 或 Goal 物理 Turn；复用现有 interrupt、Goal pause 和 terminal cleanup 语义。Stop 中的 Goal pause 是停止既有工作的一部分，不开放 Goal start/resume/clear。 |
| Binding | Release | exact active 或 inactive materialized Binding 已 idle，且满足 ADR 0028 除“当前 active”外的无活动/后台 terminal 前置条件；只取消本连接订阅，下一次消息仍 resume 同一 Thread。 |
| Binding | Compact / Goal mutation | 不提供。它们会发起或推进 Codex 工作，继续从当前飞书会话控制；Web 只展示状态。 |
| Binding | 查看完整历史 / 发送 Prompt | 不提供。Web 最多展示 native catalog 的有界 title/preview。 |
| Side Topic | Close | exact route 为 open 且当前进程仍有 Side Session；复用 interrupt、cleanup、unsubscribe、墓碑提交顺序，结果未知时保持 non-admitting route。 |
| Side Topic | 删除墓碑 / resume / rename / archive | 不提供。terminal route 永久阻止旧话题落入普通 Binding，ephemeral native Thread 不可恢复。 |
| 多个对象 | 批量 native mutation | 不提供。筛选和多选不能转换为批量 archive、stop、close 或 delete；每个未知副作用必须独立停住。 |

操作矩阵中一切非当前或跨 Scope 的 create/activate/configure/rename/archive/unarchive/
delete/stop/release/close 都是 ADR 0017、0021、0028 当前飞书入口限制之外的 Admin
authority。实现必须把 Runtime 的“当前 active”产品检查从原生 safety 检查中拆开：飞书
路径继续要求当前 Scope/exact active Binding，Admin 路径则要求 exact target 和上述矩阵
前置条件；两条路径最终调用相同的原生 operation，不能复制两套 lifecycle 实现。

### 飞书入口保持不变

`/settings` 的 Projects 分区、`/sessions`、`/resume`、`/config`、`/rename`、`/archive`、
`/unarchive`、`/delete`、`/stop`、`/release` 和 Side controls 保持现有用户语义。普通
Channel Participant 仍只能管理消息所在 Scope 的 Binding 或 exact Side；Admin 的实例级
权限不能通过飞书身份、Binding creator 或卡片 payload 获得。Project Registry 同时由两个
入口操作时，revision/CAS 和 stale UI 处理保证只有一个提交成功。

## 验证与发布门禁

自动化检查至少覆盖：

- 默认 enabled / `0.0.0.0:8787` 配置、显式 override/disable、端口冲突启动失败、半初始化
  endpoint 拒绝、双 ingress close、在途 handler drain，以及 ready/shutdown 顺序；
- Admin secret 熵、普通文件、权限和非复用约束，pre-auth nonce、constant-time 校验、双层
  登录限速、idle/absolute session expiry、rotation/restart invalidation、Cookie、CSRF、
  Origin/Host、POST-only、请求资源上限、日志脱敏；未认证 GET `/` 只以 303 重定向到
  `/login`（不返回 HTML 或状态），除 login、登录页使用的无状态 CSS、无细节 readiness
  和该重定向外的所有未认证 endpoint 返回 401；
- 全局 Scope/Binding 的 cursor pagination 与过滤，live active/archived catalog 完整分页、
  metadata 失败不返回假完整结果、Runtime snapshot 标注和 chat label fallback；
- 每个操作矩阵行的允许/拒绝路径、stale Project/settings/active pointer、跨 Scope exact target、
  inactive rename/archive、恢复是否激活、active archive/delete pointer 提交和双击去重；
- Web 与飞书并发操作同一 Scope/Binding 的锁序、running/Goal/compaction/lifecycle 互斥、
  response loss、cancel 和 lifecycle-unknown admission close；
- materialized delete、Prompt/Turn、Goal mutation、批量 mutation 与 Side tombstone delete
  在 Admin HTTP route/controller 不可达。ADR 0037 为共享 current-Binding application
  service 与窄化 Runtime port 增加 exact delete primitive，但 Admin Web 不调用它；完整
  Runtime 继续保留飞书所需能力及 fixed-method Delete boundary。

候选部署必须从另一台受信内网主机直接打开 `http://<server-ip>:<port>`，验证未登录根路径
重定向、其他 endpoint 拒绝、登录、CSRF、Project mutation、跨 Scope ordinary Binding 的
允许操作、Side close、服务重启注销和 journald 脱敏。真实 lifecycle probe 仍使用既有公开
SDK 门禁；Admin Web 产品面不获得 materialized Delete route。

## 后果

Netizen 有两个客户端控制面，但仍只有一个业务服务、一个 Channel Database 和一个原生
Codex Runtime。管理员获得明确的实例级 authority，普通飞书参与者的 Scope 权限不扩大。
默认 `0.0.0.0` 和无 TLS 以受信内网为部署前提，换取直接 URL 的低运维成本；独立 secret、
短期 session 和浏览器请求防护承担最低成本的误访问防线。

Admin credential 同时保护 Project Registry 中“登记任意可用 absolute directory”的现有
能力；一旦泄漏，攻击者可把服务账号可访问的目录暴露为后续会话 cwd，并读取全实例的会话
metadata 或执行矩阵中的管理操作。V1 明确接受这一 host-level authority，不另加
projectRoot allowlist；因此 credential 必须保持高熵、文件受保护且绝不出现在 URL/日志。

实现抽出共享 application service 和 Scope coordinator，并为 inactive exact Binding
补齐 lifecycle 语义。这是有意的架构改动，不能通过 Web handler 调用现有私有方法或直接
写表来规避。HTTP 层使用 `h11` 的公开 HTTP/1.1 状态机配合受限的 `asyncio` transport，
不拥有业务状态。当前行为由[设计文档](../design.md)维护；部署、验收过程和剩余人工发布
门禁由[部署文档](../deployment.md)维护。
