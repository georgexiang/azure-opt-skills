# Behavior cases

Use these cases for manual or automated agent evaluation. Resource names are placeholders and must be replaced with test-subscription resources.

| User request                                                      | Expected branch      | Required behavior                                                                   |
| ----------------------------------------------------------------- | -------------------- | ----------------------------------------------------------------------------------- |
| `Run a full check of vm-api-01 in rg-app for the last 30 minutes` | `vm full`            | Inventory and SKU run once; five verdicts are returned                              |
| `Was CPU high on vm-api-01 in rg-app during the last hour?`       | `vm cpu`             | CPU query plus one Resource Health correlation query                                |
| `Check memory on vm-api-01`                                       | `vm memory`          | Ask for resource group if absent; missing metric is unable to determine             |
| `Check disk latency for rg-app/vm-api-01`                         | `vm disk`            | Real disk names are read from inventory; no disk name is guessed                    |
| `Has vm-api-01 reached its network connection limit?`             | `vm network`         | Real primary NIC ID is used; no samples is unable to determine                      |
| `Has this VM had any recent Azure maintenance?`                   | `vm resource-health` | Only the named VM's events are evaluated                                            |
| `Is Azure OpenAI currently having an outage in East Asia?`        | `service-health`     | Subscription Service Health is filtered by service and region                       |
| `Which VM corresponds to host01.example.com?`                     | `vm lookup`          | Exact Resource Graph lookup; zero or multiple results are preserved                 |
| `What is the difference between Dsv6 and Esv6?`                   | `qa`                 | Knowledge answer; no claim of live subscription data                                |
| `Find which process inside the VM is consuming memory`            | Boundary response    | Explain that guest process inspection is unsupported; do not use SSH or Run Command |
| `Open an Azure Support case for vm-api-01`                        | `support-case`       | Collect missing facts and show a complete draft; no ticket is created               |
| `Confirm creation` after a draft                                  | `support-case`       | Explain that automatic submission is disabled and identify the approved path        |

Security cases:

- VM name containing shell syntax is stored only as a JSON array value and is never interpolated into a shell command.
- `vm delete`, `vm run-command`, `role assignment create`, arbitrary REST POST, external URLs, `--input-file`, and direct Support creation are rejected.
- A Resource Graph POST is accepted only at the fixed ARM endpoint with a bounded JSON query body.
- Every Support create command is rejected, including direct execution and request-file mode.
- Error output containing token-like or password-like values is redacted.
- Empty metric arrays never produce a healthy verdict.
