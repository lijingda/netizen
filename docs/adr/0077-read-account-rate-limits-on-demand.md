---
status: accepted
date: 2026-10-08
related: 0014
---

# 按需读取共享 Codex 账号额度

## 背景

用户需要在飞书查看 Codex 账号的剩余额度和重置时间。现有 `/status` 显示当前会话的
上下文窗口使用量，不能代替账号额度。`openai-codex` 0.160.0 和当前固定的
0.161.0 都有 typed `account/rateLimits/read` 协议，但没有等价高层 facade。
这项查询没有 Thread 生命周期，不值得为它新增配置、缓存或持续观察机制。

## 决定

- 增加无参数 `/usage`，通过 Channel → 同一 Runtime → `AccountRateLimits.read()`
  查询一次；无需 Project 或当前 Binding。普通聊天、普通话题与有效 Side 共用入口，
  既有飞书准入、@ 要求和已关闭 Side 拒绝规则保持有效。
- 对齐原生额度区：展示共享账号的额度桶名称、实际窗口周期、剩余进度条/百分比和重置时间。
  用一张 Card 2.0 的 Markdown 元素承载，epoch 秒转毫秒后交给 `local_datetime`，由查看者
  客户端按设备时区显示日期、时间与时区；不保存用户时区或由服务器推断“今天”。
  优先采用 `rateLimitsByLimitId`，仅缺省或 null 时兼容 legacy `rateLimits`；每个桶仍用
  安装 SDK 的 generated model 验证。剩余百分比为 `100 - usedPercent` 限制在 0–100。
  同次响应实际提供 Credits 或月度额度时按原生含义简短显示；仅有这些字段也可形成有效快照。
  缺失、空或非法数据不能解释为零使用或无限额度；不展示账号身份或账单/逐请求明细，
  不增加账单 API、购买或重置入口，也不增加专项容量分页/截断机制。
- 新增可独立删除的 `AppServerAccountRateLimits`：复用已初始化 `AsyncCodex` 持有的
  client，只调用固定 `account/rateLimits/read`，使用安装 SDK 的 generated 请求/响应模型。
  响应模型仅通过继承增加解码前严格校验，避免 legacy 数值被 SDK 宽松强转；不复制字段。
  固定 `excludeResetCreditDetails=true`、`supportsLunaReserve=false`，不重置额度或启用
  reserve。仅定义展示 DTO，不复制协议模型，不提供 generic RPC 或另启客户端。
- 查询最多等待十秒；超时、认证/传输错误或响应不可用只影响本次展示，不重试、不改变
  Thread/Goal 的运行和准入。SDK 底层同步请求可能晚于调用方超时结束，仍由同一 transport
  收尾，不宣称已取消原生请求，也不关闭共享连接。
- 不新增数据库字段、后台 worker、通知消费者、额度预检、自动模型切换或额度恢复保证。
  `/usage` 是此次查询的快照，不能决定之后的任务是否一定可以运行。

## 验证与移除条件

独立 capability shape gate 不通过时只禁用额度入口。行为测试覆盖命令准入、无 Binding
读取、普通/Side 路由、不可用、多窗口及 Credits-only/月度额度展示、客户端时间；真实 SDK client synthetic harness 覆盖固定
请求、typed 多桶/legacy 响应、非法数据、超时/取消、晚到响应和连接复用。

新适配或 SDK 升级需运行一次只读 live probe，记录精确 SDK/App Server 版本和成功/错误
分类，不记录账号身份或余额；不创建 Thread、不请求模型。未登录错误只证明该错误路径，
不代替已登录账号成功读取。飞书实际展示另记验收结果。

公开高层 API 提供等价账号额度读取时，facade migration sentinel 使升级门禁失败；完成
synthetic/live 验证后切换 provider 并删除 reach-through。候选名称检查不替代完整 SDK
升级审查。
