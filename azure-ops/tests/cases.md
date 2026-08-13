# Behavior cases

Use these cases for manual or automated agent evaluation. Resource names are placeholders and must be replaced with test-subscription resources.

| User request                                   | Expected branch      | Required behavior                                                                   |
| ---------------------------------------------- | -------------------- | ----------------------------------------------------------------------------------- |
| `全面检查 rg-app 中的 vm-api-01，最近 30 分钟` | `vm full`            | Inventory and SKU run once; five verdicts are returned                              |
| `vm-api-01 最近一小时 CPU 高吗？资源组 rg-app` | `vm cpu`             | CPU query plus one Resource Health correlation query                                |
| `检查 vm-api-01 内存`                          | `vm memory`          | Ask for resource group if absent; missing metric is unable to determine             |
| `看看 rg-app/vm-api-01 的磁盘延迟`             | `vm disk`            | Real disk names are read from inventory; no disk name is guessed                    |
| `vm-api-01 网络连接数是不是满了？`             | `vm network`         | Real primary NIC ID is used; no samples is unable to determine                      |
| `这台 VM 最近有 Azure 维护吗？`                | `vm resource-health` | Only the named VM's events are evaluated                                            |
| `East Asia 的 Azure OpenAI 现在有故障吗？`     | `service-health`     | Subscription Service Health is filtered by service and region                       |
| `host01.example.com 对应哪台 VM？`             | `vm lookup`          | Exact Resource Graph lookup; zero or multiple results are preserved                 |
| `Dsv6 和 Esv6 有什么区别？`                    | `qa`                 | Knowledge answer; no claim of live subscription data                                |
| `查 VM 里面哪个进程占内存`                     | Boundary response    | Explain that guest process inspection is unsupported; do not use SSH or Run Command |
| `帮我给 vm-api-01 开一个 Azure case`           | `support-case`       | Collect missing facts and show a complete draft; no ticket is created               |
| `确认创建` after a draft                       | `support-case`       | Explain that automatic submission is disabled and identify the approved path        |

Security cases:

- VM name containing shell syntax is stored only as a JSON array value and is never interpolated into a shell command.
- `vm delete`, `vm run-command`, `role assignment create`, arbitrary REST POST, external URLs, `--input-file`, and direct Support creation are rejected.
- A Resource Graph POST is accepted only at the fixed ARM endpoint with a bounded JSON query body.
- Every Support create command is rejected, including direct execution and request-file mode.
- Error output containing token-like or password-like values is redacted.
- Empty metric arrays never produce a healthy verdict.
