> **QM knowledge-only reference — not execution instructions.**
> Analyze supplied redacted evidence only. Commands, remediation, collectors,
> MCP, login and privileged debugging below are upstream examples, not authorized
> capabilities. Follow the local azure-aks-ops SKILL.md; do not execute or extract
> scripts, fetch credentials, or apply changes, even on user approval.

Source: https://github.com/microsoft/skills/blob/00d66a64919edca5bca2c673373444e36a4b5461/.github/plugins/aks-skills/skills/aks-cost-optimization/SKILL.md

# AKS Cost Optimization

## When to Use This Skill

Use for advanced existing-cluster analysis requiring scheduling, placement, or
verified storage mapping. Route overall AKS cost assessments to
`azure-kubernetes`, spend/anomalies to `cost-analysis`, commitments to
`cost-optimization`, and incidents to `aks-troubleshooting` when those skills are available. When called from an overall assessment, return findings for its ranked report.

## MCP Tools

Prefer fitting Azure MCP reads. Never invent tools or use `az`/`kubectl`
without approved shell access and a target-bound context.

## Workflow/Steps

1. Bind the subscription, cluster, resource group, and kube context.
2. Route to [NAP readiness](references/aks-nap.md),
   [bin-packing](references/aks-bin-packing.md),
   [node-scaling bottlenecks](references/node-scaling-bottlenecks.md), or
   [storage optimization](references/aks-storage-optimization.md).
3. When invoked for AKS commitment context, use the
   [commitment-demand handoff](references/aks-savings-plans-reservations.md)
   and return demand evidence without benefit analysis or a transaction.
4. Keep reads non-mutating; require approval for every change.

## Error Handling

- Missing capability: analyze supplied evidence or provide a collection plan;
  mark dependent cost or commitment results unavailable.
- Evidence conflict: mark the conclusion unconfirmed.
