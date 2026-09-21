# 部署步骤

本指南部署的是**告警触发路由更新链路**，不是 APIM 或 Foundry 基础设施。所有命令均在仓库根目录执行；不要把示例中的占位值直接用于 Azure。

## 外部资源前置

操作者应已经具备：

| 外部资源 | 要求 |
|---|---|
| APIM 服务 | 已存在；具备托管身份和本方案所用策略能力 |
| APIM API | 已存在；允许显式替换 API 级策略，并已备份旧策略 |
| API 操作 | 聊天与 Embedding 的 POST 操作，路径含必填参数 `deployment-id` |
| APIM backend | 每个候选的 backend ID 已存在，URL 指向对应 Foundry 账户 |
| APIM UAMI | 已关联到 APIM，其 client ID 可供策略引用 |
| Foundry 账户与部署 | 已存在，每组两个候选均提供一致的 URL 部署名称 |
| 模型数据权限 | APIM UAMI 已获对应 Foundry 账户的推理权限 |
| 控制资源组 | 已存在，用来放置工作流、Action Group、告警 |
| 钉钉机器人 | 接受包含 `Azure` 的文本，持有可长期使用的 Webhook |

本仓库不创建上表中的资源，不部署模型，不给 APIM 分配身份或授予模型数据权限。API 订阅校验方式由现有 API 配置决定；本方案不能替代调用方认证。

默认策略期望聊天和 Embedding 的 URL 分别采用：

```text
POST /openai/deployments/{deployment-id}/chat/completions
POST /openai/deployments/{deployment-id}/embeddings
```

路径参数中包含小写 `embedding` 的部署被分到 Embedding 组。部署命名不满足这个约定时，必须先明确调整策略分类，不能指望仅配置模型名称就自动改变分类规则。

## 工具与授权

使用 Python 3.9 或更高版本和 Azure CLI；生成器及部署器只依赖 Python 标准库。当前实现针对 Azure Public Cloud、Logic Apps Consumption。通过 `az login` 在操作者自己的终端登录，并确认具有目标资源的权限。命令显式使用配置中的订阅，不应为了部署改变其他用户共享的默认上下文。

部署权限与授权权限分开：

1. 控制资源部署需要相应资源写入权限。
2. APIM Named Value 创建/更新与策略安装需要对应子资源权限。
3. 角色分配需要 `Microsoft.Authorization/roleAssignments/write`；Contributor 不包含该权限。
4. 工作流系统身份只需要两个 Named Value 精确范围内的读写能力。
5. APIM UAMI 的 Foundry 模型权限由外部资源负责人准备。

## 安全发布顺序

先离线校验与渲染，再备份和进入维护窗口。控制器部署、角色授权、API 策略安装与告警启用是分开的动作，不能把部署成功理解为流量已经安全切换。

现有工作流或告警的更新需要暂停告警并排空运行；任何权限错误、外部资源不匹配或渲染失败都应停止。不要扩大到整个订阅授权，也不要绕过模型身份授权。

## 配置与离线生成

复制示例到 Git 忽略的本地配置文件，按外部资源清单填入真实值：

```bash
cp examples/config.example.json config.local.json
python3 -m apim_routing --config config.local.json validate
python3 -m apim_routing --config config.local.json render --output-dir rendered
```

`validate` 和 `render` 不访问 Azure，也不会验证资源是否真的存在。示例中的零 UUID、`replace-*` 名称仅用于展示结构；离线通过不意味着示例可部署。`rendered/` 含环境标识，默认不提交。

| 配置项 | 含义 |
|---|---|
| `subscription_id` | 控制资源部署订阅，也是 Azure CLI 获取 ARM token 使用的订阅上下文 |
| `controller_resource_group`、`location` | 已有控制资源组与工作流区域 |
| `workflow_name`、`action_group_name`、`action_group_short_name` | 本方案创建/管理的控制资源名称 |
| `apim_resource_id`、`api_id` | 外部 APIM 完整资源 ID 与现有 API ID |
| `uami_client_id` | 已关联 APIM 的用户分配托管身份 client ID，不是 object ID |
| `threshold_ms` | 四条规则共同使用的原生时延阈值，默认 2000；两组仍是独立规则 |
| `window_size`、`evaluation_frequency` | 当前只支持 `PT1M` / `PT1M` |
| `groups.chat`、`groups.embedding` | 各自的 `named_value_name` 与恰好两个 `routes` |
| route 的 `token` | 名单成员和响应头使用的稳定 token，不要求为真实区域名 |
| route 的 `backend_id` | 已有 APIM backend ID，不创建或修改 backend |
| route 的 `foundry_resource_id` | 已有 Foundry 账户完整资源 ID |
| route 的 `deployment_name` | 指标维度使用的模型部署名；组内两个成员必须相同 |
| route 的 `alert_name` | 唯一告警名称，生成器同步到控制器白名单 |

外部 backend 应直接指向该 Foundry 账户的 HTTPS 根端点，不附加路径、查询或其他服务的目标地址。资源命名和格式限制以校验错误为准。渲染后审阅 policy 中的候选、身份和 Named Value 引用，以及工作流的规则映射。

## 部署控制资源

先在自己的终端登录 Azure：

```bash
az login
python3 -m apim_routing --config config.local.json deploy
```

交互部署会隐藏输入提示，要求提供钉钉 Webhook。非交互运行可通过秘密管理工具注入 `DINGTALK_WEBHOOK` 环境变量；不要把实际 URL 写进命令行、配置、CI YAML、shell 历史或仓库。

```bash
# 环境变量由外部秘密管理机制提供；此处不展示或回显其值。
python3 -m apim_routing --config config.local.json deploy \
  --webhook-env DINGTALK_WEBHOOK
```

部署读取并核对外部资源；创建缺失的两份名单（初始 `none`），保留已有名单值；创建/更新系统身份工作流和 Action Group，并创建四条**禁用**的告警。工作流 secret 参数只在请求内传递，不写入渲染文件。策略安装不是此命令的一部分。

更新已有工作流前会禁用告警和工作流，发现仍有在途运行则停止。原本启用的工作流只在部署完整成功后恢复；原本禁用的保持禁用。部分失败可能留下已创建资源或禁用状态，不能把命令失败视为事务回滚。修复后先核对状态再重跑。

若需要保留旧硬隔离状态，先按迁移规则准备两份 degraded 名单，不能让新建 `none` 被误解为后端健康结论。工作流和命名映射不一致时部署会拒绝覆盖；重新命名应走显式迁移。

## 授予控制器权限

由具备角色授权权限的操作者运行：

```bash
python3 -m apim_routing --config config.local.json grant-controller-roles
```

此命令给工作流当前系统身份授予两个 Named Value 精确资源范围的 Contributor，不授予整个 APIM、资源组或订阅。已有同等角色应复用。若出现 403，交给有权限的管理员，不自动换身份或扩大范围。

## 显式安装 API 策略

**此动作替换现有 API 级 policy。** 先确认其继承关系与现有认证、配额、审计逻辑兼容；渲染策略不能自动合并任意现有策略。备份文件可能包含现有 policy 的秘密，保存在受限、未提交的目录中：

```bash
python3 -m apim_routing --config config.local.json install-policy \
  --confirm --backup rendered/original.policy-backup.json
```

不能通过改文件名绕过“未保存原策略就替换”的变更要求。若同一备份路径已有文件，保留原文件，使用新的受控备份路径。安装完成后先完成真实短请求鉴权和转发检查，不急于启用告警。

## 控制器 smoke 与告警启用

**smoke 会真实改变名单并发送钉钉消息，不是只读检查。** 它暂时设置/恢复两组名单，针对四条映射执行更新、重复和 Resolved 共 12 次通知，另测试三种拒绝情况，不发送模型流量。只能在没有业务流量、其他写入者和真实告警的隔离维护窗口执行。

如果工作流原本禁用，先在 Azure 中明确启用该工作流；仍保持四条告警禁用。对于受支持的新部署路径，核对返回状态和 Azure 中的工作流状态。

```bash
python3 -m apim_routing --config config.local.json smoke \
  --confirm-mutations --receipt smoke-receipt.json
```

完成后核对原名单已恢复。中断或错误需要人工查看恢复状态；不要直接重启另一轮掩盖失败。成功 receipt 有效期 24 小时，并关联当前配置，用作显式启用的前置条件；receipt 不是抗恶意篡改的安全凭证，也不是模型健康证明。

完成“模型可调用、policy 已安装、角色正确、smoke 成功”的检查后启用：

```bash
python3 -m apim_routing --config config.local.json enable-alerts \
  --confirm --smoke-receipt smoke-receipt.json
```

随后按运行文档进行有预算的真实时延告警演练。暂停四条告警：

```bash
python3 -m apim_routing --config config.local.json disable-alerts
```

禁用告警不会自动取消已在途的 Logic App 运行，也不会恢复名单或撤销 APIM 策略。

## 源码和本地检查

| 路径 | 内容 |
|---|---|
| `apim_routing/policy.xml.template` | 参数化 APIM policy 源码 |
| `apim_routing/workflow.py` | Logic Apps Workflow Definition Language 生成器 |
| `apim_routing/config.py` | 外部资源与路由配置校验 |
| `apim_routing/render.py` | 生成 policy、工作流、规则和清单 |
| `apim_routing/deploy.py` | 分阶段部署、授权、smoke 与启用 |
| `examples/config.example.json` | 无具体环境依赖的配置结构示例 |

无需安装额外测试包：

```bash
python3 -m unittest discover -s tests -v
```

这些测试不向 Azure 写入，不证明任何真实订阅已完成部署。

## 变更完成判据

启用之前，必须在受控环境确认：

- API 使用指定 UAMI 能调用两组全部成员，而不是仅证明部署人员直连可用。
- 策略引用的 Named Value 名称、后端 ID、路由 token 与控制器映射一致。
- 控制器系统身份在两个新 Named Value 上具备作用域正确的角色。
- Common Alert Schema 的实际事件能被接收和校验，名单更新与通知分别完成。
- 人工模拟并不被当成真实时延事件；真实闭环验收方法见 [运行文档](operations.md)。

## 已有环境更新和恢复

更新时不能重置已有 degraded 值。不要删除重建工作流来更新定义，否则系统身份 principal ID 会变化。资源更名或改变规则映射也可能留下旧规则或旧授权，需做显式变更计划。

保存政策和名单快照时不要导出订阅密钥、Webhook 或 callback 到源码目录。若恢复到旧 policy，请连同匹配的名单语义一起恢复；不要混用旧 enabled 名单与新 degraded 名单。

停止条件与回滚流程参见 [运行与恢复](operations.md#更新和回滚)。
