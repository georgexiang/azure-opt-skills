# 当前 Project 的提交与恢复

仅在 `SKILL.md` 的凭据作用域、目标检查和确认条件全部满足后执行写入。以下命令中的占位符必须替换成核验值；除身份发现和登录命令外，所有订阅级 Azure 调用显式指定 subscription。优先使用当前 Project 已有的 Azure CLI profile 或平台授权给本项目的凭据；缺少匹配 profile 时，在当前 Project 执行本文件定义的 tenant-specific device-code 登录。Skill 不执行角色分配，也不要求额外的执行环境绑定。

## 多租户身份发现与选择

即使没有 memory 映射，也先检查当前 Project 进程使用的 Azure CLI profile：已设置 `AZURE_CONFIG_DIR` 时沿用该目录，否则使用当前项目 HOME 下的 `$HOME/.azure`。使用下述 `account list` 核验已有登录，不读取 token/cache 文件，不因缺少名为 Support 的环境或 profile 而拒绝。

再从当前 Project memory 搜索目标 subscription ID、tenant ID、subscription 名称及 `azure-support-profile`，合并平台授权给当前 Project 的 resident profile。只接受本项目可用且获授权的来源；来自其他 Project、Personal scope、历史聊天正文或无法确认作用域的路径不进入候选集。平台提供的凭据入口按其既有认证工具使用，保持 secret 在工具/进程环境内，不将值放进命令、聊天、memory 或请求记录。只有原始凭据但尚未认证时，不能把 `account list` 为空当作无凭据；先使用本项目已授权的认证入口，再核验清单。

推荐的非敏感 memory 记录格式为：

```text
azure-support-profile tenantId=<tenant-uuid> subscriptionId=<subscription-uuid> profile=<label> configDir=<approved-absolute-path>
```

Memory 是路由提示。每个候选都必须使用其 `AZURE_CONFIG_DIR` 实时执行：

```text
az account list --all --query '[].{tenant:tenantId,subscription:id,name:name,state:state,cloud:environmentName,isDefault:isDefault}' --output json
```

执行工具通过进程环境传入选中 profile 的 `AZURE_CONFIG_DIR`，不把 profile 路径拼入用户可控的 shell 语法。不得读取、复制或输出目录内的 token/cache。对清单中每个候选 subscription，再执行 `account show --subscription <subscription-uuid>` 核验实时上下文。

向用户展示一张清单：

| 字段 | 内容 |
| ---- | ---- |
| Profile | 非敏感 label 或批准来源 |
| Tenant | tenant UUID |
| Subscription | 名称和 UUID |
| State / Cloud | 必须为 `Enabled` / `AzureCloud` |
| Selected | 本次是否选中 |

选择规则：

1. 用户指定 subscription UUID 时只选择精确匹配项。
2. 用户只指定名称时，唯一精确名称可选；多个匹配必须让用户选择。
3. 用户未指定目标时，先展示清单并让用户选择。
4. 清单无匹配项时进入 tenant-specific 登录；不得切换到当前默认 subscription 继续。
5. Memory 映射与实时结果不一致时，以实时结果为准并报告陈旧映射。

## 缺少匹配身份时登录

先让操作者提供或从受信任的订阅属性中取得目标 tenant UUID。tenant 未知时停止并引导其从 Azure Portal 的 Subscription properties 获取 Directory ID；不要通过 common/organizations 登录猜测 tenant。

在当前 Project 的 HOME 中为该 tenant 使用持久目录；优先复用本项目匹配的 profile，新目录推荐为：

```text
$HOME/.azure-support/profiles/<tenant-uuid>
```

目录权限设为 `0700`，且不得覆盖其他 tenant 的 profile。通过后台/长任务工具运行：

```text
az login --tenant <tenant-uuid> --use-device-code --output none
```

只向操作者显示 Microsoft 设备登录 URL、一次性代码和等待状态。授权完成后：

```text
az account set --subscription <subscription-uuid>
az account show --subscription <subscription-uuid> --query '{subscription:id,name:name,tenant:tenantId,cloud:environmentName,state:state}' --output json
```

目标 subscription 不可见、tenant 不匹配、状态不是 `Enabled` 或 Conditional Access 拒绝时停止。保留脱敏后的错误代码和 request/correlation ID；不刷新代码来绕过策略，不回退到无 `--tenant` 的登录。

登录核验成功后，重新生成完整 tenant/subscription 清单并展示选中项。仅保存上述非敏感 memory 映射；保存后按 subscription ID 回读。回读结果必须来自当前 Project scope 且内容匹配，否则报告“profile 可用，但 Project memory 映射未持久化”，后续 conversation 需要显式选择该 profile。

## 工具与上下文

```text
az version --output json
az extension show --name support --query version --output tsv
az account show --subscription <subscription-uuid> --query '{subscription:id,tenant:tenantId,cloud:environmentName,state:state}' --output json
```

上述命令与后续每条 Azure 命令都使用同一个选中 profile 环境。需要 Support extension 的 `in-subscription tickets` 命令。Azure CLI 文档要求 CLI 2.57.0 或更高；具体安装版本由当前项目的工具安装流程维护。验证订阅为 `Enabled`、cloud 为 `AzureCloud`、tenant/subscription 与批准目标一致。缺少 extension 时让运营方安装到选中 profile 或批准的共享 extension 目录，避免在写入步骤隐式动态安装。

在读取分类前还要核验：

```text
az provider show --subscription <subscription-uuid> --namespace Microsoft.Support --query registrationState --output tsv
az support services list --subscription <subscription-uuid> --query 'length(@)' --output tsv
```

身份返回的有效 actions 必须覆盖 `Microsoft.Support/supportTickets/write`。订阅和可读祖先 scope 的 deny assignment 不得覆盖该 action；祖先 deny 无读取权限时展示为未知，并说明创建调用可能是第一项权威验证。登录成功或服务列表可读本身不证明拥有写权限或有效支持计划。

## 服务与问题分类

```text
az support services list --subscription <subscription-uuid> --output json
az support services problem-classifications list --subscription <subscription-uuid> --service-name <service-name-from-list> --output json
```

从服务结果中选取 `name` 和完整 `id`，按显示名筛选但不猜测 ID。分类结果中保存完整 `id`；其 `/providers/Microsoft.Support/services/<service-name>/problemClassifications/<classification-name>` 路径必须属于所选服务。创建 CLI 的 `--problem-classification` 使用完整分类 ID，不额外拼接未经验证的 service ID。

## 执行参数

优先使用执行工具的 argv 数组接口，设置 `shell=False`。如果工具只支持 shell，将每个动态参数分别做 POSIX shell 引用（可使用 Python `shlex.quote`）；引号、换行、反引号和 `$()` 均作为字面数据传递。不要用字符串替换拼接未经引用的命令。用户字段不能增加选项或改变固定命令前缀。

完整复核记录和执行请求仅存放在当前 Project 的受控存储（目录 `0700`、文件 `0600`），联系人资料不写入 Git、公共日志或其他 Scope。下述固定 `--query` 将读取结果限定为恢复和确认所需字段。

## 提交前读取

```text
az support in-subscription tickets show --subscription <subscription-uuid> --ticket-name <stable-ticket-name> --query '{id:id,name:name,caseId:supportTicketId,title:title,description:description,classification:problemClassificationId,status:status,severity:severity,created:createdDate}' --output json
```

Support extension 的该命令会将 REST `properties` 展平，上述查询使用 CLI 顶层字段。当前项目升级 extension 时核对输出契约；任何必需字段缺失都不能当作成功。

- 已存在：核对请求记录中的资源 ID、标题、描述和分类；匹配则复用，不创建。冲突则停止。
- 明确的 Azure 资源不存在错误（404/`ResourceNotFound`，且指向该 ticket）：允许进入已确认的创建流程。
- 403、401、订阅不存在、网络超时或其他错误：停止；这些错误不证明 ticket 不存在。

在当前 Project 中按 subscription 和稳定 ticket 名称串行化提交，避免两个请求同时通过“不存在”检查后重复创建。使用平台已有请求锁，或当前项目持久存储上的进程间文件锁（如 `flock`），覆盖提交前读取、创建和提交后核验全过程；同项目所有会话必须使用同一锁路径。没有可靠锁时停在待提交并报告并发保护缺失，不能凭“当前只有一个任务”跳过。请求记录包含当前版本、确认状态和提交状态；确认不跨版本复用，恢复任务只读取原名称。锁和记录不要求另建执行环境。

## 技术、计费和订阅管理创建

```text
az support in-subscription tickets create --subscription <subscription-uuid> --ticket-name <stable-ticket-name> --title <confirmed-title> --description <confirmed-description> --problem-classification <verified-full-classification-id> --severity <confirmed-severity> --contact-first-name <first-name> --contact-last-name <last-name> --contact-email <email> --contact-country <country> --contact-language <language> --contact-timezone <api-timezone> --contact-method <email-or-phone> --advanced-diagnostic-consent <Yes-or-No> --require-24-by-7-response <true-or-false> --query '{id:id,name:name,caseId:supportTicketId,status:status,severity:severity,created:createdDate}' --output json
```

按确认内容添加可选参数，每项都是独立的 argv：

| 分支 | 参数 |
| ---- | ---- |
| 技术资源 | `--technical-resource <verified-full-resource-id>` |
| 已知开始时间 | `--start-time <UTC-ISO-8601>` |
| 电话联系或已确认的严重事件电话 | `--contact-phone-number <confirmed-phone-with-country-code>` |
| 用户明确要求附加联系人 | `--contact-additional-emails <confirmed-email> ...` |

不要从例子复制默认 `Yes` 诊断授权。`highestcriticalimpact` 需单独核验 Premium 资格，不把它作为 `critical` 的别名。不要自行添加 `--secondary-consent`、`--file-workspace` 或 enrollment 字段。需要这些功能时使用另行审核的流程。

## 配额分支

先解析正确的配额服务/分类，再按该服务的官方 CLI 示例/API 文档核验请求结构。有些分类只需描述，有些需要：

```text
--quota-change-version <documented-version>
--quota-change-subtype <documented-subtype>
--quota-change-requests <confirmed-JSON-array>
```

`quota-change-requests` 通常含 `region` 与服务特定 `payload`；用 JSON 序列化生成一个参数，不能凭空猜测 payload 的字段、单位、版本或 subtype。展示每个区域及请求目标值供用户确认，再附加到上述创建命令。结构无法核实时停止自动提交并移交 Portal。

## 提交后核验与不确定结果

创建后使用“提交前读取”的同一条 `show` 命令，核对 ARM 资源 ID 中的 subscription 和稳定名称，再验证非空 `supportTicketId`。`ticket-name`、本地请求编号和 ARM ID 都不是 Microsoft case ID。

创建超时或响应不明确时，仅查询原名称：最多进行 6 次查询，每次间隔 10 秒；若服务返回明确的重试等待时间，遵从该时间。等待预算耗尽或出现权限/认证错误则停止，返回“待核验”和恢复所需的稳定名称。创建后暂时 404 也不能证明提交未发生；禁止自动重新 PUT/create 或换名。

状态恢复重新执行相同 `show`。已有匹配的工单返回“已存在并核验”；仍无可验证结果时移交运营方核对。只有运营方核实原请求未创建、用户再次确认之后，才允许重新发起创建。

收到服务错误时保留脱敏后的 Azure 错误代码、故障类别及适用的 correlation/request ID；不向用户输出完整认证 stderr、联系人资料或请求正文。403 指向 RBAC，技术支持计划不满足指向订阅资格，分类失效则重新查询分类并重新确认；不要静默切换订阅、严重程度或诊断授权。
