#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ARG_API = "2022-10-01"
ARM_LIST_API = "2022-12-01"
METRICS_API = "2023-10-01"
BATCH_API = "2024-02-01"
METRICS_RESOURCE = "https://metrics.monitor.azure.com"
VM_NAMESPACE = "microsoft.compute/virtualmachines"
NAT_NAMESPACE = "microsoft.network/natgateways"
ER_NAMESPACE = "microsoft.network/expressroutecircuits"
ERGW_NAMESPACE = "microsoft.network/virtualnetworkgateways"
AGW_NAMESPACE = "microsoft.network/applicationgateways"
LB_NAMESPACE = "microsoft.network/loadbalancers"
AKS_LB_NAMES = {"kubernetes", "kubernetes-internal"}
DATAPATH_MIN = 90.0
AGW_5XX_PCT = 5.0
AGW_MIN_REQUESTS = 100.0
AGW_RATIO = 3.0
ER_CPU_MAX = 80.0
ER_CPU_MINUTES = 30
ER_ROUTES_DROP = 0.5
ER_BANDWIDTH_PCT = 80.0
NAT_RATIO = 3.0
NAT_FAILED_FLOOR = 10.0
NAT_DROPS_FLOOR = 100.0
ARG_SUBSCRIPTION_CHUNK = 100
BATCH_RESOURCE_CHUNK = 50
OUTPUT_BUDGET = 7000
HTTP_TIMEOUT = 30.0
UNHEALTHY = {"Degraded": "warning", "Unavailable": "critical"}
STOPPED = {"PowerState/deallocated", "PowerState/deallocating", "PowerState/stopped"}
HEALTH_HISTORY_API = "2022-10-01"
LOOP_TRACK_AGE = {"Unavailable": timedelta(minutes=30), "Degraded": timedelta(hours=4)}
LOOP_TRACK_DISK_DAYS = 3
AGE_BUCKETS = ((timedelta(days=1), "<24h"), (timedelta(days=7), "1-7d"))
RG_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.()")
HEX = set("0123456789abcdef")

NAT_QUERY = """Resources
| where type =~ 'microsoft.network/natgateways'
| project id = tolower(id), name, subscriptionId, resourceGroup, location,
  sku = tostring(sku.name),
  subnets = array_length(properties.subnets),
  publicIps = array_length(properties.publicIpAddresses) + array_length(properties.publicIpPrefixes)"""

VM_QUERY = """Resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id = tolower(id), name, subscriptionId, resourceGroup, location,
  computerName = tostring(properties.osProfile.computerName),
  powerState = tostring(properties.extended.instanceView.powerState.code)"""

HEALTH_KINDS = {
    "microsoft.compute/virtualmachines": None,
    "microsoft.network/expressroutecircuits": "expressRouteCircuit",
    "microsoft.network/applicationgateways": "applicationGateway",
    "microsoft.network/loadbalancers": "loadBalancer",
}
NET_KINDS = {
    "microsoft.network/expressroutecircuits": "expressRouteCircuit",
    "microsoft.network/virtualnetworkgateways": "expressRouteGateway",
    "microsoft.network/applicationgateways": "applicationGateway",
    "microsoft.network/loadbalancers": "loadBalancer",
}

NET_QUERY = """Resources
| where type in~ ('microsoft.network/expressroutecircuits', 'microsoft.network/virtualnetworkgateways',
    'microsoft.network/applicationgateways', 'microsoft.network/loadbalancers')
| where type !~ 'microsoft.network/virtualnetworkgateways' or tostring(properties.gatewayType) =~ 'ExpressRoute'
| project id = tolower(id), type = tolower(type), name, subscriptionId, resourceGroup, location,
  sku = coalesce(tostring(sku.name), tostring(properties.sku.name)),
  tier = coalesce(tostring(properties.sku.tier), tostring(sku.tier)),
  providerState = tostring(properties.serviceProviderProvisioningState),
  circuitState = tostring(properties.circuitProvisioningState),
  peerings = properties.peerings,
  operationalState = tostring(properties.operationalState),
  rules = array_length(properties.loadBalancingRules)"""

HEALTH_QUERY = """HealthResources
| where type =~ 'microsoft.resourcehealth/availabilitystatuses'
| where tostring(properties.availabilityState) in ('Degraded', 'Unavailable')
    or (tostring(properties.availabilityState) == 'Available'
        and tostring(properties.previousAvailabilityState) in ('Degraded', 'Unavailable'))
| extend target = tolower(tostring(properties.targetResourceId))
| where """ + "\n    or ".join(f"target contains '/providers/{t}/'" for t in HEALTH_KINDS) + """
| project target,
  state = tostring(properties.availabilityState),
  previous = tostring(properties.previousAvailabilityState),
  occurred = tostring(properties.occurredTime),
  reasonType = tostring(properties.reasonType),
  title = tostring(properties['title']),
  summary = tostring(properties.summary)"""

SERVICE_HEALTH_QUERY = """ServiceHealthResources
| where type =~ 'microsoft.resourcehealth/events'
| extend eventType = tostring(properties.EventType), status = tostring(properties.Status)
| where eventType in ('PlannedMaintenance', 'ServiceIssue')
| extend impact = tostring(properties.Impact)
| where impact contains 'ExpressRoute'
| project subscriptionId, eventType, status,
  trackingId = tostring(properties.TrackingId),
  title = tostring(properties.Title),
  start = tostring(properties.ImpactStartTime),
  end = tostring(properties.ImpactMitigationTime)"""

ANNOTATION_QUERY = """HealthResources
| where type =~ 'microsoft.resourcehealth/resourceannotations'
| where todatetime(properties.occurredTime) > ago({minutes}m)
| extend target = tolower(tostring(properties.targetResourceId))
| where target contains '/providers/microsoft.compute/virtualmachines/'
| project target,
  annotation = tostring(properties.annotationName),
  occurred = tostring(properties.occurredTime),
  context = tostring(properties.context),
  category = tostring(properties.category),
  impactType = tostring(properties.impactType),
  summary = tostring(properties.summary),
  reason = tostring(properties.reason)"""


@dataclass(frozen=True)
class Check:
    rule: str
    threshold: str
    uses_baseline: bool


LATENCY = Check("disk-latency", "latency_ms", True)
SATURATION = Check("disk-saturation", "saturation_pct", False)
METRIC_GROUPS: tuple[tuple[str | None, dict[str, Check]], ...] = (
    (
        None,
        {
            "OS Disk Latency": LATENCY,
            "OS Disk IOPS Consumed Percentage": SATURATION,
            "OS Disk Bandwidth Consumed Percentage": SATURATION,
        },
    ),
    (
        "LUN",
        {
            "Data Disk Latency": LATENCY,
            "Data Disk IOPS Consumed Percentage": SATURATION,
            "Data Disk Bandwidth Consumed Percentage": SATURATION,
        },
    ),
)


class CollectorError(Exception):
    pass


@dataclass
class Config:
    tools_home: str
    tenants: list[str]
    scopes: list[tuple[str, str | None]]
    arm: str
    metrics_endpoint: str
    interval: timedelta
    health_lag: timedelta
    metric_lag: timedelta
    summary_only: bool
    window: int
    sustain: int
    latency_ms: float
    latency_ratio: float
    saturation_pct: float
    baseline_days: int
    workers: int
    deadline_seconds: float
    disk_burst: int
    nat_alerts: bool
    er_alerts: bool
    agw_alerts: bool
    lb_alerts: bool


@dataclass
class Report:
    findings: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    blind: list[str] = field(default_factory=list)
    tenants: int = 0
    subscriptions: int = 0
    vms: int = 0
    running: int = 0
    unhealthy: list[dict[str, Any]] = field(default_factory=list)
    series: int = 0
    saturated: int = 0
    nats: int = 0
    network: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    er_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    shadow: dict[str, list[float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    skipped: int = 0
    tenant_names: dict[str, str] = field(default_factory=dict)
    readable: list[str] = field(default_factory=list)
    calls: int = 0


def parse_time(text: str) -> datetime | None:
    if not text:
        return None
    value = text.strip().replace("Z", "+00:00")
    head, dot, rest = value.partition(".")
    if dot:
        digits = "".join(ch for ch in rest if ch.isdigit())
        value = f"{head}.{digits[:6]}{rest[len(digits) :]}"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_uuid(text: str) -> bool:
    parts = text.lower().split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(set(p) <= HEX for p in parts)


def parse_tenant(text: str) -> str:
    if is_uuid(text.strip()):
        return text.strip().lower()
    raise argparse.ArgumentTypeError("tenant must be a tenant UUID")


def parse_scope(text: str) -> tuple[str, str | None]:
    parts = [p for p in text.strip().split("/") if p]
    if len(parts) in (2, 4) and parts[0].lower() == "subscriptions" and is_uuid(parts[1]):
        if len(parts) == 2:
            return parts[1].lower(), None
        if parts[2].lower() == "resourcegroups" and set(parts[3]) <= RG_NAME_CHARS:
            return parts[1].lower(), parts[3].lower()
    raise argparse.ArgumentTypeError(
        "scope must be /subscriptions/<uuid> or /subscriptions/<uuid>/resourceGroups/<name>"
    )


AZURE_CLI = Path(__file__).with_name("azure_cli.sh")
TOOLCHAIN_NOT_READY = 78


def az(tools_home: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["bash", str(AZURE_CLI), "--tools-home", tools_home, *args, "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError(f"AZ_FAILED: {exc}") from exc
    if result.returncode == TOOLCHAIN_NOT_READY:
        raise CollectorError(
            f"AZ_NOT_FOUND: Azure CLI is not ready in {tools_home}; without credentials run "
            f"bash {AZURE_CLI} --tools-home {tools_home} --ensure"
        )
    return result


def access_token(tools_home: str, resource: str, tenant: str | None) -> str:
    args = ["account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"]
    result = az(tools_home, args + (["--tenant", tenant] if tenant else []))
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        detail = " ".join(result.stderr.strip().splitlines()[-2:])[:400] or "no output"
        raise CollectorError(f"AUTH_REQUIRED: {detail}")
    return token


def known_tenants(tools_home: str) -> dict[str, str]:
    query = "[].{t:tenantId,n:tenantDisplayName,d:tenantDefaultDomain}"
    result = az(tools_home, ["account", "list", "--all", "--query", query, "-o", "json"])
    try:
        rows = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except ValueError:
        return {}
    return {
        str(r["t"]).lower(): str(r.get("n") or r.get("d") or "")
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("t"), str) and is_uuid(r["t"])
    }


class Http:
    def __init__(self, report: Report, deadline: float) -> None:
        self.report = report
        self.deadline = deadline
        self.lock = threading.Lock()

    def call(self, method: str, url: str, token: str, body: Any = None) -> Any:
        with self.lock:
            self.report.calls += 1
        request = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        )
        try:
            remaining = self.deadline + 15 - time.monotonic()
            timeout = max(3.0, min(HTTP_TIMEOUT, remaining))
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                error = json.loads(raw).get("error") or {}
                parts = [str(error.get("message", ""))] + [
                    f"{d.get('code')}: {d.get('message')}"
                    + (f" near '{d['token']}'" if d.get("token") else "")
                    for d in error.get("details") or []
                ]
                message = "; ".join(p for p in parts if p) or raw
            except (ValueError, AttributeError):
                message = raw
            code = "AUTH_REQUIRED" if exc.code == 401 else f"HTTP_{exc.code}"
            path = urllib.parse.urlsplit(url).path
            raise CollectorError(f"{code}: {path}: {message[:600]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CollectorError(f"UNREACHABLE: {exc}") from exc


@dataclass
class Tenant:
    id: str
    arm_token: str
    metrics_token: str
    subscriptions: list[str]


class Azure:
    def __init__(self, cfg: Config, report: Report) -> None:
        self.cfg = cfg
        self.deadline = time.monotonic() + cfg.deadline_seconds
        self.http = Http(report, self.deadline)
        self.arm = cfg.arm.rstrip("/")

    def past_deadline(self) -> bool:
        return time.monotonic() > self.deadline

    def paged(self, url: str, token: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            page = self.http.call("GET", next_url, token) or {}
            rows.extend(page.get("value") or [])
            next_url = page.get("nextLink")
        return rows

    def tenants(self) -> dict[str, str]:
        token = access_token(self.cfg.tools_home, self.arm, None)
        rows = self.paged(f"{self.arm}/tenants?api-version={ARM_LIST_API}", token)
        names = {
            str(t["tenantId"]).lower(): str(t.get("displayName") or "")
            for t in rows
            if t.get("tenantId")
        }
        local = known_tenants(self.cfg.tools_home)
        return {t: names.get(t) or local.get(t, "") for t in sorted(set(names) | set(local))}

    def open_tenant(self, tenant: str) -> Tenant:
        arm_token = access_token(self.cfg.tools_home, self.arm, tenant)
        rows = self.paged(f"{self.arm}/subscriptions?api-version={ARM_LIST_API}", arm_token)
        subscriptions = sorted(
            str(s["subscriptionId"]).lower()
            for s in rows
            if s.get("state") == "Enabled" and str(s.get("tenantId", tenant)).lower() == tenant
        )
        metrics_token = (
            access_token(self.cfg.tools_home, METRICS_RESOURCE, tenant) if subscriptions else ""
        )
        return Tenant(tenant, arm_token, metrics_token, subscriptions)

    def graph(self, tenant: Tenant, query: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i in range(0, len(tenant.subscriptions), ARG_SUBSCRIPTION_CHUNK):
            chunk = tenant.subscriptions[i : i + ARG_SUBSCRIPTION_CHUNK]
            options: dict[str, Any] = {"resultFormat": "objectArray", "$top": 1000}
            while True:
                page = self.http.call(
                    "POST",
                    f"{self.arm}/providers/Microsoft.ResourceGraph/resources?api-version={ARG_API}",
                    tenant.arm_token,
                    {"subscriptions": chunk, "query": query, "options": options},
                ) or {}
                rows.extend(page.get("data") or [])
                skip = page.get("$skipToken")
                if not skip:
                    break
                options = {**options, "$skipToken": skip}
        return rows

    def batch_metrics(
        self, tenant: Tenant, subscription: str, region: str, resources: list[str],
        names: list[str], start: datetime, end: datetime, filter_expr: str | None,
        namespace: str = VM_NAMESPACE, aggregation: str = "average", interval: str = "PT1M",
    ) -> list[dict[str, Any]]:
        query = {
            "api-version": BATCH_API,
            "metricnamespace": namespace,
            "metricnames": ",".join(names),
            "starttime": iso(start),
            "endtime": iso(end),
            "interval": interval,
            "aggregation": aggregation,
        }
        if filter_expr:
            query["filter"] = filter_expr
        base = self.cfg.metrics_endpoint.format(region=region).rstrip("/")
        url = (
            f"{base}/subscriptions/{subscription}/metrics:getBatch?"
            f"{urllib.parse.urlencode(query)}"
        )
        body = self.http.call("POST", url, tenant.metrics_token, {"resourceids": resources}) or {}
        return list(body.get("values") or [])

    def health_history(self, tenant: Tenant, resource: str) -> list[dict[str, Any]]:
        url = (
            f"{self.arm}{resource}/providers/Microsoft.ResourceHealth/availabilityStatuses"
            f"?api-version={HEALTH_HISTORY_API}"
        )
        return list((self.http.call("GET", url, tenant.arm_token) or {}).get("value") or [])

    def hourly(
        self, tenant: Tenant, resource: str, name: str, start: datetime, end: datetime,
        filter_expr: str | None, aggregation: str = "Average", interval: str = "PT1H",
    ) -> list[dict[str, Any]]:
        query = {
            "api-version": METRICS_API,
            "metricnames": name,
            "timespan": f"{iso(start)}/{iso(end)}",
            "interval": interval,
            "aggregation": aggregation,
        }
        if filter_expr:
            query["$filter"] = filter_expr
        url = (
            f"{self.arm}{resource}/providers/Microsoft.Insights/metrics?"
            f"{urllib.parse.urlencode(query)}"
        )
        return list((self.http.call("GET", url, tenant.arm_token) or {}).get("value") or [])


def series_of(
    metric: dict[str, Any], split: str | None, field_name: str = "average",
) -> list[tuple[str, dict[datetime, float]]]:
    out = []
    for ts in metric.get("timeseries") or []:
        label = ""
        if split:
            for meta in ts.get("metadatavalues") or []:
                if str((meta.get("name") or {}).get("value", "")).lower() == split.lower():
                    label = f"{split} {meta.get('value')}"
        points = {}
        for point in ts.get("data") or []:
            moment = parse_time(str(point.get("timeStamp", "")))
            if moment is not None and point.get(field_name) is not None:
                points[moment] = float(point[field_name])
        out.append((label, points))
    return out


def window_values(points: dict[datetime, float], end: datetime, minutes: int) -> list[float]:
    start = end - timedelta(minutes=minutes)
    return [v for t, v in points.items() if start < t <= end]


def sustained(values: list[float], limit: float, need: int) -> bool:
    return sum(1 for v in values if v > limit) >= need


def vm_ref(vm: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": vm["id"],
        "name": vm.get("name"),
        "computerName": vm.get("computerName") or None,
        "tenantId": vm.get("tenantId"),
        "subscriptionId": vm.get("subscriptionId"),
        "resourceGroup": vm.get("resourceGroup"),
        "location": vm.get("location"),
        "kind": vm.get("kind"),
    }


def in_scope(cfg: Config, vm: dict[str, Any]) -> bool:
    if not cfg.scopes:
        return True
    sub = str(vm.get("subscriptionId", "")).lower()
    rg = str(vm.get("resourceGroup", "")).lower()
    return any(s == sub and (g is None or g == rg) for s, g in cfg.scopes)


def check_health(
    cfg: Config, now: datetime, targets: dict[str, dict[str, Any]], rows: list[dict[str, Any]],
    report: Report,
) -> None:
    window = cfg.interval + cfg.health_lag
    for row in rows:
        vm = targets.get(str(row.get("target", "")))
        state = str(row.get("state", ""))
        if vm is None or vm.get("powerState") in STOPPED:
            continue
        if state == "Available":
            check_recovery(window, now, vm, row, report)
            continue
        if state not in UNHEALTHY:
            continue
        detail = {
            "reasonType": row.get("reasonType") or None,
            "title": row.get("title") or None,
            "summary": row.get("summary") or None,
        }
        report.unhealthy.append({**vm_ref(vm), "availabilityState": state, **detail})
        if vm.get("kind") == "expressRouteCircuit":
            detail["serviceHealth"] = report.er_events.get(str(vm.get("subscriptionId")).lower())
        previous = str(row.get("previous", ""))
        changed = parse_time(str(row.get("occurred", "")))
        if previous == state or changed is None or not timedelta(0) <= now - changed <= window:
            continue
        report.findings.append({
            "rule": "health-onset",
            "severity": UNHEALTHY[state],
            "resource": vm_ref(vm),
            "episode": f"{vm['id']}|health|{previous}->{state}|{iso(changed)}",
            "observed": {
                "availabilityState": state,
                "previousAvailabilityState": previous or None,
                "changedAt": iso(changed),
                **detail,
            },
        })


def event_time(text: str) -> datetime | None:
    value = text.strip()
    if value.isdigit():
        number = int(value)
        if number > 10**16:
            return datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=number // 10)
        return datetime.fromtimestamp(number / 1000 if number > 10**11 else number, timezone.utc)
    return parse_time(value)


def active_er_events(rows: list[dict[str, Any]], now: datetime) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        kind = str(row.get("eventType", ""))
        start = event_time(str(row.get("start") or ""))
        end = event_time(str(row.get("end") or ""))
        if kind == "ServiceIssue":
            active = row.get("status") == "Active"
        elif start is None and end is None:
            active = row.get("status") == "Active"
        else:
            active = (start is None or start <= now) and (end is None or now <= end)
        if not active:
            continue
        event = {
            "trackingId": row.get("trackingId") or None,
            "eventType": kind,
            "title": row.get("title") or None,
            "start": iso(start) if start else None,
            "end": iso(end) if end else None,
        }
        sub = str(row.get("subscriptionId", "")).lower()
        if event not in out[sub]:
            out[sub].append(event)
    return dict(out)


def in_maintenance(report: Report, resource: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        e for e in report.er_events.get(str(resource.get("subscriptionId")).lower(), [])
        if e["eventType"] == "PlannedMaintenance"
    ]


def net_resource(row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    kind = NET_KINDS[str(row.get("type", "")).lower()]
    enabled = []
    for peering in row.get("peerings") or []:
        props = peering.get("properties") or peering if isinstance(peering, dict) else {}
        if str(props.get("state", "")).lower() == "enabled":
            enabled.append(str(props.get("peeringType", "")).lower())
    return {**row, "kind": kind, "tenantId": tenant_id, "enabledPeerings": enabled}


def check_recovery(
    window: timedelta, now: datetime, vm: dict[str, Any], row: dict[str, Any], report: Report,
) -> None:
    previous = str(row.get("previous", ""))
    changed = parse_time(str(row.get("occurred", "")))
    if previous not in UNHEALTHY or changed is None or not timedelta(0) <= now - changed <= window:
        return
    report.findings.append({
        "rule": "health-recovered",
        "severity": "info",
        "resource": vm_ref(vm),
        "episode": f"{vm['id']}|health|{previous}->Available|{iso(changed)}",
        "observed": {"previousAvailabilityState": previous, "recoveredAt": iso(changed)},
    })


def unhealthy_since(history: list[dict[str, Any]], state: str) -> datetime | None:
    since = None
    for record in history:
        properties = record.get("properties") or {}
        current = properties.get("availabilityState")
        if not current:
            continue
        if current != state:
            break
        since = parse_time(str(properties.get("occuredTime") or properties.get("occurredTime") or ""))
    return since


def age_bucket(age: timedelta | None) -> str:
    if age is None:
        return "unknown"
    for limit, label in AGE_BUCKETS:
        if age < limit:
            return label
    return ">7d"


def add_unhealthy_since(
    azure: Azure, tenants: dict[str, Tenant], now: datetime, report: Report,
) -> None:
    def lookup(row: dict[str, Any]) -> None:
        if azure.past_deadline():
            return
        tenant = tenants.get(str(row.get("tenantId")))
        if tenant is None:
            return
        try:
            history = azure.health_history(tenant, str(row["id"]))
        except CollectorError as exc:
            report.gaps.append(f"{row.get('name')} health history: {exc}")
            return
        since = unhealthy_since(history, str(row["availabilityState"]))
        if since is not None:
            row["since"] = iso(since)
            row["ageHours"] = round((now - since).total_seconds() / 3600, 1)

    with ThreadPoolExecutor(max_workers=azure.cfg.workers) as pool:
        list(pool.map(lookup, report.unhealthy[:300]))


def loop_shadow(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        state = str(row.get("availabilityState"))
        hours = row.get("ageHours")
        age = None if hours is None else timedelta(hours=float(hours))
        if age is not None and age < LOOP_TRACK_AGE[state]:
            continue
        buckets[f"{state} {age_bucket(age)}"].append(str(row.get("name")))
    return {
        "rule": "health: Unavailable >=30m, Degraded >=4h",
        "wouldTrack": sum(len(v) for v in buckets.values()),
        "byAge": [
            {"bucket": k, "count": len(v), "examples": v[:3]}
            for k, v in sorted(buckets.items())
        ],
    }


def check_annotations(
    cfg: Config, now: datetime, vms: dict[str, dict[str, Any]], rows: list[dict[str, Any]],
    report: Report,
) -> None:
    window = cfg.interval + cfg.health_lag
    for row in rows:
        vm = vms.get(str(row.get("target", "")))
        if vm is None or str(row.get("context", "")).lower() == "customer initiated":
            continue
        since = parse_time(str(row.get("occurred", "")))
        if since is None or not timedelta(0) <= now - since <= window:
            continue
        observed = {
            key: row.get(key) or None
            for key in ("annotation", "context", "category", "impactType", "summary", "reason")
        }
        report.findings.append({
            "rule": "platform-event",
            "severity": "warning",
            "resource": vm_ref(vm),
            "episode": f"{vm['id']}|annotation|{row.get('annotation')}|{iso(since)}",
            "observed": {**observed, "occurred": iso(since)},
        })


def fetch_chunk(
    azure: Azure, tenant: Tenant, subscription: str, region: str, chunk: list[dict[str, Any]],
    split: str | None, names: list[str], start: datetime, end: datetime, report: Report,
) -> dict[str, list[dict[str, Any]]]:
    if azure.past_deadline():
        with azure.http.lock:
            report.skipped += 1
        return {}
    filter_expr = f"{split} eq '*'" if split else None
    ids = [vm["id"] for vm in chunk]
    attempts = [names] + ([[n] for n in names] if len(names) > 1 else [])
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, attempt in enumerate(attempts):
        try:
            values = azure.batch_metrics(
                tenant, subscription, region, ids, attempt, start, end, filter_expr
            )
        except CollectorError as exc:
            if index == 0 and len(attempts) > 1:
                continue
            report.gaps.append(f"{subscription}/{region} {','.join(attempt)}: {exc}")
            continue
        for entry in values:
            found[str(entry.get("resourceid", "")).lower()].extend(entry.get("value") or [])
        if index == 0:
            break
    return found


def baseline_of(
    azure: Azure, cfg: Config, tenant: Tenant, vm: dict[str, Any], name: str, split: str | None,
    label: str, now: datetime, report: Report,
) -> tuple[float | None, int | None]:
    filter_expr = f"{split} eq '{label.split(' ', 1)[1]}'" if split and label else None
    try:
        metrics = azure.hourly(
            tenant, vm["id"], name, now - timedelta(days=cfg.baseline_days),
            now - timedelta(hours=1), filter_expr,
        )
    except CollectorError as exc:
        report.gaps.append(f"{vm['name']} {name} baseline: {exc}")
        return None, None
    points = [(t, v) for m in metrics for _, series in series_of(m, None) for t, v in series.items()]
    if not points:
        return None, None
    days = len({t.date() for t, v in points if v > cfg.latency_ms})
    return statistics.median(v for _, v in points), days


def nat_ref(nat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": nat["id"],
        "name": nat.get("name"),
        "subscriptionId": nat.get("subscriptionId"),
        "location": nat.get("location"),
        "subnets": nat.get("subnets"),
        "publicIps": nat.get("publicIps"),
    }


def held(
    points: dict[datetime, float], end: datetime, minutes: int, need: int, hit: Any,
) -> bool:
    return sum(1 for v in window_values(points, end, minutes) if hit(v)) >= need


def onset(
    points: dict[datetime, float], end: datetime, cfg: Config, hit: Any,
    minutes: int | None = None, need: int | None = None,
) -> bool:
    minutes = minutes or cfg.window
    need = need or cfg.sustain
    return held(points, end, minutes, need, hit) and not held(
        points, end - cfg.interval, minutes, need, hit
    )


def dims_of(ts: dict[str, Any]) -> dict[str, str]:
    return {
        str((m.get("name") or {}).get("value", "")).lower(): str(m.get("value"))
        for m in ts.get("metadatavalues") or []
    }


def points_of(ts: dict[str, Any], field_name: str) -> dict[datetime, float]:
    points = {}
    for point in ts.get("data") or []:
        moment = parse_time(str(point.get("timeStamp", "")))
        if moment is not None and point.get(field_name) is not None:
            points[moment] = float(point[field_name])
    return points


def hourly_values(
    azure: Azure, cfg: Config, tenant: Tenant, resource: dict[str, Any], name: str,
    filter_expr: str | None, aggregation: str, now: datetime, report: Report,
) -> list[float] | None:
    try:
        metrics = azure.hourly(
            tenant, resource["id"], name, now - timedelta(days=cfg.baseline_days),
            now - timedelta(hours=1), filter_expr, aggregation,
        )
    except CollectorError as exc:
        report.gaps.append(f"{resource.get('name')} {name} baseline: {exc}")
        return None
    return [
        v for m in metrics for _, pts in series_of(m, None, aggregation.lower()) for v in pts.values()
    ]


ALERT_FLAGS = {"nat": "nat_alerts", "er": "er_alerts", "agw": "agw_alerts", "lb": "lb_alerts"}
LOWER_IS_WORSE = {
    "nat-datapath", "er-bgp", "er-arp", "er-maintenance", "er-routes", "agw-pool-down",
    "lb-datapath", "lb-backend",
}


def metric_finding(
    cfg: Config, report: Report, rule: str, severity: str, resource: dict[str, Any],
    end: datetime, observed: dict[str, Any], worst: float, key: str = "",
) -> None:
    with report.lock:
        report.shadow.setdefault(rule, []).append(round(float(worst), 2))
    if rule == "er-maintenance" or not getattr(cfg, ALERT_FLAGS[rule.split("-")[0]]):
        return
    report.findings.append({
        "rule": rule,
        "severity": severity,
        "resource": resource,
        "episode": "|".join(p for p in (resource["id"], rule, key, iso(end)) if p),
        "observed": observed,
    })


def regional(items: list[dict[str, Any]]) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        key = (str(item["tenantId"]), str(item["subscriptionId"]).lower(), str(item["location"]))
        groups[key].append(item)
    return [
        (tenant, sub, region, members[i : i + BATCH_RESOURCE_CHUNK])
        for (tenant, sub, region), members in sorted(groups.items())
        for i in range(0, len(members), BATCH_RESOURCE_CHUNK)
    ]


def metric_jobs(
    azure: Azure, tenants: dict[str, Tenant], items: list[dict[str, Any]], label: str,
    requests: list[tuple[list[str], str | None, str, str]], namespace: str, start: datetime,
    end: datetime, evaluate: Any, report: Report,
) -> list[Any]:
    def job(tenant_id: str, sub: str, region: str, chunk: list[dict[str, Any]]) -> None:
        if azure.past_deadline():
            report.gaps.append(f"{sub}/{region} {label} metrics: skipped at deadline")
            return
        by_id = {r["id"]: r for r in chunk}
        found: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for names, filter_expr, aggregation, interval in requests:
            try:
                values = azure.batch_metrics(
                    tenants[tenant_id], sub, region, list(by_id), names, start, end, filter_expr,
                    namespace, aggregation, interval,
                )
            except CollectorError as exc:
                report.gaps.append(f"{sub}/{region} {label} metrics: {exc}")
                continue
            for entry in values:
                found[str(entry.get("resourceid", "")).lower()].extend(entry.get("value") or [])
        for rid, metrics in found.items():
            if rid in by_id:
                evaluate(tenants[tenant_id], by_id[rid], metrics)

    return [
        (lambda t=t, s=s, r=r, c=c: job(t, s, r, c)) for t, s, r, c in regional(items)
    ]


def nat_jobs(
    azure: Azure, cfg: Config, now: datetime, tenants: dict[str, Tenant],
    nats: list[dict[str, Any]], report: Report,
) -> list[Any]:
    end = now - cfg.metric_lag
    start = end - cfg.interval - timedelta(minutes=cfg.window + 5)

    def evaluate(tenant: Tenant, nat: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
        for metric in metrics:
            evaluate_nat(azure, cfg, tenant, now, end, nat, metric, report)

    return metric_jobs(azure, tenants, nats, "NAT", [
        (["DatapathAvailability", "PacketDropCount"], None, "average,total", "PT1M"),
        (["SNATConnectionCount"], "ConnectionState eq '*'", "total", "PT1M"),
    ], NAT_NAMESPACE, start, end, evaluate, report)


def evaluate_nat(
    azure: Azure, cfg: Config, tenant: Tenant, now: datetime, end: datetime,
    nat: dict[str, Any], metric: dict[str, Any], report: Report,
) -> None:
    name = str((metric.get("name") or {}).get("value", ""))
    if name == "DatapathAvailability":
        for _, points in series_of(metric, None):
            if onset(points, end, cfg, lambda v: v < DATAPATH_MIN):
                current = window_values(points, end, cfg.window)
                metric_finding(cfg, report, "nat-datapath", "critical", nat_ref(nat), end, {
                    "metric": name, "median": round(statistics.median(current), 2),
                    "min": round(min(current), 2), "threshold": DATAPATH_MIN,
                }, min(current))
        return
    if name == "PacketDropCount":
        rule, floor, filter_expr, field_name, wanted = (
            "nat-packet-drops", NAT_DROPS_FLOOR, None, "total", None
        )
    elif name == "SNATConnectionCount":
        rule, floor, filter_expr, field_name, wanted = (
            "nat-snat-failures", NAT_FAILED_FLOOR, "ConnectionState eq 'Failed'", "total",
            "ConnectionState Failed",
        )
    else:
        return
    split = "ConnectionState" if wanted else None
    for label, points in series_of(metric, split, field_name):
        if wanted and label.lower() != wanted.lower():
            continue
        if not onset(points, end, cfg, lambda v: v > floor):
            continue
        current = window_values(points, end, cfg.window)
        level = statistics.median(current)
        hourly = hourly_values(azure, cfg, tenant, nat, name, filter_expr, "Total", now, report)
        baseline = statistics.median(hourly) / 60 if hourly else None
        if baseline is not None and level <= NAT_RATIO * baseline:
            continue
        metric_finding(cfg, report, rule, "warning", nat_ref(nat), end, {
            "metric": name, "series": label or None, "median": round(level, 2),
            "max": round(max(current), 2), "floorPerMinute": floor,
            "baselinePerMinute": None if baseline is None else round(baseline, 2),
        }, max(current))


def net_ref(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": resource["id"],
        "name": resource.get("name"),
        "kind": resource.get("kind"),
        "subscriptionId": resource.get("subscriptionId"),
        "resourceGroup": resource.get("resourceGroup"),
        "location": resource.get("location"),
    }


def er_circuits(net: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r for r in net
        if r["kind"] == "expressRouteCircuit" and r["enabledPeerings"]
        and str(r.get("providerState", "")).lower() == "provisioned"
        and str(r.get("circuitState", "")).lower() == "enabled"
    ]


def er_jobs(
    azure: Azure, cfg: Config, now: datetime, tenants: dict[str, Tenant],
    net: list[dict[str, Any]], report: Report,
) -> list[Any]:
    end = now - cfg.metric_lag
    start = end - cfg.interval - timedelta(minutes=cfg.window + 5)
    gw_minutes = max(cfg.window, ER_CPU_MINUTES)
    gw_start = end - cfg.interval - timedelta(minutes=gw_minutes + 10)

    def links(tenant: Tenant, circuit: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
        for metric in metrics:
            evaluate_er_links(cfg, end, circuit, metric, report)

    def gateway(tenant: Tenant, gw: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
        for metric in metrics:
            evaluate_er_gateway(azure, cfg, tenant, now, end, gw, metric, report)

    gateways = [r for r in net if r["kind"] == "expressRouteGateway"]
    return metric_jobs(azure, tenants, er_circuits(net), "ExpressRoute", [
        (["BgpAvailability", "ArpAvailability"], "PeeringType eq '*' and Peer eq '*'", "average", "PT1M"),
    ], ER_NAMESPACE, start, end, links, report) + metric_jobs(azure, tenants, gateways, "ER gateway", [
        (["ExpressRouteGatewayCountOfRoutesLearnedFromPeer"], None, "maximum", "PT5M"),
        (["ExpressRouteGatewayCpuUtilization"], "roleInstance eq '*'", "average", "PT5M"),
    ], ERGW_NAMESPACE, gw_start, end, gateway, report)


def evaluate_er_links(
    cfg: Config, end: datetime, circuit: dict[str, Any], metric: dict[str, Any], report: Report,
) -> None:
    name = str((metric.get("name") or {}).get("value", ""))
    rule = {"BgpAvailability": "er-bgp", "ArpAvailability": "er-arp"}.get(name)
    if rule is None:
        return
    sides: dict[str, dict[str, dict[datetime, float]]] = defaultdict(dict)
    for ts in metric.get("timeseries") or []:
        dims = dims_of(ts)
        peering = dims.get("peeringtype", "")
        if peering and any(peering.lower() in p for p in circuit["enabledPeerings"]):
            sides[peering][dims.get("peer", "")] = points_of(ts, "average")
    for peering, peers in sorted(sides.items()):
        def level(at: datetime) -> list[str]:
            return sorted(
                p for p, pts in peers.items()
                if held(pts, at, cfg.window, cfg.sustain, lambda v: v < DATAPATH_MIN)
            )

        now_down, before = level(end), level(end - cfg.interval)
        if len(now_down) <= len(before):
            continue
        current = [v for p in now_down for v in window_values(peers[p], end, cfg.window)]
        both = len(now_down) >= 2
        maintenance = in_maintenance(report, circuit)
        observed = {
            "metric": name, "peeringType": peering, "downPeers": now_down, "peers": len(peers),
            "min": round(min(current), 2), "threshold": DATAPATH_MIN,
            "serviceHealth": report.er_events.get(str(circuit.get("subscriptionId")).lower()),
        }
        metric_finding(
            cfg, report, "er-maintenance" if maintenance and not both else rule, "critical" if both else "warning", net_ref(circuit), end,
            observed, min(current), f"{peering}|{len(now_down)}",
        )


def evaluate_er_gateway(
    azure: Azure, cfg: Config, tenant: Tenant, now: datetime, end: datetime,
    gw: dict[str, Any], metric: dict[str, Any], report: Report,
) -> None:
    name = str((metric.get("name") or {}).get("value", ""))
    if name == "ExpressRouteGatewayCpuUtilization":
        need = ER_CPU_MINUTES // 5 - 1
        for ts in metric.get("timeseries") or []:
            points = points_of(ts, "average")
            if onset(points, end, cfg, lambda v: v >= ER_CPU_MAX, ER_CPU_MINUTES, need):
                current = window_values(points, end, ER_CPU_MINUTES)
                instance = dims_of(ts).get("roleinstance", "")
                metric_finding(cfg, report, "er-gateway-cpu", "warning", net_ref(gw), end, {
                    "metric": name, "instance": instance or None,
                    "median": round(statistics.median(current), 2),
                    "max": round(max(current), 2), "threshold": ER_CPU_MAX,
                }, max(current), instance)
        return
    if name != "ExpressRouteGatewayCountOfRoutesLearnedFromPeer":
        return
    for ts in metric.get("timeseries") or []:
        points = points_of(ts, "maximum")
        minutes = max(cfg.window, 15)
        need = minutes // 5 - 1
        current = window_values(points, end, minutes)
        if not current:
            continue
        if onset(points, end, cfg, lambda v: v <= 0, minutes, need):
            metric_finding(cfg, report, "er-routes", "critical", net_ref(gw), end, {
                "metric": name, "current": 0, "before": max(points.values()),
            }, 0.0)
            continue
        level = statistics.median(current)
        if level <= 0 or level >= max(points.values()):
            continue
        hourly = hourly_values(azure, cfg, tenant, gw, name, None, "Maximum", now, report)
        if not hourly:
            continue
        baseline = statistics.median(hourly)
        limit = ER_ROUTES_DROP * baseline
        if onset(points, end, cfg, lambda v: v < limit, minutes, need):
            metric_finding(cfg, report, "er-routes", "warning", net_ref(gw), end, {
                "metric": name, "median": level, "baselineMedian": baseline,
                "dropThreshold": ER_ROUTES_DROP,
            }, level)


def is_agw_v1(agw: dict[str, Any]) -> bool:
    return not str(agw.get("tier") or agw.get("sku") or "").lower().endswith("_v2")


def agw_jobs(
    azure: Azure, cfg: Config, now: datetime, tenants: dict[str, Tenant],
    net: list[dict[str, Any]], report: Report,
) -> list[Any]:
    end = now - cfg.metric_lag
    start = end - cfg.interval - timedelta(minutes=cfg.window + 5)
    agws = [
        r for r in net
        if r["kind"] == "applicationGateway" and not is_agw_v1(r)
        and str(r.get("operationalState", "")).lower() != "stopped"
    ]

    def evaluate(tenant: Tenant, agw: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
        evaluate_agw(azure, cfg, tenant, now, end, agw, metrics, report)

    return metric_jobs(azure, tenants, agws, "App Gateway", [
        (["HealthyHostCount", "UnhealthyHostCount"], "BackendSettingsPool eq '*'", "average", "PT1M"),
        (["ResponseStatus", "BackendResponseStatus"], "HttpStatusGroup eq '*'", "total", "PT1M"),
    ], AGW_NAMESPACE, start, end, evaluate, report)


def evaluate_agw(
    azure: Azure, cfg: Config, tenant: Tenant, now: datetime, end: datetime,
    agw: dict[str, Any], metrics: list[dict[str, Any]], report: Report,
) -> None:
    hosts: dict[str, dict[str, dict[datetime, float]]] = defaultdict(dict)
    status: dict[str, dict[str, dict[datetime, float]]] = defaultdict(dict)
    for metric in metrics:
        name = str((metric.get("name") or {}).get("value", ""))
        for ts in metric.get("timeseries") or []:
            dims = dims_of(ts)
            if name in ("HealthyHostCount", "UnhealthyHostCount"):
                hosts[name][dims.get("backendsettingspool", "")] = points_of(ts, "average")
            elif name in ("ResponseStatus", "BackendResponseStatus"):
                status[name][dims.get("httpstatusgroup", "")] = points_of(ts, "total")
    healthy, unhealthy = hosts["HealthyHostCount"], hosts["UnhealthyHostCount"]
    for pool in sorted(set(healthy) | set(unhealthy)):
        good = healthy.get(pool, {})
        bad = unhealthy.get(pool, {})
        before = window_values(good, end - cfg.interval, cfg.window)
        if onset(good, end, cfg, lambda v: v < 0.5) and any(v >= 0.5 for v in before):
            metric_finding(cfg, report, "agw-pool-down", "critical", net_ref(agw), end, {
                "pool": pool or None, "healthyBefore": round(max(before), 2),
                "unhealthyMax": round(max(window_values(bad, end, cfg.window) or [0.0]), 2),
            }, 0.0, pool)
        elif onset(bad, end, cfg, lambda v: v > 0):
            current = window_values(bad, end, cfg.window)
            metric_finding(cfg, report, "agw-unhealthy", "warning", net_ref(agw), end, {
                "pool": pool or None, "unhealthyMax": round(max(current), 2),
                "healthyMin": round(min(window_values(good, end, cfg.window) or [0.0]), 2),
            }, max(current), pool)
    evaluate_agw_5xx(azure, cfg, tenant, now, end, agw, status, report)


def five_xx(groups: dict[str, dict[datetime, float]], at: datetime, minutes: int) -> tuple[float, float]:
    total = sum(sum(window_values(points, at, minutes)) for points in groups.values())
    return total, sum(window_values(groups.get("5xx", {}), at, minutes))


def evaluate_agw_5xx(
    azure: Azure, cfg: Config, tenant: Tenant, now: datetime, end: datetime,
    agw: dict[str, Any], status: dict[str, dict[str, dict[datetime, float]]], report: Report,
) -> None:
    groups = status.get("ResponseStatus", {})

    def hit(at: datetime) -> bool:
        total, errors = five_xx(groups, at, cfg.window)
        return total >= AGW_MIN_REQUESTS and errors * 100 >= AGW_5XX_PCT * total

    if not hit(end) or hit(end - cfg.interval):
        return
    total, errors = five_xx(groups, end, cfg.window)
    pct = 100 * errors / total
    try:
        hourly = azure.hourly(
            tenant, agw["id"], "ResponseStatus", now - timedelta(days=cfg.baseline_days),
            now - timedelta(hours=1), "HttpStatusGroup eq '*'", "Total",
        )
    except CollectorError as exc:
        report.gaps.append(f"{agw.get('name')} ResponseStatus baseline: {exc}")
        hourly = []
    totals: dict[datetime, float] = defaultdict(float)
    failed: dict[datetime, float] = defaultdict(float)
    for metric in hourly:
        for ts in metric.get("timeseries") or []:
            group = dims_of(ts).get("httpstatusgroup", "")
            for moment, value in points_of(ts, "total").items():
                totals[moment] += value
                if group == "5xx":
                    failed[moment] += value
    shares = [100 * failed[t] / v for t, v in totals.items() if v > 0]
    baseline = statistics.median(shares) if shares else None
    if baseline is not None and pct <= AGW_RATIO * baseline:
        return
    backend_total, backend_errors = five_xx(status.get("BackendResponseStatus", {}), end, cfg.window)
    metric_finding(cfg, report, "agw-5xx", "warning", net_ref(agw), end, {
        "pct": round(pct, 2), "requests": int(total), "errors": int(errors),
        "backend5xx": int(backend_errors) if backend_total else None,
        "baselinePct": None if baseline is None else round(baseline, 2),
    }, pct)


def lb_jobs(
    azure: Azure, cfg: Config, now: datetime, tenants: dict[str, Tenant],
    net: list[dict[str, Any]], report: Report,
) -> list[Any]:
    end = now - cfg.metric_lag
    start = end - cfg.interval - timedelta(minutes=cfg.window + 5)
    lbs = [r for r in net if r["kind"] == "loadBalancer" and str(r.get("sku", "")).lower() == "standard"]

    def evaluate(tenant: Tenant, lb: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
        for metric in metrics:
            evaluate_lb(cfg, end, lb, metric, report)

    return metric_jobs(azure, tenants, lbs, "load balancer", [
        (["VipAvailability", "DipAvailability"], None, "average", "PT1M"),
    ], LB_NAMESPACE, start, end, evaluate, report)


def evaluate_lb(
    cfg: Config, end: datetime, lb: dict[str, Any], metric: dict[str, Any], report: Report,
) -> None:
    name = str((metric.get("name") or {}).get("value", ""))
    for _, points in series_of(metric, None):
        current = window_values(points, end, cfg.window)
        if name == "VipAvailability" and onset(points, end, cfg, lambda v: v < DATAPATH_MIN):
            metric_finding(cfg, report, "lb-datapath", "critical", net_ref(lb), end, {
                "metric": name, "median": round(statistics.median(current), 2),
                "min": round(min(current), 2), "threshold": DATAPATH_MIN,
            }, min(current))
        if name != "DipAvailability" or str(lb.get("name", "")).lower() in AKS_LB_NAMES:
            continue

        def level(at: datetime) -> int:
            if held(points, at, cfg.window, cfg.sustain, lambda v: v < 1):
                return 2
            return 1 if held(points, at, cfg.window, cfg.sustain, lambda v: v < 99.5) else 0

        now_level = level(end)
        if now_level > level(end - cfg.interval):
            metric_finding(
                cfg, report, "lb-backend", "critical" if now_level == 2 else "warning", net_ref(lb),
                end, {
                    "metric": name, "healthyPctMedian": round(statistics.median(current), 2),
                    "healthyPctMin": round(min(current), 2),
                }, min(current), str(now_level),
            )


def er_bandwidth(
    azure: Azure, cfg: Config, now: datetime, tenants: dict[str, Tenant],
    net: list[dict[str, Any]], report: Report,
) -> list[dict[str, Any]]:
    out = []
    for circuit in er_circuits(net):
        if azure.past_deadline():
            break
        try:
            metrics = azure.hourly(
                tenants[circuit["tenantId"]], circuit["id"],
                "IngressBandwidthUtilization,EgressBandwidthUtilization",
                now - timedelta(days=1), now, None, "Maximum", "PT1M",
            )
        except CollectorError as exc:
            report.gaps.append(f"{circuit.get('name')} bandwidth: {exc}")
            continue
        row: dict[str, Any] = {"name": circuit.get("name")}
        for metric in metrics:
            name = str((metric.get("name") or {}).get("value", ""))
            values = [v for _, pts in series_of(metric, None, "maximum") for _, v in sorted(pts.items())]
            if not values:
                continue
            run = longest = 0
            for v in values:
                run = run + 1 if v >= ER_BANDWIDTH_PCT else 0
                longest = max(longest, run)
            key = "ingress" if name.startswith("Ingress") else "egress"
            row[f"{key}PeakPct"] = round(max(values), 1)
            row[f"{key}LongestMinutesOver"] = longest
        if any(row.get(f"{k}LongestMinutesOver", 0) >= cfg.sustain for k in ("ingress", "egress")):
            out.append(row)
    return out


def check_disks(
    azure: Azure, cfg: Config, now: datetime, tenants: dict[str, Tenant],
    vms: dict[str, dict[str, Any]], report: Report,
) -> None:
    end = now - cfg.metric_lag
    start = end - cfg.interval - timedelta(minutes=cfg.window + 5)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for vm in vms.values():
        if vm.get("powerState") == "PowerState/running":
            report.running += 1
            key = (str(vm["tenantId"]), str(vm["subscriptionId"]).lower(), str(vm["location"]))
            groups[key].append(vm)
    jobs = []
    for (tenant_id, subscription, region), members in sorted(groups.items()):
        for i in range(0, len(members), BATCH_RESOURCE_CHUNK):
            for split, checks in METRIC_GROUPS:
                jobs.append((
                    tenants[tenant_id], subscription, region,
                    members[i : i + BATCH_RESOURCE_CHUNK], split, checks,
                ))
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        results = list(pool.map(
            lambda job: fetch_chunk(
                azure, job[0], job[1], job[2], job[3], job[4], list(job[5]), start, end, report
            ),
            jobs,
        ))
    for (tenant, _, _, members, split, checks), found in zip(jobs, results):
        for vm in members:
            disks: dict[str, dict[str, dict[datetime, float]]] = defaultdict(dict)
            for metric in found.get(vm["id"], []):
                name = str((metric.get("name") or {}).get("value", ""))
                if name not in checks:
                    continue
                for label, points in series_of(metric, split):
                    report.series += 1
                    disks[label][name] = points
            for label, metrics in disks.items():
                latency = [
                    v for n, pts in metrics.items() if checks[n] is LATENCY
                    for v in window_values(pts, end, cfg.window)
                ]
                latency_level = statistics.median(latency) if latency else None
                slow = latency_level is not None and latency_level > cfg.latency_ms
                for name, points in metrics.items():
                    check = checks[name]
                    limit = float(getattr(cfg, check.threshold))
                    if check is SATURATION and not slow:
                        if sustained(window_values(points, end, cfg.window), limit, cfg.sustain):
                            report.saturated += 1
                        continue
                    extra = (
                        {"latencyMedian": round(latency_level, 2)}
                        if check is SATURATION and latency_level is not None
                        else {}
                    )
                    evaluate_series(
                        azure, cfg, tenant, now, end, vm, name, split, label, points, check,
                        limit, report, extra,
                    )


def evaluate_series(
    azure: Azure, cfg: Config, tenant: Tenant, now: datetime, end: datetime,
    vm: dict[str, Any], name: str, split: str | None, label: str,
    points: dict[datetime, float], check: Check, limit: float, report: Report,
    extra: dict[str, Any],
) -> None:
    current = window_values(points, end, cfg.window)
    if len(current) < cfg.sustain:
        report.gaps.append(f"{vm['name']} {name} {label}".strip() + f": {len(current)} samples")
        return
    if not sustained(current, limit, cfg.sustain):
        return
    if sustained(window_values(points, end - cfg.interval, cfg.window), limit, cfg.sustain):
        return
    level = statistics.median(current)
    baseline = None
    days = None
    if check.uses_baseline and azure.past_deadline():
        report.gaps.append(f"{vm['name']} {name} {label}: baseline skipped at deadline")
        return
    if check.uses_baseline:
        baseline, days = baseline_of(azure, cfg, tenant, vm, name, split, label, now, report)
        if baseline is not None and level <= cfg.latency_ratio * baseline:
            return
    report.findings.append({
        "rule": check.rule,
        "severity": "warning",
        "resource": vm_ref(vm),
        "episode": f"{vm['id']}|{name}|{label}|{iso(end)}",
        "observed": {
            "metric": name,
            "series": label or None,
            "windowEnd": iso(end),
            "windowMinutes": cfg.window,
            "samplesOverThreshold": sum(1 for v in current if v > limit),
            "threshold": limit,
            "median": round(level, 2),
            "max": round(max(current), 2),
            "baselineMedian": None if baseline is None else round(baseline, 2),
            "baselineDays": cfg.baseline_days if check.uses_baseline else None,
            "daysOverThreshold": days,
            "wouldTrack": None if days is None else days >= LOOP_TRACK_DISK_DAYS,
            **extra,
        },
    })


def tenant_label(report: Report, tenant_id: str) -> str:
    name = report.tenant_names.get(tenant_id)
    return f"{tenant_id} ({name})" if name else tenant_id


def unhealthy_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("kind") or "virtualMachine"),
            str(row.get("subscriptionId")),
            str(row.get("location")),
            str(row.get("availabilityState")),
            str(row.get("title") or row.get("reasonType") or ""),
        )
        groups[key].append(row)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    out = []
    for (kind, sub, loc, state, reason), members in ordered[:20]:
        since = sorted(str(m["since"]) for m in members if m.get("since"))
        out.append({
            "kind": kind, "subscription": sub, "location": loc, "state": state, "reason": reason,
            "count": len(members),
            "newIn24h": sum(1 for m in members if m.get("ageHours", 99) < 24),
            "oldestSince": since[0] if since else None,
            "examples": [str(m.get("name")) for m in members[:3]],
        })
    return out


def compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def short_finding(finding: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "rule": finding["rule"],
        "severity": finding["severity"],
        "episode": finding["episode"],
        "observed": finding["observed"],
    }
    resource = finding.get("resource")
    if resource:
        out["episode"] = hashlib.sha1(finding["episode"].encode()).hexdigest()[:12]
        out["resource"] = {k: resource.get(k) for k in ("id", "name", "computerName", "kind", "location")}
        out["observed"] = {
            k: v for k, v in finding["observed"].items()
            if k not in ("windowEnd", "windowMinutes", "baselineDays", "threshold")
        }
    return dict(compact(out))


DISK_RULES = ("disk-latency", "disk-saturation")


def disk_latency(finding: dict[str, Any]) -> float:
    observed = finding.get("observed") or {}
    if finding.get("rule") == "disk-latency":
        return float(observed.get("median") or 0)
    return float(observed.get("latencyMedian") or 0)


def merge_disk_burst(cfg: Config, now: datetime, report: Report) -> None:
    disks = [f for f in report.findings if f["rule"] in DISK_RULES]
    if len(disks) <= cfg.disk_burst:
        return
    report.findings = [f for f in report.findings if f["rule"] not in DISK_RULES]
    groups: dict[tuple[str, str, str], int] = defaultdict(int)
    for f in disks:
        r = f["resource"]
        groups[(str(r.get("subscriptionId")), str(r.get("location")), f["rule"])] += 1
    worst = sorted(disks, key=lambda f: -disk_latency(f))[:5]
    report.findings.append({
        "rule": "disk-burst",
        "severity": "warning",
        "resource": None,
        "episode": f"disk-burst|{iso(now)}",
        "observed": {
            "count": len(disks),
            "groups": [
                {"subscription": sub, "location": loc, "rule": rule, "count": n}
                for (sub, loc, rule), n in sorted(groups.items(), key=lambda kv: -kv[1])[:10]
            ],
            "worst": [
                {
                    "name": f["resource"].get("name"),
                    "computerName": f["resource"].get("computerName"),
                    "rule": f["rule"],
                    "series": f["observed"].get("series"),
                    "metric": f["observed"].get("metric"),
                    "latencyMs": disk_latency(f),
                    "baselineMedian": f["observed"].get("baselineMedian"),
                    "consumedPct": (
                        f["observed"].get("median") if f["rule"] == "disk-saturation" else None
                    ),
                }
                for f in worst
            ],
        },
    })


def finding_rank(finding: dict[str, Any]) -> tuple[int, int, float]:
    return (
        0 if finding["severity"] == "critical" else 1,
        0 if finding["rule"] in ("health-onset", "health-recovered", "platform-event", "daily-summary", "disk-burst") else 1,
        -disk_latency(finding),
    )


def collector_error(exc: CollectorError) -> dict[str, Any]:
    return {
        "rule": "collector-error",
        "severity": "critical",
        "resource": None,
        "episode": f"collector|{str(exc).split(':', 1)[0]}",
        "observed": {"error": str(exc)},
    }


def collect(cfg: Config, now: datetime, report: Report) -> None:
    azure = Azure(cfg, report)
    wanted = {sub for sub, _ in cfg.scopes}
    tenants: dict[str, Tenant] = {}
    report.tenant_names = azure.tenants()
    for tenant_id in cfg.tenants or list(report.tenant_names):
        try:
            tenant = azure.open_tenant(tenant_id)
        except CollectorError as exc:
            report.blind.append(f"tenant {tenant_label(report, tenant_id)}: {exc}"[:240])
            continue
        if wanted:
            tenant.subscriptions = [s for s in tenant.subscriptions if s in wanted]
        if tenant.subscriptions:
            tenants[tenant_id] = tenant
    for sub in sorted(wanted - {s for t in tenants.values() for s in t.subscriptions}):
        report.blind.append(f"subscription {sub}: not visible to this identity")
    if not tenants:
        detail = "; ".join(report.blind) or "identity sees no enabled subscriptions"
        raise CollectorError(f"NO_COVERAGE: {detail}"[:900])
    report.tenants = len(tenants)
    report.readable = [
        f"{tenant_label(report, t.id)}: {len(t.subscriptions)} subscription(s)"
        for t in tenants.values()
    ]
    report.subscriptions = sum(len(t.subscriptions) for t in tenants.values())
    vms: dict[str, dict[str, Any]] = {}
    nats: list[dict[str, Any]] = []
    net: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    health: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    lookback = int((cfg.interval + cfg.health_lag).total_seconds() // 60) + 1
    unreadable = 0
    for tenant in tenants.values():
        try:
            for vm in azure.graph(tenant, VM_QUERY):
                vm["tenantId"] = tenant.id
                if in_scope(cfg, vm):
                    vms[str(vm["id"]).lower()] = vm
            for nat in azure.graph(tenant, NAT_QUERY):
                nat["tenantId"] = tenant.id
                if in_scope(cfg, nat):
                    nats.append(nat)
            for row in azure.graph(tenant, NET_QUERY):
                if in_scope(cfg, row) and str(row.get("type", "")).lower() in NET_KINDS:
                    net.append(net_resource(row, tenant.id))
            if any(r["kind"] == "expressRouteCircuit" for r in net):
                events.extend(azure.graph(tenant, SERVICE_HEALTH_QUERY))
            health.extend(azure.graph(tenant, HEALTH_QUERY))
            annotations.extend(azure.graph(tenant, ANNOTATION_QUERY.format(minutes=lookback)))
        except CollectorError as exc:
            unreadable += 1
            report.blind.append(f"tenant {tenant_label(report, tenant.id)} Resource Graph: {exc}"[:240])
    if unreadable == len(tenants):
        raise CollectorError(f"NO_COVERAGE: {'; '.join(report.blind)}"[:900])
    report.vms = len(vms)
    report.nats = len(nats)
    report.er_events = active_er_events(events, now)
    for row in net:
        report.network[row["kind"]] += 1
        if row["kind"] == "applicationGateway" and is_agw_v1(row):
            report.network["applicationGatewayV1"] += 1
        if row["kind"] == "loadBalancer" and str(row.get("sku", "")).lower() == "basic":
            report.network["loadBalancerBasic"] += 1
        if row["kind"] == "loadBalancer" and str(row.get("name", "")).lower() in AKS_LB_NAMES:
            report.network["loadBalancerAks"] += 1
    targets = {**vms, **{r["id"]: r for r in net if r["kind"] != "expressRouteGateway"}}
    check_health(cfg, now, targets, health, report)
    if cfg.summary_only:
        report.findings = []
        report.running = sum(1 for vm in vms.values() if vm.get("powerState") == "PowerState/running")
        add_unhealthy_since(azure, tenants, now, report)
        report.findings.append({
            "rule": "daily-summary",
            "severity": "warning",
            "resource": None,
            "episode": f"summary|{now.date().isoformat()}",
            "observed": {
                "notAvailableCount": len(report.unhealthy),
                "newIn24h": sum(1 for r in report.unhealthy if r.get("ageHours", 99) < 24),
                "notAvailableGroups": unhealthy_groups(report.unhealthy),
                "loopShadow": loop_shadow(report.unhealthy, now),
                "natGateways": report.nats,
                "expressRouteBandwidth": er_bandwidth(azure, cfg, now, tenants, net, report) or None,
                "network": dict(report.network) or None,
                "expressRouteServiceHealth": [
                    e for e in {
                        e["trackingId"]: e for v in report.er_events.values() for e in v
                    }.values()
                ] or None,
                "readableTenants": report.readable,
            },
        })
        return
    check_annotations(cfg, now, vms, annotations, report)
    jobs = (
        nat_jobs(azure, cfg, now, tenants, nats, report)
        + er_jobs(azure, cfg, now, tenants, net, report)
        + agw_jobs(azure, cfg, now, tenants, net, report)
        + lb_jobs(azure, cfg, now, tenants, net, report)
    )
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        list(pool.map(lambda job: job(), jobs))
    check_disks(azure, cfg, now, tenants, vms, report)
    merge_disk_burst(cfg, now, report)


def run(cfg: Config) -> tuple[int, str]:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    report = Report()
    try:
        collect(cfg, now, report)
    except CollectorError as exc:
        failed = Report(calls=report.calls, blind=report.blind)
        failed.findings.append(collector_error(exc))
        return 1, render(now, cfg, failed, time.monotonic() - started)
    return (1 if report.findings else 0), render(now, cfg, report, time.monotonic() - started)


def shadow_text(report: Report, family: str) -> str:
    rules = {r: v for r, v in report.shadow.items() if r.split("-")[0] == family}
    if not rules:
        return f"{family}: 0"
    parts = [
        f"{rule.removeprefix(family + '-')} {len(values)} "
        f"(worst {round(min(values) if rule in LOWER_IS_WORSE else max(values), 1)})"
        for rule, values in sorted(rules.items())
    ]
    return f"{family}: " + ", ".join(parts)


def render(now: datetime, cfg: Config, report: Report, elapsed: float) -> str:
    summary = (
        f"azure-ops watch {iso(now)}: {len(report.findings)} finding(s); "
        f"{report.tenants} tenant(s), {report.subscriptions} subscription(s), "
        f"{len(report.blind)} blind; {report.vms} VM(s), {report.running} running, "
        f"{len(report.unhealthy)} not Available, {report.series} disk series, "
        f"{report.saturated} saturated without latency, "
        f"{report.nats} NAT gateway(s) ({shadow_text(report, 'nat')}), "
        f"{report.network['expressRouteCircuit']} ER circuit(s) and "
        f"{report.network['expressRouteGateway']} ER gateway(s) ({shadow_text(report, 'er')}), "
        f"{report.network['applicationGateway']} app gateway(s) ({shadow_text(report, 'agw')}), "
        f"{report.network['loadBalancer']} load balancer(s) ({shadow_text(report, 'lb')}), "
        f"{len(report.gaps)} gap(s), {report.skipped} disk batch(es) skipped at deadline; "
        f"{report.calls} call(s) in {elapsed:.0f}s"
    )
    if not report.findings:
        return summary
    findings = [short_finding(f) for f in sorted(report.findings, key=finding_rank)]
    omitted: list[dict[str, Any]] = []
    while True:
        body: dict[str, Any] = {"generatedAt": iso(now), "findings": findings}
        if omitted:
            rules = sorted({f["rule"] for f in omitted})
            body["omitted"] = {
                "count": len(omitted),
                "rules": {r: sum(1 for f in omitted if f["rule"] == r) for r in rules},
            }
        if report.blind:
            body["blind"] = [b[:200] for b in report.blind]
        if report.gaps:
            body["gaps"] = {"count": len(report.gaps), "first": [g[:160] for g in report.gaps[:3]]}
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= OUTPUT_BUDGET or len(findings) <= 1:
            return text + "\n" + summary
        omitted.insert(0, findings.pop())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Azure VM, disk and network watch across every tenant and subscription "
        "the signed-in identity can read. Exit 0: nothing new. Exit 1: findings or collection "
        "failure, printed as JSON."
    )
    parser.add_argument("--tools-home", required=True)
    parser.add_argument("--tenant", action="append", default=[], type=parse_tenant)
    parser.add_argument("--scope", action="append", default=[], type=parse_scope)
    parser.add_argument("--arm-endpoint", default="https://management.azure.com")
    parser.add_argument("--metrics-endpoint", default="https://{region}.metrics.monitor.azure.com")
    parser.add_argument("--interval-minutes", type=int, default=5)
    parser.add_argument("--health-lag-minutes", type=int, default=10)
    parser.add_argument("--metric-lag-minutes", type=int, default=3)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--window-minutes", type=int, default=15)
    parser.add_argument("--sustain-minutes", type=int, default=10)
    parser.add_argument("--latency-ms", type=float, default=20.0)
    parser.add_argument("--latency-ratio", type=float, default=3.0)
    parser.add_argument("--saturation-pct", type=float, default=90.0)
    parser.add_argument("--baseline-days", type=int, default=7)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--deadline-seconds", type=float, default=100.0)
    parser.add_argument("--disk-burst", type=int, default=20)
    parser.add_argument("--nat-alerts", action="store_true")
    parser.add_argument("--er-alerts", action="store_true")
    parser.add_argument("--agw-alerts", action="store_true")
    parser.add_argument("--lb-alerts", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.sustain_minutes <= args.window_minutes:
        parser.error("--sustain-minutes must be between 1 and --window-minutes")
    if not args.tools_home.startswith("/"):
        parser.error("--tools-home must be an absolute path")
    cfg = Config(
        tools_home=args.tools_home,
        tenants=list(dict.fromkeys(args.tenant)),
        scopes=args.scope,
        arm=args.arm_endpoint,
        metrics_endpoint=args.metrics_endpoint,
        interval=timedelta(minutes=args.interval_minutes),
        health_lag=timedelta(minutes=args.health_lag_minutes),
        metric_lag=timedelta(minutes=args.metric_lag_minutes),
        summary_only=args.summary_only,
        window=args.window_minutes,
        sustain=args.sustain_minutes,
        latency_ms=args.latency_ms,
        latency_ratio=args.latency_ratio,
        saturation_pct=args.saturation_pct,
        baseline_days=args.baseline_days,
        workers=args.workers,
        deadline_seconds=args.deadline_seconds,
        disk_burst=args.disk_burst,
        nat_alerts=args.nat_alerts,
        er_alerts=args.er_alerts,
        agw_alerts=args.agw_alerts,
        lb_alerts=args.lb_alerts,
    )
    code, text = run(cfg)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
