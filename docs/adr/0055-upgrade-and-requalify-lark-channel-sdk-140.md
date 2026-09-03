---
status: accepted
date: 2026-09-03
amends: 0008, 0011, 0015
related: 0030, 0039, 0048
---

# 升级并重新认证 lark-channel-sdk 1.4.0

## 背景

Netizen 此前精确锁定 `lark-channel-sdk==1.2.0`。1.3.0 新增会议频道，1.4.0 又为
飞书富文本消息加入顶层 `post.files` 附件区的规范化与渲染。产品需要跟进已发布版本，
但本次升级不以修复 Card Action 重复点击为目标；1.4.0 仍保留 SDK 自身的 action
去重，文件分页连续点击问题继续作为独立议题处理。

对实际 1.4.0 wheel 的源码、公开类型和归一化契约进行验证后，得到以下结论：

- 非话题首层回复仍只提供 `parent_id == root_id`，不生成公开 `ReplyRef`；
- 普通 CardKit 2.0 入站消息已有可见 `content_text`，但 `QuoteResolver` 产生的
  `QuotedContext.text` 仍不遍历卡片顶层 `header`/`body`；
- `content_v2` 图片仍可能只出现在公开 post AST/渲染文本中而不进入 `resources`；
- 话题根 post 的 bot mention 占位符与 open_id 错配仍会让 SDK `body_text` 留下
  机器人名称；
- 顶层 `post.files` 是 locale 文档的同级字段。普通文件会生成 `file` 资源描述符，
  文件夹只渲染 `<folder/>` 而不生成资源描述符；
- `send()`、`update_card()`、`download_resource()`、`SendError.raw_code/hint` 和
  `DedupStore.seen/mark` 的既有公开合同保持兼容；
- `FeishuChannel` 会构造会议组件并在 dispatcher 上注册会议事件，但现有消息和卡片
  事件路径无需调用会议 API，也不要求业务注册会议 handler。

## 决定

1. `pyproject.toml` 与 `requirements.lock` 同步精确锁定
   `lark-channel-sdk==1.4.0`，不接受 1.4.x 浮动升级。
2. ADR 0011 的两处兼容层继续保留，但运行时只接受已经复核的精确 1.4.0：
   非话题首层引用仍从公共 `InboundMessage.raw` 恢复 exact parent；CardKit 2.0 仍只在
   SDK quote text 为空且公共 raw 满足既有结构/容量边界时投影可见文本。其他版本继续
   失败关闭。
3. 当前 `post` 输入在 locale 图片 AST 之外独立检查公共 `PostContent.post["files"]`。
   非空附件区以及非空但无法解释的附件区都显式拒绝；不能因普通文件来自 hidden
   descriptor 或文件夹没有 descriptor 而静默进入 prompt。空附件区不影响现有图片输入。
4. 保留话题根 post bot mention 的窄修复，并继续要求 `mentioned_bot`、公开 bot
   open_id、normalized mention key 与首个 post AST 节点同时匹配。上游缺口修复前不按
   显示名猜测。
5. 卡片瞬时锁仍只按 `SendError.raw_code == 230099` 且 `hint` 中存在独立数字
   `11310` 识别并有界重试。不得把 1.4.0 升级解释为 Card Action 去重或分页修复。
6. Netizen 不注册 `meetingInvited` handler，不调用 join/follow/session API，不申请会议
   scopes，也不新增会议状态、配置或事件路由。会议代码在 SDK 内的存在只作为启动、连接、
   dispatcher 和关闭兼容面验证。

## 验证

- SDK 契约测试必须使用实际安装的 1.4.0，固定首层引用缺口、CardKit 2.0 quote 缺口、
  `content_v2` 图片、顶层 file/folder、post mention、卡片发送/更新、资源下载、
  `SendError` 与 `DedupStore` 接口；不得只修改版本断言。
- webhook 生命周期契约必须让真实 `FeishuChannel` 构造背景 loop 和 dispatcher，投递一条
  普通消息并正常关闭，以证明新增会议注册没有阻断既有事件路径。
- `make check`、固定 SDK 的完整 native live phase、真实飞书首层/嵌套引用、CardKit 2.0、
  图片、顶层 file/folder 拒绝、卡片发送/更新和服务连接按 `docs/deployment.md` 执行。

## 后果与移除触发器

升级不会扩大用户文件输入能力，也不会产生第二个 Channel、会议运行时或新权限层。
代价是继续维护两处 exact-version quote fallback 和一处 post mention 修复，并在每次
Channel SDK 升级时重新做行为认证。

当发布版 SDK 为非话题首层回复生成正确 `ReplyRef` 后，删除 relation raw fallback；
当 `QuotedContext.text` 覆盖真实 CardKit 2.0 header/body 后，删除 raw card flattener；
当 placeholder post mention 的 `body_text` 正确移除 bot 后，删除 mention 修复。三个触发器
彼此独立，任何一个都必须先通过 synthetic 与真实飞书契约，不能因版本号变化机械删除。
