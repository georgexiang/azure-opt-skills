# QM 的 Azure Ops Skill

## 概述


该 Skill 资源名为：

```text
/azure-ops
```

主 `SKILL.md` 负责意图路由、工作流编排与安全约束。详细命令、阈值、输出模板与执行脚本仍作为同一 Skill 的内部资产保留。

仓库另提供独立 `/azure-support-ticket`，用于在**任何具备获授权 Azure 凭据和目标 Support 写权限的 Project** 中，经用户复核确认后实际创建 Azure Support 工单并核验 Microsoft case ID；直接在当前会话执行，不要求额外环境绑定。原 `/azure-ops` 和只读 guard 的权限边界保持不变。

另提供独立 [`/case-summary-oncall-schedule`](case-summary-oncall-schedule/SKILL.md)：将当前项目用户上传的原始 `.md` Case、On-call 排班或混合资料导入项目日报，分别展示 `Case Summary` 和 `On-call Schedule`。任一主题明确即可保存；没有明确主题但有疑义时请求真人确认，确认不等于核实不确定业务事实。仅使用报告日上传资料，排班列表突出报告日期、不自动跨日复用；导入轮次及回复不进入新版会话摘要，普通讨论保留。依赖平台原生 `project_case_upload` 及匹配 Core/Digest 版本，不创建工单或排班台账、不需要 Azure 凭据。交互验收见 [测试清单](case-summary-oncall-schedule/tests/cases.md)。

### Case / On-call Skill 名称迁移

规范名称和目录统一为 **`case-summary-oncall-schedule`**。旧的 `case-summery-oncall-schedule`、`case-summery` 和 `project-case-upload` 只作为历史部署/兼容标识，不是新的首选名称。

GitHub 改名不等于平台已完成迁移：

1. 先确认目标 Core 的 Skill 加载门禁允许规范名称，导入轮次追踪与日报会话排除也识别新名；迁移期间按需保留旧名兼容。
2. 在目标平台受控验证后，将 Git Pack 固定到本次改名提交，更新 subset 为 `["case-summary-oncall-schedule"]`，导入并发布新 Skill；验收通过后才归档旧名。
3. 验证完整读取新 Skill 后能调用原生工具并取得回执，且导入轮次仍不混入会话摘要。仅更改名称、合并 Git、Sync now 或复制文件不构成端到端验收。

后端尚未支持新名称的已部署环境，应暂时保留已验证的旧 pack 提交和 Skill，不要直接切换。此次仓库改名不增加自动化导入权限，也不将用户附件校验改为接受 Loop 生成文件。

## 目标

1. 将源仓库中的 Azure Skills 合并为一个 QM Skill。
2. 保留 Azure VM 诊断、平台健康查询、资源查找、知识问答以及 Support 工单工作流。
3. 消除原有 Skills 中重复的 VM 查询、SKU 查询、时间转换与 Resource Health 查询。
4. 去除对 Feishu、Microsoft Agent Framework 及源项目自定义 `run_az` 接口的运行时耦合。
5. 与 QM 的 Scope、Sandbox、凭据体系、Skill 物化与 Web UI 展示集成。
6. `azure-ops` 仅执行只读 Azure 操作并生成草稿；实际提交由独立 `azure-support-ticket` 在当前 Project 核验权限和用户确认后执行。
7. 让该 Skill 在 QM Web UI 的 Skills 页面与 `/` 命令选择器中可见。

## 非目标

- 不迁移源项目的 Feishu 机器人、会话服务或独立 Python Agent 服务。
- 不在 Skill 中存储 Azure 令牌、客户端密钥、证书私钥或其他敏感信息。
- 不允许 Agent 进入 VM、执行来宾命令，或读取进程、文件系统、应用日志。
- 不将未经校验的 Azure CLI 命令直接暴露给模型。
- 默认不授予创建、修改、重启或删除 Azure 资源的权限。

## 源能力清单

源仓库包含以下十个 Skill：

| 原始 Skill                 | 用途                            | 统一 Skill 分支      |
| -------------------------- | ------------------------------- | -------------------- |
| `vm-full-diagnosis`        | 全面的 VM 健康检查              | `vm full`            |
| `vm-cpu-check`             | CPU 指标诊断                    | `vm cpu`             |
| `vm-memory-check`          | 内存指标诊断                    | `vm memory`          |
| `vm-disk-check`            | 磁盘 IOPS、吞吐与延迟诊断       | `vm disk`            |
| `vm-network-check`         | 网络流量、流向与加速能力诊断    | `vm network`         |
| `vm-resource-health-check` | 单个 VM 的 Resource Health 查询 | `vm resource-health` |
| `service-health-check`     | 订阅级 Service Health 查询      | `service-health`     |
| `vm-hostname-lookup`       | 主机名与 VM 资源名互查          | `vm lookup`          |
| `azure-qa`                 | Azure 知识问答与能力边界说明    | `qa`                 |
| `azure-support-case`       | Azure Support 工单草稿生成      | `support-case`       |

## 统一 Skill 设计

### 路由

主 `SKILL.md` 会根据用户意图选择一个执行分支：

- 全量诊断：CPU、内存、磁盘、网络与 Resource Health。
- 定向诊断：仅查询用户明确指定的指标。
- 平台健康：查询订阅级 Azure Service Health。
- 资源健康：查询指定 VM 的 Resource Health。
- 资源查找：在主机名、VM 资源名与资源 ID 之间映射。
- Azure 问答：回答不依赖实时资源数据的 Azure 知识问题。
- Support 工单：查询 Service 与 Problem Classification 值，并生成用于外部审批与提交流程的草稿。

### 共享工作流

以下步骤在原始 Skills 中共享：

1. 解析订阅、资源组、VM 名称与时间范围。
2. 检查 Azure CLI 可用性与当前认证上下文。
3. 查询 VM 身份、主 NIC、挂载磁盘与资源 ID。
4. 查询 VM SKU 的 vCPU 与内存能力。
5. 将用户输入时间转换为 Azure Monitor 所需的 UTC 时间戳。
6. 查询并解释 Resource Health 事件。
7. 统一处理资源不存在、权限不足、指标缺失与输出过大等情况。
8. 以用户语言生成报告，包含数据来源、评估时间范围、结论与建议。

### 诊断边界

- 所有指标均来自 Azure 控制平面、Azure Monitor、Resource Health 或 Resource Graph。
- 不进入 VM，也不使用 Run Command、SSH 或其他远程执行方式。
- 指标缺失时返回“无法判定”或 `N/A`；绝不将缺失数据解释为健康。
- 不以单个瞬时峰值诊断持续性故障。
- 不猜测 NIC 名称、磁盘名称、资源 ID、SKU 限制或 Problem Classification ID。
- 对产品限制、价格、区域可用性等易变事实，通过 Microsoft Learn 或实时 Azure 查询进行核验。

## QM 打包

推荐目录结构：

```text
azure-opt-skills/
  README.md
  azure-ops/
    SKILL.md
    references/
      vm-diagnostics.md
      vm-performance-patterns.md
      health-and-lookup.md
      support-case.md
    scripts/
      install_azure_cli.sh
      az_guard.py
    tests/
      cases.md
      test_az_guard.py
```

在 QM Admin Skills 页面注册该 Git Pack：

```text
https://github.com/georgexiang/azure-opt-skills
```

注册后选择 `Browse skills...`，并将 `azure-ops` 导入目标 Scope。仓库更新后使用 `Sync now`。无需构建或替换 Sandbox 镜像。

如需实际提交，另行导入 `azure-support-ticket` bundle（包含 `SKILL.md`、`references/submission.md` 和 `tests/cases.md`）。该 bundle 不依赖另一个 Skill 的文件路径，也不内置认证或安装脚本；使用当前 Project 的 Azure CLI、Support extension 和获授权凭据。当前项目已有匹配的 Azure CLI 登录即可核验并继续，无需额外环境绑定或 memory 映射。缺少身份时可按 Skill 在当前项目执行 tenant-specific device-code 登录；导入 Skill 本身不授予 Azure 权限。

### 多租户 Support 提交流程

`azure-support-ticket` 将 Project 视为多租户、多订阅执行环境，而不是把当前 Azure CLI 默认订阅当作提交目标：

1. 先检查当前 Project 的 Azure CLI profile，再用目标信息搜索 Project memory 中的非敏感 Support profile 映射；没有映射也检查已有登录。
2. 实时核验每个候选 profile，并在提交前展示当前 Project 可访问的 tenant/subscription 清单及本次选中项。
3. 目标 subscription 没有匹配身份时，先取得正确 tenant ID，再在当前 Project 中执行显式 `--tenant` 的 device-code 登录。
4. 登录后固定目标 subscription，重新核验清单、Support extension、provider、服务目录和可读取的写入权限。
5. 仅保存 tenant/subscription/profile 路由信息；认证材料留在批准的 resident profile 中。

该流程修复了以下失败模式：

- 单租户假设使 Agent 只检查默认订阅，并把“目标订阅不可见”误认为整个 Environment 没有身份。
- device-code 登录落入 common/organizations 路径，触发 Conditional Access；明确 tenant 的同类登录可以走正确策略。
- Project profile 登录成功，但提交 conversation 没有选择它，因而继续使用旧订阅。
- Environment attachment、resident credential、Keychain grant 和 Project memory 被误认为同一个授权面。
- Memory 因 attachment 写入其他 scope 或无法回读，却被错误宣称为可供后续 conversation 自动使用。

Memory 只负责发现候选；Azure 的实时 `account list/show`、目标精确匹配和提交前用户确认才构成选择证据。Skill 不从其他 Project 复制 token/cache，也不以当前默认订阅替代目标。

QM 会将该 Git Pack 发布为 Skill bundle，并在 Web UI 中展示以下信息：

- 名称：`/azure-ops`
- 描述与支持场景
- Scope 与发布状态
- 内部资产数量

Web UI 的直接创建表单仅支持 `name`、`description`、`body` 与 `scopeId`。对于包含脚本与参考文件的 bundle，它不是最终维护入口。

## 已实现组件

- `azure-ops/SKILL.md`：统一发现描述、十个意图分支、共享安全边界与报告契约。
- `azure-ops/references/vm-diagnostics.md`：共享 VM 查询与 CPU、内存、磁盘、网络诊断。
- `azure-ops/references/vm-performance-patterns.md`：CPU/disk burst、VM 级限流、VF、Host Reboot 与计划维护的证据组合和处置边界。
- `azure-ops/references/health-and-lookup.md`：Resource Health、Service Health 与主机名查找工作流。
- `azure-ops/references/support-case.md`：Support 分类查询、工单草稿与外部提交流程指引。
- `azure-ops/scripts/install_azure_cli.sh`：在当前 Scope 的持久化 `$HOME` 中安装固定版本 Azure CLI。
- `azure-ops/scripts/az_guard.py`：只读命令白名单、ARM REST 限制、输出限幅、敏感信息脱敏与 Support 写操作拒绝。
- `azure-ops/tests/`：行为用例与 guard 脚本单元测试。

## Sandbox 与认证要求

该 Skill 不修改 Sandbox 镜像。在某个 Scope 首次发起实时查询时，会通过后台任务安装以下组件：

```text
Azure CLI 2.89.1
Azure Support extension 2.0.1
```

安装路径：

```text
$HOME/.local/share/azure-cli/2.89.1
$HOME/.local/bin/az
$HOME/.local/share/azure-cli/extensions
```

QM Local Sandbox 会将整个 `$HOME` 存储到 Scope 专属卷中，因此安装结果可跨轮次与容器重建持续保留。测试中完整安装约占 717 MB。Personal、Project、Channel 等不同 Scope 相互隔离，需分别安装。

安装器要求基础环境具备 Python 3.11、`venv`、`curl`、`flock` 与 `sha256sum`。完整 Azure CLI 传递依赖被固定在 `azure-cli-2.89.1.lock` 中，并以 SHA-256 哈希校验。Support extension wheel 也固定了 URL、版本与 SHA-256 哈希。安装器仅写入 `$HOME/.local`；不会创建或修改 `$HOME/.azure`。

实时查询还需要以下配置：

1. 由操作人员为目标 Scope 配置 Azure 身份。不要通过聊天发送密钥、证书或令牌。
2. 每次操作先校验当前租户、订阅与调用身份。
3. Azure CLI 使用 `$HOME/.azure` 作为缓存目录，QM 会按 Scope 进行捕获与恢复。
4. `azure-ops` 诊断身份通过 Azure RBAC 保持只读；独立提交 Skill 的身份需要目标 Support 写权限。
5. 出网策略需允许 Azure 登录、ARM、PyPI 及 Azure CLI extension 下载端点。

Azure RBAC 是最终授权边界：诊断身份保持只读，提交身份按目标 Support 权限授权，不能通过命令别名绕过。生产环境优先使用 service principal 或 managed identity。仅在 Conditional Access 允许时，才可用 Device Code 进行临时交互式登录。

对于明确接受组织内所有 Scope 共享同一身份的可信 QM 部署，管理员可以通过 **Admin → Shared service credentials → Env delivery** 配置 `AZURE_OPS_TENANT_ID`、`AZURE_OPS_CLIENT_ID` 和 `AZURE_OPS_CLIENT_CERTIFICATE_PEM`。Guard 只接受完整的三项组合，并使用临时 `0600` PEM完成固定的 certificate login；它不会开放任意 `az login`，也不会把 PEM转发给读取命令。此模式必须配合 tenant root management group 或目标 subscriptions 上的 Reader RBAC，不能授予写角色。

当 QM 注入 target 绑定元数据时，Guard 会在命令执行前强制目标契约：

- `QM_AZURE_OPS_TARGET_TENANT_ID`
- `QM_AZURE_OPS_TARGET_SUBSCRIPTION_ID`
- `QM_AZURE_OPS_TARGET_ALLOWLIST_JSON`（格式固定：`[{tenantId,subscriptionIds[]}]`）

三项全部缺失表示兼容旧行为；只要部分缺失、UUID 非法、allowlist JSON 非法、或默认 subscription 不在 allowlist 内，Guard 都会返回 `TARGET_CONFIG_INVALID`。启用 target 绑定后，所有常规 Azure 读取命令必须落在 allowlist subscription 内，缺失 `--subscription` 时由 Guard 自动注入默认 subscription；重复或不匹配订阅将返回 `TARGET_DENIED`。Resource Graph POST 的 `subscriptions` 也必须是 allowlist 子集。

对于用户明确提出“连接 Azure / 重连 Azure”的请求，允许一次平台批准的显式 Device Code 流程：先执行安装检查（必要时后台安装），再通过后台工具运行固定命令 `az login --tenant <requested tenant> --use-device-code --allow-no-subscriptions --only-show-errors`。该交互登录不经过 Guard，不接受任何 secret 输入；完成后必须通过 Guard 执行 `account show` 校验上下文。

建议将权限拆分为两个身份：

| 身份         | 用途                                                              | 权限原则                                   |
| ------------ | ----------------------------------------------------------------- | ------------------------------------------ |
| 诊断身份     | VM、Monitor、Resource Health、Service Health、Resource Graph 读取 | 只读与最小权限                             |
| Support 提交 | 当前 Project 中获授权的 Azure 凭据；不要求单独环境             | 目标订阅的 Support Request Contributor 或审核过的最小权限自定义角色 |

独立 `/azure-support-ticket` 的实际提交流程是：搜索 Project memory 的非敏感 profile 映射 → 实时展示可访问 tenant/subscription 并精确选择目标 → 缺少匹配身份时执行 tenant-specific device-code 登录 → 核验 Support 工具与权限 → 实时解析服务与分类 → 补齐联系人、严重程度及诊断授权 → 展示完整内容并请求本次确认 → 按稳定 ticket 名称检查已有工单 → 创建一次 → 读取并核验 `supportTicketId`。超时只恢复查询，不自动换名或重复创建。技术支持计划、分类和配额 schema 以 Azure 当前规则为准，真实提交的行为验收见 `azure-support-ticket/tests/cases.md`。

## Azure CLI 安全层

统一 Skill 不会直接执行任意 `az` 参数，而是使用受控封装并强制以下要求：

- 使用 argv 数组并设置 `shell=False`；绝不拼接 shell 命令。
- 强制命令前缀白名单。
- 拒绝 delete、update、start、stop、restart、SSH、Run Command 及其他变更操作。
- 常规 `az rest` 仅允许对 `https://management.azure.com/` 发起 GET 请求。
- Resource Graph 的 POST 请求仅允许固定的只读查询端点。
- 拒绝任意请求体、输入文件与外部 URL。
- 限制执行时长与输出大小。
- 对 stdout 与 stderr 中的敏感信息进行脱敏。
- 返回结构化 JSON 错误，避免模型从含糊文本中推断结果。

`azure-ops` 的 Azure Support 分支仅查询分类并生成草稿，guard 仍拒绝创建。实际提交切换到独立 `azure-support-ticket` Skill，在当前 Project 使用获授权凭据执行固定 Support 命令，逐次确认、共享锁查重并核验 case ID。Skill 确认是工作流约束，不宣称为不可绕过的沙箱安全边界；工具授权、凭据作用域和 Azure RBAC 仍由平台及 Azure 强制。

## 实施阶段

### 阶段 1：内容抽取

- 构建十个原始 Skills 的意图与命令矩阵。
- 合并共享参数与查询步骤。
- 删除 Feishu 特定与原 Agent Framework 特定说明。
- 删除固定资源组等组织特定默认值。
- 统一错误处理、阈值与报告格式。

交付物：统一 Skill 内容草稿与测试用例清单。

### 阶段 2：执行能力

- 实现受控 Azure CLI 封装。
- 在 Scope 持久化卷中安装用户本地 Azure CLI。
- 集成非交互凭据与订阅选择规则。
- 配置所需 Azure 出网。

交付物：不修改 Sandbox 镜像的可执行 Skill bundle。

### 阶段 3：QM Git Pack 发布

- 在 Admin Skills 中注册该 Git 仓库。
- 浏览该 pack 并将 `azure-ops` 导入目标 Scope。
- 确认该 Skill 在 Web UI Skills 页面与 `/` 选择器中可见。
- 后续更新使用 `Sync now` 或 Auto-sync。

交付物：在 QM 中可调用的 `/azure-ops` 资源。

### 阶段 4：验证与优化

- 运行静态校验、安全测试与意图路由测试。
- 使用真实只读 Azure 查询完成冒烟测试。
- 验证 Personal 与 Project Scope 间的凭据与资源隔离。
- 验证诊断 guard 拒绝创建；独立提交 Skill 在有权限的普通 Project 中可完成确认、创建与核验。
- 根据测试结果优化说明并减少重复查询。

交付物：验收记录、已知限制与发布建议。

## 验收标准

### 资源验收

- QM 能解析 `SKILL.md` 的 frontmatter、name、description 与 body。
- QM Git Pack 导入器可读取整个 bundle。
- 所有资产均具备有效文本路径，且可执行脚本具备正确 mode。
- `/azure-ops` 出现在 Web UI Skills 页面与命令选择器中。

### 功能验收

- 十个原始意图均路由到正确分支。
- 一次全量诊断仅执行一次共享 VM 与 SKU 查询。
- CPU、内存、磁盘或网络数据缺失时，绝不产出健康结论。
- Service Health 与 Resource Health 能正确区分平台级事件与单资源事件。
- 主机名查找绝不猜测资源名。
- Azure 知识问答绝不宣称自身来自实时查询结果。

### 安全验收

- 白名单之外的 Azure CLI 命令会被拒绝。
- 任意 ARM 写操作、外部 URL、输入文件与 shell 注入尝试会被拒绝。
- 日志与报告中不包含令牌、密钥或证书私钥。
- 通用诊断身份不能创建或修改 Azure 资源。
- `azure-ops` 及其 guard 不创建工单；独立提交 Skill 仅在当前 Project 获授权且用户确认后创建。
- Azure 凭据不能跨 QM Scope 复用。

## 测试场景

至少覆盖以下场景：

- 全量 VM 健康检查。
- 定向 CPU、内存、磁盘与网络诊断。
- CPU credits、磁盘 burst credits、VM cached/uncached 存储上限诊断。
- Host Reboot 与 Planned Maintenance 的 Resource Health、Service Health、Activity Log 对照。
- VM 缺失、已解除分配或无指标数据。
- 未启用 Azure Monitor Agent 时的内存指标缺失。
- Resource Health 的无事件、已恢复事件与当前异常事件。
- Service Health 按时间、服务与区域过滤。
- 主机名到资源名、资源名到主机名的双向查找。
- Azure 权限不足、认证过期与订阅选择错误。
- 危险 Azure CLI 命令与命令注入尝试。
- Support 工单信息不完整、完整草稿生成与创建拒绝。

## 当前状态

- 统一 `SKILL.md`、三个参考文件、行为用例与安全测试已完成。
- `az_guard.py` 已实现读取白名单、REST 限制、JSON 请求隔离、输出限幅、脱敏与 Support 写操作拒绝。
- Scope 本地 Azure CLI 安装器可在不修改 Sandbox 镜像的前提下工作。
- Git Pack 已注册并导入到 `org:qm-local`。
- guard 脚本单元测试通过。
- 目标只读 Azure 身份与真实资源冒烟测试仍待完成。
