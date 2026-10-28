# 当前 Project 的提交与恢复

仅在 `SKILL.md` 的凭据作用域、目标检查和确认条件全部满足后执行写入。以下命令中的占位符必须替换成核验值；除身份发现和登录命令外，所有订阅级 Azure 调用显式指定 subscription。优先使用当前 Project 已有的 Azure CLI profile 或平台授权给本项目的凭据；缺少匹配 profile 时，在当前 Project 执行本文件定义的 tenant-specific device-code 登录。Skill 不执行角色分配，也不要求额外的执行环境绑定。

## 工具初始化与统一入口

在加载凭据和身份发现之前，先运行：

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh --check
```

检查失败时，通过后台/长任务工具运行以下命令，等待安装结束，再重新执行 `--check`；成功后继续同一个请求，不要求用户手动安装：

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh --ensure
```

本 bundle 自带固定版本安装器、依赖哈希锁和统一入口，可独立导入。只修复当前 Project 持久 `$HOME/.local` 中的工具，不加载凭据、不修改认证缓存、RBAC 或 Sandbox 镜像。已有安装可幂等复用；安装器用项目内进程锁串行化安装，下载或验收失败保留原安装。下载域名见本 Skill 的 requiredCapabilities；出网或基础依赖被拒绝时报告实际阻塞项，不绕过平台限制。

后续所有 Azure 命令都使用下列完整入口，包括 `account`、`login`、`rest`、分类读取、查重、创建和恢复查询。argv 接口使用 `["bash", "skills/azure-support-ticket/scripts/azure_cli.sh", ...]`，不改为系统 `az`。入口自动选择受控 CLI、设置扩展目录并关闭动态安装，保留当前选中的 `AZURE_CONFIG_DIR`；无需 source `.bashrc` 或继承上一条命令的 export。安装器和入口的版本/路径配置来自同一 `azure_cli_env.sh`。

`--check` 使用临时空 profile 验证 CLI、Support 扩展版本及创建命令可加载，不验证 Azure 身份或写权限。`SUPPORT_TOOLCHAIN_NOT_READY` 表示工具未就绪；与凭据授权、401/403、支持计划错误分别报告。普通 Azure 调用不会隐式安装、登录、修复或重试；创建结果不明确时仍只按原稳定名称恢复查询。若运行中发现工具问题，先停止并重新完成工具预检，再核验身份；工具修好本身不构成重新创建授权。

## Project 共享身份选择

将聊天请求者、凭据所有者和 Azure 执行身份视为三个不同角色。Project 共享 grant 的 `audienceScopeId` 为本项目，且长期 purpose 允许项目成员提交 Support 工单时，项目内有访问权限的成员均可使用该 grant；原申请人的姓名或 requesterId 不把它变成个人专属授权。保持平台的项目成员校验，不能用于项目外用户或其他 Project。

用户要求使用项目已有共享 Azure 凭据时，按目标 tenant/subscription、有效 grant、Support 用途和实际 Azure 权限选身份，不要求“George 的 Azure 凭据”或其他与聊天者同名的账号。没有聊天者个人 profile 不是失败条件；已有匹配的项目 standing grant 时，不为每个成员重复申请凭据授权。若 grant 的真实 purpose 另有限定，仍须满足，不能把仅登录成功或数据刷新用途当作 Support 写入授权。

只有用户当前明确要求验证某个具体 Azure 账号或其个人 RBAC 时，才把该身份作为硬性目标；其他共享账号成功不能证明这个指定账号有权限。用户后续明确改为共享项目身份时，以最新选择为准，重新读取实时授权并展示选中的共享身份，不继续沿用旧身份测试条件，也不谎称已验证用户个人权限。

普通工单创建不要求先具备关闭工单能力。当前 Skill 仍不执行关闭操作；若用户明确要求“测试后必须关闭”，先告知这一范围限制，取得允许保持打开的确认或提供已批准的关闭流程后再创建测试单，不把关闭限制泛化为禁止所有正式工单。

## 凭据授权用途与历史限制恢复

在读取或加载凭据前，通过平台的 Keychain/凭据目录读取当前 Project 可见的 credential、grant、audience、status、expiresAt 和 purpose 元数据；不读取 secret 值。认证可用、Azure RBAC 允许和凭据所有者批准本次用途是三个独立条件。

- 当前有效 grant 的 audience 必须覆盖本 Project，purpose 必须允许本次 Support 操作。只有诊断/数据刷新用途的 grant 不足以授权创建工单。
- 如果旧 grant 仍限定另一执行上下文，使用平台的凭据申请流程，请求所有者批准在当前 Project 会话中读取分类、按稳定名称查重并创建用户确认的 Support 工单。说明目标订阅、用途及请求一次性还是持续授权；等待所有者审批。不要修改授权记录、伪造审批或用已有登录缓存绕过 purpose 限制。
- QM 提供 Keychain 时，先按当前 Keychain manifest 获取元数据；缺少合适授权时调用 `POST /v1/keychain/asks`，body 为 `{credential, purpose, requestedMode}`。平台按凭据、Project 和请求来源去重，不按 purpose 去重：返回 existing/pending ask 时必须核对其冻结的 purpose 是否覆盖本次 Support 用途。相符才复用；不相符则明确报告旧申请用途冲突，请所有者先拒绝/处理旧申请后再按当前用途申请，不能修改原申请、谎称新用途已送审或更换来源绕过去重。返回实际 ask ID 和平台审批入口；未收到有效审批时不调用 use 或 Azure 创建命令。
- 批准后重新读取 grant 的 audience/status/expiry/purpose，优先按当前 Keychain manifest 定义的 use 命令在当前项目加载凭据。`POST /v1/keychain/use`（`{grant}`）返回的是含敏感材料的 shell 初始化脚本，发出 POST 本身不等于已加载：将响应写入权限为 `0600` 的临时文件，在同一 shell 中 source 后执行所需 Azure 命令，最后清理临时脚本；不显示、读取到聊天、记录日志或启用 shell tracing。不要预先单独调用 use 来试探一次性 grant，以免消耗授权却未使用。每次 Azure 命令使用相同选中 profile，核验实际 account 上下文。遇到 use 拒绝时报告实际拒绝，不换 profile 绕过。
- 旧聊天结论和 Memory 中的环境名称只是历史上下文，不是当前授权证明。授权已经更新时以实时元数据为准，重新加载当前 Skill 和 reference，不沿用之前的拒绝结论。报告必须区分“待凭据用途审批”“平台加载拒绝”和“Azure 权限/计划失败”，不能统称为缺少执行环境。

### 持续凭据授权与工单确认

用户说“永久授权”“一直允许”“后续不用重复授权”时，`POST /v1/keychain/asks` 必须使用 `requestedMode: "standing"`，不向申请或最终 grant 添加人为指定的到期时间。平台默认的审批申请 TTL 只限制审批窗口，不等于批准后的持续 grant 到期。向所有者解释：持续授权直到撤销，仅限当前 Project 和明确选定的订阅，允许读取认证上下文、Support 分类与工单、诊断失败，以及提交每次经用户确认的 Support 工单；不允许修改 Azure 资源、RBAC 或在其他 Project 使用。

持续授权的 purpose 应描述上述长期工作范围，不写成“仅本工单”“最多一次创建”或“只重试一次”；否则即使模式是 standing，也不能宣称可用于之后的工单。已存在有效、匹配用途的 standing grant 时直接复用，不再请求一次性 grant。用户明确要求一次性时才申请 once。

所有者批准后核验返回的实际 grant：`mode == "standing"`、`status == "active"`、audience 为当前 Project、purpose 覆盖目标订阅和 Support 工作流、`expiresAt` 未设置。所有者若选择 once，明确说明尚未取得所要求的持续授权，不把它升级或伪装成 standing。原有 pending ask 的冻结 purpose 仅覆盖一次尝试时，先请所有者拒绝/处理旧申请，再在原请求来源发起长期用途的 standing 申请；不能修改原 ask、换来源绕过去重或替所有者审批。若旧申请的 purpose 已覆盖长期用途，所有者可选择 Always/持续允许，最终以返回 grant 为准。

持续 grant 不保证 Azure 登录令牌、凭据本身或 RBAC 永久有效；认证过期仍须重新认证，grant 被撤销或凭据失效时停止。每张新工单以及已确认正文、目标、分类、严重程度或联系人发生变化时，仍需用户确认。明确的参数拒绝不消耗 standing grant；在相同名称下确认未创建、定位并修正字段且用户重新确认后，可执行一次纠正尝试，不重新申请凭据授权。超时、网络中断或创建后 404 等不确定结果仍只恢复查询。

## 多租户身份发现与选择

即使没有 memory 映射，也先检查当前 Project 进程使用的 Azure CLI profile：已设置 `AZURE_CONFIG_DIR` 时沿用该目录，否则使用当前项目 HOME 下的 `$HOME/.azure`。使用下述 `account list` 核验已有登录，不读取 token/cache 文件，不因缺少名为 Support 的环境或 profile 而拒绝。

再从当前 Project memory 搜索目标 subscription ID、tenant ID、subscription 名称及 `azure-support-profile`，合并平台授权给当前 Project 的 resident profile。只接受本项目可用且获授权的来源；来自其他 Project、Personal scope、历史聊天正文或无法确认作用域的路径不进入候选集。平台提供的凭据入口按其既有认证工具使用，保持 secret 在工具/进程环境内，不将值放进命令、聊天、memory 或请求记录。只有原始凭据但尚未认证时，不能把 `account list` 为空当作无凭据；先使用本项目已授权的认证入口，再核验清单。

推荐的非敏感 memory 记录格式为：

```text
azure-support-profile tenantId=<tenant-uuid> subscriptionId=<subscription-uuid> profile=<label> configDir=<approved-absolute-path>
```

Memory 是路由提示。每个候选都必须使用其 `AZURE_CONFIG_DIR` 实时执行：

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh account list --all --query '[].{tenant:tenantId,subscription:id,name:name,state:state,cloud:environmentName,isDefault:isDefault}' --output json
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
bash skills/azure-support-ticket/scripts/azure_cli.sh login --tenant <tenant-uuid> --use-device-code --output none
```

只向操作者显示 Microsoft 设备登录 URL、一次性代码和等待状态。授权完成后：

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh account set --subscription <subscription-uuid>
bash skills/azure-support-ticket/scripts/azure_cli.sh account show --subscription <subscription-uuid> --query '{subscription:id,name:name,tenant:tenantId,cloud:environmentName,state:state}' --output json
```

目标 subscription 不可见、tenant 不匹配、状态不是 `Enabled` 或 Conditional Access 拒绝时停止。保留脱敏后的错误代码和 request/correlation ID；不刷新代码来绕过策略，不回退到无 `--tenant` 的登录。

登录核验成功后，重新生成完整 tenant/subscription 清单并展示选中项。仅保存上述非敏感 memory 映射；保存后按 subscription ID 回读。回读结果必须来自当前 Project scope 且内容匹配，否则报告“profile 可用，但 Project memory 映射未持久化”，后续 conversation 需要显式选择该 profile。

## 工具与上下文

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh version --output json
bash skills/azure-support-ticket/scripts/azure_cli.sh extension show --name support --query version --output tsv
bash skills/azure-support-ticket/scripts/azure_cli.sh account show --subscription <subscription-uuid> --query '{subscription:id,tenant:tenantId,cloud:environmentName,state:state}' --output json
```

上述命令与后续每条 Azure 命令都使用同一个选中 profile 和统一入口。工具版本由 bundle 内共享配置及安装器维护；工具异常回到“工具初始化与统一入口”处理，不在写入步骤动态安装。验证订阅为 `Enabled`、cloud 为 `AzureCloud`、tenant/subscription 与批准目标一致。

在读取分类前还要核验：

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh provider show --subscription <subscription-uuid> --namespace Microsoft.Support --query registrationState --output tsv
bash skills/azure-support-ticket/scripts/azure_cli.sh support services list --subscription <subscription-uuid> --query 'length(@)' --output tsv
```

身份返回的有效 actions 必须覆盖 `Microsoft.Support/supportTickets/write`。订阅和可读祖先 scope 的 deny assignment 不得覆盖该 action；祖先 deny 无读取权限时展示为未知，并说明创建调用可能是第一项权威验证。登录成功或服务列表可读本身不证明拥有写权限或有效支持计划。

## 服务与问题分类

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh support services list --subscription <subscription-uuid> --output json
bash skills/azure-support-ticket/scripts/azure_cli.sh support services problem-classifications list --subscription <subscription-uuid> --service-name <service-name-from-list> --output json
```

从服务结果中选取 `name` 和完整 `id`，按显示名筛选但不猜测 ID。分类结果中保存完整 `id`；其 `/providers/Microsoft.Support/services/<service-name>/problemClassifications/<classification-name>` 路径必须属于所选服务。创建 CLI 的 `--problem-classification` 使用完整分类 ID，不额外拼接未经验证的 service ID。

## 执行参数

优先使用执行工具的 argv 数组接口，设置 `shell=False`。如果工具只支持 shell，将每个动态参数分别做 POSIX shell 引用（可使用 Python `shlex.quote`）；引号、换行、反引号和 `$()` 均作为字面数据传递。不要用字符串替换拼接未经引用的命令。用户字段不能增加选项或改变固定命令前缀。

完整复核记录和执行请求仅存放在当前 Project 的受控存储（目录 `0700`、文件 `0600`），联系人资料不写入 Git、公共日志或其他 Scope。下述固定 `--query` 将读取结果限定为恢复和确认所需字段。

## 提交前读取

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh support in-subscription tickets show --subscription <subscription-uuid> --ticket-name <stable-ticket-name> --query '{id:id,name:name,caseId:supportTicketId,title:title,description:description,classification:problemClassificationId,status:status,severity:severity,created:createdDate}' --output json
```

Support extension 的该命令会将 REST `properties` 展平，上述查询使用 CLI 顶层字段。当前项目升级 extension 时核对输出契约；任何必需字段缺失都不能当作成功。

- 已存在：核对请求记录中的资源 ID、标题、描述和分类；匹配则复用，不创建。冲突则停止。
- 明确的 Azure 资源不存在错误（404/`ResourceNotFound`，且指向该 ticket）：允许进入已确认的创建流程。
- 403、401、订阅不存在、网络超时或其他错误：停止；这些错误不证明 ticket 不存在。

在当前 Project 中按 subscription 和稳定 ticket 名称串行化提交，避免两个请求同时通过“不存在”检查后重复创建。使用平台已有请求锁，或当前项目持久存储上的进程间文件锁（如 `flock`），覆盖提交前读取、创建和提交后核验全过程；同项目所有会话必须使用同一锁路径。没有可靠锁时停在待提交并报告并发保护缺失，不能凭“当前只有一个任务”跳过。请求记录包含当前版本、确认状态和提交状态；确认不跨版本复用，恢复任务只读取原名称。锁和记录不要求另建执行环境。

## 技术、计费和订阅管理创建

```text
bash skills/azure-support-ticket/scripts/azure_cli.sh support in-subscription tickets create --subscription <subscription-uuid> --ticket-name <stable-ticket-name> --title <confirmed-title> --description <confirmed-description> --problem-classification <verified-full-classification-id> --severity <confirmed-severity> --contact-first-name <first-name> --contact-last-name <last-name> --contact-email <email> --contact-country <country> --contact-language <language> --contact-timezone <api-timezone> --contact-method <email-or-phone> --advanced-diagnostic-consent <Yes-or-No> --require-24-by-7-response <true-or-false> --query '{id:id,name:name,caseId:supportTicketId,status:status,severity:severity,created:createdDate}' --output json
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

### 参数错误证据与纠正

创建调用的 stderr 必须先捕获到当前 Project 的私有临时文件（目录 `0700`、文件 `0600`），不要在工具输出中直接显示或使用 `--debug` 暴露完整请求。CLI 退出后保留退出码；提交进程释放该请求的文件锁后，用同一 Skill 的 helper 将脱敏证据合并到稳定名称对应的请求记录：

```text
python3 skills/azure-support-ticket/scripts/capture_azure_error.py --record <controlled-request-record.json> --error-file <private-stderr-file> --attempt-time <recorded-attempt-time>
```

helper 只处理本地错误证据，不调用 Azure、不加载凭据、不创建或重试工单。它自行获取同一 `.json.lock` 文件锁，并校验 `attemptTime` 仍属于本次调用，避免旧错误覆盖新尝试；不要在持有该锁的进程内等待这个独立命令。成功持久化并回读 `lastError` 后清理原始 stderr 临时文件，异常时将原始文件保留在受控存储用于恢复，不复制到聊天或 Git。错误记录应包含 `code`、脱敏 `message`、`target`、嵌套 `details`（若服务提供）及适用的 request/correlation ID，而不是只保存 `InvalidParameterValue`。

若历史记录只有错误码，先检查该请求既有的私有日志/响应证据；缺少详细信息时明确标记 `detailsMissing`，不能猜测时区、国家、语言或资源字段，也不能再创建一次仅为了获得错误。可在有效且用途匹配的 standing grant 下执行范围内的只读诊断；缺少该授权才请求所有者批准持续用途。

确认服务指出的字段后，按当前 CLI/API 官方契约核验并展示具体修正：联系人 country 使用 ISO 3166-1 alpha-3（如 `CHN`），支持语言使用 API 列表（如 `zh-hans`），时区使用 Windows 时区名称（如 `China Standard Time`）；这三个示例本身是有效格式，不应仅凭错误码修改。检查目标 tenant/subscription、完整服务/分类路径、技术资源 ID 和 UTC 开始时间，不伪造有效值。

明确的请求拒绝、相同名称的权威查重结果及纠正内容均已记录且用户重新确认后，才允许下一次创建；不确定结果按前述恢复流程停止。403 指向权限或策略限制，不自动扩大 RBAC；技术支持计划不满足指向订阅资格，分类失效则重新查询并重新确认。不要静默切换订阅、严重程度或诊断授权。
