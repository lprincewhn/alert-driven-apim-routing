# 方案说明：告警驱动路由更新

## 目标与外部依赖

目标是把模型部署的**原生时延告警**及可选的 **APIM 诊断日志后端时延 p95 告警**变为 APIM 的**后续请求路由优先级变化**，而不是在 Logic App 中代理推理请求。

APIM 服务、API 操作、backend、用户分配托管身份、Foundry 账户和模型部署由外部管理。本项目引用它们，不创建 APIM、不改模型容量、不修改 backend URL。控制资源与 APIM 可以位于不同资源组；跨订阅引用是否支持，以配置校验和部署权限为准，不能假定当前登录身份拥有目标权限。

每组恰有两个候选成员，分为 `chat` 和 `embedding`。后端的路由标识是配置中的 `backend_name`：组内唯一的稳定逻辑后端名称，不必是 Azure 区域名，也不是可随意修改的展示名称。它用于名单和响应头；`backend_id` 才是实际 APIM 转发目标。

## 数据面

默认策略根据路径参数 `deployment-id` 是否包含小写 `embedding` 分类，判断区分大小写。API 操作须声明该参数；同组两个 Foundry 后端须提供与请求 URL 一致的模型部署名称。本方案不做部署名别名改写，不实现任意路径代理，也不把聊天重试语义应用到文件、任务或有状态会话。

两份 Named Value 均是非 secret 字符串：

```text
none                  # 无降级成员
primary               # backend_name 为 primary 的成员降级
primary,secondary     # 两个成员均降级
```

`primary`、`secondary` 仅为示例后端名称，并不表示固定主备角色。非法后端名称、重复项、空值、空格、跨组值、尾逗号或混用 `none` 都应拒绝；不能默认为全部正常。

首次选择排除降级成员；若没有正常成员，则恢复为全组候选。两者处于同一优先级时，按请求 ID 哈希近似均分。重试候选始终保留全组，所以正常后端返回 429/5xx 时可以回退到降级后端。

策略使用配置的 APIM 用户分配托管身份，获取 audience 为 `https://cognitiveservices.azure.com` 的令牌。调用方的 APIM 订阅授权与 APIM 到 Foundry 的模型授权是不同环节。托管身份数据权限需在外部准备。

保留请求体缓冲以支持一次重试，不新增长请求总时限或流式故障恢复机制。无响应对象的传输异常不保证进入基于状态码的重试分支。最终后端响应头不等于完整尝试日志。

## 控制面

```text
有效 Fired
  → 静态映射匹配：规则 → 资源/模型 → 组/backend_name → Named Value
  → GET 当前名单和 ETag
  → 校验名单
  → 已包含 backend_name：AlreadyDegraded
  → 未包含 backend_name：If-Match PATCH 并集 → Updated
  → 发送通知

有效 Resolved → ResolvedIgnored → 发送通知；不改变名单
非法事件      → 拒绝；不写名单、不发通知
读写失败      → ControllerFailed → 尝试失败通知；运行仍失败
```

控制器校验 Common Alert Schema、指标身份、资源 ID、部署维度、阈值和规则映射；不接受告警携带任意 Named Value 路径或 HTTP 目标。事件的 schema 校验只是输入约束，不能替代入口凭据保护。

Fired 事件要求指标大于相应阈值，时间位于过去 30 分钟至未来 2 分钟。Resolved 使用恢复时间判断新鲜度；非常旧的失败运行不应盲目重新提交。

并发控制与覆盖保护承担不同职责：

- 工作流串行执行，防止同一工作流实例之间并发修改。
- ETag 条件写入防止覆盖管理员或其他控制器的更新。
- 无条件 `If-Match: *` 不允许；遇到冲突必须可见，不能用旧名单覆盖新状态。
- 重复事件依靠集合并集保持幂等，不依靠事件传递恰好一次。

当前没有定时对账或自动重试失败写入。系统是事件驱动，不是持续强一致健康数据库。

## 指标契约

每个模型部署各自绑定一条告警；聊天和 Embedding 不共用同一条规则：

| 属性 | 语义 |
|---|---|
| namespace | `Microsoft.CognitiveServices/accounts` |
| metric | `AzureOpenAITTLTInMS` |
| dimension | `ModelDeploymentName`，仅一个部署 |
| aggregation | `Average` |
| operator | `GreaterThan` |
| 默认 threshold | 2000 ms |
| window / evaluation | 由 `window_size` / `evaluation_frequency` 配置；省略时 PT1M / PT1M，示例 PT5M / PT1M；评估间隔不大于窗口 |
| autoMitigate | 告警自身允许进入 Resolved，不清除路由降级 |

这里的“整体时延”是服务暴露的原生 TTLT 边界，不是网络、APIM、重试和客户端读取时间的简单总和。它也不是 TTFT。低流量下缺少额外最小样本门槛，单个长请求可能影响一分钟平均值；高流量下短请求可能稀释长请求影响。

配置变更必须同步规则与控制器，否则白名单/阈值校验会拒绝事件。

## 可选 APIM 日志契约

`apim_log_alerts` 存在时额外生成四条 `Microsoft.Insights/scheduledQueryRules`（API `2023-12-01`），每条绑定一个已有 route；未配置时仍仅生成原有四条 Foundry 告警。两种来源共用 Action Group、ETag 并集写入和钉钉通知；任一有效 Fired 都可降级，Resolved 均不移除名单。

只支持资源专用表 `ApiManagementGatewayLogs`，不把旧 `AzureDiagnostics` 列名混入查询。查询以 `_ResourceId`、`ApiId`、`BackendId`、POST 和确切部署路径隔离候选；只统计 `BackendTime > 0` 且有后端响应码的记录，不将缓存命中、缺值、零耗时或无后端响应当成健康样本。有响应的 429/5xx 不因失败状态被排除。每条规则在其评估窗口内产生单个后端的 `BackendLatencyP95Ms`，只在 `SampleCount >= min_samples` 时返回结果，不按小时间桶先求 p95 再平均。规则的 `Maximum` 只是读取这一个 p95 值，不是把后端时延取最大值。

日志 scope 是 workspace，事件契约为 `signalType=Log`、`monitoringService=Log Alerts V2`、`conditionType=LogQueryCriteria`。控制器校验受信规则名、workspace、完整生成查询、`metricMeasureColumn`、`Maximum`、阈值和唯一 `BackendId` 维度，再映射到对应组/名单；不使用事件中的 URL 进行查询或更新。Fired 必须严格大于阈值且在新鲜度窗口内。Resolved 可以为 null 测量值；缺少恢复时间时使用评估窗口结束时间判断新鲜度，不恢复路由。原有 Metric/Platform 的 TTLT 校验保持独立，不能互换规则名绕过来源校验。

`BackendTime` 是网关记录的后端耗时，不等于 TTLT、客户端端到端耗时或首 token 时延。网关诊断记录不保证每次跨后端重试分别提供一条可归因记录，不能从最终 `BackendId` 推断第一次尝试的时延；流式长响应也有不同测量边界。上线前必须用实际网关记录确认 BackendId、耗时单位、重试和流式行为。若日志缺少 BackendId，本实现不回退按 URL 猜测后端，应先解决采集兼容性。

日志摄取延迟、采样和低流量均会影响 p95 与告警速度；诊断/查询和告警也会增加费用。样本不足或数据中断不是健康证明，必要时另设采集可用性告警。无需给控制器身份增加日志读取权限；日志采集、workspace/table 访问和告警查询权限由操作者按 Azure Monitor 要求独立准备。

参考：[表结构](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/apimanagementgatewaylogs)、[Scheduled Query Rules](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-12-01/scheduledqueryrules)、[Log Alerts V2 Common Alert Schema](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-common-schema#sample-log-search-alert-when-the-monitoringservice--log-alerts-v2)。

## 通知与失败

钉钉通知使用中文，包含 `Azure`、业务类型、逻辑后端名称、处理结果和可获得的名单前后值。时延根据来源明确标为“平均总响应时延（TTLT）”或“APIM 后端时延 p95”，单位为毫秒；无数据的日志恢复事件显示 `no data`，不会伪报 0 ms。

更新成功、重复降级、告警恢复但不自动恢复路由、控制器失败分别给出中文说明；区域标识和错误代码保留原值便于排查。生成的 JSON 使用 UTF-8 可读中文，不转换为 Unicode 转义序列。HTTP 响应中的机器状态码保持不变。仅在 HTTP 成功且业务 `errcode=0` 时认定通知成功。

| 名单写入 | 通知 | 结果 |
|---|---|---|
| 成功 | 成功 | 更新与通知完成 |
| 成功 | 失败 | 名单已经改变；运行失败不代表回滚 |
| 失败 | 成功 | 已通知故障；运行仍失败 |
| 失败 | 失败 | 需要分别处理更新故障和通知故障 |

HTTP 触发器的异步接收成功不是整体处理成功。必须检查运行、写入动作和通知完成状态。

`dingtalkWebhook` 使用 `SecureString`，定义中的 `defaultValue` 为占位字符串 `"none"`，不是真实 Webhook，也不是关闭通知的开关。部署时必须通过 `properties.parameters.dingtalkWebhook.value` 注入真实地址；未配置时通知不能成功，运行会报告失败。通知 HTTP 动作启用安全输入/输出。Webhook 和 Action Group 使用的 Logic App callback URL 都不能放进仓库或公开日志。本实现不提供每次请求动态生成钉钉 HMAC 签名；固定带时间戳的签名 URL 不适用于长期部署。

## 权限与最小影响范围

| 主体 | 所需权限 |
|---|---|
| 部署人员 | 控制资源部署，以及已有 APIM 所需子资源读取/写入；不因此获得模型数据面权限 |
| RBAC 操作者 | 目标范围的角色授权能力；Contributor 本身不能授予角色 |
| Logic App 系统身份 | 两个 Named Value 各自范围的 Contributor；不是整个 APIM 或订阅 |
| APIM UAMI | 对应 Foundry 账户的模型调用权限，例如 Cognitive Services OpenAI User |

资源范围收窄的 Contributor 仍不是动作级最小自定义角色。更严格的组织可以基于 `namedValues/read` 和 `namedValues/write` 制定自定义角色，但需自行验证。

## 明确不做

不创建 APIM 或模型；不做硬隔离；不启用原生 APIM pool/circuit breaker；不自动恢复或主动探测；不保证候选后端容量；不自动把鉴权错误转换为重试；不保证 SSE 断流续传；不跨后端迁移有状态会话；不将历史实验结果作为新环境的验收。
