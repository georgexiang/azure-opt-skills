#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
ARG_SUBSCRIPTION_CHUNK = 100
BATCH_RESOURCE_CHUNK = 50
UNHEALTHY = {"Degraded": "warning", "Unavailable": "critical"}
STOPPED = {"PowerState/deallocated", "PowerState/deallocating", "PowerState/stopped"}
RG_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.()")
HEX = set("0123456789abcdef")

VM_QUERY = """Resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id = tolower(id), name, subscriptionId, resourceGroup, location,
  computerName = tostring(properties.osProfile.computerName),
  powerState = tostring(properties.extended.instanceView.powerState.code)"""

HEALTH_QUERY = """HealthResources
| where type =~ 'microsoft.resourcehealth/availabilitystatuses'
| where tostring(properties.availabilityState) in ('Degraded', 'Unavailable')
| extend target = tolower(tostring(properties.targetResourceId))
| where target contains '/providers/microsoft.compute/virtualmachines/'
| project target,
  state = tostring(properties.availabilityState),
  occurred = tostring(properties.occurredTime),
  reasonType = tostring(properties.reasonType),
  title = tostring(properties['title']),
  summary = tostring(properties.summary)"""

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
    scopes: list[tuple[str, str | None]]
    arm: str
    metrics_endpoint: str
    interval: timedelta
    health_lag: timedelta
    metric_lag: timedelta
    reminder: timedelta
    coverage_hour: int
    window: int
    sustain: int
    latency_ms: float
    latency_ratio: float
    saturation_pct: float
    baseline_days: int
    workers: int


@dataclass
class Report:
    findings: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    blind: list[str] = field(default_factory=list)
    tenants: int = 0
    subscriptions: int = 0
    vms: int = 0
    running: int = 0
    unhealthy: int = 0
    series: int = 0
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
            timeout=60,
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


def known_tenants(tools_home: str) -> set[str]:
    result = az(tools_home, ["account", "list", "--all", "--query", "[].tenantId", "-o", "json"])
    try:
        values = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except ValueError:
        return set()
    return {str(v).lower() for v in values if isinstance(v, str) and is_uuid(v)}


class Http:
    def __init__(self, report: Report) -> None:
        self.report = report
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
            with urllib.request.urlopen(request, timeout=90) as response:
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
        self.http = Http(report)
        self.arm = cfg.arm.rstrip("/")

    def paged(self, url: str, token: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            page = self.http.call("GET", next_url, token) or {}
            rows.extend(page.get("value") or [])
            next_url = page.get("nextLink")
        return rows

    def tenants(self) -> list[str]:
        token = access_token(self.cfg.tools_home, self.arm, None)
        rows = self.paged(f"{self.arm}/tenants?api-version={ARM_LIST_API}", token)
        found = {str(t["tenantId"]).lower() for t in rows if t.get("tenantId")}
        return sorted(found | known_tenants(self.cfg.tools_home))

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
    ) -> list[dict[str, Any]]:
        query = {
            "api-version": BATCH_API,
            "metricnamespace": VM_NAMESPACE,
            "metricnames": ",".join(names),
            "starttime": iso(start),
            "endtime": iso(end),
            "interval": "PT1M",
            "aggregation": "average",
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

    def hourly(
        self, tenant: Tenant, resource: str, name: str, start: datetime, end: datetime,
        filter_expr: str | None,
    ) -> list[dict[str, Any]]:
        query = {
            "api-version": METRICS_API,
            "metricnames": name,
            "timespan": f"{iso(start)}/{iso(end)}",
            "interval": "PT1H",
            "aggregation": "Average",
        }
        if filter_expr:
            query["$filter"] = filter_expr
        url = (
            f"{self.arm}{resource}/providers/Microsoft.Insights/metrics?"
            f"{urllib.parse.urlencode(query)}"
        )
        return list((self.http.call("GET", url, tenant.arm_token) or {}).get("value") or [])


def series_of(metric: dict[str, Any], split: str | None) -> list[tuple[str, dict[datetime, float]]]:
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
            if moment is not None and point.get("average") is not None:
                points[moment] = float(point["average"])
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
    }


def in_scope(cfg: Config, vm: dict[str, Any]) -> bool:
    if not cfg.scopes:
        return True
    sub = str(vm.get("subscriptionId", "")).lower()
    rg = str(vm.get("resourceGroup", "")).lower()
    return any(s == sub and (g is None or g == rg) for s, g in cfg.scopes)


def check_health(
    cfg: Config, now: datetime, vms: dict[str, dict[str, Any]], rows: list[dict[str, Any]],
    report: Report,
) -> None:
    window = cfg.interval + cfg.health_lag
    for row in rows:
        vm = vms.get(str(row.get("target", "")))
        state = str(row.get("state", ""))
        if vm is None or state not in UNHEALTHY or vm.get("powerState") in STOPPED:
            continue
        report.unhealthy += 1
        since = parse_time(str(row.get("occurred", "")))
        if since is None:
            report.gaps.append(f"{vm['name']}: {state} without occurredTime")
            continue
        age = now - since
        if timedelta(0) <= age <= window:
            rule = "health-onset"
        elif age >= cfg.reminder and (age % cfg.reminder) < cfg.interval:
            rule = "health-persisting"
        else:
            continue
        report.findings.append({
            "rule": rule,
            "severity": UNHEALTHY[state],
            "resource": vm_ref(vm),
            "episode": f"{vm['id']}|health|{state}|{iso(since)}",
            "observed": {
                "availabilityState": state,
                "since": iso(since),
                "ageHours": round(age.total_seconds() / 3600, 1),
                "reasonType": row.get("reasonType") or None,
                "title": row.get("title") or None,
                "summary": row.get("summary") or None,
            },
        })


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
) -> float | None:
    filter_expr = f"{split} eq '{label.split(' ', 1)[1]}'" if split and label else None
    try:
        metrics = azure.hourly(
            tenant, vm["id"], name, now - timedelta(days=cfg.baseline_days),
            now - timedelta(hours=1), filter_expr,
        )
    except CollectorError as exc:
        report.gaps.append(f"{vm['name']} {name} baseline: {exc}")
        return None
    values = [v for m in metrics for _, points in series_of(m, None) for v in points.values()]
    return statistics.median(values) if values else None


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
            for metric in found.get(vm["id"], []):
                name = str((metric.get("name") or {}).get("value", ""))
                check = checks.get(name)
                if check is None:
                    continue
                limit = float(getattr(cfg, check.threshold))
                for label, points in series_of(metric, split):
                    report.series += 1
                    evaluate_series(
                        azure, cfg, tenant, now, end, vm, name, split, label, points, check,
                        limit, report,
                    )


def evaluate_series(
    azure: Azure, cfg: Config, tenant: Tenant, now: datetime, end: datetime,
    vm: dict[str, Any], name: str, split: str | None, label: str,
    points: dict[datetime, float], check: Check, limit: float, report: Report,
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
    if check.uses_baseline:
        baseline = baseline_of(azure, cfg, tenant, vm, name, split, label, now, report)
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
        },
    })


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
    for tenant_id in azure.tenants():
        try:
            tenant = azure.open_tenant(tenant_id)
        except CollectorError as exc:
            report.blind.append(f"tenant {tenant_id}: {exc}")
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
    report.subscriptions = sum(len(t.subscriptions) for t in tenants.values())
    vms: dict[str, dict[str, Any]] = {}
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
            health.extend(azure.graph(tenant, HEALTH_QUERY))
            annotations.extend(azure.graph(tenant, ANNOTATION_QUERY.format(minutes=lookback)))
        except CollectorError as exc:
            unreadable += 1
            report.blind.append(f"tenant {tenant.id} Resource Graph: {exc}")
    if unreadable == len(tenants):
        raise CollectorError(f"NO_COVERAGE: {'; '.join(report.blind)}"[:900])
    report.vms = len(vms)
    check_health(cfg, now, vms, health, report)
    check_annotations(cfg, now, vms, annotations, report)
    check_disks(azure, cfg, now, tenants, vms, report)
    if report.blind and now.hour == cfg.coverage_hour and now.minute < cfg.interval.total_seconds() // 60:
        report.findings.append({
            "rule": "coverage-report",
            "severity": "warning",
            "resource": None,
            "episode": f"coverage|{now.date().isoformat()}",
            "observed": {"blind": report.blind},
        })


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


def render(now: datetime, cfg: Config, report: Report, elapsed: float) -> str:
    summary = (
        f"azure-ops watch {iso(now)}: {len(report.findings)} finding(s); "
        f"{report.tenants} tenant(s), {report.subscriptions} subscription(s), "
        f"{len(report.blind)} blind; {report.vms} VM(s), {report.running} running, "
        f"{report.unhealthy} not Available, {report.series} disk series, "
        f"{len(report.gaps)} gap(s); {report.calls} call(s) in {elapsed:.0f}s"
    )
    if not report.findings:
        return summary
    body = {
        "generatedAt": iso(now),
        "scopes": [
            f"/subscriptions/{sub}" + (f"/resourceGroups/{rg}" if rg else "")
            for sub, rg in cfg.scopes
        ] or "all tenants and subscriptions visible to the signed-in identity",
        "findings": report.findings,
        "blind": report.blind,
        "gaps": report.gaps[:50],
    }
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n" + summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Azure VM health and disk watch across every tenant and subscription "
        "the signed-in identity can read. Exit 0: nothing new. Exit 1: findings or collection "
        "failure, printed as JSON."
    )
    parser.add_argument("--tools-home", required=True)
    parser.add_argument("--scope", action="append", default=[], type=parse_scope)
    parser.add_argument("--arm-endpoint", default="https://management.azure.com")
    parser.add_argument("--metrics-endpoint", default="https://{region}.metrics.monitor.azure.com")
    parser.add_argument("--interval-minutes", type=int, default=5)
    parser.add_argument("--health-lag-minutes", type=int, default=10)
    parser.add_argument("--metric-lag-minutes", type=int, default=3)
    parser.add_argument("--reminder-hours", type=int, default=24)
    parser.add_argument("--coverage-hour-utc", type=int, default=1)
    parser.add_argument("--window-minutes", type=int, default=15)
    parser.add_argument("--sustain-minutes", type=int, default=10)
    parser.add_argument("--latency-ms", type=float, default=20.0)
    parser.add_argument("--latency-ratio", type=float, default=3.0)
    parser.add_argument("--saturation-pct", type=float, default=90.0)
    parser.add_argument("--baseline-days", type=int, default=7)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args(argv)
    if not 0 < args.sustain_minutes <= args.window_minutes:
        parser.error("--sustain-minutes must be between 1 and --window-minutes")
    if not args.tools_home.startswith("/"):
        parser.error("--tools-home must be an absolute path")
    cfg = Config(
        tools_home=args.tools_home,
        scopes=args.scope,
        arm=args.arm_endpoint,
        metrics_endpoint=args.metrics_endpoint,
        interval=timedelta(minutes=args.interval_minutes),
        health_lag=timedelta(minutes=args.health_lag_minutes),
        metric_lag=timedelta(minutes=args.metric_lag_minutes),
        reminder=timedelta(hours=args.reminder_hours),
        coverage_hour=args.coverage_hour_utc,
        window=args.window_minutes,
        sustain=args.sustain_minutes,
        latency_ms=args.latency_ms,
        latency_ratio=args.latency_ratio,
        saturation_pct=args.saturation_pct,
        baseline_days=args.baseline_days,
        workers=args.workers,
    )
    code, text = run(cfg)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
