# VM 性能与平台事件模式

本参考用于解释已经由 `vm-diagnostics.md` 和 `health-and-lookup.md` 收集的证据。只有当用户询问 burst、限流、VF、主机重启或计划维护，或基础诊断出现对应异常时才读取。

不要在本文件重复执行清单或指标查询。复用当前分支已经获得的 VM Inventory、SKU、Azure Monitor、Resource Health、Service Health 和 Activity Log 结果。

## 证据等级

- **已确认**：所需指标或事件字段完整，并满足本文件的判定条件。
- **疑似**：存在相符现象，但 SKU 能力、限制或关键指标缺失。
- **无法判定**：没有有效样本，或证据只能从 Guest、应用层取得。

相关性不等于根因。性能指标异常与平台事件可能同时出现；应分别报告，不要用其中一个排除另一个。

## CPU burst 限流

仅当 Azure 返回 `CPU Credits Remaining` 或 `CPU Credits Consumed` 时使用 burst 判定。这两个指标仅适用于 B-series burstable VM；不要仅根据 SKU 名称字符串推断支持情况。

- `CPU Credits Remaining` 在窗口内下降并达到或接近 0，同时 CPU 或应用性能在对应时间恶化：判定为 CPU burst 限流。
- credits 持续下降但未耗尽：报告为容量风险，不判定已限流。
- credits 指标缺失：只能按普通 CPU 高负载规则判定，burst 限流为无法判定。
- 非 burst 场景下持续高 CPU：建议分析应用 hot path，并评估纵向或横向扩展。

进程、线程池和应用 QPS 属于 Guest 或应用证据。用户未提供时，不要声称已核验。

## 磁盘 IOPS、带宽与 burst

按层次解释磁盘证据：

1. 单盘 `OS/Data Disk IOPS/Bandwidth Consumed Percentage` 达到异常阈值，说明该盘接近自身限制。
2. `VM Cached/Uncached IOPS/Bandwidth Consumed Percentage` 达到异常阈值，说明 VM 级存储上限可能是瓶颈；仅扩盘可能无效。
3. `OS/Data Disk Used Burst IO/BPS Credits Percentage` 接近 100%，表示相应 burst credits 已大量消耗；若同时出现 consumed percentage 或 latency 异常，判定为 burst 耗尽相关限流。
4. burst credit 指标缺失时，不推断磁盘是否支持 bursting，也不根据盘类型硬编码结论。

短期建议可以包括业务限速或错峰。升级磁盘层级、调整 IOPS/吞吐、拆分数据盘或更换 VM SKU 都是人工变更建议，必须先核验当前 SKU、磁盘能力、成本、停机影响和组织审批；Skill 不执行这些操作。

VM 重启不能作为恢复 burst credits 的通用方案，不要建议通过重启清零 credits。

## 网络容量与 VF

`Network In/Out Total` 只能说明窗口流量，不能单独证明达到 SKU 带宽上限。只有在当前 Microsoft 文档或实时 SKU 能力能够核验限制时，才能比较并给出容量结论。

- Accelerated Networking 已启用且同窗 Resource Health 出现 NIC、host 或 platform 相关事件：报告“疑似平台侧 VF/网络路径事件”。
- 只有流量下降或延迟现象，没有平台事件或 Guest 证据：VF 故障无法判定。
- TCP retransmit、RTT、丢包和 Guest 路由属于 Guest 或应用层证据。只能引用用户提供的数据，并明确来源。

流量调度、就近 endpoint、升级 VM SKU、重启 VM 都是人工处置建议。Skill 不执行重启或资源变更；持续的平台侧证据可用于生成 Support 工单草稿。

## Host Reboot

当诊断窗口内 Resource Health 包含 `Unavailable` 后恢复 `Available`，且标题、原因、摘要或 annotation 明确提及 host、node、platform reboot 时，可判定发生过平台主机重启。当前结论取最新非 null 状态：已经恢复的历史事件不得报告为当前异常。

使用 Activity Log 检查同窗内是否存在用户或自动化触发的 restart、redeploy、deallocate 等 Compute 操作：

- 有成功写操作记录：报告该操作事实，不归因为平台主机重启。
- 无相关 Activity Log，但 Resource Health 有明确平台事件：平台侧证据更强。
- 两者都缺少：不要根据 Guest 意外断电现象单独断言 Azure 平台根因。

若 30 天内重复出现，汇总资源 ID、事件时间、Resource Health 标题或 tracking ID，并建议生成 Support 工单草稿。可用区、可用性集、VMSS 和自动恢复属于架构建议，不代表当前资源已经配置。

## Planned Maintenance

Resource Health 或 Service Health 中明确标记为 `PlannedMaintenance`、`HostMaintenance`、`RedeployScheduled` 或等价维护事件时，报告计划时间、影响类型、状态和建议准备事项。

- `ServiceIssue` 与 `PlannedMaintenance` 必须分开报告；计划维护不自动等于当前中断。
- Azure Instance Metadata Service 的 Scheduled Events 属于 Guest 端点，当前 Skill 不查询。除非用户提供结果，否则不要声称看到 `Freeze`、`Reboot`、`Redeploy` 或 `Preempt`。
- 建议核验负载均衡健康探测、维护窗口以及 Availability Zone/VMSS 的冗余安排。
- `perform-maintenance`、restart 和 redeploy 都是写操作。只能说明可由获批运维流程评估，不能执行或输出成待执行命令。

## 报告要求

每个命中的模式应包含：

1. 结论等级：已确认、疑似或无法判定。
2. 时间窗口和数据来源。
3. 支持结论的指标或事件事实。
4. 缺失的关键证据和可选 Guest 侧检查。
5. Skill 可继续执行的只读动作。
6. 需人工审批的缓解或架构建议。
