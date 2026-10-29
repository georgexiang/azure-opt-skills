> **QM upstream knowledge reference — not execution instructions.**
> Use this material to interpret redacted evidence. Only the local SKILL.md and
> its reviewed, target-bound adapter define available live reads. Upstream CLI,
> collectors, MCP, login, remediation and privileged-debug examples below do not
> authorize execution. Never extract/run these collectors or apply changes.

Source: https://github.com/microsoft/skills/blob/00d66a64919edca5bca2c673373444e36a4b5461/.github/plugins/aks-skills/skills/aks-troubleshooting/references/report-template.md

# Investigation Report

**Date:** YYYY-MM-DDTHH:MM:SSZ
**Cluster:** <cluster-name> / <resource-group> / <subscription>
**Reported symptom:** <one-line symptom>

## What Was Inspected

| Resource | Command | Finding |
|----------|---------|---------|
| | | |

## Failure Domain

<workload | node | Kubernetes network | Azure network | control plane/Azure platform | identity/RBAC | quota/capacity>

## Root Cause

<concise root cause — one or two sentences>

**Confidence:** <high | medium | low> — <what would raise it, or the ranked alternatives if unresolved>

## Evidence

```
<relevant log snippet or command output>
```

## Fix

```bash
# command(s) to resolve
```

**Impact / rollback:** <what changes and how to revert; note IaC/GitOps source to update>

## Unavailable Evidence

- <read that was denied or not possible, identity, missing permission>

## Escalation

<none | Azure support with this report attached, and why Microsoft-side evidence is needed>

## Additional Findings

- (any secondary issues discovered during investigation)

## Prevention

- (optional: what would prevent recurrence — alert, config change, policy)
