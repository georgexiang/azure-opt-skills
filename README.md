# Azure Ops Skill for QM

## Overview

This repository extracts the Azure operations capabilities from
[`georgexiang/azure-ops-agent`](https://github.com/georgexiang/azure-ops-agent/tree/main)
into a unified Skill that users can discover and invoke from the QM Web UI.

This extraction is based on the following commit from the source repository's `main` branch:

```text
b2774d3db5a536ca2dd33b13714ff9f5fe399baa
```

The Skill resource name is:

```text
/azure-ops
```

The main `SKILL.md` handles intent routing, workflow orchestration, and safety constraints. Detailed commands, thresholds, output templates, and execution scripts remain internal assets of the same Skill.

## Goals

1. Consolidate the source repository's Azure Skills into one QM Skill.
2. Preserve Azure VM diagnostics, platform health queries, resource lookup, knowledge answers, and Support case workflows.
3. Remove duplicate VM queries, SKU queries, time conversions, and Resource Health queries across the original Skills.
4. Remove runtime coupling to Feishu, Microsoft Agent Framework, and the source project's custom `run_az` interface.
5. Integrate with QM Scopes, Sandboxes, credentials, Skill materialization, and Web UI presentation.
6. Allow only read-only Azure operations. Generate Support case drafts without submitting them from a general-purpose Sandbox.
7. Make the Skill visible on the QM Web UI Skills page and in the `/` command picker.

## Non-goals

- Do not migrate the source project's Feishu bot, session service, or standalone Python Agent service.
- Do not store Azure tokens, client secrets, certificate private keys, or other sensitive information in the Skill.
- Do not allow the Agent to enter a VM, execute guest commands, or read processes, filesystems, or application logs.
- Do not expose unvalidated Azure CLI commands directly to the model.
- Do not grant permission to create, modify, restart, or delete Azure resources by default.

## Source Capability Inventory

The source repository contains the following ten Skills:

| Original Skill             | Purpose                                              | Unified Skill branch |
| -------------------------- | ---------------------------------------------------- | -------------------- |
| `vm-full-diagnosis`        | Comprehensive VM health check                        | `vm full`            |
| `vm-cpu-check`             | CPU metric diagnostics                               | `vm cpu`             |
| `vm-memory-check`          | Memory metric diagnostics                            | `vm memory`          |
| `vm-disk-check`            | Disk IOPS, throughput, and latency diagnostics       | `vm disk`            |
| `vm-network-check`         | Network traffic, flow, and acceleration diagnostics  | `vm network`         |
| `vm-resource-health-check` | Resource Health for one VM                           | `vm resource-health` |
| `service-health-check`     | Subscription-level Service Health                    | `service-health`     |
| `vm-hostname-lookup`       | Hostname and VM resource-name lookup                 | `vm lookup`          |
| `azure-qa`                 | Azure knowledge answers and capability-boundary help | `qa`                 |
| `azure-support-case`       | Azure Support case drafts                            | `support-case`       |

## Unified Skill Design

### Routing

The main `SKILL.md` selects one execution branch from the user's intent:

- Full diagnosis: CPU, memory, disk, network, and Resource Health.
- Focused diagnosis: query only the metric that the user explicitly requested.
- Platform health: query subscription-level Azure Service Health.
- Resource health: query Resource Health for a specified VM.
- Resource lookup: map among hostnames, VM resource names, and resource IDs.
- Azure Q&A: answer Azure knowledge questions that do not require live resource data.
- Support case: query Service and Problem Classification values and prepare a draft for an external approval and submission workflow.

### Shared Workflow

The following steps are shared across the original Skills:

1. Resolve the subscription, resource group, VM name, and time range.
2. Check Azure CLI availability and the current authentication context.
3. Query VM identity, the primary NIC, attached disks, and the resource ID.
4. Query the VM SKU's vCPU and memory capabilities.
5. Convert user-provided times to the UTC timestamps required by Azure Monitor.
6. Query and interpret Resource Health events.
7. Handle missing resources, insufficient permissions, missing metrics, and oversized output consistently.
8. Generate a report in the user's language with data sources, the evaluated time range, conclusions, and recommendations.

### Diagnostic Boundary

- All metrics come from the Azure control plane, Azure Monitor, Resource Health, or Resource Graph.
- Do not enter a VM or use Run Command, SSH, or other remote execution methods.
- Return "unable to determine" or `N/A` for missing metrics. Never interpret missing data as healthy.
- Do not diagnose a sustained incident from one instantaneous peak.
- Do not guess NIC names, disk names, resource IDs, SKU limits, or Problem Classification IDs.
- Verify changing facts such as product limits, prices, and regional availability through Microsoft Learn or live Azure queries.

## QM Packaging

Recommended directory structure:

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
      install_azure_cli.sh
      az_guard.py
    tests/
      cases.md
      test_az_guard.py
```

Register the Git Pack on the QM Admin Skills page:

```text
https://github.com/georgexiang/azure-opt-skills
```

After registration, select `Browse skills...` and import `azure-ops` into the target Scope. Use `Sync now` after repository updates. You do not need to build or replace the Sandbox image.

QM publishes the Git Pack as a Skill bundle and shows the following information in the Web UI:

- Name: `/azure-ops`
- Description and supported scenarios
- Scope and publication status
- Internal asset count

The Web UI's direct-creation form supports only `name`, `description`, `body`, and `scopeId`. It is not the final maintenance surface for a bundle that includes scripts and reference files.

## Implemented Components

- `azure-ops/SKILL.md`: unified discovery description, ten intent branches, shared safety boundaries, and report contract.
- `azure-ops/references/vm-diagnostics.md`: shared VM queries and CPU, memory, disk, and network diagnostics.
- `azure-ops/references/health-and-lookup.md`: Resource Health, Service Health, and hostname lookup workflows.
- `azure-ops/references/support-case.md`: Support classification queries, case drafting, and external submission guidance.
- `azure-ops/scripts/install_azure_cli.sh`: installs a pinned Azure CLI in the current Scope's persistent `$HOME`.
- `azure-ops/scripts/az_guard.py`: read-only command allowlist, ARM REST restrictions, output limits, redaction, and Support write denial.
- `azure-ops/tests/`: behavior cases and guard-script unit tests.

## Sandbox and Authentication Requirements

The Skill does not modify the Sandbox image. On the first live query in a Scope, it installs the following components with a background task:

```text
Azure CLI 2.89.1
Azure Support extension 2.0.1
```

Installation paths:

```text
$HOME/.local/share/azure-cli/2.89.1
$HOME/.local/bin/az
$HOME/.local/share/azure-cli/extensions
```

QM Local Sandbox stores the entire `$HOME` in a Scope-specific volume, so the installation persists across turns and container rebuilds. A complete installation uses approximately 717 MB in testing. Personal, Project, Channel, and other Scopes are isolated and require separate installations.

The installer requires Python 3.11, `venv`, `curl`, `flock`, and `sha256sum` in the base environment. The complete Azure CLI transitive dependency set is pinned in `azure-cli-2.89.1.lock` and verified with SHA-256 hashes. The Support extension wheel also has a pinned URL, version, and SHA-256 hash. The installer writes only to `$HOME/.local`; it does not create or modify `$HOME/.azure`.

Live queries also require the following configuration:

1. An operator provisions an Azure identity for the target Scope. Never send secrets, certificates, or tokens through chat.
2. Every operation verifies the current tenant, subscription, and calling identity first.
3. Azure CLI uses `$HOME/.azure` for its cache, which QM captures and restores per Scope.
4. Azure RBAC keeps the identity read-only. Use a dedicated service principal where possible.
5. Egress allows Azure sign-in, ARM, PyPI, and Azure CLI extension download endpoints.

Azure RBAC must keep the identity read-only. This is the final authorization boundary and cannot be bypassed with command aliases. Prefer a service principal or managed identity in production. Use Device Code only for temporary interactive sign-in when Conditional Access permits it.

Separate permissions into two identities:

| Identity            | Purpose                                                                | Permission principle                                       |
| ------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| Diagnostic identity | VM, Monitor, Resource Health, Service Health, and Resource Graph reads | Read-only and least privilege                              |
| Support submission  | Not configured in a general-purpose Sandbox                            | Use Azure Portal or an organization-approved external flow |

## Azure CLI Safety Layer

The unified Skill does not execute arbitrary `az` arguments directly. It uses a controlled wrapper that enforces the following requirements:

- Use an argv array with `shell=False`; never concatenate shell commands.
- Enforce a command-prefix allowlist.
- Reject delete, update, start, stop, restart, SSH, Run Command, and other mutation operations.
- Allow general `az rest` only for GET requests to `https://management.azure.com/`.
- Allow Resource Graph POST requests only to the fixed read-only query endpoint.
- Reject arbitrary request bodies, input files, and external URLs.
- Limit execution time and output size.
- Redact sensitive information from stdout and stderr.
- Return structured JSON errors so the model does not infer results from ambiguous text.

The Azure Support branch only queries Service and Problem Classification values and generates a draft. A general-purpose Local Sandbox cannot enforce non-bypassable cross-turn write confirmation or prevent arbitrary processes from accessing a mounted write identity, so it never executes ticket-creation commands automatically. Users submit cases through Azure Portal or organization-approved external automation.

## Implementation Phases

### Phase 1: Content Extraction

- Build an intent and command matrix for the ten original Skills.
- Consolidate shared parameters and query steps.
- Remove Feishu-specific and original Agent Framework-specific instructions.
- Remove organization-specific defaults such as a fixed resource group.
- Standardize error handling, thresholds, and report formats.

Deliverable: unified Skill content draft and test-case inventory.

### Phase 2: Execution Capability

- Implement the controlled Azure CLI wrapper.
- Install the user-local Azure CLI in the Scope's persistent volume.
- Integrate non-interactive credentials and subscription-selection rules.
- Configure the required Azure egress.

Deliverable: an executable Skill bundle that does not modify the Sandbox image.

### Phase 3: QM Git Pack Publication

- Register this Git repository in Admin Skills.
- Browse the pack and import `azure-ops` into the target Scope.
- Confirm that the Skill appears on the Web UI Skills page and in the `/` picker.
- Use `Sync now` or Auto-sync for subsequent updates.

Deliverable: an invocable `/azure-ops` resource in QM.

### Phase 4: Validation and Refinement

- Run static validation, safety tests, and intent-routing tests.
- Complete smoke tests with real read-only Azure queries.
- Verify credential and resource isolation between Personal and Project Scopes.
- Verify that Support cases remain drafts and that all creation commands are rejected.
- Refine the instructions and reduce duplicate queries based on test results.

Deliverable: acceptance record, known limitations, and release recommendations.

## Acceptance Criteria

### Resource Acceptance

- QM parses the `SKILL.md` frontmatter, name, description, and body.
- The QM Git Pack importer reads the entire bundle.
- Every asset has a valid text path, and executable scripts have the correct mode.
- `/azure-ops` appears on the Web UI Skills page and in the command picker.

### Functional Acceptance

- All ten original intents route to the correct branch.
- A full diagnosis runs shared VM and SKU queries only once.
- Missing CPU, memory, disk, or network data never produces a healthy verdict.
- Service Health and Resource Health correctly distinguish platform-wide events from single-resource events.
- Hostname lookup never guesses resource names.
- Azure knowledge answers never claim to be live query results.

### Security Acceptance

- Azure CLI commands outside the allowlist are rejected.
- Arbitrary ARM writes, external URLs, input files, and shell injection attempts are rejected.
- Logs and reports contain no tokens, secrets, or certificate private keys.
- A general diagnostic identity cannot create or modify Azure resources.
- The Skill and a general-purpose Sandbox cannot create Support cases.
- Azure credentials cannot be reused across QM Scopes.

## Test Scenarios

Cover at least the following scenarios:

- Full VM health check.
- Focused CPU, memory, disk, and network diagnostics.
- Missing, deallocated, or metric-less VMs.
- Missing memory metrics when Azure Monitor Agent is not enabled.
- Resource Health with no events, recovered events, and a current abnormal event.
- Service Health filtering by time, service, and region.
- Hostname-to-resource-name and resource-name-to-hostname lookup.
- Insufficient Azure permissions, expired authentication, and incorrect subscription selection.
- Dangerous Azure CLI commands and command-injection attempts.
- Incomplete Support case information, complete draft generation, and creation refusal.

## Current Status

- The unified `SKILL.md`, three reference files, behavior cases, and safety tests are complete.
- `az_guard.py` implements a read allowlist, REST restrictions, JSON request isolation, bounded output, redaction, and Support write denial.
- The Scope-local Azure CLI installer works without modifying the Sandbox image.
- The Git Pack has been registered and imported into `org:qm-local`.
- Guard-script unit tests pass.
- The target read-only Azure identity and real-resource smoke tests remain pending.
