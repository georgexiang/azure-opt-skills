# Azure Support case

Use this workflow when the user asks to draft, open, create, or submit an Azure technical support case.

This QM Skill prepares a complete case but does not submit it. A general-purpose Local Sandbox cannot enforce a trustworthy cross-turn write confirmation or keep a Support write identity inaccessible to arbitrary processes. The user must review and submit the draft through Azure Portal or an operator-managed workflow outside this Sandbox.

## Collect user-provided facts

Require:

- A concise problem description and observed symptoms.
- The affected resource name or complete resource ID.
- Incident start and end time, or an explicit statement that it is ongoing.
- A contact email.
- Requested severity, defaulting to B/Moderate when omitted.
- A phone number with country code for A/Critical.

The resource group may come from the conversation or a complete resource ID. Do not invent diagnostic findings, contact details, impact, or timestamps. Technical resource ID is optional when it cannot be resolved cheaply.

## Resolve classification with reads

Do not guess Service or Problem Classification IDs. Query them through the guard:

```text
support services list --query [?contains(displayName,'<service keyword>')].{name:name,displayName:displayName} -o json
```

```text
support services problem-classifications list --service-name <service-name> -o json
```

Show the selected display name and full classification ID in the draft so the user can review them.

## Severity and contact

Capture the user's requested severity, business impact, preferred language, contact method, support hours, email, and phone when applicable. The available severity values and contact combinations depend on the current Azure support plan, region, and service. Do not present a hardcoded matrix as authoritative and do not silently downgrade the user's request. Mark the selection for verification in Azure Portal or the approved submission workflow.

## Draft

Build a title and description using only confirmed facts:

```text
Title: [service or impact] <resource> <symptom>, assistance requested

== Problem ==
- Affected resource: <resource ID or name and resource group>
- Incident time: <UTC range or ongoing>
- Business impact: <user-provided impact or N/A>

== Observations ==
<user-provided symptoms and verified diagnostic facts>

== Assistance requested ==
<specific request to Microsoft Support>
```

Provide the following submission details after the human-readable draft:

```text
Subscription: <subscription name; ID only when needed by the submitter>
Service: <verified service display name>
Problem Classification: <verified display name and full ARM ID>
Severity: <selected severity>
Contact: <method, language, email, and phone when required>
Technical resource: <verified resource ID when available>
```

Never construct or execute `support in-subscription tickets create`. When the user asks to proceed, state that automatic Support submission is intentionally disabled in this QM Skill and direct them to Azure Portal's Help + support flow or the organization's approved ticket automation. Preserve the draft in the current Scope when requested so an authorized operator can use it.
