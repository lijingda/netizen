---
status: superseded
superseded_by: 0007
---

# 生产 WebSocket 入站必须先证明失败重投

Durable inbox 只能在 PostgreSQL 提交后确认飞书事件。当前 Node SDK 的 WSClient 会 await EventDispatcher，并在业务异常时返回 500，但公开资料未明确保证该响应一定触发服务端重投。因此 Phase 0 必须在真实租户注入数据库失败并验证同一事件重投；通过后才能把 WebSocket 作为唯一生产入站。若未通过或行为随锁定版本变化，消息与卡片 action 改用签名验证的 Webhook，按公开 Webhook 重试契约在数据库失败时返回非 2xx；WebSocket 只能作为可丢失的低延迟辅助通道。
