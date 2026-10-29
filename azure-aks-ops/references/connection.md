# 连接 AKS：原生凭据、临时 kubeconfig、固定只读动作

## 支持的执行面

需要宿主真实提供 `execute.credentials` 文件凭据暂存与清理、当前凭据目录、Keychain API 和同一 Sandbox 的后台任务。
已核对的 Core 基线为 `4420b241`（v0.1.15 集成）；该版本已移除 `/v1/keychain/use`。
本 bundle 不 source 返回 secret 的初始化脚本，不使用环境中的旧 `$HOME/.azure` 作为授权证据。
宿主没有这些接口时明确报告不兼容，保留 knowledge-only，不自行实现浏览器 token 复制或 OBO。

个人 owner：在其 personal 会话，选择目录提供的文件 execution handle。
共享会话：平台必须在每次 execute 校验成员和有效 grant；本地 runtime 再校验 grant 的目标、namespace、用途、有效期。
个人登录不会自动分享给 Project。共享 v1 只支持 active standing grant；不支持 once-grant，尤其不要先加载它再发现不支持。
若用户不愿持续共享，使用个人会话或保持证据分析，不自动申请 standing。

本地检查不是不可绕过安全边界：同 UID 的其他进程可能读取临时文件；需要额外隔离的部署应使用隔离执行环境。
不宣称 Project 共享身份就是当前聊天者个人 Azure RBAC。网页登录不构成 Azure 登录。

## 1. 工具检查/安装（不带任何 credentials）

先通过 skill read 得到本 bundle 的绝对目录，记为 `<skill-dir>`，固定同一宿主 `sandbox_id`。
在该执行环境的持久 HOME 中运行：

```text
python3.11 -I <skill-dir>/scripts/aks_toolchain.py ensure
```

通过后台工具等待安装完成，必要时继续 poll，不把中断当成安装成功。
成功返回 `root`、`azure_python`、`kubelogin` 和各版本 `kubectl` 的绝对路径；这都是非敏感工具路径。
后续每次命令显式使用这些路径，因为 `execute.credentials` 会切换到临时 HOME。
不要把工具复制到临时凭据 HOME，不写 `.bashrc`，不依赖上次 execute 的 export。

检查固定 root：

```text
python3.11 -I <skill-dir>/scripts/aks_toolchain.py check --root <toolchain-root>
```

安装依赖 Linux amd64/arm64、Python 3.11 + venv、pip 出网。
Azure CLI 固定 2.89.1；kubectl 固定 1.33.5、1.34.1、1.35.0；kubelogin 固定 0.2.22。
所有下载由随附锁文件校验，安装有锁和失败回退，不安装 Support 扩展、不修改其他 Azure 工具链。
客户端根据 ARM 所报 API server minor 选择，并在连接后复核实际 server version；无 ±1 minor 匹配即停止，不能下载 latest。

## 2. 发现登录或发起 device-code 登录

先读取当前宿主原生凭据目录。使用可见 execution handle，不把 credential ID 当成 handle，也不按聊天姓名猜 Azure 账号。
`GET /v1/keychain/credentials` 只返回当前 owner 的元数据；共享来源以当前 catalog/grant 为准，不搜索其他 Project 的缓存。

没有匹配 Azure 文件登录时，先确认 tenant UUID，再在同一 Sandbox、不带 credentials 的后台任务运行：

```text
<azure_python> -I <skill-dir>/scripts/aks_auth.py login --tools-root <toolchain-root> --tenant <tenant-uuid>
```

脚本在全新临时 HOME 内执行固定的 `az login --tenant ... --use-device-code`。
只转达输出中的 verification URL、一次性 code 和进度，不停止正在等待用户批准的后台任务。
登录成功后脚本核验 tenant/cloud，在进程内读取身份 claims，直接通过 native Keychain 保存允许的 `.azure` 文件。
服务 slot 包含 tenant 和 Azure object ID，避免重新登录另一个 Azure 账号时无意继承原账号的共享 grants。
运行器只接受这种绑定的 file handle；通用 Azure 登录、环境 secret 或旧 resident cache 必须通过本入口重新登录，不凭名称猜身份。
仅保存 `azureProfile.json` 与 `msal_token_cache.json`；不保存 cloud/config 覆盖、插件或可执行反序列化的 HTTP 二进制缓存。
只输出 handle/credential ID、非敏感身份与订阅清单；token、MSAL cache 不进入对话或日志。
临时 HOME 无论成功失败都会清理；保存失败需重新登录，不复用残留 profile。不要让用户上传 kubeconfig 或 secret。

这不会自动授权共享 Project。个人 owner 可在个人会话使用；共享会话继续下一节。

## 3. 共享用途申请与核验

先向用户解释：standing 授权让当前共享会话/Project 成员在批准范围内复用该 Azure 身份，直到撤销或授权到期。
取得共享意愿后，通过批准的执行环境运行（此步不加载 credential）：

```text
<azure_python> -I <skill-dir>/scripts/aks_auth.py request-access --credential-id <可见credential-ID> --tenant <tenant-uuid> --subscription <subscription-uuid> --resource-group <rg> --cluster <cluster> --namespace <ns> --cluster-read
```

按需重复 `--namespace`；未批准集群级节点读取时省略 `--cluster-read`。权限范围应最小化。
脚本通过 `/v1/keychain/asks` 提交结构化 purpose，所有者仍须在平台原生卡片审批；脚本不会批准自己。
purpose 是固定 JSON，包含 `kind: azure-aks-readonly/v1`、tenant、subscription、resourceGroup、clusterName、namespaces、clusterRead。
Support、数据刷新、其他集群或自由文本用途的旧 grant 不会被静默扩大。
平台若返回不同冻结 purpose/mode 的旧 ask，停止并让 owner 先处理旧申请；不改 grant、不换来源绕过去重。

批准后重新读取 `/v1/keychain/grants` 和当前 execution catalog：

- status 必须 active、mode 为 standing、未过期；audienceScopeId 精确匹配当前机器 scope ID。
- 目标及 namespace 必须在 purpose 范围内；clusterRead 只能按批准值使用。
- 使用平台实际 scope 标识（例如 `group:...`），不要从 UI 的 Project 名称猜出 `project:...`。
- 记录 actual grant ID 与 execution handle。memory 只可存非敏感路由提示，不能作为审批证明。

## 4. 每次读取用一个原生 credential execution

每次调用下面的固定命令时，**同一个 execute** 必须填写 `credentials: [所选handle]`，按当前 catalog 填写 execution scope 和 sandbox_id。
预计超过前台时限时用 `execute(background:true, credentials:[...])` 一次启动，后续仅 poll/watch 该 process_id；不要重启第二个诊断进程。
本地脚本先让 Core API 验证同一个 capability token，再解码其 actor/scope claims，拒绝用 `--scope` 冒充另一会话。
它取得实际 ARM token，通过固定 Microsoft tenant JWKS 核验 RS256 签名、issuer、audience、有效期，再核验 tenant/object ID 与 handle 中的固定身份一致，避免把 A 的 grant 套到 B 的缓存上。
签名公钥读取失败也停止，不把仅解码 JWT payload 当成身份验证，不使用 profile 自定义 cloud endpoint。
不要单独加载凭据后再在另一条不带 credentials 的命令复用临时路径；每次执行都重新通过原生授权。
按宿主实际工具 schema 提供 command，参数逐项传递/引用，不把用户文本拼成 shell 语法。示意命令：

```text
<azure_python> -I <skill-dir>/scripts/aks_runtime.py --tools-root <toolchain-root> --handle <execution-handle> --scope <当前scope-ID> --tenant <tenant-uuid> --subscription <subscription-uuid> --resource-group <rg> --cluster <cluster> --namespace <ns> --cluster-read status
```

共享时追加 `--grant <实际grant-ID>`。个人 owner 仅在其 personal scope 不带 grant。
以下 action 由运行器构造固定 argv，不接受额外 az/kubectl 参数：

| Action | 范围 |
| --- | --- |
| `accounts` | 查看当前 tenant 的本地订阅元数据；仅需 tenant；共享时只返回 purpose 允许的 subscription |
| `cluster` | ARM 中指定集群的 ID、版本、provisioning/power state |
| `nodepools` | 指定集群的 node pool 概要 |
| `connect` | 临时获取 user kubeconfig 并验证受信 endpoint/CA、Entra 身份与 API version |
| `nodes` | 需要 `--cluster-read` 的 Node Ready/version 投影 |
| `pods` / `events` | 需要显式 `--namespace` 的有限状态投影 |
| `status` | ARM + 显式选择的 nodes/namespace 证据；没有选择时只查 ARM，不擅自全群读取 |

参数 `<...>` 均为占位符，不可原样执行。诊断前不要把仅集群名当成唯一资源；缺 subscription/RG 时先解析可信映射或询问用户。

## 5. 自动获取连接配置与权限/网络

运行器只接受 native 暂存的 `/tmp/qm-credentials.<随机值>/home/.azure`，拒绝 resident profile 和任意外部 AZURE_CONFIG_DIR。
它实时核验 tenant/subscription/Enabled/AzureCloud 和完整 AKS resource ID，再在该次执行的 private 目录内获取 user kubeconfig。
不使用 `--admin`，不覆盖默认 config，不执行下载 kubeconfig 中的 exec/auth-provider，不接受外部 CA 文件、代理覆盖或 insecure TLS。
核对 server 与 ARM fqdn/privateFqdn 一致后，用其 CA 重建固定、绝对路径的 kubelogin Azure CLI provider。
仅支持同 tenant 的 managed Entra AKS；本地账户、静态证书、主权云和不兼容版本明确拒绝。

Azure ARM Reader、获取 user kubeconfig 的 action、Kubernetes 数据面授权是三个条件，任何一个都不能由 Skill 自行授予。
运营方按需配置 `Microsoft.ContainerService/managedClusters/listClusterUserCredential/action` 与 namespace/Node 最小读取权限。
`kubectl version` 还需要可访问 server version。Private AKS 必须有实际 DNS/私网连通性；管理员为集群 API hostname 配置出网策略，不能假定固定 ARM 域名白名单足够。

kubeconfig 和发现缓存位于 native execution 目录中、HOME 之外，不写回 Keychain；执行结束删除，原生进程回收再清理整个暂存目录。
Keychain 仅保留两个允许的 Azure JSON 登录文件；每次执行清理 CLI 产生的配置、插件或 HTTP 二进制缓存，防止下次执行加载不可信覆盖。
owner 刷新由平台回写，grant 的刷新不持久化。授权撤销/过期、401、403、网络失败时停止，不尝试替换身份或提权。

## 输出与故障边界

Kubernetes Pod/Event API 会返回完整对象，包括 spec/env 或事件 message；这些字段会短暂存在 CLI/Python 本地内存中。
授权读取 Pod/Event 对象前应向操作者说明这一点。它们不进入模型输出、不写日志/持久化；输出仅含固定状态投影、最多 200 项和 truncation 标记。
Secrets 资源和日志接口根本不请求；这不等于 Kubernetes 支持对 Pod 对象做字段级 RBAC。
原始 stderr 不进入模型；返回分层错误码，如 TOOLCHAIN、KEYCHAIN/GRANT、IDENTITY/TARGET、ENDPOINT、AUTH、FORBIDDEN、NETWORK、TIMEOUT、OUTPUT_LIMIT。
`status` 可保留已读 ARM/其他证据并将失败项标 unavailable；不将缺失证据解释为健康。
Azure token只通过子进程内部认证，平台 capability token不传入 az/kubectl/kubelogin 子进程。
不执行任何上游 collector、任意 shell 管道、MCP fallback 或修复命令。
