# 主动巡检（watch）

`scripts/watch.py` 由 cron 每 5 分钟执行一次：没有新情况时退出码为 0，不唤醒模型；有发现或采集失败时退出码为 1，输出 findings JSON 并唤醒 Agent。`az` 只通过同目录的 `azure_cli.sh --tools-home <tools-home>` 调用，用来取得令牌，同时保持持久工具目录与临时凭据 HOME 分离；令牌只在进程内使用，不输出。

默认巡检登录身份可读的**全部租户的全部已启用订阅**：
- 租户来自 ARM `/tenants` 与 `az account list --all` 的并集；对每个租户用 `az account get-access-token --tenant` 分别取 ARM 和 Azure Monitor 令牌，再列出该租户的订阅。
- 每个租户分别调用 Resource Graph（每批 100 个订阅）。
- 指标按"租户/订阅/区域"分组，使用 Azure Monitor 的 `metrics:getBatch`，每批最多 50 台 VM，默认并发 16 个（`--workers`）。约 5,400 台 VM、2 个订阅时，一次完整巡检约 420 次调用、174 秒。只有疑似异常的序列，才额外读取一次 7 天基线。

`--tenant <tenant-uuid>`（可重复）只巡检指定租户，用于"一个凭据一个 cron"的分工；`--scope` 只在临时缩小到订阅或资源组时使用。

脚本不保存状态。是否为"新发生"，根据 Azure 记录的状态变化（`previousAvailabilityState` 与当前状态不同）和指标历史计算，因此每个事件只唤醒一次；持续未恢复的 VM 汇总进每日一次的 `daily-summary`。注意 Resource Graph 的 `occurredTime` 会随记录刷新而更新，不是状态的开始时间；真实开始时间需要用 Resource Health 历史查询。

## 规则

所有时间均为 UTC。`interval` 即 `--interval-minutes`（默认 5），必须与 cron 的固定间隔一致。

| 规则 | 数据来源 | 触发条件 | 严重度 |
| --- | --- | --- | --- |
| `health-onset` | Resource Graph `HealthResources` 中的 availability status | VM 变为 Degraded 或 Unavailable：上一个状态（`previousAvailabilityState`）与当前状态不同，且变化发生在 `interval + --health-lag-minutes`（默认 5 + 10 分钟）之内。Degraded → Unavailable 也算。记录只是刷新、上一个状态相同的不算 | Unavailable 为 critical，Degraded 为 warning |
| `health-recovered` | 同上 | 运行中的 VM 从 Degraded / Unavailable 恢复为 Available，变化发生在 `interval + --health-lag-minutes` 之内。Resource Graph 记录约每天刷新一次，刷新后上一个状态被覆盖，因此只能在恢复后的下一轮可靠发现 | info |
| `platform-event` | Resource Graph 中的 resource annotations | 非 Customer Initiated 的事件，开始于 `interval + health lag` 之内，如 Live Migration Failed、加速网络异常 | warning |
| `disk-latency` | `OS Disk Latency`、按 LUN 拆分的 `Data Disk Latency`，1 分钟平均值 | 最近 `--window-minutes`（15）分钟中至少 `--sustain-minutes`（10）分钟超过 `--latency-ms`（20 ms），提前一个 interval 的同长窗口未达到该条件，且窗口中位数大于过去 `--baseline-days`（7）天小时均值中位数的 `--latency-ratio`（3）倍。基线缺失时不抑制。同时给出 `daysOverThreshold`（基线 7 天内有小时均值超过 `--latency-ms` 的天数）与 `wouldTrack`（≥ 3 天，loop 影子标记） | warning |
| `disk-saturation` | OS 盘和数据盘的 IOPS、带宽 Consumed Percentage，按 LUN 拆分 | 与上一条相同的持续和"新发生"判断，阈值为 `--saturation-pct`（90%），不使用基线；并且**同一块盘、同一 LUN 的延迟中位数也超过 `--latency-ms`**，该延迟记在 `latencyMedian`。只跑满配额、延迟正常的盘是批处理等负载的常态，不报，只计入摘要行的 `saturated without latency` 和 `daily-summary` 的 `saturatedWithoutLatency` | warning |
| `disk-burst` | 采集器自身 | 一轮里 `disk-latency` 与 `disk-saturation` 合计超过 `--disk-burst`（默认 20）时，合并为这一条：总数、按订阅/区域/规则的分组计数（前 10 组）、延迟最高的 5 块盘（`latencyMs`，饱和类附 `consumedPct`）。大批磁盘同时变慢通常是批处理负载或共同依赖，不逐块报告 | warning |
| `collector-error` | 采集器自身 | 主身份不可用（`AZ_NOT_FOUND`、`AUTH_REQUIRED`），或没有任何可读的租户或订阅（`NO_COVERAGE`） | critical |
| `daily-summary` | `--summary-only` | 只由每日汇总 cron 以 `--summary-only` 运行时产生，总是输出：`notAvailableCount`、`newIn24h`（由 Resource Health 历史得出的真实开始时间在 24 小时内的数量）、按订阅/区域/状态/原因分组的 `notAvailableGroups`（数量、`newIn24h`、最早开始时间 `oldestSince`、3 台代表 VM）、`loopShadow`（如启用 loop 会跟踪的数量，按持续时长分桶，只统计不写入）、可读租户（ID、名称、订阅数）；看不到的租户在输出的 `blind` 中。`--summary-only` 不查磁盘指标、不跑其他规则 | warning |

指标窗口截止于当前时间之前 `--metric-lag-minutes`（3）分钟，用来避开数据写入延迟。

单个租户或订阅看不到时，只计入摘要行的 `blind` 数量和 `daily-summary`，不会每 5 分钟唤醒一次。

刻意忽略的情况：Unknown 状态（不能作为故障证据）；已停止或已释放 VM 的健康状态；Customer Initiated 事件；非运行中 VM 的磁盘规则。VM 未发布的指标，以及样本数不足的序列，只记入 `gaps`，不单独唤醒 Agent。

每条 finding 带有简短的 `episode` 键，同一事件在多次运行中保持不变，用于去重。

输出是单行紧凑 JSON，最后一行是摘要，总长控制在约 7,000 字符内，不会被平台截断。发现过多时，按严重程度保留前面的，其余计入 `omitted`（数量和规则分布）。`gaps` 只给数量和前 3 条。

cron 脚本受平台 120 秒执行超时限制。脚本在 `--deadline-seconds`（默认 100）后不再查询剩余的磁盘批次，先输出已有结果，摘要行给出 `disk batch(es) skipped at deadline`。这是本轮磁盘覆盖不全，不是故障，不要为此重跑脚本。

阈值通过 cron 命令中的参数调整，不要修改脚本。VM 数以千计时，轻微的延迟上升每轮都会出现，可以追加 `--latency-ms 50`，只对明显的延迟和"跑满且变慢"唤醒；如果仍然几乎每轮唤醒，用 `--latency-ms 200 --latency-ratio 10`，只留下延迟严重、且远高于自身基线的盘。

## 启用巡检（每个 Project 一次）

带 credentials 的执行运行在本轮专用的临时计算机上，结束后文件就会丢弃，cron 每次触发也是新的临时计算机。所以**不要把脚本复制到任何计算机目录**。cron 命令每次都从本 pack 仓库按固定 commit 下载这三个脚本再运行。

**多个 Azure 凭据**：每个 Azure 文件凭据都是一个 `az login` 缓存，都写在 `~/.azure`，平台不允许一条命令同时携带多个（"overlapping file paths"）。Project 里有多个 Azure 凭据、每个对应不同租户时，**每个凭据建一个 cron**：`script.credentials` 只放这一个凭据，命令追加 `--tenant <该凭据负责的租户>`，`title` 写成 "Azure IaaS 巡检 · <租户名>"，都发回同一个巡检告警会话。不要为巡检启动 swarm 或 subagent：各个 cron 本来就会并行运行，没问题时不调用模型。用户要求巡检"所有租户"时，先列出 Project 可用的 Azure 凭据及各自的租户（不同凭据可能是不同账号，用 `account show` 核对），请用户确认分工，再逐个按下面的步骤启用。

1. 在**不带 credentials** 的执行中记下持久 HOME 为 `<tools-home>`（绝对路径），并确认 `bash <skill-dir>/scripts/azure_cli.sh --tools-home <tools-home> --check` 通过；失败则用 `--ensure` 安装。
2. 确定固定版本：用 `git ls-remote https://github.com/georgexiang/azure-opt-skills refs/heads/main` 取得 `<commit>`（40 位），下载该 commit 的 `azure-ops/scripts/watch.py`，与 `<skill-dir>/scripts/watch.py` 比较 sha256。不一致说明 skill pack 还没同步到这个 commit：停止，请管理员在 Skill packs 中 Sync，不要用不一致的版本启用。
3. 生成巡检命令 `<watch-command>`，原样使用，只替换 `<commit>` 和 `<tools-home>`：

   ```sh
   d=$(mktemp -d) && for f in watch.py azure_cli.sh azure_cli_env.sh; do curl -fsSL --retry 2 -o "$d/$f" "https://raw.githubusercontent.com/georgexiang/azure-opt-skills/<commit>/azure-ops/scripts/$f" || { echo "WATCH_FETCH_FAILED: $f"; exit 1; }; done; python3 "$d/watch.py" --tools-home <tools-home>
   ```

4. 携带 Azure 凭据 handle（在 Project 中通常是共享的 `azure`）运行一次 `<watch-command>`，再追加 `--summary-only` 运行一次（后者输出 `daily-summary`）。向用户报告可读租户（`readableTenants`）、看不到的租户及原因（`blind`）、VM 数和耗时。
   - VM 数为 0 时，先解决可见性问题，不要启用。
   - 摘要行出现 `skipped at deadline` 时，先在命令末尾加 `--workers 24` 再试一次；仍然出现的话，把 cron 间隔改为 `{"everyMs": 600000}`（10 分钟），同时追加 `--interval-minutes 10`。
5. 用 `cron` 工具创建：
   - `title`："Azure IaaS 巡检"；多个凭据时为 "Azure IaaS 巡检 · <租户名>"
   - `schedule`：`{"everyMs": 300000}`。**巡检 cron 必须用固定间隔 `{"everyMs": <毫秒>}`，不要用 cron 表达式**（如 `*/10 * * * *`），并且 `--interval-minutes` 必须等于间隔的分钟数。原因：QM 对 cron 表达式这类日历计划，触发后从**计划时间**推算下一次；cron 停用、中断后恢复时，会把错过的每个时间点逐个补跑（每几秒一轮，持续到追上当前时间），补跑的每一轮都扫描"现在"的数据，只增加 Azure 调用和 Agent 唤醒。`everyMs` 从**实际触发时间**推算下一次，恢复后只运行一次，然后按间隔继续。
   - `script`：`{"command": "<watch-command>", "credentials": ["<该凭据 handle>"]}`。只有一个凭据时不加 `--tenant`；多个凭据时加上它负责的 `--tenant`。不要追加 `--scope`，除非用户明确要求缩小范围。
   - `task`："加载 azure-ops skill，按 references/watch.md 的'每次唤醒的处理流程'处理本次巡检结果。"。任务说明**始终只保留这一句**：规则和处理方式都以 skill 为准，不要往任务说明里追加规则、凭据要求或暂停条件。运行中的发现只写进 fire note。
   - 投递目标：不传 destination，默认发回创建 cron 的会话。应在 Project 内专门用于巡检告警的会话中创建；当前会话不是专用会话时，先请用户新建一个专用会话。只有用户明确指定，并且该目标出现在 "Where scheduled tasks post" 列表中时，才设置 `destinationKey`。
6. cron 在后台使用凭据需要所有者单独授权；试运行报告缺少授权时，发起申请，不要绕过。
7. 告诉用户：巡检什么、多久一次、发到哪里、固定的 `<commit>`、如何暂停。

升级：管理员 Sync 新版本后，重新执行第 2、3 步，并用 `cron` 的 patch 更新 `script.command` 里的 `<commit>`，同时把 `task` 恢复为第 5 步的那一句；发现 `schedule` 是 cron 表达式时，一并改为等效的 `{"everyMs": …}` 并核对 `--interval-minutes`。修改前若先停用 cron，修改完成后必须重新启用并确认，避免巡检停在停用状态。旧 cron 会继续使用原来固定的版本，不会自动升级。

唤醒原因为 `WATCH_FETCH_FAILED` 时，说明无法从 GitHub 下载脚本，巡检本次没有执行：报告一次，并检查网络出口。`AZ_NOT_FOUND` 表示工具链未就绪：在不带 credentials 的执行中运行错误信息给出的 `--ensure` 命令。

## 每次唤醒的处理流程

1. **去重与分组**：读取上一次 fire note，跳过其中已报告的 `episode`；结束前在 fire note 中只记本轮报告的 episode。不要建 checkpoint 文件，也不要上传 Files。输出带 `omitted` 时，按其中的数量和规则分布汇报，不要为补全而重跑脚本。把要报告的 findings 按订阅、区域、规则分组，同因的放在同一组。
2. **先发事实**：有 critical（Unavailable 的 `health-onset` 或 `collector-error`）时，先发出 VM、状态、变化时间（UTC 和用户时区），再继续诊断并在同一线程补充。
3. **顺序诊断，不要并发**：按组逐个诊断，不要为诊断启动 subagent，也不要并行执行带凭据的命令。同一个文件凭据被并发使用时，平台在命令结束写回刷新后的登录时会冲突，把整条命令判为失败（"Credential changed during execution"）。遇到这个错误时，重试一次该命令，不要换用其他凭据。 只对 `health-onset`、`platform-event` 做第 4 步的深度诊断；磁盘类（`disk-latency`、`disk-saturation`、`disk-burst`）不逐台诊断，直接按订阅、区域汇总数量，**按延迟从高到低**列出最严重的至多 5 块盘，表格列为：VM（计算机名）、LUN（数据盘插槽号，OS 盘标为 OS）、延迟中位数（ms）与 7 天基线、IOPS/带宽使用率（饱和类才有）。延迟取 `disk-latency` 的 `median`、`disk-saturation` 的 `latencyMedian`、`disk-burst` 的 `worst[].latencyMs`；不要按使用率或超阈值样本数排序，跑满的盘使用率都是 100%，排不出先后。一条消息发完，用户追问某块盘时再展开。
4. **只读诊断**（按 `vm-diagnostics.md`、`health-and-lookup.md`、`vm-performance-patterns.md` 执行）：
   - 先确认当前状态：重新读取 VM 当前的 Resource Health；如果已恢复为 Available，说明该事件已恢复。需要说明持续了多久时，用 Resource Health 历史（`availabilityStatuses` 列表）确定真实开始时间，不要用 Resource Graph 的 `occurredTime`。
   - 关联分析：同资源组或同区域本次有 finding 的其他 VM、该区域的 Service Health、最近 24 小时的 Activity Log 变更。多台 VM 同时降级，更可能是共同依赖的问题，而不是各自宿主机的问题。
   - 磁盘：与同角色 VM 的同一指标对比，查看同一 LUN 的队列深度和 IOPS/带宽占用，以及磁盘 SKU 上限；区分磁盘级上限、VM 级上限、负载变化和平台问题。
   - 平台事件：迁移失败或宿主机功能降级是宿主机风险信号，不能证明已经发生故障，要明确说明。
5. **建议**：给出符合证据、影响最小的动作，写明已验证和未验证的前提、预期效果、副作用、执行方、执行后如何验证、何时回退。不得把重启、重新部署、调整规格或区域切换作为无条件结论。不在 Azure 中执行任何变更。
6. **发布**：在投递会话中，每个事件发一条消息，同因的 finding 合并。**消息标题必须包含租户名**（取 cron 标题中 "·" 之后的部分，例如 "Azure IaaS 巡检 · Contoso-EU — 19:16"）。结论在前，然后是受影响 VM（Azure 名称和计算机名）、带时间的证据、不确定性、建议。长证据放到线程回复。

`health-recovered`：发一条简短的恢复消息（VM、恢复前状态、恢复时间），不做诊断。

磁盘发现带 `wouldTrack: true` 时，在消息中注明"该盘近 7 天有 N 天超过阈值（达到跟踪条件，影子统计）"。非 Available 的 VM 按订阅、区域和原因分组列出数量和代表 VM，不逐台诊断；只对用户追问或新出现的组再展开。`blind` 部分按租户汇总原因：需要 MFA 或重新登录的，请凭据所有者针对该租户执行 `az login --tenant <id>` 并保存登录。不要把这些租户当作健康。

`collector-error` 表示巡检失明。每种错误只报告一次，写明原因（登录过期、缺少授权、Azure 限流）以及谁能修复。共享登录过期时，由凭据所有者重新登录；不要向其他成员索要凭据。

## 每日汇总 cron（与 Project Digest 同步）

Project Digest 每天北京 08:00 生成，收集前 24 小时的项目会话；`DIGEST_LATEST_ONLY_SESSIONS` 中列出的会话只取窗口内**最后一条** Agent 回复原文作为独立一节。因此每日汇总由一个**单独的 cron** 在北京 07:30 生成，发到**只有它发言**的会话，并且**只发一条消息**。

启用（每个 Project 一次）：

1. 请用户在 Project 内新建会话 "Azure 巡检日报"，在该会话中执行以下步骤。
2. 按"启用巡检"第 1–3 步确定 `<tools-home>`、`<commit>`，为每个 Azure 凭据生成命令 `<watch-command> --tenant <该凭据的租户> --summary-only`。
3. 用 `cron` 工具创建：
   - `title`："Azure IaaS 每日汇总"
   - `schedule`：`{"cron": "30 7 * * *", "timezone": "Asia/Shanghai"}`。每日汇总要对齐北京时间 07:30，所以这里保留 cron 表达式（这是唯一的例外）。代价：停用期间每错过一个 07:30，重新启用后就会立即补跑一次，停用 N 天会连续补跑 N 次。每次触发的第 1 步按北京日期去重，所以补跑不会产生重复汇总；停用超过一天时，也可以先把 `schedule` 重新设置一次再启用。
   - **不设 `script`**（一次执行只能带一个文件凭据，多个租户必须分开运行）
   - `task`："加载 azure-ops skill，按 references/watch.md 的'每日汇总 cron'生成并发布今天的汇总。"
   - 不传 destination，发回本会话。
4. 告诉用户这个会话的 ID，由管理员在 Project Digest 的 App、preview、dispatch 进程设置 `DIGEST_LATEST_ONLY_SESSIONS=<本会话 ID>=Azure IaaS Daily Summary,<巡检告警会话 ID>=Latest Azure Alert`。

每次触发：

1. 读取本 cron 的 fire note，取得各租户的命令（凭据 handle、`--tenant`、`<commit>`）以及最近一次发布汇总的北京日期；第一次运行时按上面第 2 步生成并写入 fire note。如果当前北京日期的汇总已经发布过（例如停用恢复后的补跑），直接 `finish_silently`，不要再发。
2. **依次**执行每个租户的命令，每次只带该租户的一个凭据，不启动 subagent，不并发。
3. 用下面的模板合成**一条**消息发布，不发线程回复，不发其他消息；发布后在 fire note 记下本次的北京日期。

模板（Markdown，数值取自各租户的 `daily-summary` 与摘要行）：

```text
# Azure IaaS 每日巡检汇总 — <YYYY-MM-DD> 07:30（北京时间）

## <租户名>
| 项目 | 值 |
| --- | --- |
| 读取状态 | 正常 / 无法读取：<错误码与原因> |
| 订阅 / VM | <订阅数> 个订阅，<VM 数> 台 VM（运行中 <N>） |
| 非 Available | <notAvailableCount> 台（24 小时内新增 <newIn24h>） |
| 影子统计 | 如启用 loop 将跟踪 <wouldTrack> 项：<bucket 计数> |

| 订阅 | 区域 | 状态 | 原因 | 数量 | 24h 新增 | 最早开始 | 示例 VM |
| --- | --- | --- | --- | --- | --- | --- | --- |
| … |
```

- 每个租户一节；没有非 Available VM 时写"无"。
- 某租户命令失败（`collector-error`、超时、`WATCH_FETCH_FAILED`）时，该节"读取状态"写"无法读取"及原因，其余行写"—"，**不要省略该租户**，不要当作健康。
- 时间用北京时间；持续时长只用 `oldestSince` 与 `newIn24h`。

## 临时扫描

用需要的 scope 运行脚本，`--interval-minutes` 设为要回看的时长（例如 1440 表示报告最近一天开始的健康事件和平台事件）。磁盘的"新发生"判断只看最近的窗口。

## 局限

- 看不到 Guest 内部：进程、文件系统、OS 日志和 Scheduled Events。
- Resource Graph 每台 VM 只保留最新一条 annotation，同一 interval 内被覆盖的事件可能漏掉。
- 某次 cron 没有运行时，可能漏掉那次磁盘异常或状态变化的起点；持续未恢复的 VM 仍会出现在下一次 `daily-summary` 里。
- finding 只是候选，相关性不等于根因。
