# AKS bound-readonly 候选：验证范围与发布门禁

## 来源与运行基线

技能源码：干净 worktree `skills-aks-runtime`，分支 `feature/aks-readonly-runtime`，基于本仓库 `origin/main b668e72408ec270fe468f72bb7dee9f2e1b574c8`。
原 topic checkout 与未提交 `docs/` 保留；没有使用脏 `qm/` 或其他 topic 作为发布源。

本次通过只读 SSH 检查实际 AgentOps 三组件，而非拿本机 rehearsal 推断生产：

| 组件 | 运行容器 `.Image` / `.Config.Image` Engine 坐标 |
| --- | --- |
| Core | `sha256:b4898f8461ea594bab4d477d3f741b48ab152f58e5d60ea031f9d5c245f26ea0` |
| Web UI | `sha256:aa38d2f1d48ef68afee722b48f8acd7ae4bcd4326847e7efdb6a0f6a7d99920d` |
| Portal | `sha256:f06766973e8b403a21000853a5cc5e4b4446d0cc1986af46b6f884fe21b5a03e` |

三者声明 source/revision `4420b2416507cb00c34c831839a793b3e1a0aeff`，release operation `qm-v015-h15-20261027`。
匹配的干净平台 worktree 是 `upgrade-v015-20261023`，分支 `upgrade/qm-v0.1.15-20261023`，HEAD 为同一 source。
其 lineage 包含 `ec1d59e7`（v0.1.15 集成）及其后的 `4420b241`（harness contract 修复）。
容器 image.version 仍写 v0.1.14，与运行 release/source metadata 不一致，不能据此推断实际仍为 v0.1.14。
未观测到更晚的 source 标签；这不是对未标注内容的独立审计。
本次远端镜像 `RepoDigests` 为空，因此上表**只作为 Engine/回退坐标，不冒称已核验 registry manifest digest**。
若涉及后续平台镜像发布，仍须按现有 release record 重新核验不可变 registry digests 与 hotfix。

本次没有修改 Core/Web/Portal，没有构建或替换平台/Sandbox 镜像，也没有生产 import/sync、Azure 登录或集群调用。
这里只复用已部署 Core 的 native credentials 协议，不调用已移除的 `/v1/keychain/use`。

## 已验证的源码/工具范围

- 运行时 fixture：个人/共享授权、用途/范围/期限/once 拒绝、Core 验证后的 scope 绑定、Azure token RS256/JWKS/issuer/audience/lifetime 与 tid/oid 绑定、错误脱敏、端点/CA/版本检查、固定输出及临时文件清理。
- 固定工具安装 fixture：哈希、并发锁、路径/symlink、修复、跨 lock 更新及失败回退。
- 真实工具安装：在本地 VM 隔离的非凭据 HOME 安装锁定 Azure CLI 2.89.1、kubectl 1.33.5/1.34.1/1.35.0、kubelogin 0.2.22；检查完整性、实际版本与入口可加载。
- 原生协议可选测试：加载匹配的真实 Core route/keychain/签名验证代码，用 in-memory store 与 localhost HTTP，测试真实 header、保存/请求/授权响应、压缩 capability、失效 token 拒绝。
- 原生 staging：执行真实 `prepareExecutionFiles/finish`，仅在 Sandbox 系统边界把 `/tmp` 映射到独立 fixture 目录，避免扫到宿主真实暂存凭据；验证 JSON 文件暂存、HOME 结构、排除 sibling kubeconfig 与整目录清理。
- 真实 ingest/in-memory 协调：一个 AKS 入口、显式资产清单、没有共享维护文件；验证不同 Scope 的完整保留集、漏选会归档、Pack-wide Sync 影响范围，以及旧 ref/选择集回滚。
- 独立审查发现的 CWD 导入、Scope 冒充、A handle/B 身份串用、锁文件升级失败已补修复和回归；保留同 UID 边界限制，不声称本地 Python guard 是隔离沙箱。

最终本地复验：runtime/toolchain/native contract **31 项通过、无 skip**（显式提供匹配 Core），上游同步 **26 项通过**，既有 guard/安装器 **62 项通过**。
类型检查、Ruff lint/format、MJS/YAML 格式与语法、网络拉取的上游可复现校验全部通过。
真实 SDK 额外通过带伪造 `azure/cli.py` 项目 CWD 的 isolated invocation 与 AKS get-credentials flags 可加载检查，未执行 Azure 登录/资源读取。
独立安全审查与规格审查的问题已修复，最终 source closeout 未发现剩余阻塞项；审查不替代下面未执行的门禁。

上述 fixture 不使用真实 Azure token、Keychain 记录、Project 数据或集群；不等于真实 AKS E2E。

### 可重复命令

```bash
python3 .github/aks-upstream/validate_aks_bundle.py
python3 .github/aks-upstream/sync_aks_upstream.py --check
PYTHONDONTWRITEBYTECODE=1 /path/to/dev-python -m unittest discover -s .github/aks-upstream/tests -v
PYTHONDONTWRITEBYTECODE=1 /path/to/dev-python -m unittest discover -s .github/aks-upstream/runtime-tests -v
PYTHONDONTWRITEBYTECODE=1 /path/to/dev-python -m unittest discover -s azure-ops/tests -v
QM_SOURCE_FOR_AKS_TESTS=/path/to/matched-clean-core PYTHONDONTWRITEBYTECODE=1 /path/to/dev-python -m unittest discover -s .github/aks-upstream/runtime-tests -p test_native_contract.py -v
node .github/aks-upstream/check_qm_ingest.mjs /path/to/matched-clean-core
```

普通 CI 未提供 `QM_SOURCE_FOR_AKS_TESTS` 时会明确 skip 原生 Core contract 测试，不能当作此项通过。
该可选测试当前固定 Core commit `4420b241`；升级平台时须重新审查并更新其基线，不以任意最新源码冒充部署兼容。
开发依赖见同目录 requirements。上游同步 PR job 自身也运行 runtime、类型、lint 和格式检查，不依赖 token 创建的 PR 再触发工作流。

## 尚未通过的上线门禁

1. 缺少本次明确批准的测试 Azure 身份与 tenant/subscription/RG/cluster/namespace，尚未执行真实 device-code → Keychain → owner approval → native execute → AKS workflow。
2. 尚未在本地 VM 的实际 Project/Scope 中导入候选并验证真实健康、日志、持久化授权/版本、撤销、网络失败、数据与回滚。
3. 尚未同步生产。不得因为上述源码/工具检查通过，就将候选称为 AgentOps-ready。

测试身份应在 Azure 和 Kubernetes 层尽量只读。把个人 Owner/Contributor 登录共享出去，不会因 purpose 写“只读”而剥夺其底层写权限。
如需不可绕过的只读保证，必须配置宿主仅允许该凭据用于固定 adapter 的执行策略，或采用专用只读身份/隔离执行服务；本仓库没有悄悄修改平台策略。

首期只支持 Linux、AzureCloud、同 tenant managed Entra AKS、本入口绑定 tenant/object ID 的原生 file login，以及个人 owner 或共享 standing 授权。
不支持共享 once、环境 client-secret、local-account/admin kubeconfig、跨 tenant AAD、主权云、Secrets/日志/修复。

真正发布前保存目标 Pack 的旧 ref、配置、每个 Scope 的完整选择集和实际 Skill ID/版本；`b668e72` 只是仓库旧知识版本，不是推断出的生产 Pack ref。
按维护指南先本地后生产，完整保留集协调，避免误归档同 Pack 旧技能；Pack-wide Sync、共享资产和回滚都必须按真实平台结果验收。
