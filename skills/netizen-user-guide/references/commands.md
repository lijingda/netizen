# 完整命令索引

[手册目录](user-guide.md)

以下是普通会话的操作索引；命令的前提、限制和后果见对应章节。每条消息只能包含一个
命令或一条任务，不能串联多个命令。群聊中的命令同样需要 `@机器人`。

| 输入 | 用途与重要前提 | 完整说明 |
| --- | --- | --- |
| 普通文本 | 空闲时开始任务；运行中补充到当前任务，不排队 | [任务与补充消息](concepts.md#一轮任务与补充消息) |
| `//...` | 将字面 `/...` 作为普通任务文本发送 | [文本与斜杠](messages.md#普通文本和斜杠) |
| `/new` | 用卡片选择 Project 及可选受管 worktree，新建当前会话；不接受参数 | [新建会话](sessions.md#新建与配置-project) |
| `/defaults` | 用一张表单查看、保存或删除当前聊天的会话默认配置，不接受参数 | [默认配置](sessions.md#默认会话配置与自动创建) |
| `/projects` | 登记、创建、启用或停用 Project | [Project 管理](sessions.md#新建与配置-project) |
| `/config` | 修改当前空闲会话的模型、思考强度、速度、任务反馈和群聊消息范围，后续新任务生效 | [会话设置](sessions.md#session-settings) |
| `/sessions`、`/threads` | 列出当前聊天或话题的会话，切换或按行管理 | [查找与切换](sessions.md#查找切换和命名会话) |
| `/sessions archived` | 查看归档会话，恢复或经确认永久删除 | [归档管理](sessions.md#归档恢复和删除) |
| `/resume <短 ID>` | 切换到当前聊天位置的另一个普通会话，不停止其他会话的任务 | [查找与切换](sessions.md#查找切换和命名会话) |
| `/fork` | 从当前空闲会话另建持久分支话题，可选当前聊天、群聊或填写聊天 ID | [持久分支](fork-side-goal.md#fork-持久分支) |
| `/rename [名称]` | 重命名当前会话；省略名称打开卡片，可自动生成候选后编辑保存 | [命名会话](sessions.md#查找切换和命名会话) |
| `/compact` | 压缩当前空闲普通会话的上下文 | [压缩说明](status-and-control.md#compact) |
| `/release` | 断开当前空闲会话在本服务中的连接，保留历史；不代表 Codex 立即释放该会话 | [订阅释放](sessions.md#查找切换和命名会话) |
| `/archive` | 确认后归档当前会话，保留历史并清空当前指针 | [归档与恢复](sessions.md#归档恢复和删除) |
| `/unarchive <短 ID>` | 恢复归档会话并切换到它 | [归档与恢复](sessions.md#归档恢复和删除) |
| `/delete` | 二次确认后永久删除当前会话；已有 Codex 历史时会一并删除它的子任务 | [删除后果](sessions.md#归档恢复和删除) |
| `/status` | 查看会话、任务步骤、上下文窗口和配置来源 | [状态快照](status-and-control.md#status) |
| `/usage` | 按需查看共享 Codex 账号的剩余额度和重置时间；无需创建会话 | [账号额度](status-and-control.md#usage) |
| `/stop` | 中断当前任务；Goal 先暂停，不保证所有工具进程退出 | [停止语义](status-and-control.md#stop) |
| `/side [首轮问题]` | 从已有历史创建临时分支话题，与来源会话共享工作目录 | [Side](fork-side-goal.md#side-临时话题) |
| `/side close` | 在 Side 话题内结束临时会话 | [Side](fork-side-goal.md#side-临时话题) |
| `/goal [目标]` | 查看或启动持续目标 | [Goal](fork-side-goal.md#goal) |
| `/goal pause`、`/goal resume`、`/goal clear` | 暂停、恢复或显式结束当前 Goal | [Goal](fork-side-goal.md#goal) |
| `/cron` | 管理定时计划；暂停计划不停止已认领的执行 | [定时任务](scheduled-tasks.md#定时任务) |
| `/admin` | 返回当前实例已绑定的管理地址与根目录；无需 Project 或当前会话 | [Admin Web](admin.md#admin-web) |
| `/enable` | 实例管理员在群里启用本群，不接受参数；无需创建会话 | [群聊准入](admin.md#群聊准入) |
| `$skill-name ...` | 在普通消息开头显式调用一个或多个 Codex Skills | [Skills](native-capabilities.md#codex-skills) |
| `/help`、`/` | 查看当前实例开放的帮助 | [能力差异](native-capabilities.md#与-codex-appcli-的差异) |

`/model`、`/effort`、`/fast`
使用 `/new` 或 `/config` 替代；查询可用 Skill 直接用自然语言，不提供 `/skills`。
`/plan`、`/apps`、`$app` 及 CLI/App 宿主命令的限制见[能力差异](native-capabilities.md#与-codex-appcli-的差异)。
Side 仅接受其[命令白名单](fork-side-goal.md#side-临时话题)，不能把上表所有普通会话操作用于 Side。
