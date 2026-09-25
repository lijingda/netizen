# 可选决策模型：安装后按需配置的产品参考

核验日期：2026-09-24。仅为设计调研，不是已接受 ADR 或已实现功能。未安装产品、调用推理服务、读取本机凭据或测试热更新。

后续讨论已收敛：决策模型只在 Admin 配置，不实现自然语言配置或文件变更热加载；
`/new`、`/config` 仅选择模式。以下外部证据保留，原建议不自动成为当前方案；
最新共识以 [设计里程碑第 8—10 节](jev-autonomous-design-milestone-2026-09-24.md) 为准。

## 第一手证据

### Open WebUI：运行后管理外部连接

- 官方连接指南要求已有运行实例与管理员权限，在 Settings → Admin → Connections 中添加 URL、API Key；兼容协议的连接可以集中管理，并可停用而不删除配置。它证明“安装后再配置外部服务”是现成产品模式，但不能证明 Netizen 应照搬其协议、存储或权限设计。[连接指南](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/)、[兼容协议连接](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)
- 官方说明，选择云端连接会把提示及所含上下文发送给对应提供方。因此模型配置不仅是可用性设置，也应告知数据去向。[连接指南](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/)
- 本次未核验 Open WebUI 的底层密钥存储加密或任意配置文件热加载。文档有“保存后可以使用”的 UI 流程，不等于保证外部直接改文件也立即生效。

### n8n：用到节点时才补凭据，自然语言与密钥录入分离

- 官方文档支持从管理入口创建凭据，也支持编辑具体工作流节点时在凭据下拉框中新建；保存时会测试凭据。这直接支持“首次使用具体功能时引导完成配置”。[官方文档源码：Create and edit credentials](https://github.com/n8n-io/n8n-docs/blob/main/docs/build/understand-workflows/create-and-edit-credentials.md)
- 当前官方 `master` 分支的 instance-ai 工具文档规定，agent 不处理原始凭据秘密；`credentials(action="setup")` 引导用户通过前端配置，`list`／`get` 不返回解密秘密，另有 `test` 操作。它是“自然语言协助配置，但 secret 经私密 UI 输入”的具体例子；这里只核验源码文档，未确认所有发布版本、套餐或部署方式都已提供这些入口。[官方工具文档](https://github.com/n8n-io/n8n/blob/master/packages/%40n8n/instance-ai/docs/tools.md)
- 官方说明首次运行自动生成加密密钥，凭据加密后写入数据库。这说明产品可封装密钥保管而不要求每个用户自行设计；不意味着 Netizen 必须引入同样的数据库凭据模型。[官方加密说明源码](https://github.com/n8n-io/n8n-docs/blob/main/docs/deploy/host-n8n/configure-n8n/basic-configuration/configuration-examples/set-a-custom-encryption-key.md)

## 当时对 Netizen 的建议（讨论记录，已由后续共识收敛）

1. 基础安装不增加决策模型必答项；完成信息只给一条可选提示。用户首次开启自主模式、发现尚未配置时，再进入同一个配置向导。没有配置不影响原有模式，也不算安装不完整。
2. 将“实例连接已配置”与“本会话开启自主模式”分开。连接管理负责提供方、端点、模型、凭据及测试；会话入口只负责选择模式并显示可用性。
3. 自然语言是合理的配置入口：agent 可以解释、补全非敏感字段、引用既有凭据并引导验证；不应默认要求用户把 API Key 发到群聊，或将密钥全文读入模型上下文。秘密由私密 Admin／本机入口录入，之后只显示已配置状态。
4. 自然语言、命令与 Admin 应修改同一份权威配置并共享校验，不各存一份。Skill 记录位置、字段和生效方式，但 Skill 文本本身不提供权限控制或热更新能力。
5. 生效规则必须在 Netizen 自己明确设计，例如“校验通过后供下一次判定使用，已开始的请求继续用原配置”；不要从别人的 UI 文档推断我们已有热加载。连接测试与完整判断质量评估也不是同一回事。

## 证据局限

- 这些例子支持一种可行的渐进配置模式，不构成“行业统一标准”，也没有提供减少部署流失的量化对照结果。
- Open WebUI 的模型是核心能力，n8n 是通用自动化平台；均不与 Netizen 的可选群聊决策功能完全等价。这里借鉴配置入口和密钥交互，不照搬架构。
- 未核验实际重载时序、文件写入一致性、鉴权实现、费用或外部模型服务 SLA；未建议自动安装或代管 Laya 推理服务。
