# 部署步骤

按下面六步部署告警驱动 APIM 路由方案。**脚本只用于离线生成定义；Named Value、APIM 策略、Logic App、Action Group、四条告警分别发布，不运行一次性 `deploy` 命令。** 每个写入步骤先审阅、确认，再执行；不能把本文当成可整段运行的脚本。

本文适用于 Azure Public Cloud、Logic Apps Consumption。APIM、API/操作、backend、用户分配托管身份（UAMI）、Foundry 账户/模型部署及资源组均为外部既有资源，不由本文创建。需准备 Python 3.9+、Azure CLI、`jq`，并在操作者自己的终端登录 Azure。命令在仓库根目录的 Bash 中执行，显式指定订阅，不切换共享机器的默认账户或订阅。

真实配置、生成文件和环境快照不提交 Git。Webhook、回调 URL、订阅密钥和令牌不贴到评论、不放在命令参数或日志中；请求体含秘密时限制文件权限，用后清理，不作为附件分享。

## 1. 将 APIM 及相关资源信息更新到 config.local.json

首次配置时复制示例；已有本地配置不要覆盖：

```bash
umask 077
test ! -e config.local.json && cp examples/config.example.json config.local.json
```

编辑 `config.local.json`，将占位值替换为实际环境信息：

| 配置项 | 填写内容 |
|---|---|
| `subscription_id`、`controller_resource_group`、`location` | 控制资源所在订阅、已有资源组和工作流区域 |
| `apim_resource_id`、`api_id` | 现有 APIM 完整资源 ID、现有 API ID |
| `uami_client_id` | 已关联到 APIM 的 UAMI client ID，不是 principal/object ID |
| `workflow_name` | 要创建的控制器 Logic App 名称 |
| `action_group_name`、`action_group_short_name` | Action Group 名称和不超过 12 字符的短名称 |
| `groups.chat`、`groups.embedding` | 各自的 `named_value_name` 及恰好两个 `routes` |
| 每条 route | `backend_name`、已有 `backend_id`、`foundry_resource_id`、`deployment_name`、唯一 `alert_name` |
| `threshold_ms` | 四条告警共用的 TTLT 阈值，默认 2000 ms；第五步依据历史数据确定正式值 |
| `window_size`、`evaluation_frequency` | 分别生成告警的 `windowSize`、`evaluationFrequency`；示例为 `PT5M` / `PT1M`，省略时均默认 `PT1M`；可选值见第五步 |

确认 API 有下列 POST 操作，路径参数名为 `deployment-id`：

```text
/openai/deployments/{deployment-id}/chat/completions
/openai/deployments/{deployment-id}/embeddings
```

同组两个成员的模型部署名必须相同，因为策略不改写部署名。Embedding 部署名必须包含小写 `embedding`，聊天部署名不能包含它；不满足时需先调整并审阅分类策略。backend 必须指向对应 Foundry 的 HTTPS 根端点，不能附加路径或冲突的 API key/Authorization 凭据。APIM UAMI 必须已有各 Foundry 账户的推理权限。

`backend_name` 是组内唯一的稳定逻辑后端名称，必须与 Named Value 中的名单成员一致；它不是 APIM backend 资源名称，也不要求等于 Azure 区域。`backend_id` 才指向实际 APIM backend。已有名单需核对语义：本方案是 **degraded（降级）名单**，不是旧版 enabled（可用）名单。

旧配置的 `token` 暂作为兼容别名接受，加载时归一化为 `backend_name`；同一条 route 不允许同时填写两者，即使值相同。新配置只使用 `backend_name`，重命名字段时保持现有名单成员值不变。生成的 workflow 使用 `backend_name`、`backend_names` 和 `degraded_backend_names`，policy 使用 `backend_name` / `backend_names`。部署前映射检查兼容旧 workflow 的 `region` / `allowed`，但仍拒绝实际成员或名单映射变更。固定 APIM 属性 `backend-id` 不变，响应头 `X-Backend-Region` 为客户端兼容保留，其值仍为逻辑后端名称；工作流的 `route` 响应字段及状态码不变。

字段迁移后需重新生成、审阅并分别发布 policy 与 workflow；更新 workflow 时仍须暂停告警、排空运行并保留原身份和安全参数，不能仅修改线上 JSON 字段。旧 smoke receipt 不应复用，应在迁移后的配置和定义下重新验证。

**本步完成条件：**配置中的资源确实存在、模型部署就绪、身份及路径匹配；不只是在 JSON 中填完字符串。

## 2. 调用脚本生成资源定义

```bash
python3 -B -m apim_routing --config config.local.json validate
python3 -B -m apim_routing --config config.local.json render --output-dir rendered
```

这两个命令只在本地运行，不访问或修改 Azure。输出如下：

| 文件 | 用途 |
|---|---|
| `rendered/policy.xml` | 安装到 APIM API 级别的完整策略 |
| `rendered/workflow.json` | Logic App 流程代码（WDL definition），不是完整 ARM 资源请求体 |
| `rendered/alerts.json` | 四条告警的“完整资源 ID → 请求体”映射，不是 ARM 部署模板 |
| `rendered/manifest.json` | 资源 ID、阈值及配置摘要 |

审核候选后端、身份、Named Value 引用和四条告警映射。`workflow.json` 的 `Rule_map` 已填入环境资源，`dingtalkWebhook` 声明为 `{"defaultValue": "none", "type": "SecureString"}`；`none` 仅为非秘密占位值，不能发送通知，真实秘密必须在第四步通过 `properties.parameters.dingtalkWebhook.value` 注入。钉钉通知及生成文件中的文本使用可读中文。保留 `@parameters(...)`、`@outputs(...)` 等运行时表达式，不要手动替换它们。

**配置改变后重新生成并审阅所有相关定义。** 特别是阈值同时写入 `alerts.json` 和 `workflow.json` 的事件校验表达式，不能只改告警文件。

## 3. 审核 policy.xml、添加 Named Value、更新 APIM 策略并手动验证

### 维护窗口与备份

**这一步可能立即影响生产环境流量，不必等到告警启用才生效。** 事先通知业务负责人，约定维护窗口、验证请求预算、停止条件和回滚负责人。迁移已有链路时先暂停相关告警和自动写入者，排空工作流运行；不要与真实事故处置并行修改名单。

保存当前 API policy、两份名单及 ETag、后端映射和告警状态到受限、未提交的目录。原策略可能含秘密。审核认证、配额、审计、操作级策略和继承关系；本模板会替换 API 级策略，不能自动合并，尤其 `backend` 段没有 `<base />`，不能假定父级 backend 逻辑仍会执行。

### 添加两份 Named Value

使用配置中两个 `named_value_name`，要求 `displayName` 与名称相同、`secret=false`、非 Key Vault 引用。**只创建缺失项**，新建初值为 `none`；已有合法值保留，不借部署清空降级状态。

在 Portal 的 APIM → Named values 中逐项创建；或对每个缺失资源单独发送 ARM PUT：

```text
PUT {apim_resource_id}/namedValues/{named_value_name}?api-version=2024-05-01
If-None-Match: *
{"properties":{"displayName":"与 named_value_name 相同","secret":false,"value":"none"}}
```

上面是请求结构说明，不是可原样提交的占位请求。

### 更新 API 级策略

可在 APIM → APIs → 目标 API → All operations → 策略代码编辑器中安装审核后的 `policy.xml`。**先备份，再保存，不要安装到服务全局或错误的操作级作用域。**

若采用项目的单独策略安装命令：

```bash
python3 -B -m apim_routing --config config.local.json install-policy \
  --confirm --backup rendered/original.policy-backup.json
```

该命令先检查外部资源、备份旧策略，再用 ETag 条件更新。备份路径已存在会停止，不覆盖旧备份。注意：它根据配置和模板重新生成策略，**不读取手工编辑后的 `rendered/policy.xml`**。若审阅中合并了业务策略，应按审阅版本单独发布并另行核对，而不能用该命令覆盖合并结果。

### 手动修改 Named Value 验证选路

两组分别验证，每次写入前读取最新值和 ETag，使用 `If-Match: <实际 ETag>` PATCH；遇到冲突停止重读，不使用通配 ETag。`A`、`B` 指本组配置中的实际 `backend_name`：

| 名单值 | 预期 |
|---|---|
| `none` | 两个成员均可被选中；少量请求不保证严格 50/50 |
| `A` | 首次优先 B |
| `B` | 首次优先 A |
| `A,B` | 两个成员仍可访问，不因全降级主动拒绝 |
| 正常成员受控返回 429/503 | 最多向另一个成员重试一次，包括降级成员 |
| 401/403/404、慢但成功的 200 | 不扩大重试范围 |

通过 APIM 使用短小非敏感输入调用 chat 和 embedding，查看响应状态、请求 ID、`X-Backend-Region` 与必要的受控诊断；确保全部候选都有成功样本。更改 chat 名单不得影响 embedding，反之亦然。配置传播有延迟，不能紧接写入就断言失败。

错误注入和非法名单场景只在隔离、获授权的测试范围执行，不破坏真实后端或生产名单。结束时基于当前值/ETag 恢复测试前状态；如发生真实事故或其他写入，不得盲目恢复快照。失败则回滚匹配的策略与名单，不进入下一步。

**本步完成条件：**不依赖告警或 Logic App，已证明 APIM 身份鉴权、组间隔离、降级优先级和一次跨后端重试符合预期。

## 4. 创建 Logic App，并授权其操作 Named Value

先单独创建 Consumption Logic App，流程代码使用第二步的 `workflow.json`。工作流启用 **SystemAssigned** 身份；它与 APIM 的 UAMI 是两种不同身份。

用 `az rest` 时需将 definition 包装为完整请求体：

| 字段 | 内容 |
|---|---|
| `location` | 配置中的工作流区域 |
| `identity.type` | `SystemAssigned` |
| `properties.state` | 初次创建可设 `Enabled`；此时尚无启用的告警 |
| `properties.definition` | `workflow.json` 完整内容 |
| `properties.parameters.dingtalkWebhook.value` | 通过受控渠道输入的长期钉钉机器人 Webhook |

以下 `$SUB`、`$RG`、`$WORKFLOW` 分别取配置的 `subscription_id`、`controller_resource_group`、`workflow_name`。`workflow.request.secret.json` 需按上表安全准备，权限为 `600`，不提交、不作为附件发送，使用后删除：

```bash
az rest --method put --subscription "$SUB" \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Logic/workflows/$WORKFLOW?api-version=2019-05-01" \
  --headers Content-Type=application/json \
  --body @workflow.request.secret.json --output none
```

确认 provisioningState 为 `Succeeded`、定义一致且系统身份 principal ID 已生成。更新既有工作流时先暂停来源告警、禁用并排空运行，保留原身份、秘密参数及资源设置；**不要用新建请求体直接覆盖或删除重建已有工作流**。

随后单独授权：

```bash
python3 -B -m apim_routing --config config.local.json grant-controller-roles
```

该命令只给配置指定工作流的系统身份授予两份 Named Value **各自精确资源范围**的 Contributor。操作者需有 `Microsoft.Authorization/roleAssignments/write`；Contributor 本身不含此权限。若用 Portal/CLI 手工授权，也必须采用相同精确 scope，不能扩大到 APIM、资源组或订阅。等待 RBAC 传播，出现 403 时核对主体及作用域，不用扩大权限解决。

如要复用其他工作流，应先确认定义、规则映射、身份和两份名单一致，并向实际接收告警的工作流身份授权；不能仅凭工作流名称相似就复用。

**本步完成条件：**工作流定义和安全参数已配置，系统身份稳定，并具备目标 Named Value 的精确范围权限。

## 5. 参考历史时延调整告警配置，连接 Action Group，分别创建告警

### 用历史指标确定正式阈值和评估窗口

在各 Foundry 账户的 Azure Monitor Metrics 中选择 `AzureOpenAITTLTInMS`，按 `ModelDeploymentName` 区分四个候选，使用与告警一致的 `Average` 聚合。选取包含正常峰谷、工作日/非工作日及已知异常的代表性时段（例如最近 7 天），观察正常范围、持续高延迟和低流量缺数；不要把缺失值当作 0。

结合业务延迟目标和历史误报情况确定阈值。窗口越短越敏感，越长越平滑但检测变慢；评估频率影响检查间隔。这里的指标是服务端总响应时延 TTLT，不是客户端端到端耗时或首 token 时延，不能混用。

**当前代码限制必须遵守：**

- `threshold_ms` 是四条告警共用的正数；不支持直接配置四个独立阈值。
- `window_size` 可选 `PT1M`、`PT5M`、`PT15M`、`PT30M`、`PT1H`、`PT6H`、`PT12H`、`P1D`；`evaluation_frequency` 可选 `PT1M`、`PT5M`、`PT10M`、`PT15M`、`PT30M`、`PT1H`。评估间隔不能大于窗口。两项省略时均默认 `PT1M`，保留旧配置行为；示例使用 `PT5M` / `PT1M`，表示每分钟评估最近五分钟的平均时延。
- 配置文件继续使用 `window_size` / `evaluation_frequency`，生成的告警使用 Azure 的 `windowSize` / `evaluationFrequency`。这些选项参考 [Azure Monitor 静态指标告警模板](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/resource-manager-alerts-metric)，实际资源/指标限制仍以 Azure 返回为准。修改后重新生成并单独更新告警；只改 Portal 会造成配置漂移。工作流不校验这两个字段，仅调整窗口/频率无需更新其定义。
- 工作流要求事件中的 threshold 与生成值相等。正式阈值变化时先暂停告警、排空运行，同步更新配置、重新生成并单独更新工作流，再逐条更新禁用告警，不能只调告警规则。

### 先单独创建或确认 Action Group

四条告警必须绑定一个明确的 Action Group。新建时单独创建，不与告警混在一条发布脚本里：

```text
PUT {action_group_resource_id}?api-version=2023-01-01
location: global
properties.groupShortName: 配置中的短名称
properties.enabled: true
properties.logicAppReceivers:
  - name: degraded-routing-controller
    resourceId: 第四步的 Logic App 完整 ID
    callbackUrl: 该工作流 receive 触发器的回调 URL
    useCommonAlertSchema: true
```

回调通过工作流 `triggers/receive/listCallbackUrl?api-version=2019-05-01` 的 POST 获取，它含访问凭据，不能输出或提交到非秘密文件。用受控请求体执行单独的 `az rest --method put`；Action Group 启用不等于告警已启用。

**复用已有 Action Group（包括跨资源组）时，不修改其其他接收器。** 核对其启用状态、实际 Logic App 目标、回调和 Common Alert Schema，并评估所有接收器的通知影响。配置生成器默认把 Action Group 放在控制资源组；当前没有独立的外部 Action Group ID 配置项。若选用外部组，需在未提交的告警发布副本中把四条 `actions[].actionGroupId` 改为确认的完整 ID，审阅后发布并保留该差异记录。

这类手工定制路径不能盲目重跑原 `deploy`、`smoke`、`enable-alerts`：它们按配置推导的工作流、Action Group、原始策略和告警定义进行写入或检查，可能覆盖绑定或因漂移失败。应先统一实现/配置，或使用审阅后的单资源命令并逐项完成等价验收；不要伪造 smoke receipt。

### 每次只创建一条告警

使用审核后的 `alerts.json`，确保每条 `enabled` 为 `false`。不是把整个文件作为 ARM 模板提交，而是按资源 ID 提取：

```bash
# SUB/RG 取控制资源订阅和资源组；NAME 每次选择一个已审核的 alert_name。
ALERTS=./rendered/alerts.json
RID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Insights/metricAlerts/$NAME"
BODY=$(jq -ec --arg id "$RID" '.[$id] // error("Alert not found")' "$ALERTS") &&
az rest --method put --subscription "$SUB" \
  --url "https://management.azure.com$RID?api-version=2018-03-01" \
  --headers Content-Type=application/json --body "$BODY" \
  --query '{name:name,enabled:properties.enabled,actions:properties.actions}' -o json
```

四条分别执行、分别确认。`PUT` 会覆盖已有同名告警，先读取现状；遇到权限、维度或资源不匹配错误停止，不继续批量提交。完成后读取四条规则，核对 Foundry scope、模型部署维度、指标、阈值、窗口、频率、Action Group 和禁用状态。

**本步完成条件：**正式配置有历史数据依据，Action Group 指向预期工作流，四条规则正确且尚未启用。

## 可选：新增 APIM 诊断日志后端时延 p95 告警

以下是在原四条 Foundry 告警之外新增一套规则；不替换原规则、不修改 APIM 策略。先按前五步准备现有控制链路。无 `apim_log_alerts` 配置时行为不变。

### 采集前置条件

准备已有 Log Analytics workspace，配置 APIM Azure Monitor 诊断设置，将 `GatewayLogs` 发往该 workspace，使用 **Resource specific / Dedicated** 模式。表必须为 `ApiManagementGatewayLogs`，使用支持此日志告警查询的 **Analytics** 表计划；当前不支持旧 `AzureDiagnostics` 或 Basic 表方案。项目不自动创建 workspace、开启诊断、修改日志计划或采样比例。

等待真实日志摄取后，确认目标 API 的各后端均有 `BackendId`、`BackendTime`（毫秒）、`BackendUrl`、`ApiId` 等字段；BackendId 应与配置 `backend_id` 一致。不要为本方案启用请求/响应正文或认证头采集。结合采样比例、摄取延迟、重试和流式响应确认数据可归因；缺字段时先修复采集，不跳过查询验证。创建者需要 workspace 查询及日志告警写入权限；Logic App 系统身份仍只需原两份 Named Value 权限。

### 独立配置与生成

在未提交的 `config.local.json` 根节点加入：

```json
{
  "apim_log_alerts": {
    "workspace_resource_id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/replace-logs-rg/providers/Microsoft.OperationalInsights/workspaces/replace-existing-workspace",
    "location": "replacewithworkspacelocation",
    "threshold_ms": 2000,
    "window_size": "PT5M",
    "evaluation_frequency": "PT5M",
    "min_samples": 20
  }
}
```

这是需合并到已有配置的片段，不是完整配置。每条 route 同时新增唯一 `log_alert_name`，例如分别为 `replace-chat-a-p95`、`replace-chat-b-p95`、`replace-embedding-c-p95`、`replace-embedding-d-p95`；保留原 `alert_name`。八个名称必须全局唯一（忽略大小写）。

workspace ID 和其实际区域必填；其余字段省略时采用上例默认值。p95 阈值独立于根节点 Foundry `threshold_ms`，必须为有限正数且不超过 86400000 ms。`min_samples` 是每个后端每个评估窗口的有效记录数，范围 1–1000000；建议根据代表性历史日志确定阈值与门槛，不直接复用 TTLT 平均值阈值。

日志窗口可选 `PT5M`、`PT15M`、`PT30M`、`PT1H`、`PT6H`、`PT12H`、`P1D`；评估频率可选 `PT5M`、`PT10M`、`PT15M`、`PT30M`、`PT1H`，不得大于窗口。本实现有意不支持一分钟日志评估，避免一分钟查询限制和采集延迟造成误解。窗口由 Scheduled Query Rule 施加到 `TimeGenerated`，生成的 KQL 不另行固定 `ago(...)`。

重新运行第二步离线命令。`alerts.json` 现在包含八条禁用规则；`manifest.json.alert_api_versions` 给出每条资源的正确 API 版本；`workflow.json` 包含八个静态映射。从每条日志规则取出 `properties.criteria.allOf[0].query`，在目标 workspace 选择与窗口一致的时间范围执行，确认只返回目标后端的 `BackendId` 与 `BackendLatencyP95Ms`；无数据或有效样本不足应返回零行，而不是 p95=0。

### 分别发布和验收

1. 暂停原四条及任何已存在的日志规则，禁用工作流并排空运行。保留原名单、ETag、工作流身份、Webhook 及 Action Group，不重装策略、不清空名单。
2. 按第四步独立更新工作流 definition。标准部署映射检查允许从四条扩为八条，但不允许直接删除/重命名已有规则，避免遗留启用的孤立规则。
3. 按第五步逐条创建四条日志规则，确认 `enabled=false`；日志资源不是 `metricAlerts`，请求结构如下。复用外部 Action Group 时应修改 `properties.actions.actionGroups` 数组，不能沿用指标规则的 `actions[].actionGroupId` 格式。

```bash
# NAME 每次选择一个已审核的 log_alert_name；SUB/RG 仍为控制资源订阅和资源组。
RID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Insights/scheduledQueryRules/$NAME"
BODY=$(jq -ec --arg id "$RID" '.[$id] // error("Alert not found")' ./rendered/alerts.json) &&
az rest --method put --subscription "$SUB" \
  --url "https://management.azure.com$RID?api-version=2023-12-01" \
  --headers Content-Type=application/json --body "$BODY" \
  --query '{name:name,enabled:properties.enabled,scopes:properties.scopes}' -o json
```

4. 核对 workspace scope、区域、查询、样本门槛、BackendId 维度、`metricMeasureColumn=BackendLatencyP95Ms`、`timeAggregation=Maximum`、阈值、窗口和 Common Alert Schema 接收器。查询在整个窗口计算一次 p95，Maximum 只是读取该单值；不要改为 Count 或对小窗口 p95 求平均。
5. 标准路径的 `smoke` 现在覆盖两种来源，共 24 次通知、6 次拒绝检查，并分别临时修改/恢复名单；原 12 次检查的 receipt 不能启用八条规则。`disable-alerts`、`enable-alerts` 和保留的 `deploy` 命令都处理全部配置规则及各自 API 版本，但部署仍推荐本文的逐资源发布方式。
6. 按第六步在授权窗口单独演练四条日志规则：临时降低 `apim_log_alerts.threshold_ms` 并同步工作流，原生指标规则保持禁用以隔离证据。向指定候选发送预算内请求，满足 `min_samples` 后等待日志摄取。验证实际 KQL p95 严格越阈值 → Log Alerts V2 Fired → 正确组名单更新 → 钉钉显示“APIM 后端时延 p95” → 后续选路变化；重复事件幂等，Resolved（包括无数据恢复）不清名单。恢复正式配置后重新验收，再按计划逐条启用两类规则。

回滚日志功能时先禁用/按需删除四条日志规则，排空工作流，再恢复旧定义和旧配置；仅删除本地 `apim_log_alerts` 不能停掉云端规则。手动恢复某后端时必须同时暂停对应两种来源，否则另一规则可能继续降级。不要把无数据或样本不足造成的 Resolved 当作后端恢复证据。

## 6. 临时调低阈值，触发真实告警完成闭环验证

**仅在获授权的维护窗口执行。** 通知业务及钉钉接收方，设置请求数、费用、最长等待时间和中止条件。记录正式配置、阈值、窗口、各告警启用状态、工作流版本、名单及 ETag。这一步会产生真实推理费用、告警、名单变更和通知。

### 同步临时阈值，不只修改告警

1. 保持四条告警禁用，暂停其他名单写入者；禁用工作流并等待在途运行结束。
2. 将配置复制为未提交的 `config.drill.local.json`，只把 `threshold_ms` 调为根据历史指标选出的较低正数（不能设为 0）。保持正式 `config.local.json` 不变。
3. 离线生成演练定义：

```bash
python3 -B -m apim_routing --config config.drill.local.json validate
python3 -B -m apim_routing --config config.drill.local.json render --output-dir rendered/drill
```

4. 单独更新原工作流 definition 中的临时阈值，保留系统身份、Webhook 安全参数和其他设置；恢复工作流运行，再逐条更新四条告警的临时定义，仍保持禁用。外部 Action Group 绑定需在演练定义中同样保留，不能被重新生成的默认 ID 覆盖。
5. 完成模型调用、权限和通知链路的受控预检。标准配置路径可先执行下面的 synthetic smoke；手工定制路径按第五步说明完成等价预检。

```bash
python3 -B -m apim_routing --config config.drill.local.json smoke \
  --confirm-mutations --receipt rendered/drill/smoke-receipt.json
```

smoke 会临时修改并恢复名单、发送 12 次通知并检查三种拒绝情况，**不调用模型，不是真实时延闭环证据**。receipt 有效期为 24 小时，关联配置和工作流状态；修改配置/工作流后旧 receipt 不能复用。只在隔离窗口、无其他写入时运行。

### 触发并观察真实链路

每次仅启用一条待验收告警（审阅后在 Portal 或单资源 ARM PATCH 中设 `properties.enabled=true`），其他规则保持禁用。确认该规则不是已经持续 Fired；清除名单本身不会保证再次发送 Fired。

经 APIM 向对应模型发少量、预算内的正常请求，确保待测候选实际收到流量；查看真实 TTLT 指标是否在临时阈值之上。依次对四条映射执行，不能用手工构造事件或客户端耗时替代真实指标。

| 环节 | 必须观察的证据 |
|---|---|
| 原生指标 | 正确 Foundry、部署维度、时间桶的 TTLT Average 超过临时阈值 |
| Azure Monitor | 对应规则产生真实 Fired 及告警 ID |
| Action Group / Logic App | 预期工作流收到该事件，校验通过，运行成功 |
| Named Value | 对应组新增正确 backend_name，ETag 条件写入成功，另一组不受影响 |
| 钉钉 | 通知发送成功且实际收到，不能只看 HTTP 触发器接受成功 |
| 后续 APIM 请求 | 配置传播后优先选择未降级成员；不自动屏蔽所有降级成员 |

若没有可用指标或始终未越阈值，记录“未触发”，停止并分析，不无限加压。持续 401/403、严重限流、影响业务、ETag 冲突、通知失败或预算耗尽时立即停止；通知失败也可能已经写入名单，不假定自动回滚。

### 恢复正式设置

演练后先逐条禁用告警、停止演练请求，禁用并排空工作流；恢复正式 workflow definition 和四条正式告警阈值/窗口/绑定，保留身份和秘密。用最新 ETag 仅移除演练产生的名单项，保留真实故障状态；核对各候选健康及策略选路后再决定恢复名单。

恢复工作流，完成正式配置预检，再按审批逐条恢复或启用正式告警。若使用标准路径的 `enable-alerts` 命令，需要在正式配置下重新完成 smoke，不能使用临时配置 receipt；该命令会启用四条规则，不适用于要求逐条审批的发布方式。

**Resolved 只通知，不会自动清除 degraded 名单。** 禁用告警也不会取消已在途的工作流或撤回 APIM 策略。恢复失败时保持相关告警禁用，记录残留资源/状态并交接，不宣布闭环完成。

**最终完成条件：**四条映射均有真实指标 → Fired → 工作流 → 名单 → 钉钉 → 后续选路的证据，正式阈值与窗口已恢复，演练状态已按审批清理，运行负责人明确。日常操作与回滚见 [运行与恢复](operations.md)。
