# 专用环境的提交与恢复

仅在 `SKILL.md` 的执行隔离、目标检查和确认条件全部满足后执行写入。以下命令中的占位符必须替换成核验值；全部 Azure 调用显式指定 subscription。身份由运营方预配置，Skill 不执行登录或角色分配。

## 工具与上下文

```text
az version --output json
az extension show --name support --query version --output tsv
az account show --subscription <subscription-uuid> --query '{subscription:id,tenant:tenantId,cloud:environmentName,state:state}' --output json
```

需要 Support extension 的 `in-subscription tickets` 命令。Azure CLI 文档要求 CLI 2.57.0 或更高；具体安装版本由专用执行器固定和维护。验证订阅为 `Enabled`、cloud 为 `AzureCloud`、tenant/subscription 与批准目标一致。缺少 extension 时让运营方安装，避免隐式动态安装。

## 服务与问题分类

```text
az support services list --subscription <subscription-uuid> --output json
az support services problem-classifications list --subscription <subscription-uuid> --service-name <service-name-from-list> --output json
```

从服务结果中选取 `name` 和完整 `id`，按显示名筛选但不猜测 ID。分类结果中保存完整 `id`；其 `/providers/Microsoft.Support/services/<service-name>/problemClassifications/<classification-name>` 路径必须属于所选服务。创建 CLI 的 `--problem-classification` 使用完整分类 ID，不额外拼接未经验证的 service ID。

## 执行参数

优先使用执行工具的 argv 数组接口，设置 `shell=False`。如果工具只支持 shell，将每个动态参数分别做 POSIX shell 引用（可使用 Python `shlex.quote`）；引号、换行、反引号和 `$()` 均作为字面数据传递。不要用字符串替换拼接未经引用的命令。用户字段不能增加选项或改变固定命令前缀。

完整复核记录和执行请求仅存放在专用环境的受控存储，联系人资料不写入 Git、公共日志或诊断 Scope。下述固定 `--query` 将读取结果限定为恢复和确认所需字段。

## 提交前读取

```text
az support in-subscription tickets show --subscription <subscription-uuid> --ticket-name <stable-ticket-name> --query '{id:id,name:name,caseId:supportTicketId,title:title,description:description,classification:problemClassificationId,status:status,severity:severity,created:createdDate}' --output json
```

Support extension 的该命令会将 REST `properties` 展平，上述查询使用 CLI 顶层字段。专用环境升级 extension 时核对输出契约；任何必需字段缺失都不能当作成功。

- 已存在：核对请求记录中的资源 ID、标题、描述和分类；匹配则复用，不创建。冲突则停止。
- 明确的 Azure 资源不存在错误（404/`ResourceNotFound`，且指向该 ticket）：允许进入已确认的创建流程。
- 403、401、订阅不存在、网络超时或其他错误：停止；这些错误不证明 ticket 不存在。

专用执行器必须按 subscription 和稳定 ticket 名称串行化提交，避免两个请求同时通过“不存在”检查后重复创建。请求记录包含当前版本、确认状态和提交状态；确认不跨版本复用，恢复任务只读取原名称。

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
