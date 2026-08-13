# 健康与查询

本参考用于 `vm resource-health`、`service-health` 和 `vm lookup`。

所有 Azure 命令都必须通过 `scripts/az_guard.py` 执行。只要参数包含用户提供文本，就必须使用 JSON 请求文件。

## 订阅上下文

实时查询从以下命令开始：

```text
account show --query {subscription:id,subscriptionName:name,tenant:tenantId,user:user.name} -o json
```

如果用户明确请求了其他订阅，先验证其可访问性，再使用 `--subscription`。禁止静默切换订阅。

## VM Resource Health

必须提供精确的 VM 名称和资源组。先通过精简 `vm show` 查询确认 VM 存在，再调用：

```text
rest --method get --url https://management.azure.com/subscriptions/<subscription>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions --query value[].{time:properties.occuredTime,state:properties.availabilityState,title:properties.title,category:properties.category,cause:properties.healthEventCause,reason:properties.reasonType,summary:properties.summary,actions:properties.recommendedActions[].action} -o json
```

规则：

- 按事件时间降序排序，不要信任返回顺序。
- `state=null` 的维护记录是事件描述，不是 `Unknown` 状态。
- 有明确时间窗口时，窗口外事件不得参与展示与判定。
- 未提供时间窗口时，展示用户要求的条数，默认 5 条；当前状态描述基于最新的非 null 状态。
- `Available` 判定为健康。`Unavailable` 和 `Degraded` 判定为异常。`Unknown` 判定为无法判断。
- 历史平台事件即使曾异常但后来恢复为 `Available`，也不应使当前状态判定为异常。
- 摘要翻译要忠实，保留 Azure 资源与产品名称，不要臆造根因。

## 订阅级 Service Health

Service Health 是订阅级能力，不能替代单个 VM 的 Resource Health。使用 ARM 查询事件：

```text
rest --method get --url https://management.azure.com/subscriptions/<subscription>/providers/Microsoft.ResourceHealth/events?api-version=2022-10-01 --query value[].{trackingId:name,type:properties.eventType,status:properties.status,level:properties.level,title:properties.title,start:properties.impactStartTime,mitigated:properties.impactMitigationTime,updated:properties.lastUpdateTime,summary:properties.summary,impact:properties.impact[].{service:impactedService,regions:impactedRegions[].impactedRegion}} -o json
```

用户未提供时间范围时，使用 7 天窗口。仍在进行中的更早事件也要纳入，因为仍具相关性。将用户指定的服务、区域、事件类型和时间筛选应用到返回的结构化字段上。

判定规则：

- 处于活动状态的 `ServiceIssue` 表示存在活动中的平台事件。
- 已解决的 `ServiceIssue` 属于历史背景，不是当前中断。
- 计划维护、健康公告、安全公告和 RCA 记录是重要通知，但不自动等价于服务中断。
- 过滤结果为空仅表示没有返回匹配事件，不等于“所有 Azure 服务在全球范围完全健康”。

报告中应包含受影响服务、区域、开始与缓解时间、状态、可用时的 tracking ID，以及最近一次更新。对于活动事件，建议持续关注 Azure Service Health。

## VM 名称到计算机名

当输入为 VM 资源名时，需提供或解析资源组，然后调用：

```text
vm get-instance-view -g <rg> -n <vm> --query instanceView.computerName -o json
```

若 VM 已停止或来宾代理不可用导致未上报计算机名，返回 `N/A`。不要从 `osProfile` 或 VM 名称进行推断。

## 计算机名到 VM 资源名

通过 guard 的固定只读 POST 端点使用 Azure Resource Graph。写入请求文件，参数等价于：

```text
rest --method post --url https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01 --body {"subscriptions":["<subscription>"],"query":"Resources | where type =~ 'microsoft.compute/virtualmachines' | extend computerName=tostring(properties.extended.instanceView.computerName) | where computerName in~ ('<fqdn>') | project computerName,vmName=tostring(split(id,'/')[-1]),resourceGroup,location,id | limit 50"} -o json
```

在写入 JSON 请求前，将计算机名按 KQL 字符串值正确转义。优先使用不区分大小写的精确匹配。若精确匹配无结果，可在用户确认片段后提供单独的 contains 搜索；需标注为模糊匹配，并展示全部候选，禁止静默选择其一。

对于多个输入，使用 `in~` 合并查询，并将每个请求值映射为 0 条、1 条或多条结果。保留 Azure 返回的 VM 资源名大小写。

## 查询输出

对每个输入，仅展示有用映射；在需要消歧时附带资源组：

```text
<computer name> -> <VM resource name> (<resource group>)
<VM resource name> -> <computer name or N/A>
```

除非用户确实需要用订阅 ID 消歧，否则不要暴露订阅 ID。
