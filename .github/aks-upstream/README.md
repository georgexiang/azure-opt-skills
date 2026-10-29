# AKS 上游跟踪与 Git Pack 发布

## 发布模型与目录

```text
microsoft/skills main
  → 固定 commit 的 Git blobs（不执行、不 checkout 上游）
  → 显式选择 + 本地转换 + 自包含 azure-aks-ops bundle
  → 更新 PR → 独立审查 → 本地 VM/Scope 验收
  → 本仓库已验证 commit → 现有 Git Pack → 目标 Scope
```

这是 Git Pack 内的新增 bundle，不是 npm/PyPI 包，不需要新注册微软仓库。
原知识 bundle 已发布到仓库；当前新增 bound-readonly 执行器候选，仍未完成真实 AKS 的本地平台验收或生产同步，不能宣称 AgentOps-ready。
当前修改在干净的 `feature/aks-readonly-runtime` worktree 完成，来源是本技能仓库的
`origin/main`（基线 `b668e72`）；不使用原 topic checkout，也不覆盖其未提交的 `docs/`。
平台发布仍须另行确认运行容器与 release/hotfix lineage；本机 rehearsal 容器不作为生产版本证据。
本次实际只读基线、工具验证与未通过的上线门禁见 [RUNTIME-VALIDATION.md](RUNTIME-VALIDATION.md)。

目录职责：

- `azure-aks-ops/`：实际发布的自包含 bundle。人工维护入口和行为用例，生成 reference、license 和 manifest。
- `.github/aks-upstream/`：维护工具、单元测试、sources、lock、开发依赖与本文。
- `.github/workflows/`：验证与更新 PR 的自动化。

**维护文件必须放在 `.github/`。** QM `collectSharedBundle` 会收集不属于任何技能的普通根目录文件；
将同步脚本放在根目录 `scripts/`，会使它们意外成为所有技能的共享运行时资产。
`.github/` 由该导入器的元数据规则排除，不依赖管理员额外配置 exclude。

## 所有权、安全与依赖

不要手改生成的 `references/upstream/`、`LICENSES/microsoft-aks-skills.txt`、`UPSTREAM.json` 或 lock。
相同输入必须生成相同字节，`--check` 检测漂移和多余文件。本地适配集中在同步器与入口维护，
转换版本记录在 lock/manifest；首期没有额外 patch 引擎。将来需要补丁时须实现确定性应用与失配即失败测试。

上游是参考数据，不是宿主执行授权。入口 frontmatter 移除并改名为 `guide.md`，防止嵌套 Skill 被发现；
shell source 转为带 fence 的 `.sh.md`、mode 0644，不安装 collector、hooks 或 MCP。
不修改既有 guard，不依赖其他 bundle 的认证或安装器。实时读取仅由本地 `scripts/aks_*.py` 固定接口提供，
需经过原生凭据、目标/身份核验；绝不因用户同意而运行上游命令或开放修复。运行流程见 bundle 内 `references/connection.md`。
`runtime-assets.json` 显式列出发布资产；Python 单元测试仍放在 `.github/`，不会进入运行时 bundle。

同步在缺失所选技能、本地引用断开、未选择的跨技能文件依赖、新资产类型、symlink/submodule、
非 UTF-8/二进制和过大文件时失败。Markdown 支持普通内联/引用链接、空白、标题与尖括号目标；
不支持的内联目标语法明确失败，不静默跳过。新依赖或新格式须人工适配并补测试。
生成错误不会替换旧产物；磁盘写入为逐文件原子替换，不是整包事务，写入中断后需重新生成验证。

上游兄弟技能 inventory 和插件元数据 blob 变化进入 lock/PR 供审查，但不自动进入 bundle。
新增能力必须先调整 allowlist、依赖、能力契约和测试。保留对应插件原始 MIT 许可证；
原始与生成文件分别记录 SHA-256。每次 PR 人工审查许可差异，不能凭根仓库许可推断其他内容许可。

## 本地维护

从仓库根目录执行以下命令；只生成/校验文件，不调用 Azure/Kubernetes 或平台 API。
运行依赖 Python 3.11+、Git；同步器只用标准库。

按锁定版本重建及校验：

```bash
python3 .github/aks-upstream/sync_aks_upstream.py
python3 .github/aks-upstream/sync_aks_upstream.py --check
python3 .github/aks-upstream/validate_aks_bundle.py
```

默认只消费 lock 内完整 commit。无 source 时创建临时 no-checkout clone 并获取锁定对象；
已有真实上游 clone 时可以离线运行：

```bash
python3 .github/aks-upstream/sync_aks_upstream.py --source /path/to/microsoft-skills --check
```

显式 source 读取 Git blobs 而非工作区，不联网核验 remote；调用者负责来源真实性。
浅 clone 必须先获取所需历史对象。

提议更新：

```bash
python3 .github/aks-upstream/sync_aks_upstream.py --update --summary /tmp/aks-update.md
```

无 source 时抓取上游 main；有 source 时使用其 HEAD，不自动 fetch。
若所选文件、inventory、许可和插件元数据未变，保留原 commit，避免无关 monorepo 更新噪声。
同步失败须审查结构/依赖变化，不要删掉失败检查继续发布。
首次导入或选择历史版本使用 `--ref <40位上游commit>`；尖括号是占位符，不可原样执行。
上游 SHA 不是本仓库 Git Pack ref。

测试与开发检查：

```bash
python3 -m unittest discover -s .github/aks-upstream/tests -v
python3 -m unittest discover -s azure-ops/tests -v
python3 -m venv /tmp/aks-dev
/tmp/aks-dev/bin/python -m pip install -r .github/aks-upstream/requirements.txt
/tmp/aks-dev/bin/ruff check .github/aks-upstream azure-aks-ops/scripts
/tmp/aks-dev/bin/ruff format --check .github/aks-upstream azure-aks-ops/scripts
/tmp/aks-dev/bin/mypy .github/aks-upstream/*.py azure-aks-ops/scripts/aks_*.py
PYTHONDONTWRITEBYTECODE=1 /tmp/aks-dev/bin/python -m unittest discover -s .github/aks-upstream/runtime-tests -v
```

可选：在 Node 24+ 和已有依赖的 QM 源码树旁，使用真实导入器做纯内存兼容检查：

```bash
node .github/aks-upstream/check_qm_ingest.mjs /path/to/qm-source
```

先暂存本次文件；该检查用 `git ls-files` 构造仓库快照，调用 QM 的 normalize/ingest/in-memory store，
核对仅一个 AKS 入口、全部预期文本/本地执行器资产、无共享维护文件，且 requiredCapabilities 仅包含显式的出网域名。
它不会连接平台、创建 Scope 或访问持久化数据；此外会用真实 Core 协调函数验证两个内存 Scope 的完整保留集、漏选归档、Pack-wide Sync 和旧 ref 回滚。
原生 HTTP/Keychain/staging 的本地协议检查可设置 `QM_SOURCE_FOR_AKS_TESTS=/path/to/matched-clean-core` 运行 runtime-tests。
该测试固定匹配的 Core commit，仅用 localhost、内存 store 和映射到私有 fixture 目录的 Sandbox 边界，不访问真实凭据。
未设置此变量时会明确 skip，不能当作此项通过；仍不替代实际 Project/Scope 和真实 AKS 验收。

[模型/平台行为场景](../../azure-aks-ops/tests/cases.md) 需另行执行；离线单元测试不证明 Scope 隔离或真实集群验收。

## 自动更新 PR

`sync-aks-upstream.yml` 每周和手动触发：生成 → 验证/回归 → 提出 PR。
Actions 使用完整 SHA；机器人仅提交明确的生成路径，不修改本地入口、测试或权限配置。
不自动 merge、不创建发布 tag、不调用平台；仓库需允许 Actions 创建 PR。默认 token 足够时不新增 PAT。

默认 `GITHUB_TOKEN` 创建的 PR 通常不会自动触发其他 Actions。同步 job 自身运行测试；
维护者仍须独立审查，并按需手动触发 `Validate AKS bundle` 或推入审核后的提交触发检查。
不要启用 `pull_request_target` 执行不可信代码。失败在 Actions 可见；团队需配置失败通知并安排处理责任人。

## 通过现有 Git Pack 发布

先审查命令、特权、工具、网络、依赖和许可证差异。生产前按 workspace AGENTS.md 核验实际
容器、feature/release、组件 digest 和 hotfix；涉及执行面/平台变更时，必须在匹配 release lineage
的工作树完成镜像与本地 VM 门禁。本次自包含 bundle 不要求重建平台或 Sandbox 镜像，但执行能力变化必须通过真实原生凭据与 AKS 的本地 VM 验收，不因 fixture 测试通过而豁免。

### 必须先核验导入器的协调语义

已检查的 QM 实现 `src/api/app-skills.ts` 有两个不能忽略的行为：

- `importSkillPack` 会归档同 Pack、同目标 Scope 中不在本次 `selected` 保留集内的技能。
  **在已有技能的 Scope 里只选 AKS，可能把 VM/Support/Case 归档。**
- `syncSkillPack`（Sync now）会协调该 Pack 所有已导入 Scope 的所有已发布技能，**不是单技能同步**。
  `subset`/`skillGlobs` 也是 Pack 级筛选，不能通过临时缩小它们来实现无副作用的单技能同步。

因此同一个 Pack 的发布必须保留完整选择集；若要求独立升级周期/严格的单组件切换，
应先核验平台支持同仓库 URL 的独立 AKS Pack 记录，或另行实现单技能协调能力。不要假定 UI 已支持。
独立 Pack 仍来自本仓库，不需要直接注册微软上游；导入前核验路径/名称不与现有技能冲突。

### 首次发布（复用现有 Pack）

1. 保存候选本仓库 commit，记录原 Pack ref、筛选配置、所有已导入 Scope 和各自完整的 Skill ID/版本/选择集。
   暂停或避免该 Pack 的自动同步，防止 ref 切换时其他 Scope 被更新。
2. 比较旧 ref 与候选的其他 bundle 和 shared bundle；任何额外变更都必须纳入审查和本地验收。
   本次 `azure-ops` 路由修改也属于显式候选范围，不应误认为 VM bundle 字节完全不变。
3. 在本地 VM 无 Azure/Kubernetes 凭据测试 Scope，用候选 ref 导入。新空 Scope 可只选 `azure-aks-ops`；
   已有技能的 Scope 必须使用“原完整选择集 ∪ azure-aks-ops”。确认 catalog 中每个保留项仍 eligible；
   缺失、冲突或筛选失败时停止，不用删掉保留项来绕过错误。
4. 按行为场景验收完整资产、版本、日志、数据、回滚。必须检查其他技能未被归档，未批准内容未改变，
   且 shared bundle 没有维护脚本。恢复候选后重验。
5. 本地通过后，将正式 Pack 固定到同一本仓库 commit；保留原 subset/skillGlobs 并追加 AKS。
   对每个获准目标 Scope 分别通过 Browse/import 明确传入该 Scope 的完整保留集加 AKS，
   不一次对选择集不同的多个 Scope 发送相同 selected。不要点会影响所有 Scope 的 Sync now。
6. 读取导入结果，要求 `archived` 为空，并确认变更仅限批准项。验证 `/azure-aks-ops`、实际版本和知识工作流。
   若有意外归档/更新，停止后续 Scope 操作并执行人工回滚，不能只看 HTTP 成功。

后续更新同样使用针对目标 Scope 的完整保留集 import；先证明其他内容未变化，再允许 skip 未变项。
若实际平台只提供 Pack-wide Sync，则必须验收并批准其所有受影响 Scope/技能；不能声称只同步 AKS。
同一 Pack 的 shared bundle 为 Pack 级更新，即便指定 Scope 导入也须审查其跨 Scope 影响。
UI/API 字段和新增技能加载门禁以实际部署版本核验。

### 回滚

先恢复旧 Pack ref，再用旧完整选择集协调已受影响的 Scope，读取并核验持久化版本和归档状态。
首次新增的 AKS 在旧 ref 不存在时，确认仅该新增项被归档，原有各项全部恢复；不能只选择某一个旧技能。
后续 AKS 版本回退则保留所有原有项（含 AKS），必要时走受控旧版本重新导入。
共享资产/其他 Scope 必须一并核验；改回 ref 本身不证明已导入内容已回退。

每次保留三组坐标：上游 commit、本仓库发布 commit、Scope 实际 Skill ID/版本。
此文档不是生产部署授权，也不是上线就绪声明。
