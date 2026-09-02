# VM 诊断

本参考用于 `vm full`、`vm cpu`、`vm memory`、`vm disk` 和 `vm network`。

## 输入要求

- 必须提供精确的 VM 资源名称和资源组。优先复用当前会话中已明确给出的值；若缺失则向用户询问。
- 默认使用当前 Azure CLI 订阅，除非用户明确指定了另一个可访问订阅。
- 默认时间窗口为过去 30 分钟。支持用户显式提供绝对时间范围或回溯时长。
- 将 API 时间戳转换为用户时区。中文会话在未指定其他时区时使用 Asia/Shanghai。

不要臆造默认资源组。必须保留完整 VM 名称，包括尾部数字组成部分。

## Guard 调用

不含用户可控值的静态命令可直接传参：

```bash
python3 skills/azure-ops/scripts/az_guard.py -- account show --query '{subscription:id,tenant:tenantId,user:user.name}' -o json
```

对于包含资源名、ID、筛选条件、时间或其他用户可控值的命令，必须使用文件原语写入 JSON 请求，并在 shell 中仅传递文件路径：

```json
{
  "args": [
    "vm",
    "show",
    "-d",
    "-g",
    "RESOURCE_GROUP",
    "-n",
    "VM_NAME",
    "-o",
    "json"
  ]
}
```

```bash
python3 skills/azure-ops/scripts/az_guard.py --request-file .azure-ops/request.json
```

仅可在完整替换整个 JSON 文档后复用该请求文件。禁止使用 shell 插值拼接构造。

## 共享清单

每次 VM 诊断都先执行一次：

```text
vm show -d -g <rg> -n <vm> --query {id:id,name:name,location:location,vmSize:hardwareProfile.vmSize,osType:storageProfile.osDisk.osType,powerState:powerState,osDisk:storageProfile.osDisk.{name:name,sku:managedDisk.storageAccountType},dataDisks:storageProfile.dataDisks[].{lun:lun,name:name,sku:managedDisk.storageAccountType},nics:networkProfile.networkInterfaces[].{id:id,primary:primary}} -o json
```

然后获取来宾系统上报的计算机名：

```text
vm get-instance-view -g <rg> -n <vm> --query instanceView.computerName -o json
```

若 VM 不存在，立即停止并说明该精确名称与资源组未解析到资源。若电源状态不是 running，仅在控制平面数据仍有意义时继续，并提示指标可能缺失。

从返回的资源 ID 中提取订阅 ID。然后查询一次 VM SKU：

```text
rest --method get --url https://management.azure.com/subscriptions/<subscription>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location%20eq%20'<location>' --query value[?name=='<vmSize>']|[0].{vCPUs:capabilities[?name=='vCPUs'].value|[0],MemoryGB:capabilities[?name=='MemoryGB'].value|[0]} -o json
```

若 SKU 能力字段不可用，显示为 `N/A`；禁止根据 SKU 名称推断。

## CPU

在不超过两小时的窗口内，以一分钟粒度查询 `Percentage CPU`。更长窗口需使用受支持的更粗粒度 interval。请求 `Average` 和 `Maximum`，并通过 JMESPath 在服务端做归约：

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric Percentage CPU --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Average Maximum --query {average:avg(value[0].timeseries[0].data[?average!=null].average),peak:max(value[0].timeseries[0].data[?maximum!=null].maximum),high:length(value[0].timeseries[0].data[?maximum>=`90`]),samples:length(value[0].timeseries[0].data[?maximum!=null])} -o json
```

判定规则：

- 当 CPU 大于等于 90% 的样本占有效样本的 20% 及以上时，判定为异常。
- 当存在有效样本且高占比样本低于 20% 时，判定为健康。
- 当不存在有效样本时，判定为无法判断。

单次孤立峰值仅是需要排查的线索，不能单独认定为持续 CPU 事件。

当用户询问 B-series、CPU burst 或 CPU credits，或 `Percentage CPU` 异常且 Azure 返回 credits 指标时，额外查询 `CPU Credits Remaining` 和 `CPU Credits Consumed`，使用 `Average` 与一分钟粒度，并归约窗口起点、终点和最小剩余值。只有 Azure 实际返回有效 credits 样本时，才按 `vm-performance-patterns.md` 解释 burst 限流。credits 指标缺失时不得根据 SKU 名称推断。

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "CPU Credits Remaining" "CPU Credits Consumed" --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Average --query value[].{metric:name.value,first:timeseries[0].data[?average!=null]|[0].average,last:timeseries[0].data[?average!=null]|[-1].average,minimum:min(timeseries[0].data[?average!=null].average),samples:length(timeseries[0].data[?average!=null])} -o json
```

## 内存

只有在已配置所需监控代理和采集规则时，Azure 才会暴露来宾内存指标。查询 `Available Memory Percentage`：

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric Available Memory Percentage --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Average Minimum --query {averageAvailable:avg(value[0].timeseries[0].data[?average!=null].average),minimumAvailable:min(value[0].timeseries[0].data[?minimum!=null].minimum),high:length(value[0].timeseries[0].data[?minimum<=`10`]),samples:length(value[0].timeseries[0].data[?minimum!=null])} -o json
```

计算方式：

- 平均已用百分比 = $100 - averageAvailable$。
- 峰值已用百分比 = $100 - minimumAvailable$。
- 高占比 = 已用百分比大于等于 90% 的样本数 ÷ 有效样本数。

判定规则：

- 当高占比大于等于 20% 时，判定为异常。
- 当存在有效样本且高占比低于 20% 时，判定为健康。
- 当该指标缺失时，判定为无法判断。应建议检查 Azure Monitor Agent 与数据采集配置；不得声称 VM 内存充足。

## 磁盘

使用共享清单中的 OS 盘和数据盘名称。查询每块盘的当前属性：

```text
disk show -g <rg> -n <disk> --query {sku:sku.name,sizeGB:diskSizeGB,tier:tier,iops:diskIOPSReadWrite,mbps:diskMBpsReadWrite} -o json
```

使用以下指标组：

```text
OS Disk IOPS Consumed Percentage
OS Disk Bandwidth Consumed Percentage
OS Disk Latency
Data Disk IOPS Consumed Percentage
Data Disk Bandwidth Consumed Percentage
Data Disk Latency
VM Uncached IOPS Consumed Percentage
VM Uncached Bandwidth Consumed Percentage
VM Cached IOPS Consumed Percentage
VM Cached Bandwidth Consumed Percentage
```

在 VM 资源上查询 OS 盘与 VM 指标。对所有数据盘 LUN 序列使用 `--filter "LUN eq '*'"` 一次性查询。使用 `Maximum` 以及服务端 JMESPath 归约，避免向 guard 返回整段长时间序列。

按时间窗口选择 interval：

| 窗口                         | Interval |
| ---------------------------- | -------- |
| 不超过 2 小时                | `PT1M`   |
| 超过 2 小时且不超过 12 小时  | `PT5M`   |
| 超过 12 小时且不超过 48 小时 | `PT15M`  |
| 超过 48 小时                 | `PT1H`   |

任一有效值满足以下任一条件时，判定为异常：

- Disk 或 VM uncached IOPS consumed percentage 大于等于 95%。
- Disk 或 VM uncached bandwidth consumed percentage 大于等于 95%。
- 任一磁盘 latency 峰值大于 200 ms。

若某磁盘序列缺失，将该磁盘或维度标记为无法判断。若 Azure 返回了自定义 IOPS 与吞吐上限，需如实报告。不要基于过时的内嵌 SKU 表推导上限；应改为链接到最新 Azure managed disk 文档。

当用户询问磁盘 burst，或单盘 consumed percentage/latency 异常时，按需查询以下指标：

```text
OS Disk Used Burst IO Credits Percentage
OS Disk Used Burst BPS Credits Percentage
Data Disk Used Burst IO Credits Percentage
Data Disk Used Burst BPS Credits Percentage
```

这些指标表示已经使用的 credits 百分比，因此接近 100% 表示 credits 接近耗尽，不是“剩余接近 100%”。数据盘仍按所有实际 LUN 一次查询。只有存在有效样本时，才按 `vm-performance-patterns.md` 组合判断；指标缺失不得推断为“不支持 burst”或“credits 充足”。

当单盘与 VM 级指标同时异常时，分别报告。`VM Cached/Uncached IOPS/Bandwidth Consumed Percentage` 达到 95% 表示 VM 级存储上限可能是瓶颈，此时不得把“仅升级或扩展单盘”作为确定有效的处置。

## 网络

从返回的 NIC ID 中选择主 NIC。优先 `primary=true`；否则使用返回列表中的第一个 NIC。禁止根据 VM 名称拼接 NIC 名称。

读取 NIC 加速属性：

```text
network nic show --ids <primary-nic-id> --query {accelerated:enableAcceleratedNetworking,auxMode:auxiliaryMode,auxSku:auxiliarySku} -o json
```

查询 VM 网络总量：

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric Network In Total Network Out Total --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Maximum --query value[].{metric:name.value,peak:max_by(timeseries[0].data[?maximum!=null],&maximum)} -o json
```

对于连接流量峰值：

- 未启用 Accelerated Connections 时，查询 VM 指标 `Inbound Flows` 和 `Outbound Flows`。
- 当 `auxiliaryMode=AcceleratedConnections` 且 `auxiliarySku` 属于 `A1`、`A2`、`A4`、`A8` 时，使用完整 NIC 资源 ID 查询 `CurrentTotalFlowsIn` 和 `CurrentTotalFlowsOut`。传入完整资源 ID 时不得再附带 `--resource-type`。

没有流量样本应判定为无法判断，而非健康。将有效峰值与当前文档中 VM 或 Accelerated Connections SKU 的限制进行比对。若限制无法核验，只报告峰值与加速配置，不要臆造容量结论。带宽仅作辅助证据，不能独立证明饱和，因为 Azure Monitor 在该查询中不会暴露所有 SKU 的硬网络上限。

## Resource Health 关联

对 CPU、内存、磁盘、网络事件，按 `references/health-and-lookup.md` 查询一次 Resource Health。仅诊断窗口内事件参与关联。平台事件可支持“与平台相关”的表述，但不能单独证明指标异常的根因。

## 完整诊断

针对 `vm full`：

1. 执行一次共享清单、计算机名查询与 SKU 查询。
2. 每个指标组执行一次归约查询。
3. 仅在清单返回真实 NIC ID 后执行 NIC 配置查询。
4. 每块实际磁盘各执行一次属性查询。
5. 执行一次 Resource Health 查询。
6. 分别给出 CPU、内存、磁盘、网络和 Resource Health 的判定。
7. 仅在用户询问或基础指标命中相应线索时，读取 `vm-performance-patterns.md` 并执行按需 credits 查询。

在汇总中保留 `N/A` 维度，避免把部分结果误呈现为“完全健康”。
