# VM diagnostics

Use this reference for `vm full`, `vm cpu`, `vm memory`, `vm disk`, and `vm network`.

## Inputs

- Require the exact VM resource name and resource group. Reuse values already stated in the current conversation; otherwise ask for them.
- Use the current Azure CLI subscription unless the user explicitly identifies another accessible subscription.
- Default the time window to the previous 30 minutes. Accept an explicit absolute range or lookback.
- Convert API timestamps to the user's timezone. For Chinese conversations without another timezone, use Asia/Shanghai.

Never invent a default resource group. Preserve the full VM name, including trailing numeric components.

## Guard invocation

Static commands with no user-controlled values may use direct arguments:

```bash
python3 skills/azure-ops/scripts/az_guard.py -- account show --query '{subscription:id,tenant:tenantId,user:user.name}' -o json
```

For commands containing resource names, IDs, filters, times, or other user-controlled values, write a JSON request with the file primitive and pass only its path through the shell:

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

Reuse the request file only after replacing the entire JSON document. Never build it with shell interpolation.

## Shared inventory

Run this once for every VM diagnostic:

```text
vm show -d -g <rg> -n <vm> --query {id:id,name:name,location:location,vmSize:hardwareProfile.vmSize,osType:storageProfile.osDisk.osType,powerState:powerState,osDisk:storageProfile.osDisk.{name:name,sku:managedDisk.storageAccountType},dataDisks:storageProfile.dataDisks[].{lun:lun,name:name,sku:managedDisk.storageAccountType},nics:networkProfile.networkInterfaces[].{id:id,primary:primary}} -o json
```

Then get the guest-reported computer name:

```text
vm get-instance-view -g <rg> -n <vm> --query instanceView.computerName -o json
```

If the VM is absent, stop and report that the exact name and resource group did not resolve. If the power state is not running, continue only where control-plane data remains meaningful and warn that metrics may be absent.

Extract the subscription ID from the returned resource ID. Query the VM SKU once:

```text
rest --method get --url https://management.azure.com/subscriptions/<subscription>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location%20eq%20'<location>' --query value[?name=='<vmSize>']|[0].{vCPUs:capabilities[?name=='vCPUs'].value|[0],MemoryGB:capabilities[?name=='MemoryGB'].value|[0]} -o json
```

If a SKU capability is unavailable, show it as `N/A`; never infer it from the SKU name.

## CPU

Query `Percentage CPU` with one-minute granularity for windows up to two hours. For longer windows use a supported coarser interval. Request `Average` and `Maximum` and reduce the response with JMESPath:

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric Percentage CPU --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Average Maximum --query {average:avg(value[0].timeseries[0].data[?average!=null].average),peak:max(value[0].timeseries[0].data[?maximum!=null].maximum),high:length(value[0].timeseries[0].data[?maximum>=`90`]),samples:length(value[0].timeseries[0].data[?maximum!=null])} -o json
```

Verdict:

- Abnormal when CPU is at least 90% for 20% or more of valid samples.
- Healthy when valid samples exist and the high-sample share is below 20%.
- Unable to determine when no valid samples exist.

An isolated peak is evidence to inspect, not by itself a sustained CPU incident.

## Memory

Azure exposes guest memory only when the required monitoring agent and collection are configured. Query `Available Memory Percentage`:

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric Available Memory Percentage --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Average Minimum --query {averageAvailable:avg(value[0].timeseries[0].data[?average!=null].average),minimumAvailable:min(value[0].timeseries[0].data[?minimum!=null].minimum),high:length(value[0].timeseries[0].data[?minimum<=`10`]),samples:length(value[0].timeseries[0].data[?minimum!=null])} -o json
```

Calculate:

- Average used percentage = $100 - averageAvailable$.
- Peak used percentage = $100 - minimumAvailable$.
- High share = samples with used percentage at least 90% divided by valid samples.

Verdict:

- Abnormal when the high share is at least 20%.
- Healthy when valid samples exist and the high share is below 20%.
- Unable to determine when the metric is absent. Recommend checking Azure Monitor Agent and data-collection configuration; do not claim the VM has free memory.

## Disk

Use the shared inventory's OS disk and data disk names. Query each disk's current properties:

```text
disk show -g <rg> -n <disk> --query {sku:sku.name,sizeGB:diskSizeGB,tier:tier,iops:diskIOPSReadWrite,mbps:diskMBpsReadWrite} -o json
```

Use the following metric groups:

```text
OS Disk IOPS Consumed Percentage
OS Disk Bandwidth Consumed Percentage
OS Disk Latency
Data Disk IOPS Consumed Percentage
Data Disk Bandwidth Consumed Percentage
Data Disk Latency
VM Uncached IOPS Consumed Percentage
VM Uncached Bandwidth Consumed Percentage
```

Query OS disk and VM metrics on the VM resource. Query all data-disk LUN series in one request with `--filter "LUN eq '*'"`. Use `Maximum` and server-side JMESPath reduction so the guard does not receive an entire long time series.

Choose the interval by window:

| Window                          | Interval |
| ------------------------------- | -------- |
| Up to 2 hours                   | `PT1M`   |
| More than 2 and up to 12 hours  | `PT5M`   |
| More than 12 and up to 48 hours | `PT15M`  |
| More than 48 hours              | `PT1H`   |

Verdict is abnormal if any valid value meets one of these conditions:

- Disk or VM uncached IOPS consumed percentage is at least 95%.
- Disk or VM uncached bandwidth consumed percentage is at least 95%.
- Any disk latency peak is greater than 200 ms.

If a disk series is missing, mark that disk or dimension unable to determine. Report Azure-returned custom IOPS and throughput limits when present. Do not derive a limit from a stale embedded SKU table; link to the current Azure managed disk documentation instead.

## Network

Select the primary NIC from the returned NIC IDs. Prefer `primary=true`; otherwise use the first returned NIC. Never construct a NIC name from the VM name.

Read NIC acceleration properties:

```text
network nic show --ids <primary-nic-id> --query {accelerated:enableAcceleratedNetworking,auxMode:auxiliaryMode,auxSku:auxiliarySku} -o json
```

Query VM network totals:

```text
monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric Network In Total Network Out Total --start-time <utc-start> --end-time <utc-end> --interval PT1M --aggregation Maximum --query value[].{metric:name.value,peak:max_by(timeseries[0].data[?maximum!=null],&maximum)} -o json
```

For connection-flow peaks:

- Without Accelerated Connections, query VM metrics `Inbound Flows` and `Outbound Flows`.
- With `auxiliaryMode=AcceleratedConnections` and `auxiliarySku` in `A1`, `A2`, `A4`, or `A8`, query the full NIC resource ID for `CurrentTotalFlowsIn` and `CurrentTotalFlowsOut`. Do not also pass `--resource-type` with a full resource ID.

No flow samples means unable to determine, not healthy. Compare a valid peak with the current documented limit for the VM or Accelerated Connections SKU. If the limit cannot be verified, report the peak and acceleration configuration without inventing a capacity verdict. Bandwidth is supporting evidence and does not independently prove saturation because Azure Monitor does not expose every SKU's hard network ceiling in this query.

## Resource Health correlation

For CPU, memory, disk, and network incidents, query Resource Health once using `references/health-and-lookup.md`. Only events inside the diagnostic window participate in correlation. A platform event can support a platform-correlation statement; it does not by itself prove the metric anomaly's root cause.

## Full diagnosis

For `vm full`:

1. Run shared inventory, computer-name lookup, and SKU lookup once.
2. Run one reduced query per metric group.
3. Run NIC configuration only after the inventory returns the real NIC ID.
4. Run disk property requests once per actual disk.
5. Run Resource Health once.
6. Produce one verdict for CPU, memory, disk, network, and Resource Health.

Keep `N/A` dimensions visible in the summary so a partial result is never presented as a clean bill of health.
