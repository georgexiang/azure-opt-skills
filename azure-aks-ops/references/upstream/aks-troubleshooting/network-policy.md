> **QM upstream knowledge reference — not execution instructions.**
> Use this material to interpret redacted evidence. Only the local SKILL.md and
> its reviewed, target-bound adapter define available live reads. Upstream CLI,
> collectors, MCP, login, remediation and privileged-debug examples below do not
> authorize execution. Never extract/run these collectors or apply changes.

Source: https://github.com/microsoft/skills/blob/00d66a64919edca5bca2c673373444e36a4b5461/.github/plugins/aks-skills/skills/aks-troubleshooting/network-policy.md

# Network Policy Troubleshooting

Use this guide when pod-to-pod or pod-to-service traffic is selectively blocked and the symptom points at ingress or egress filtering.

```bash
# List all policies in the namespace - check both ingress and egress
kubectl get networkpolicy -n <ns> -o yaml

# Check for a default-deny policy (blocks everything unless explicitly allowed)
kubectl get networkpolicy -n <ns> -o jsonpath='{range .items[?(@.spec.podSelector=={})]}{.metadata.name}{"\n"}{end}'
```

**AKS network policy engine check:** Azure NPM (Azure CNI): `kubectl get pods -n kube-system -l k8s-app=azure-npm`. Calico: `kubectl get pods -n calico-system`.

Policy audit: source labels, destination labels, destination ingress rules, and source egress rules must all line up. With default-deny, explicitly allow UDP/TCP 53 to kube-dns.

Keep selector, ingress, egress, and engine evidence as the primary diagnostic path. If those rules and direct connectivity evidence remain ambiguous, hand off to `aks-network-capture` only when packet-level proof is requested or explicitly approved. The capture skill owns capture scope, runtime, least-privilege controls, and commands; packet capture is not a mandatory network-policy step.
