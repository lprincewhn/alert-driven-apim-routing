# Alert-driven APIM Routing

**Azure Monitor 告警触发 Logic Apps，更新已有 Azure API Management 的路由优先级。**

本项目把 Foundry 原生时延告警转换为 APIM Named Value 的状态变化。聊天和 Embedding 各维护一份 **degraded（降级）名单**：首次请求优先正常后端，遇到 429/5xx 仍可重试降级后端。降级不是屏蔽。

## 方案边界

- **Bring your own APIM**：引用现有 APIM、API、操作、backend 和用户分配托管身份；不创建 APIM。
- Foundry 账户和模型部署也是外部资源；不创建或扩容模型，不替调用方决定区域与配额。
- 本项目负责控制器、Action Group、四条模型时延告警和两份降级名单，并提供可显式安装的 API 策略。
- 订阅、资源组、资源 ID、后端 ID、模型部署名称、路由标识和通知凭据均由操作者提供。源码不包含任何实验环境配置。
- 不是 APIM 原生负载均衡池或断路器；不执行主动健康探测，不自动恢复，不承诺自动补偿漏投事件。

## 工作原理

```text
已有 Foundry 模型部署
  └─ AzureOpenAITTLTInMS / Average > threshold
       └─ Azure Monitor metric alert
            └─ Action Group / Common Alert Schema
                 └─ Logic App（系统托管身份）
                      ├─ 校验事件及静态规则映射
                      ├─ GET 对应组的 Named Value + ETag
                      ├─ If-Match PATCH：加入降级后端标识
                      └─ 钉钉通知（含 Azure）

已有 APIM API
  └─ 读取 chat 或 embedding 的 degraded 名单
       ├─ 首次：正常成员优先；全降级仍可选择
       └─ 429/5xx：最多重试另一个成员一次
```

默认告警阈值 **2,000 ms**。省略窗口/频率时，按 **1 分钟平均值**判断、每分钟评估一次；配置示例演示 **5 分钟窗口、1 分钟评估间隔**。使用 `window_size` 和 `evaluation_frequency` 选择时长，评估间隔不能大于窗口，可选值见部署文档。指标是 Foundry 原生 TTLT，不是客户端端到端耗时，也不是首 token 时延。

## 路由行为

| 当前组状态 / 响应 | 行为 |
|---|---|
| 两个成员均未降级 | 请求 ID 哈希近似 50/50 选择 |
| 一个降级 | 首次优先另一个，429/5xx 可以回退到降级成员 |
| 两个均降级 | 两者仍可访问，不因全降级主动返回无可用后端 |
| 429 或 5xx | 最多一次跨成员重试，总计最多两次转发 |
| 401 / 403 / 404 / 慢但成功的 200 | 不重试 |
| 名单非法 | 对应组返回配置错误，不静默当作健康 |
| 告警 Resolved | 通知并保留降级标记；恢复由操作者决定 |

每组固定两个成员。一个请求使用一个配置快照；控制器写入后，网关配置传播并非瞬时完成。每次重试可能产生额外推理费用，不保证 exactly-once。

## 文档

- [方案说明](docs/architecture.md)：事件契约、状态机、权限、安全与失败边界。
- [部署步骤](docs/deployment.md)：配置、离线生成、策略与名单手动验证、工作流与授权、告警创建、低阈值闭环演练六步发布。
- [运行与恢复](docs/operations.md)：告警观察、验收、手动恢复、变更和回滚。

使用 Python 3.9+，无需安装 Python 第三方依赖：

```bash
cp examples/config.example.json config.local.json
# 编辑 config.local.json，填入已有资源；不要提交该文件。
python3 -m apim_routing --config config.local.json validate
python3 -m apim_routing --config config.local.json render --output-dir rendered
```

这两个命令只做离线校验/生成，不会写入 Azure。实际部署按部署文档逐步执行。

| 源码 | 用途 |
|---|---|
| `apim_routing/policy.xml.template` | APIM 路由策略模板 |
| `apim_routing/workflow.py` | Logic Apps 定义生成器 |
| `apim_routing/deploy.py` | 控制资源部署、授权和启用流程 |
| `examples/config.example.json` | 环境独立的配置示例 |

**部署按六步分开执行，不使用一次性部署脚本。** 先填写配置并离线生成定义，在维护窗口通知业务、备份并审阅策略，准备 Named Value、安装策略并手动修改名单验证选路；随后单独创建 Logic App 并精确授权，确认 Action Group，依据历史时延逐条创建禁用告警，最后同步调低工作流与告警阈值完成真实闭环演练并恢复正式设置。策略安装即可影响流量，不必等到告警启用。

## 安全与环境隔离

仓库示例只描述配置结构，不是可以直接访问的环境。真实配置、生成目录及凭据不得提交。DingTalk Webhook 通过秘密输入传给 Logic App `SecureString` 参数，通知动作隐藏输入输出。Logic App 回调 URL 同样是凭据。

本仓库没有自动调用生产 Azure 的 CI，也不附带模型压测或历史实验日志。离线测试只能证明配置与生成逻辑；部署后仍须在获授权的隔离环境完成真实闭环验收。
