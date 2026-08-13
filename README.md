# Azure Ops Skill for QM

## 项目说明

本目录用于把
[`georgexiang/azure-ops-agent`](https://github.com/georgexiang/azure-ops-agent/tree/main)
中的 Azure 运维能力提炼为一个可在 QM Web UI 中发现和调用的统一 Skill。

本次分析基于源仓库 `main` 分支提交：

```text
b2774d3db5a536ca2dd33b13714ff9f5fe399baa
```

目标资源名称暂定为：

```text
/azure-ops
```

最终由一个主 `SKILL.md` 负责意图识别、流程编排和安全约束，详细命令、阈值表、输出模板和执行脚本作为同一个 Skill 的内部资产保存。

## 工作目标

1. 将源仓库中的多个 Azure Skills 合并为一个统一的 QM Skill。
2. 保留 Azure VM 诊断、平台健康查询、资源互查、知识问答和支持工单能力。
3. 消除不同 Skills 之间重复的 VM 查询、SKU 查询、时间转换和 Resource Health 查询步骤。
4. 移除飞书、Microsoft Agent Framework 和源项目自定义 `run_az` 接口等运行时耦合。
5. 适配 QM 的 Scope、Sandbox、凭据、Skill 物化和 Web UI 展示机制。
6. 仅采用只读 Azure 操作；Support 工单只生成草稿，不在普通 Sandbox 中提交。
7. 让 Skill 在 QM Web UI 的 Skills 页面和 `/` 命令选择器中可见。

## 非目标

- 不迁移源项目的飞书机器人、会话服务或独立 Python Agent 服务。
- 不在 Skill 中保存 Azure Token、Client Secret、证书私钥或其他敏感信息。
- 不允许 Agent 进入 VM 执行命令、读取进程、文件系统或应用日志。
- 不把未经验证的 Azure CLI 命令直接开放给模型。
- 不默认授予创建、修改、重启或删除 Azure 资源的权限。

## 源能力清单

源仓库当前包含以下十个 Skill：

| 原 Skill                   | 主要用途                       | 统一 Skill 分支      |
| -------------------------- | ------------------------------ | -------------------- |
| `vm-full-diagnosis`        | VM 综合体检                    | `vm full`            |
| `vm-cpu-check`             | CPU 指标诊断                   | `vm cpu`             |
| `vm-memory-check`          | 内存指标诊断                   | `vm memory`          |
| `vm-disk-check`            | 磁盘 IOPS、吞吐和延迟诊断      | `vm disk`            |
| `vm-network-check`         | 网络流量、连接数和加速网络诊断 | `vm network`         |
| `vm-resource-health-check` | 单台 VM Resource Health        | `vm resource-health` |
| `service-health-check`     | 订阅级 Service Health          | `service-health`     |
| `vm-hostname-lookup`       | 主机名和 VM 资源名互查         | `vm lookup`          |
| `azure-qa`                 | Azure 通识和能力边界说明       | `qa`                 |
| `azure-support-case`       | Azure Support 工单草稿         | `support-case`       |

## 统一 Skill 设计

### 路由层

主 `SKILL.md` 根据用户意图选择一个执行分支：

- 整体诊断：CPU、内存、磁盘、网络和 Resource Health。
- 单项诊断：只查询用户明确指定的指标。
- 平台健康：查询订阅级 Azure Service Health。
- 资源健康：查询指定 VM 的 Resource Health。
- 资源互查：在主机名、VM 资源名和 Resource ID 之间转换。
- Azure 问答：回答不需要实时资源数据的 Azure 知识问题。
- 支持工单：查询 Service 与 Problem Classification，并生成供外部审批流程提交的草稿。

### 公共流程

以下步骤从原有 Skills 中提取为共享流程：

1. 解析订阅、资源组、VM 名称和时间范围。
2. 检查 Azure CLI 和当前认证上下文。
3. 查询 VM 基础信息、主网卡、磁盘列表和 Resource ID。
4. 查询 VM SKU 的 vCPU 和内存规格。
5. 将用户时间转换为 Azure Monitor 所需的 UTC 时间。
6. 查询并解释 Resource Health 事件。
7. 统一处理资源不存在、权限不足、指标缺失和输出过大。
8. 生成带数据来源、时间范围、结论和建议的中文报告。

### 诊断边界

- 所有指标来自 Azure 控制面、Azure Monitor、Resource Health 或 Resource Graph。
- 不进入 VM，不使用 Run Command、SSH 或远程执行。
- 指标为空时返回“无法判定”或 `N/A`，不能把无数据解释为正常。
- 不根据单个瞬时峰值直接判定持续性异常。
- 不猜测 NIC 名、磁盘名、Resource ID、SKU 上限或 Problem Classification ID。
- 产品限额、价格和区域可用性等易变化信息应以 Microsoft Learn 或 Azure 实时查询为准。

## QM 资源形态

建议目录结构：

```text
azure-opt-skills/
  README.md
  azure-ops/
    SKILL.md
    references/
      vm-diagnostics.md
      health-and-lookup.md
      support-case.md
    scripts/
      az_guard.py
    tests/
      cases.md
      test_az_guard.py
  scripts/
    sync-to-deployment.sh
```

实现完成后，将 `skill/` 中的内容同步到部署层：

```text
deployment/sandbox/skills/azure-ops/
```

`azure-opt-skills/azure-ops/` 是编辑源，部署目录是发布镜像。修改后运行：

```bash
./azure-opt-skills/scripts/sync-to-deployment.sh
```

QM 将部署目录作为一个 Skill bundle 发布，并在 Web UI 中显示：

- 名称：`/azure-ops`
- 描述和适用场景
- Scope 和发布状态
- 内部资产数量

Web UI 的直接创建表单只支持 `name`、`description`、`body` 和 `scopeId`，不适合作为带脚本与参考文件的最终维护入口。

## 已实现组件

- `azure-ops/SKILL.md`：统一发现描述、十类意图路由、公共安全边界和报告契约。
- `azure-ops/references/vm-diagnostics.md`：VM 公共查询与 CPU、内存、磁盘、网络诊断。
- `azure-ops/references/health-and-lookup.md`：Resource Health、Service Health 和主机名互查。
- `azure-ops/references/support-case.md`：Support 分类查询、工单草稿和外部提交说明。
- `azure-ops/scripts/az_guard.py`：只读命令白名单、ARM REST 约束、输出限额、脱敏和 Support 写拒绝。
- `azure-ops/tests/`：行为用例和防护脚本单元测试。
- `deployment/sandbox/Dockerfile`：基于 pinned QM Sandbox 镜像安装固定版本 Azure CLI。
- `deployment/sandbox/tools/azure-cli/tool.json`：Azure CLI 凭据目录、egress、认证检查和命令策略。

## Sandbox 与认证要求

当前 QM 部署使用 Local Sandbox。正式启用前需要完成以下准备：

1. 在 Sandbox 镜像中固定安装 Azure CLI，不能假设基础镜像已有 `az`。
2. Local Sandbox 不依赖跨轮次 Device Code Flow，生产查询使用非交互认证。
3. 凭据通过 QM 的凭据边界或部署 Secret 注入，不写入 Skill 文件。
4. 每次操作先验证当前 Tenant、Subscription 和调用身份。
5. Azure CLI 缓存和凭据必须遵循 QM Scope 隔离，不跨 Personal 或 Project Scope 共享。
6. Egress 只开放 Azure 登录、ARM、Monitor、Resource Graph 和相关服务端点。

部署工具将诊断身份缓存声明为 `$HOME/.azure`，普通诊断使用 `AZURE_CONFIG_DIR=/root/.azure`。该身份必须通过 Azure RBAC 保持只读；这是无法被命令别名绕过的最终权限边界。Local Sandbox 上不要依赖跨轮次 Device Code 轮询；生产环境应由运营方通过 QM 凭据边界准备非交互身份或预置隔离的 Azure CLI 缓存。任何凭据值都不得写入本目录。

建议把权限分成两个身份：

| 身份         | 用途                                                                | 权限原则                               |
| ------------ | ------------------------------------------------------------------- | -------------------------------------- |
| 诊断身份     | VM、Monitor、Resource Health、Service Health 和 Resource Graph 查询 | 只读、最小权限                         |
| Support 提交 | 不在普通 Sandbox 中配置                                             | 使用 Azure Portal 或组织批准的外部流程 |

## Azure CLI 安全层

统一 Skill 不直接执行任意 `az` 参数，而是通过受控包装脚本执行。包装层至少需要：

- 使用 argv 数组和 `shell=False`，禁止拼接 shell 命令。
- 使用命令前缀白名单。
- 拒绝删除、更新、启动、停止、重启、SSH、Run Command 等操作。
- 通用 `az rest` 只允许访问 `https://management.azure.com/` 的 GET 请求。
- Resource Graph 只允许访问固定查询端点的只读 POST。
- 禁止任意请求体、输入文件和外部 URL。
- 限制执行时间和输出大小。
- 对 stdout 和 stderr 做敏感信息过滤。
- 返回结构化 JSON 错误，不让模型根据模糊文本猜测结果。

Azure Support 分支只查询 Service 与 Problem Classification 并生成草稿。普通 Local Sandbox 无法提供不可绕过的跨轮次写确认，也不能让任意进程无法接触已挂载的写身份，因此不自动执行建单命令。提交由用户在 Azure Portal 或组织批准的外部自动化中完成。

## 实施阶段

### 阶段一：内容提炼

- 建立十个原始 Skills 的意图和命令矩阵。
- 合并公共参数与公共查询步骤。
- 删除飞书和原 Agent Framework 专属指令。
- 删除组织特有默认值，例如固定资源组。
- 统一错误处理、阈值和报告格式。

交付物：统一 Skill 内容草案和测试用例清单。

### 阶段二：执行能力

- 实现 Azure CLI 受控包装脚本。
- 固化 Azure CLI 的 Sandbox 安装。
- 接入非交互凭据和订阅选择策略。
- 配置所需 Azure egress。

交付物：可运行的 Skill bundle 和 Sandbox 配置。

### 阶段三：QM 发布

- 将 bundle 放入 `deployment/sandbox/skills/azure-ops/`。
- 运行 QM deployment layer 校验。
- 发布到目标 QM 实例。
- 确认 Web UI Skills 页面和 `/` 选择器可见。

交付物：QM 中可调用的 `/azure-ops` 资源。

本地发布前执行：

```bash
cd deployment
npm run check
npm run qm -- sandbox build
npm run deploy
```

`qm.config.jsonc` 的 `LOCAL_SANDBOX_IMAGE` 必须与构建产物 `qm-local-sandbox:local` 一致。
QM contract v1 的 Sandbox 构建目标是 `linux/amd64`；在 ARM 主机上构建和运行需要 Docker Buildx 与 amd64 模拟支持。

### 阶段四：验证与收敛

- 执行静态校验、安全测试和意图路由测试。
- 使用真实 Azure 只读查询完成烟雾测试。
- 验证 Personal 和 Project Scope 的凭据与资源隔离。
- 验证 Support Case 只能生成草稿，所有创建命令均被拒绝。
- 根据测试结果压缩说明和减少重复查询。

交付物：验收记录、已知限制和上线建议。

## 验收标准

### 资源验收

- `SKILL.md` frontmatter、名称、描述和正文均能被 QM 解析。
- 整个 bundle 小于 QM deployment layer 的 1 MB 限制。
- 所有资产为合法文本路径，执行脚本具有正确执行位。
- `/azure-ops` 在 Web UI 的 Skills 页面和命令选择器中可见。

### 功能验收

- 十类原始意图均能路由到正确分支。
- 综合诊断只执行一次公共 VM 和 SKU 查询。
- CPU、内存、磁盘和网络无数据时不会误报正常。
- Service Health 和 Resource Health 能正确区分平台级事件与单资源事件。
- 主机名互查不会猜测资源名称。
- Azure 问答不会伪装成实时查询结果。

### 安全验收

- 非白名单 Azure CLI 命令被拒绝。
- 任意 ARM 写请求、外部 URL、输入文件和 shell 注入被拒绝。
- 日志和报告不包含 Token、Secret 或证书私钥。
- 普通诊断身份无法创建或修改 Azure 资源。
- Support Case 不能从该 Skill 或普通 Sandbox 创建。
- 不同 QM Scope 之间不能复用对方的 Azure 凭据。

## 测试场景

至少覆盖以下场景：

- VM 整体体检。
- CPU、内存、磁盘和网络单项诊断。
- VM 不存在、已释放或没有指标。
- Azure Monitor Agent 未启用导致内存指标缺失。
- Resource Health 无事件、有已恢复事件和有当前异常事件。
- Service Health 按时间、服务和区域筛选。
- 主机名到资源名以及资源名到主机名的互查。
- Azure 权限不足、认证失效和订阅选择错误。
- 危险 Azure CLI 命令和命令注入尝试。
- Support Case 信息不足、生成完整草稿和拒绝创建。

## 当前状态

- 已完成统一 `SKILL.md`、三份 reference、行为用例和安全测试。
- 已实现 `az_guard.py` 读取白名单、REST 限制、JSON 请求隔离、有界输出、脱敏和 Support 写拒绝。
- 已把 Skill bundle 同步到 `deployment/sandbox/skills/azure-ops/`。
- 已添加 Azure CLI 工具描述、凭据目录和直接命令拒绝策略。
- 已添加固定 Azure CLI 版本的 Sandbox Dockerfile，并将 Local Sandbox 指向构建产物。
- 已通过防护脚本单元测试和 `qm check`。
- 待配置目标 Azure 只读身份并完成真实资源烟雾测试。
- 待部署或重启 QM，使新的 deployment layer 和 Sandbox 镜像进入运行态。
