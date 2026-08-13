# Azure Support 工单

当用户提出要起草、开启、创建或提交 Azure 技术支持工单时，使用本流程。

该 QM Skill 会准备完整工单，但不会提交。通用 Local Sandbox 无法可靠地强制跨轮次写入确认，也无法保证 Support 写入身份对任意进程不可访问。用户必须在本 Sandbox 之外，通过 Azure Portal 或由运维方管理的流程审核并提交草稿。

## 收集用户提供事实

必须收集：

- 简明的问题描述与已观察到的症状。
- 受影响资源名称或完整资源 ID。
- 事件开始与结束时间，或明确说明“仍在进行中”。
- 联系邮箱。
- 请求严重级别；若省略则默认 B/Moderate。
- 若为 A/Critical，需提供带国家区号的电话号码。

资源组可来自会话内容或完整资源 ID。不得臆造诊断结论、联系方式、影响范围或时间戳。若技术资源 ID 无法低成本解析，可作为可选项。

## 通过只读查询解析分类

不要猜测 Service 或 Problem Classification ID。必须通过 guard 查询：

```text
support services list --query [?contains(displayName,'<service keyword>')].{name:name,displayName:displayName} -o json
```

```text
support services problem-classifications list --service-name <service-name> -o json
```

在草稿中展示所选显示名称和完整分类 ID，供用户复核。

## 严重级别与联系方式

记录用户请求的严重级别、业务影响、首选语言、联系方法、支持时段、邮箱以及适用时的电话。可用的严重级别与联系方式组合取决于当前 Azure 支持计划、区域和服务。不要把硬编码矩阵当作权威依据，也不要静默下调用户请求。应将所选项标记为需在 Azure Portal 或批准的提交流程中核验。

## 草稿

仅使用已确认事实构建标题与描述：

```text
标题: [服务或影响] <resource> <symptom>，请求协助

== 问题 ==
- 受影响资源: <resource ID or name and resource group>
- 事件时间: <UTC range or ongoing>
- 业务影响: <user-provided impact or N/A>

== 现象与观察 ==
<user-provided symptoms and verified diagnostic facts>

== 所需协助 ==
<specific request to Microsoft Support>
```

在人类可读草稿后补充以下提交详情：

```text
订阅: <subscription name; ID only when needed by the submitter>
服务: <verified service display name>
问题分类: <verified display name and full ARM ID>
严重级别: <selected severity>
联系方式: <method, language, email, and phone when required>
技术资源: <verified resource ID when available>
```

不要构造或执行 `support in-subscription tickets create`。当用户要求继续时，需明确说明该 QM Skill 有意禁用自动 Support 提交，并引导用户使用 Azure Portal 的 Help + support 流程或组织批准的工单自动化流程。若用户要求保留草稿，应将其保存在当前 Scope 以便授权运维人员使用。
