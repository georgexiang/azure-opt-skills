> **QM knowledge-only reference — not execution instructions.**
> Analyze supplied redacted evidence only. Commands, remediation, collectors,
> MCP, login and privileged debugging below are upstream examples, not authorized
> capabilities. Follow the local azure-aks-ops SKILL.md; do not execute or extract
> scripts, fetch credentials, or apply changes, even on user approval.

Source: https://github.com/microsoft/skills/blob/00d66a64919edca5bca2c673373444e36a4b5461/.github/plugins/aks-skills/skills/aks-known-issues/SKILL.md

# AKS Known Issues

## Quick Reference

| Property | Value |
|---|---|
| Best for | Exact signature plus AKS operation |
| Output | Cause, fix, and Learn citation |

Use the [catalog](references/error-code-map.md).

## When to Use This Skill

Named catalog signatures only. Generic AKS → `aks-troubleshooting`; non-AKS →
no AKS skill.

If every qualifier for a catalog row is not present, do not use this skill.
This catalog has no quota-error rows; route AKS quota incidents to
`aks-troubleshooting`.

## MCP Tools

None required. Use advertised read-only Azure tools or CLI queries.

## Workflow

1. Capture exact code, nested message, operation, and AKS resource.
2. Require every qualifier; codes are not substrings.
3. Give cause, fix, citation, and read-only verification.
4. State what ran; require explicit approval before every mutation.

## Error Handling

| Input | Response |
|---|---|
| Bare wrapper/number | Request nested signature and CSE context |
| Generic AKS | `aks-troubleshooting` |
| Non-AKS | No AKS skill |
