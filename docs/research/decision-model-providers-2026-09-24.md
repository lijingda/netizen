# 自主决策模型供应方：Jev 与 Laya

核验日期：2026-09-24。仅做官方文档及源码研究，未安装依赖、加载模型、调用推理或启动服务。
设计起点为 [9 月 24 日讨论里程碑](jev-autonomous-design-milestone-2026-09-24.md)，不改变其中已确认的上下文和 Turn/steer 语义。

## 来源与修订范围

Laya 的 [v0.3.11 Release](https://github.com/NandhaKishorM/laya/releases/tag/v0.3.11)
显示发布于 9 月 23 日，关联提交 `1e28ac2`。以下 HTTP/推理细节核验的是 9 月 24 日读取的 `main` 源码；
未取得该分支完整提交 SHA，因此不能把它们全部断言为发布版 v0.3.11 行为。
`common.py` 的截断与置信度逻辑也核对了 [v0.3.11 文件](https://raw.githubusercontent.com/NandhaKishorM/laya/v0.3.11/laya/common.py)。
TypeSafe 使用当日 [API 文档](https://docs.typesafe.ai/api)和 [Models 文档](https://docs.typesafe.ai/models)。实施时应固定并复核实际版本。

## 共同协议及不能忽略的差异

| 项目 | TypeSafe Jev | Laya 自托管 HTTP |
| --- | --- | --- |
| 入口 | `POST https://api.typesafe.ai/v1/systemone` | `laya-serve` 的 `POST /v1/systemone`；不是示例 GUI 的 `/predict` |
| 请求核心 | `state`、`model`、`questions` | 接收同形 JSON；`model` 可缺省，问题使用 `type`、`instructions`、`criteria` |
| 鉴权 | Bearer API key | 仅设置 `LAYA_API_KEY` 后检查 Bearer；否则不要求凭据 |
| Choice | `type`、`choice`、`probabilities`、`confidence` | 公共字段相同，另有 `action.act_probability` |
| Noul | `type`、`noul` | 公共字段相同，另有 `confidence` 和 `action` |
| 实际模型 | 当前 `jev-1.13.0`；`model` 选择具体版本/别名 | `model` 选择 Router 检查点或退回自动路由，详见下文 |

依据：[TypeSafe API](https://docs.typesafe.ai/api)、[Laya HTTP 源码](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/serve.py)、
[Laya 答案解码](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/agent.py)。
这是窄协议交集的源码证据，不是完整 SDK 互操作或性能一致性的实测。

Laya `_resolve_model` 识别 `english`、`multilingual`、`typed-decisions` 及其别名，
也识别已发布的 `convaiinnovations/laya-multilingual` 和 `convaiinnovations/laya-typed-decisions`。
不认识的值（包括 `jev-*` 和拼错的名字）会被静默当成未指定，交由 Router 自动选择；
`convaiinnovations/laya` 本身也走自动路由。故只替换地址并不能保证请求到了预期检查点。
依据：[服务端模型解析](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/serve.py)、[Router 别名与选择](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/router.py)。

Laya 顶层 `model` 在 Agent 输出中为通用的 `laya-rl-agent`，Router 另外增加 `routing` 元数据；
不要把顶层 `model` 当成不可变权重版本。Laya 的 Choice confidence 按归一化熵计算，
Noul confidence 为 `max(p, 1-p)`；TypeSafe Noul 没有独立 confidence。
选择/概率可以映射，confidence 阈值不可未经评测跨供应方搬用。
依据：[输出实现](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/agent.py)、
[confidence 公式](https://raw.githubusercontent.com/NandhaKishorM/laya/v0.3.11/laya/common.py)、[TypeSafe Confidence](https://docs.typesafe.ai/confidence)。

## 上下文限制是主要适配点

Jev 当前每请求 64k token、`state + 最长问题` 32k；Laya README 列出的英文检查点为 512 token，
multilingual 和 typed-decisions 为 1024 token。中文应评估 multilingual 检查点；
“支持多语言”本身不证明中文群聊消费判断准确，也不证明效果等同于 Jev。
依据：[Jev 模型限制](https://docs.typesafe.ai/models)、[Laya 模型表](https://github.com/NandhaKishorM/laya#readme)。

Laya 使用检查点 tokenizer，对每道问题构造“问题 + 选项 + state”序列。
`max_len` 包含问题、选项和特殊 token；state 的实际预算小于总窗口，不能把 1024 当作可用历史长度。
问题头和每个选项也有截断。state 为字符串或对象时默认保留开头；为列表时默认保留尾部。
因此把“摘要 + 历史 + 当前候选”拼成一段长字符串，可能静默丢掉最重要的当前消息。
改成列表只改变截断方向，不能解决完整摘要/候选超预算的问题。
依据：[token 序列构造](https://raw.githubusercontent.com/NandhaKishorM/laya/v0.3.11/laya/common.py)、
[Agent 列表截断分支](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/agent.py)。

## 已确认的最小设计方向

用户已认可供应方解耦方向；共识已同步至 [讨论里程碑](jev-autonomous-design-milestone-2026-09-24.md)。
以下是设计方向，不表示实现、部署或互操作验证已经完成，具体配置字段仍待细化。

- 业务只依赖“判断当前消息消费或跳过”的异步接口；首版可用一个 `consume/skip` Choice。
  不把通用多问题工作流、动态插件加载或服务启动管理带入 Netizen。
- 共享 HTTP 协议编码/解码与连接管理；Jev/Laya 只维护供应方默认地址、模型选择、鉴权方式和预算。
  若采用官方 TypeSafe SDK，也应置于该边界后并验证本地无鉴权服务、额外字段及模型标识；目前未做互操作测试。
- 持久记录保持供应方无关的文本：已消费消息、最终回复与摘要。每次调用按选定模型的 tokenizer/预算构建输入，
  为当前候选和问题预留空间，压缩历史；当前候选自身过长时的处理需要明确，不能默许服务端静默截断。
- 摘要仍按既有共识由共享 Codex 的独立纯文本任务产生。换供应方不改变消费记录含义、不补 catch-up，
  被接收的消息仍进入既有 start/steer。供应方切换可能要求重新压缩上下文，但不创建另一套执行流程。
- 只验证本场景需要的请求和响应字段；不得因为字段名相同，就假设延迟、中文质量、概率校准、token 计数或成本等价。

未验证项：实际部署连通性、官方 SDK 对 Laya 的端到端兼容性、模型权重修订、中文聊天效果与实测耗时。
该结论支持“一个小接口 + 共享协议 + 少量供应方配置/映射”，尚不支持无条件宣称“任何 Jev 客户端改地址就完全兼容”。
