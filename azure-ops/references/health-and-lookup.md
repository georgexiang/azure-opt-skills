# Health and lookup

Use this reference for `vm resource-health`, `service-health`, and `vm lookup`.

Run every Azure command through `scripts/az_guard.py`. Use a JSON request file whenever an argument contains user-provided text.

## Subscription context

Start live queries with:

```text
account show --query {subscription:id,subscriptionName:name,tenant:tenantId,user:user.name} -o json
```

If the user explicitly requested another subscription, verify it is accessible before using `--subscription`. Never silently switch subscriptions.

## VM Resource Health

Require an exact VM name and resource group. Confirm the VM exists with a narrow `vm show` query, then call:

```text
rest --method get --url https://management.azure.com/subscriptions/<subscription>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions --query value[].{time:properties.occuredTime,state:properties.availabilityState,title:properties.title,category:properties.category,cause:properties.healthEventCause,reason:properties.reasonType,summary:properties.summary,actions:properties.recommendedActions[].action} -o json
```

Rules:

- Sort by event time descending instead of trusting response order.
- A maintenance record with `state=null` is an event description, not an `Unknown` state.
- With an explicit time window, exclude outside events from both display and verdict.
- Without a time window, show the requested count, defaulting to five, and base current-status wording on the newest non-null state.
- `Available` is healthy. `Unavailable` and `Degraded` are abnormal. `Unknown` is unable to determine.
- Historical platform events that later returned to `Available` do not make the current state abnormal.
- Translate summaries faithfully, preserve Azure resource and product names, and do not invent a root cause.

## Subscription Service Health

Service Health is subscription-level and is not a substitute for one VM's Resource Health. Query events with ARM:

```text
rest --method get --url https://management.azure.com/subscriptions/<subscription>/providers/Microsoft.ResourceHealth/events?api-version=2022-10-01 --query value[].{trackingId:name,type:properties.eventType,status:properties.status,level:properties.level,title:properties.title,start:properties.impactStartTime,mitigated:properties.impactMitigationTime,updated:properties.lastUpdateTime,summary:properties.summary,impact:properties.impact[].{service:impactedService,regions:impactedRegions[].impactedRegion}} -o json
```

Use a seven-day window when the user gives no time range. Include older active incidents because they are still relevant. Apply requested service, region, event-type, and time filters to the returned structured fields.

Verdict:

- An active `ServiceIssue` means an active platform incident.
- A resolved `ServiceIssue` is historical context, not a current outage.
- Planned maintenance, health advisories, security advisories, and RCA records are important notices but are not automatically outages.
- An empty filtered result means no matching event was returned, not proof that every Azure service is globally healthy.

Report impacted services, regions, start and mitigation times, status, tracking ID when present, and the most recent update. Recommend following Azure Service Health for active incidents.

## VM name to computer name

When given a VM resource name, require or extract its resource group and call:

```text
vm get-instance-view -g <rg> -n <vm> --query instanceView.computerName -o json
```

Return `N/A` when a stopped VM or unavailable guest agent does not report a computer name. Do not infer it from `osProfile` or the VM name.

## Computer name to VM resource name

Use Azure Resource Graph through the guard's fixed read-only POST endpoint. Write a request file containing arguments equivalent to:

```text
rest --method post --url https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01 --body {"subscriptions":["<subscription>"],"query":"Resources | where type =~ 'microsoft.compute/virtualmachines' | extend computerName=tostring(properties.extended.instanceView.computerName) | where computerName in~ ('<fqdn>') | project computerName,vmName=tostring(split(id,'/')[-1]),resourceGroup,location,id | limit 50"} -o json
```

Escape the computer name as a KQL string value before writing the JSON request. Prefer exact case-insensitive matching. If exact matching returns no rows, offer a separate contains search using a user-approved fragment; label it as fuzzy and show every candidate instead of silently picking one.

For multiple inputs, query them together with `in~` and map every requested value to zero, one, or multiple results. Preserve the Azure-returned VM resource-name casing.

## Lookup output

For each input show only the useful mapping plus resource group when needed to disambiguate:

```text
<computer name> -> <VM resource name> (<resource group>)
<VM resource name> -> <computer name or N/A>
```

Never expose the subscription ID unless the user needs it to disambiguate results.
