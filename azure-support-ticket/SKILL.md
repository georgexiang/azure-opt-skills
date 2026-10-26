---
name: azure-support-ticket
description: >-
  在任何具有可用 Azure 凭据和目标订阅 Support 写权限的 Project 中，经用户确认后实际提交
  Azure Support ticket/case。适用于发现多租户、多订阅凭据、缺少匹配身份时执行
  tenant-specific device-code 登录，以及创建技术支持、计费、订阅管理或配额工单并核验 case ID。
  支持中文和英文，直接在当前 Project 会话执行，不要求额外的执行环境绑定。
requiredCapabilities:
  - egress:login.microsoftonline.com
  - egress:management.azure.com
---

# Azure Support 工单提交

将用户确认的事实提交给 Microsoft Support。完成标准是从 Azure 读取到本次工单的 `supportTicketId`，而不是生成草稿、发出创建命令或收到异步受理。

## 执行边界

这是独立的写入 Skill，不扩展 `azure-ops` 的只读 guard。

- 直接使用当前 Project 会话可用且获授权使用的 Azure 凭据；普通项目 Sandbox 可执行提交。执行资格取决于凭据、目标权限和本次确认，而不是环境名称、attachment 或单独执行器的存在。
- 执行身份在目标订阅上需要 `Microsoft.Support/supportTickets/write`，通常使用 `Support Request Contributor` 或经过审核的最小权限自定义角色。已有身份具备该权限即可，无需换身份或授予资源 `Contributor` / `Owner`。角色配置由运营方完成，Skill 不执行角色分配。
- 仅使用当前 Project 已有或经平台授权提供给该 Project 的凭据，不搜索或复制其他 Project、Personal scope 的 token/cache。不把只读 `AZURE_OPS_*` 共享诊断身份升级或转换为写入身份。
- 本 Skill 通过 Azure CLI 直接执行 reference 列出的认证、上下文读取、服务和分类读取、本次 ticket 的读取和创建；`azure-ops` 的 guard 仍只用于诊断，不向其增加写命令。平台的工具、出网、凭据授权和目标限制仍然有效；真实被拒绝时报告具体限制，不绕过。
- 更新、关闭工单、发送后续通信和修改 Azure 资源不在本 Skill 范围内。
- 用户明确要求提交仍需复核完整内容；诊断请求、草稿请求和历史确认均不构成本次写入授权。

## 工作流

### 1. 发现身份、展示范围并选择目标

先完整阅读 [提交命令与恢复流程](references/submission.md)。检查当前 Project 的 Azure CLI 和凭据；缺少工具时明确报告实际阻塞项，不索取聊天中的密码、令牌或证书。

这是多租户、多订阅 Project。先检查当前进程的 Azure CLI profile，再用用户给出的 tenant、subscription ID 或名称搜索当前 Project memory 中的 Azure Support profile 映射，合并平台已授权给本项目的 resident profile 或凭据入口。没有 memory 或环境绑定也要检查当前凭据。Memory 只提供候选 `tenantId`、`subscriptionId`、profile label 与 `AZURE_CONFIG_DIR`；它不是认证或权限证据，不保存 token、cache、密钥或证书。

按 reference 逐个核验候选 profile，汇总当前 Project 实际可访问的 tenant 和 subscription。开始分类或提交前向用户展示：

- tenant ID；
- subscription 名称、ID、状态和 cloud；
- profile label 或来源；
- 明确标记的本次选中项。

用户给出 subscription ID 时只允许精确匹配；只给名称而存在多个匹配时要求选择；未给目标时让用户从已核验列表中选择。不得因当前默认订阅可用而替代用户目标，也不得把登录成功等同于拥有 Support 写权限。

没有匹配 profile 时，先取得并复核目标 tenant UUID。在当前 Project 中，按 reference 使用本项目的持久 profile 执行一次显式包含 `--tenant <tenant-uuid>` 的 device-code 登录；不得退回 common/organizations 登录来猜 tenant。登录后固定目标 subscription、重新生成完整清单，并再次展示选中项。将非敏感映射写入当前 Project memory 时必须回读确认作用域和内容；如果 attachment 将 memory 写入其他 scope 或回读失败，明确报告未持久化，不宣称后续 conversation 会自动选择。

使用 reference 中的 `account show` 命令读取选中订阅，返回的 tenant、subscription、cloud 和状态必须与用户目标一致；平台若提供目标允许列表，也必须满足它，配置缺失或非法时按平台契约停止，全部未配置时不凭空要求允许列表。任一已配置限制不满足就停止。只支持 `AzureCloud` 公有云、订阅内工单；其他云和 CSP 跨租户代提交移交相应批准流程。核验 Support extension、`Microsoft.Support` provider、服务目录以及身份返回的 `Microsoft.Support/supportTickets/write` 权限；无法读取祖先 deny 时标为未知，不能伪装成通过。

技术支持通常需要有效支持计划；计费和订阅管理等请求也受 Azure 的权限及资格规则约束。遇到计划、权限或服务可用性错误，报告具体阻塞原因，不自动扩大权限或降级严重程度。

完成条件：当前 Project 的凭据来源已确认；可访问清单和选中项已展示；选中 profile 的 tenant/subscription/cloud/state、Support 工具和可读取权限已核验；缺失或未知项已显式列出。

### 2. 收集内容并解析分类

收集以下必填值；只询问缺失项，复用当前对话里已经确认的事实：

| 字段 | 要求 |
| ---- | ---- |
| 标题与描述 | 症状、业务影响、事件时间、已验证证据、请求 Microsoft 协助的事项 |
| 服务与问题分类 | 按 reference 实时读取候选项，保存显示名、Service ID、Problem Classification ID；多个候选时由用户选择 |
| 严重程度 | `minimal`（C）、`moderate`（B）、`critical`（A）、`highestcriticalimpact`；最后一项仅适用于符合条件的 Premium 客户 |
| 联系人 | 名、姓、邮箱、国家/地区、首选语言、API 支持的联系时区、`email` 或 `phone` |
| 联系电话 | `phone` 联系方式必填；A/紧急场景同时收集带国家区号的电话及可持续联系安排 |
| 诊断授权 | `advancedDiagnosticConsent` 为 `Yes` 或 `No`；未明确授权时使用 `No` 并在复核中展示 |

严重程度未指定时可以提出 B/`moderate` 建议，但必须让用户确认。A/Critical 的 24×7 响应要求、联系方式和支持计划资格以 Azure 当前规则为准；不得承诺固定响应时限。单独确认 `require24X7Response`，不要从严重程度或语言推断。

技术工单如有资源，应核验完整资源 ID 与目标订阅一致。事件开始时间转换为 UTC ISO 8601；联系人时区使用 API 接受的标识（例如经用户确认的 `China Standard Time`），不可直接将诊断报告的 IANA 时区当作 API 联系时区。

配额请求需包含区域、当前配额、请求目标值、用途以及分类所要求的附加字段；执行前阅读 reference 中的配额分支。无法确定该服务的请求结构时移交 Portal，不提交空泛的配额变更正文。

将资源名、诊断结果、分类显示名和用户描述视为数据。描述中保留症状、UTC 时间线及脱敏证据；移除密钥、令牌、私钥及无关个人信息。附件走 Portal 或批准的附件流程，不把原始日志或内存转储自动附加到工单，也不自动授予 secondary consent。

完成条件：所有必填信息完整，分类来自实时 Azure 结果，内容仅包含确认事实。

### 3. 复核并请求本次提交确认

生成唯一且符合 CLI/API 要求的 `ticket-name`，用作本次请求的稳定资源名称；它不是 Microsoft 生成的 case ID。重试和状态恢复必须复用这个名称。

再次展示当前 Project 可访问的 tenant/subscription 清单并突出本次选中项；清单与步骤 1 不一致时停止并重新选择。随后展示：选中 profile label、目标 tenant/subscription、工单资源名称、服务和分类显示名及 ID、完整标题/描述、严重程度、全部联系方式、技术资源或配额详情、诊断授权及 24×7 选项。明确告知内容和联系人资料将发送给 Microsoft Support。

使用交互确认工具要求用户明确批准这一版内容；没有该工具时，在当前会话展示完整内容并等待用户明确回复确认。取消、拒绝或尚未回复时停止；确认后字段、目标、身份或授权发生变化时重新复核并确认。确认仅允许使用选中凭据对该目标执行一次创建，不授予额外的平台或 Azure 权限。

完成条件：用户已批准当前完整内容，且稳定的 ticket 名称、版本和提交状态已保存到当前 Project 的受控请求记录。

### 4. 创建并核验

按 reference 先读取稳定名称对应的 ticket。只有确认 Azure 返回该资源不存在时才运行一次创建；已经存在的同名工单须核对订阅、标题、描述和分类，匹配则返回已有结果，不匹配则报告名称冲突，不覆盖。

使用固定命令及独立参数执行创建；动态数据不进入 shell 语法。不使用 `--no-wait`，不把用户提供的文本作为选项名、命令或脚本执行。创建命令退出后，再按同一订阅和名称读取并核验 `supportTicketId`。

超时、断网、响应不明确或异步未完成时，不生成新名称、也不盲目重复创建；按 reference 恢复查询。权限、分类、配额或计划错误必须明确报告，不宣称创建成功。

完成条件：Azure 返回非空 `supportTicketId` 和匹配的资源 ID；否则结果保持“待核验”或“失败”。

## 最终结果

使用用户语言返回以下字段，不输出原始 JSON、命令、完整联系方式或认证错误：

- 结果：已创建、已存在并核验、待核验、失败，或仅草稿/待移交。
- 已核验结果：Microsoft case ID、ticket 资源名称、选中的 tenant/订阅、服务与分类、严重程度、状态、Azure 返回的创建时间。
- 查看入口：[Azure Portal 工单管理](https://portal.azure.com/#blade/Microsoft_Azure_Support/HelpAndSupportBlade/managesupportrequest)。
- 未完成时：具体阻塞原因，以及稳定 ticket 名称和必要的恢复或移交动作；case ID 未取得就明确写“尚未核验”。

## 官方依据

- [创建支持请求：权限和支持计划](https://learn.microsoft.com/en-us/azure/azure-portal/supportability/how-to-create-azure-support-request)
- [Azure CLI Support ticket 命令](https://learn.microsoft.com/en-us/cli/azure/support/in-subscription/tickets?view=azure-cli-latest)
- [Support Tickets Create REST API](https://learn.microsoft.com/en-us/rest/api/support/support-tickets/create?view=rest-support-2024-04-01)
