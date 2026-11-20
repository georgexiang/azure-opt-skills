# 主动巡检（watch）

`scripts/watch.py` 由 cron 每 5 分钟执行一次：没有新情况时退出码为 0，不唤醒模型；有发现或采集失败时退出码为 1，输出 findings JSON 并唤醒 Agent。`az` 只通过同目录的 `azure_cli.sh --tools-home <tools-home>` 调用，用来取得令牌，同时保持持久工具目录与临时凭据 HOME 分离；令牌只在进程内使用，不输出。

默认巡检登录身份可读的**全部租户的全部已启用订阅**：
- 租户来自 ARM `/tenants` 与 `az account list --all` 的并集；对每个租户用 `az account get-access-token --tenant` 分别取 ARM 和 Azure Monitor 令牌，再列出该租户的订阅。
- 每个租户分别调用 Resource Graph（每批 100 个订阅）。
- 指标按"租户/订阅/区域"分组，使用 Azure Monitor 的 `metrics:getBatch`，每批最多 50 台 VM，默认并发 16 个（`--workers`）。约 5,400 台 VM、2 个订阅时，一次完整巡检约 420 次调用、174 秒。只有疑似异常的序列，才额外读取一次 7 天基线。

`--scope` 是可选参数，只在临时缩小范围时使用。

脚本不保存状态。是否为"新发生"，根据 Azure 记录的状态变化（`previousAvailabilityState` 与当前状态不同）和指标历史计算，因此每个事件只唤醒一次；持续未恢复的 VM 汇总进每日一次的 `daily-summary`。注意 Resource Graph 的 `occurredTime` 会随记录刷新而更新，不是状态的开始时间；真实开始时间需要用 Resource Health 历史查询。

## 规则

所有时间均为 UTC。`interval` 即 `--interval-minutes`（默认 5），必须与 cron 周期一致。

| 规则 | 数据来源 | 触发条件 | 严重度 |
| --- | --- | --- | --- |
| `health-onset` | Resource Graph `HealthResources` 中的 availability status | VM 变为 Degraded 或 Unavailable：上一个状态（`previousAvailabilityState`）与当前状态不同，且变化发生在 `interval + --health-lag-minutes`（默认 5 + 10 分钟）之内。Degraded → Unavailable 也算。记录只是刷新、上一个状态相同的不算 | Unavailable 为 critical，Degraded 为 warning |
| `platform-event` | Resource Graph 中的 resource annotations | 非 Customer Initiated 的事件，开始于 `interval + health lag` 之内，如 Live Migration Failed、加速网络异常 | warning |
| `disk-latency` | `OS Disk Latency`、按 LUN 拆分的 `Data Disk Latency`，1 分钟平均值 | 最近 `--window-minutes`（15）分钟中至少 `--sustain-minutes`（10）分钟超过 `--latency-ms`（20 ms），提前一个 interval 的同长窗口未达到该条件，且窗口中位数大于过去 `--baseline-days`（7）天小时均值中位数的 `--latency-ratio`（3）倍。基线缺失时不抑制 | warning |
| `disk-saturation` | OS 盘和数据盘的 IOPS、带宽 Consumed Percentage，按 LUN 拆分 | 与上一条相同的持续和"新发生"判断，阈值为 `--saturation-pct`（90%），不使用基线 | warning |
| `collector-error` | 采集器自身 | 主身份不可用（`AZ_NOT_FOUND`、`AUTH_REQUIRED`），或没有任何可读的租户或订阅（`NO_COVERAGE`） | critical |
| `daily-summary` | 采集器自身 | 每天在 `--summary-hour-utc`（默认 01，即北京时间 09 点）所在的那个 interval 发一次：列出所有非 Available 的运行中 VM（最多 300 台，附总数），以及看不到的租户或订阅（`blind`，例如需要 MFA 重新登录、Resource Graph 报错、`--scope` 中的订阅不可见）。两者都为空时不发 | warning |

指标窗口截止于当前时间之前 `--metric-lag-minutes`（3）分钟，用来避开数据写入延迟。

单个租户或订阅看不到时，只计入摘要行的 `blind` 数量和 `daily-summary`，不会每 5 分钟唤醒一次。

刻意忽略的情况：Unknown 状态（不能作为故障证据）；已停止或已释放 VM 的健康状态；Customer Initiated 事件；非运行中 VM 的磁盘规则。VM 未发布的指标，以及样本数不足的序列，只记入 `gaps`，不单独唤醒 Agent。

每条 finding 带有 `episode` 键（VM、规则来源、状态变化或事件、发生时间），同一事件在多次运行中保持不变，用于去重。阈值通过 cron 命令中的参数调整，不要修改脚本。Standard HDD 盘的正常延迟可能超过 20 ms，必要时调高 `--latency-ms`；7 天基线已能抑制大部分此类情况。

## 启用巡检（每个 Project 一次）

带 credentials 的执行运行在本轮专用的临时计算机上，结束后文件就会丢弃，cron 每次触发也是新的临时计算机。所以**不要把脚本复制到任何计算机目录**。cron 命令每次都从本 pack 仓库按固定 commit 下载这三个脚本再运行。

1. 在**不带 credentials** 的执行中记下持久 HOME 为 `<tools-home>`（绝对路径），并确认 `bash <skill-dir>/scripts/azure_cli.sh --tools-home <tools-home> --check` 通过；失败则用 `--ensure` 安装。
2. 确定固定版本：用 `git ls-remote https://github.com/georgexiang/azure-opt-skills refs/heads/main` 取得 `<commit>`（40 位），下载该 commit 的 `azure-ops/scripts/watch.py`，与 `<skill-dir>/scripts/watch.py` 比较 sha256。不一致说明 skill pack 还没同步到这个 commit：停止，请管理员在 Skill packs 中 Sync，不要用不一致的版本启用。
3. 生成巡检命令 `<watch-command>`，原样使用，只替换 `<commit>` 和 `<tools-home>`：

   ```sh
   d=$(mktemp -d) && for f in watch.py azure_cli.sh azure_cli_env.sh; do curl -fsSL --retry 2 -o "$d/$f" "https://raw.githubusercontent.com/georgexiang/azure-opt-skills/<commit>/azure-ops/scripts/$f" || { echo "WATCH_FETCH_FAILED: $f"; exit 1; }; done; python3 "$d/watch.py" --tools-home <tools-home>
   ```

4. 携带 Azure 凭据 handle（在 Project 中通常是共享的 `azure`）运行一次 `<watch-command>`，并在末尾追加 `--summary-hour-utc <当前 UTC 小时> --interval-minutes 60`。这两个参数只用于这次试运行，目的是让输出带上 `blind` 明细。向用户报告摘要行中的租户数、订阅数、`blind`、VM 数和耗时，并逐条说明看不到的租户及原因。
   - VM 数为 0 时，先解决可见性问题，不要启用。
   - 耗时超过 240 秒时，先在命令末尾加 `--workers 24` 再试一次；仍然超过的话，把 cron 周期改为 10 分钟，同时追加 `--interval-minutes 10`。
5. 用 `cron` 工具创建：
   - `title`："Azure IaaS 巡检"
   - `schedule`：`{"everyMs": 300000}`
   - `script`：`{"command": "<watch-command>", "credentials": ["azure"]}`。不要追加 `--scope`，除非用户明确要求缩小范围。
   - `task`："加载 azure-ops skill，按 references/watch.md 的'每次唤醒的处理流程'处理本次巡检结果。"
   - 投递目标：不传 destination，默认发回创建 cron 的会话。应在 Project 内专门用于巡检告警的会话中创建；当前会话不是专用会话时，先请用户新建一个专用会话。只有用户明确指定，并且该目标出现在 "Where scheduled tasks post" 列表中时，才设置 `destinationKey`。
6. cron 在后台使用凭据需要所有者单独授权；试运行报告缺少授权时，发起申请，不要绕过。
7. 告诉用户：巡检什么、多久一次、发到哪里、固定的 `<commit>`、如何暂停。

升级：管理员 Sync 新版本后，重新执行第 2、3 步，并用 `cron` 的 patch 更新 `script.command` 里的 `<commit>`。旧 cron 会继续使用原来固定的版本，不会自动升级。

唤醒原因为 `WATCH_FETCH_FAILED` 时，说明无法从 GitHub 下载脚本，巡检本次没有执行：报告一次，并检查网络出口。`AZ_NOT_FOUND` 表示工具链未就绪：在不带 credentials 的执行中运行错误信息给出的 `--ensure` 命令。

## 每次唤醒的处理流程

1. **去重与分组**：读取上一次 fire note，跳过已报告过的 `episode`；结束前在 fire note 中记下本次报告的 episode。把剩余 findings 按 订阅/区域/规则 分组，同因的放在同一组。
2. **先发事实**：有 critical（Unavailable 的 `health-onset` 或 `collector-error`）时，先发出 VM、状态、变化时间（UTC 和用户时区），再继续诊断并在同一线程补充。
3. **分派**：分组不超过 3 个时，自己按下面的第 4 步诊断。超过 3 个时，每组启动一个只读 subagent 并行诊断，交给它该组的 findings 和下面的诊断要求；subagent 只返回结论和证据，不发消息、不创建 cron、不修改任何东西，由你汇总后统一发布。`collector-error` 和 `daily-summary` 始终由你自己处理。
4. **只读诊断**（按 `vm-diagnostics.md`、`health-and-lookup.md`、`vm-performance-patterns.md` 执行）：
   - 先确认当前状态：重新读取 VM 当前的 Resource Health；如果已恢复为 Available，说明该事件已恢复。需要说明持续了多久时，用 Resource Health 历史（`availabilityStatuses` 列表）确定真实开始时间，不要用 Resource Graph 的 `occurredTime`。
   - 关联分析：同资源组或同区域本次有 finding 的其他 VM、该区域的 Service Health、最近 24 小时的 Activity Log 变更。多台 VM 同时降级，更可能是共同依赖的问题，而不是各自宿主机的问题。
   - 磁盘：与同角色 VM 的同一指标对比，查看同一 LUN 的队列深度和 IOPS/带宽占用，以及磁盘 SKU 上限；区分磁盘级上限、VM 级上限、负载变化和平台问题。
   - 平台事件：迁移失败或宿主机功能降级是宿主机风险信号，不能证明已经发生故障，要明确说明。
5. **建议**：给出符合证据、影响最小的动作，写明已验证和未验证的前提、预期效果、副作用、执行方、执行后如何验证、何时回退。不得把重启、重新部署、调整规格或区域切换作为无条件结论。不在 Azure 中执行任何变更。
6. **发布**：在投递会话中，每个事件发一条消息，同因的 finding 合并。结论在前，然后是受影响 VM（Azure 名称和计算机名）、带时间的证据、不确定性、建议。长证据放到线程回复。

`daily-summary`：发一条当日汇总，非 Available 的 VM 按订阅、区域和原因分组列出数量和代表 VM，不逐台诊断；只对用户追问或新出现的组再展开。`blind` 部分按租户汇总原因：需要 MFA 或重新登录的，请凭据所有者针对该租户执行 `az login --tenant <id>` 并保存登录。不要把这些租户当作健康。

`collector-error` 表示巡检失明。每种错误只报告一次，写明原因（登录过期、缺少授权、Azure 限流）以及谁能修复。共享登录过期时，由凭据所有者重新登录；不要向其他成员索要凭据。

## 临时扫描

用需要的 scope 运行脚本，`--interval-minutes` 设为要回看的时长（例如 1440 表示报告最近一天开始的健康事件和平台事件）。磁盘的"新发生"判断只看最近的窗口。

## 局限

- 看不到 Guest 内部：进程、文件系统、OS 日志和 Scheduled Events。
- Resource Graph 每台 VM 只保留最新一条 annotation，同一 interval 内被覆盖的事件可能漏掉。
- 某次 cron 没有运行时，可能漏掉那次磁盘异常或状态变化的起点；持续未恢复的 VM 仍会出现在下一次 `daily-summary` 里。
- finding 只是候选，相关性不等于根因。
