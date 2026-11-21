#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

WATCH = Path(__file__).parents[1] / "scripts" / "watch.py"
HOME_TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GUEST_TENANT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LOCKED_TENANT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SUB = "11111111-1111-4111-8111-111111111111"
SUB2 = "22222222-2222-4222-8222-222222222222"
GUEST_SUB = "33333333-3333-4333-8333-333333333333"
RG = "ops-rg"


def vm_id(sub: str, name: str, rg: str = RG) -> str:
    return f"/subscriptions/{sub}/resourcegroups/{rg}/providers/microsoft.compute/virtualmachines/{name}"


VM_A = vm_id(SUB, "vm-a")
VM_B = vm_id(SUB, "vm-b")

FAKE_AZURE_CLI = """#!/bin/sh
[ "$1" = "--tools-home" ] || exit 64
tools="$2"
shift 2
echo "$tools" >> "$HOME/tools-homes.log"
[ -f "$tools/not-ready" ] && exit 78
tenant=home
prev=
for arg in "$@"; do
  [ "$prev" = "--tenant" ] && tenant="$arg"
  prev="$arg"
done
if [ "$2" = "list" ]; then
  cat "$HOME/known-tenants.json" 2>/dev/null || echo '[]'
  exit 0
fi
if [ -f "$HOME/fail-$tenant" ]; then
  echo "AADSTS50076: multi-factor authentication required for tenant $tenant." >&2
  echo "az login --tenant $tenant" >&2
  exit 1
fi
echo "token-$tenant"
"""


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeArm(BaseHTTPRequestHandler):
    world: dict[str, Any] = {}
    calls: list[tuple[str, str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def reply(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def token_tenant(self) -> str:
        token = self.headers["authorization"].removeprefix("Bearer token-")
        return HOME_TENANT if token == "home" else token

    def tenant_of(self, sub: str) -> str:
        return next(t for t, subs in self.world["tenants"].items() if sub in subs)

    def series(self, resource: str, names: list[str], luns: list[Any], start: datetime,
               end: datetime, hourly: bool, dim: Any = "lun", step: timedelta | None = None) -> list[dict[str, Any]]:
        value = []
        for name in names:
            series = []
            for lun in luns:
                label = "/".join(lun) if isinstance(lun, tuple) else lun
                spec = self.world["metrics"].get(f"{resource}|{name}|{label}", {})
                base = spec.get("base", 1.0)
                points, moment, index = [], end, 0
                while moment > start:
                    if hourly and "hourlySeries" in spec:
                        series_values = spec["hourlySeries"]
                        v = series_values[-1 - index] if index < len(series_values) else base
                    elif hourly:
                        v = spec.get("hourly", base)
                    else:
                        v = spec.get("recent", base) if index < spec.get("recentMinutes", 0) else base
                    points.append({"timeStamp": iso(moment), "average": v, "total": v, "maximum": v})
                    moment -= step or (timedelta(hours=1) if hourly else timedelta(minutes=1))
                    index += 1
                if isinstance(lun, tuple):
                    meta = [{"name": {"value": d}, "value": x} for d, x in zip(dim, lun)]
                    lun = "/".join(lun)
                else:
                    meta = [{"name": {"value": dim}, "value": lun}] if lun else []
                series.append({"metadatavalues": meta, "data": list(reversed(points))})
            value.append({"name": {"value": name}, "timeseries": series})
        return value

    def do_GET(self) -> None:
        self.calls.append(("GET", self.path, self.token_tenant()))
        url = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/tenants":
            names = {HOME_TENANT: "Home Org", GUEST_TENANT: "Guest Org"}
            return self.reply(200, {"value": [{"tenantId": t, "displayName": names.get(t)} for t in self.world["tenants"]
                                              if t != LOCKED_TENANT]})
        if url.path == "/subscriptions":
            tenant = self.token_tenant()
            subs = self.world["tenants"].get(tenant, [])
            rows = [{"subscriptionId": s, "tenantId": tenant, "state": "Enabled"} for s in subs]
            rows.append({"subscriptionId": "44444444-4444-4444-8444-444444444444",
                         "tenantId": tenant, "state": "Disabled"})
            return self.reply(200, {"value": rows})
        if "/providers/Microsoft.ResourceHealth/availabilityStatuses" in url.path:
            vid = url.path.split("/providers/Microsoft.ResourceHealth")[0].lower()
            if vid not in self.world.get("history", {}):
                return self.reply(404, {"error": {"message": "not found"}})
            return self.reply(200, {"value": self.world["history"][vid]})
        resource = url.path.split("/providers/Microsoft.Insights/metrics")[0].lower()
        if self.tenant_of(resource.split("/")[2]) != self.token_tenant():
            return self.reply(401, {"error": {"message": "wrong tenant token"}})
        start, end = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in params["timespan"].split("/"))
        flt = params.get("$filter")
        dims, luns = split_dims(flt)
        if flt and "'*'" not in flt:
            luns = [flt.split("'")[1]]
        step = timedelta(minutes=1) if params.get("interval") == "PT1M" else None
        self.world.setdefault("hourly", []).append((resource, params["metricnames"], params["interval"]))
        self.reply(200, {"value": self.series(resource, params["metricnames"].split(","), luns, start, end,
                                              step is None, dims, step)})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        tenant = self.token_tenant()
        self.calls.append(("POST", self.path, tenant))
        url = urllib.parse.urlsplit(self.path)
        if url.path.endswith("/providers/Microsoft.ResourceGraph/resources"):
            if any(self.tenant_of(s) != tenant for s in body["subscriptions"]):
                return self.reply(401, {"error": {"message": "wrong tenant token"}})
            if tenant in self.world.get("argFail", []):
                return self.reply(400, {"error": {"message": "bad", "details": [{"code": "InvalidQuery", "message": "parse"}]}})
            query = body["query"]
            self.world.setdefault("queries", []).append(query)
            subs = set(body["subscriptions"])
            if query.startswith("ServiceHealthResources"):
                rows = [e for e in self.world.get("servicehealth", []) if e["subscriptionId"] in subs]
            elif "natgateways" in query:
                rows = [n for n in self.world.get("nats", []) if n["subscriptionId"] in subs]
            elif "applicationgateways" in query and query.startswith("Resources"):
                rows = [n for n in self.world.get("net", []) if n["subscriptionId"] in subs]
            elif query.startswith("Resources"):
                rows = [v for v in self.world["vms"] if v["subscriptionId"] in subs]
            else:
                key = "health" if "availabilitystatuses" in query else "annotations"
                rows = [r for r in self.world[key] if r["target"].split("/")[2] in subs]
            return self.reply(200, {"data": rows})
        region, _, sub = url.path.strip("/").split("/")[:3]
        if self.tenant_of(sub) != tenant:
            return self.reply(401, {"error": {"message": "wrong tenant token"}})
        params = dict(urllib.parse.parse_qsl(url.query))
        names = params["metricnames"].split(",")
        if set(names) & set(self.world.get("broken", [])):
            return self.reply(400, {"error": {"message": "Failed to find metric configuration"}})
        start = datetime.fromisoformat(params["starttime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(params["endtime"].replace("Z", "+00:00"))
        dim, luns = split_dims(params.get("filter"))
        step = timedelta(minutes=5) if params["interval"] == "PT5M" else None
        self.world.setdefault("batches", []).append((region, sub, len(body["resourceids"])))
        self.world.setdefault("namespaces", []).append(params["metricnamespace"])
        self.world.setdefault("batch_intervals", []).append(params["interval"])
        values = [
            {"resourceid": rid, "value": self.series(rid.lower(), names, luns, start, end, False, dim, step)}
            for rid in body["resourceids"]
        ]
        self.reply(200, {"values": values})


DIM_VALUES = {
    "lun": ["0", "1"], "connectionstate": ["Attempted", "Failed"], "peeringtype": ["Private", "Microsoft"],
    "peer": ["Primary", "Secondary"], "roleinstance": ["IN_0", "IN_1"],
    "backendsettingspool": ["pool-a", "pool-b"], "httpstatusgroup": ["2xx", "5xx"],
}


def split_dims(flt: str | None) -> tuple[Any, list[Any]]:
    if not flt:
        return "lun", [""]
    dims = [part.split(" eq ")[0].strip() for part in flt.split(" and ")]
    values = [DIM_VALUES[d.lower()] for d in dims]
    if len(dims) == 1:
        return ("lun" if dims[0] == "LUN" else dims[0]), values[0]
    combos: list[Any] = [()]
    for options in values:
        combos = [c + (o,) for c in combos for o in options]
    return dims, combos


def vm(vid: str, power: str = "PowerState/running", location: str = "westeurope") -> dict[str, Any]:
    parts = vid.split("/")
    return {
        "id": vid,
        "name": parts[-1],
        "subscriptionId": parts[2],
        "resourceGroup": parts[4],
        "location": location,
        "computerName": f"{parts[-1]}.example.internal",
        "powerState": power,
    }


def record(state: str | None, at: datetime) -> dict[str, Any]:
    return {"properties": {"availabilityState": state, "occuredTime": iso(at)}}


def health(target: str, state: str, changed: datetime, previous: str = "Available") -> dict[str, Any]:
    return {"target": target, "state": state, "previous": previous, "occurred": iso(changed),
            "reasonType": "Unplanned", "title": state, "summary": "platform reported"}


class WatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.tools = self.home / "tools"
        self.tools.mkdir()
        scripts = self.home / "scripts"
        scripts.mkdir()
        self.script = scripts / "watch.py"
        self.script.write_text(WATCH.read_text())
        (scripts / "azure_cli.sh").write_text(FAKE_AZURE_CLI)
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.world: dict[str, Any] = {
            "tenants": {HOME_TENANT: [SUB, SUB2], GUEST_TENANT: [GUEST_SUB]},
            "vms": [], "health": [], "annotations": [], "metrics": {},
        }
        FakeArm.world = self.world
        FakeArm.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeArm)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {**os.environ, "HOME": str(self.home)}

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def watch(self, *extra: str) -> tuple[int, dict[str, Any] | None, str, subprocess.CompletedProcess[str]]:
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        argv = [sys.executable, str(self.script), "--tools-home", str(self.tools), "--arm-endpoint", base,
                "--metrics-endpoint", base + "/{region}", *extra]
        result = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=120)
        lines = result.stdout.strip().splitlines()
        body = json.loads("\n".join(lines[:-1])) if len(lines) > 1 else None
        return result.returncode, body, lines[-1] if lines else "", result

    def rules(self, body: dict[str, Any] | None) -> list[tuple[str, str | None]]:
        assert body is not None
        return sorted((f["rule"], f["resource"]["name"] if "resource" in f else None) for f in body["findings"])

    def report_case(self) -> None:
        self.world["vms"] = [vm(VM_A), vm(VM_B)]
        self.world["health"] = [
            health(VM_A, "Degraded", self.now - timedelta(minutes=2), previous="Degraded"),
            health(VM_B, "Degraded", self.now - timedelta(days=20, hours=1), previous="Unknown"),
        ]
        self.world["annotations"] = [{
            "target": VM_B, "annotation": "AcceleratedNetworkingDegraded",
            "occurred": iso(self.now - timedelta(minutes=4)), "context": "Platform Initiated",
            "category": "Unplanned", "impactType": "Degraded",
            "summary": "Accelerated Networking is degraded",
        }]
        self.world["metrics"] = {
            f"{VM_A}|Data Disk Latency|0": {"base": 4.0, "recent": 31.6, "recentMinutes": 12, "hourly": 4.0},
        }

    def test_replays_reported_degradation(self) -> None:
        self.report_case()
        code, body, summary, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.rules(body),
            [("disk-latency", "vm-a"), ("platform-event", "vm-b")],
        )
        assert body is not None
        disk = next(f for f in body["findings"] if f["rule"] == "disk-latency")
        self.assertEqual(disk["observed"]["series"], "LUN 0")
        self.assertEqual(disk["observed"]["median"], 31.6)
        self.assertEqual(disk["observed"]["baselineMedian"], 4.0)
        self.assertEqual(disk["resource"]["id"], VM_A)
        self.assertIn("2 tenant(s), 3 subscription(s), 0 blind; 2 VM(s), 2 running, 2 not Available", summary)
        self.assertEqual(set((self.home / "tools-homes.log").read_text().split()), {str(self.tools)})

    def test_every_tenant_and_subscription_is_watched_with_its_own_token(self) -> None:
        guest = vm_id(GUEST_SUB, "vm-guest", "guest-rg")
        other = vm_id(SUB2, "vm-other", "other-rg")
        self.world["vms"] = [vm(VM_A), vm(other), vm(guest, location="eastasia")]
        self.world["health"] = [
            health(guest, "Unavailable", self.now - timedelta(minutes=3)),
            health(other, "Degraded", self.now - timedelta(minutes=2)),
        ]
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("health-onset", "vm-guest"), ("health-onset", "vm-other")])
        self.assertEqual(
            sorted(self.world["batches"]),
            [("eastasia", GUEST_SUB, 1), ("eastasia", GUEST_SUB, 1),
             ("westeurope", SUB, 1), ("westeurope", SUB, 1),
             ("westeurope", SUB2, 1), ("westeurope", SUB2, 1)],
        )

    def test_vms_are_batched_fifty_per_regional_call(self) -> None:
        self.world["vms"] = [vm(vm_id(SUB, f"vm-{i:03}")) for i in range(120)]
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)
        self.assertEqual(sorted(n for _, _, n in self.world["batches"]), [20, 20, 50, 50, 50, 50])
        self.assertIn("120 VM(s), 120 running", summary)

    def test_scope_narrows_to_a_resource_group(self) -> None:
        elsewhere = vm_id(SUB, "vm-elsewhere", "other-rg")
        self.world["vms"] = [vm(VM_A), vm(elsewhere)]
        self.world["health"] = [health(elsewhere, "Unavailable", self.now - timedelta(minutes=3))]
        code, _, summary, _ = self.watch("--scope", f"/subscriptions/{SUB}/resourceGroups/{RG.upper()}")
        self.assertEqual(code, 0, summary)
        self.assertIn("1 tenant(s), 1 subscription(s)", summary)
        self.assertIn("1 VM(s)", summary)

    def test_refreshed_record_of_an_old_degradation_is_not_new(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["health"] = [health(VM_A, "Degraded", self.now - timedelta(minutes=1), previous="Degraded")]
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None), summary)
        self.assertIn("1 not Available", summary)

    def test_escalation_between_unhealthy_states_is_new(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["health"] = [health(VM_A, "Unavailable", self.now - timedelta(minutes=4), previous="Degraded")]
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("health-onset", "vm-a")])
        assert body is not None
        observed = body["findings"][0]["observed"]
        self.assertEqual((observed["previousAvailabilityState"], observed["availabilityState"]), ("Degraded", "Unavailable"))

    def test_daily_summary_lists_every_unhealthy_vm_and_blind_tenant_once(self) -> None:
        self.report_case()
        self.world["tenants"][LOCKED_TENANT] = []
        (self.home / "known-tenants.json").write_text(json.dumps([{"t": HOME_TENANT}, {"t": LOCKED_TENANT, "n": None, "d": "locked.example"}]))
        (self.home / f"fail-{LOCKED_TENANT}").write_text("")
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        assert body is not None
        summary = next(f for f in body["findings"] if f["rule"] == "daily-summary")
        self.assertEqual(summary["observed"]["notAvailableCount"], 2)
        groups = summary["observed"]["notAvailableGroups"]
        self.assertEqual([(g["state"], g["count"], sorted(g["examples"])) for g in groups], [("Degraded", 2, ["vm-a", "vm-b"])])
        self.assertEqual(summary["observed"]["readableTenants"], [f"{HOME_TENANT} (Home Org): 2 subscription(s)", f"{GUEST_TENANT} (Guest Org): 1 subscription(s)"])
        self.assertIn(f"{LOCKED_TENANT} (locked.example): AUTH_REQUIRED", body["blind"][0])
        self.assertIn("AADSTS50076", body["blind"][0])
        self.assertEqual(summary["episode"], f"summary|{self.now.date().isoformat()}")
        code, body, _, _ = self.watch()
        self.assertNotIn("daily-summary", [f["rule"] for f in (body or {"findings": []})["findings"]])

    def test_locked_tenant_is_blind_and_reported_once_a_day(self) -> None:
        self.world["tenants"][LOCKED_TENANT] = []
        (self.home / "known-tenants.json").write_text(json.dumps([{"t": HOME_TENANT}, {"t": LOCKED_TENANT}]))
        (self.home / f"fail-{LOCKED_TENANT}").write_text("")
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)
        self.assertIn("2 tenant(s), 3 subscription(s), 1 blind", summary)
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("daily-summary", None)])
        assert body is not None
        self.assertIn("AADSTS50076", body["blind"][0])

    def test_unreadable_resource_graph_in_one_tenant_is_blind_not_fatal(self) -> None:
        self.world["argFail"] = [GUEST_TENANT]
        self.world["vms"] = [vm(VM_A)]
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)
        self.assertIn("1 blind", summary)

    def test_next_quiet_fire_wakes_nobody(self) -> None:
        self.report_case()
        self.world["health"] = [health(VM_A, "Degraded", self.now - timedelta(minutes=40))]
        self.world["annotations"][0]["occurred"] = iso(self.now - timedelta(minutes=40))
        self.world["metrics"][f"{VM_A}|Data Disk Latency|0"]["recentMinutes"] = 200
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None))
        self.assertIn("0 finding(s)", summary)

    def test_customer_actions_and_stopped_vms_are_ignored(self) -> None:
        self.world["vms"] = [vm(VM_A, "PowerState/deallocated"), vm(VM_B)]
        self.world["health"] = [health(VM_A, "Unavailable", self.now - timedelta(minutes=3))]
        self.world["annotations"] = [{
            "target": VM_B, "annotation": "VirtualMachineRestarted",
            "occurred": iso(self.now - timedelta(minutes=2)), "context": "Customer Initiated",
        }]
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)

    def test_latency_within_its_own_baseline_is_not_a_finding(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["metrics"] = {
            f"{VM_A}|Data Disk Latency|0": {"base": 15.0, "recent": 31.6, "recentMinutes": 12, "hourly": 15.0},
        }
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)

    def test_saturation_with_slow_disk_is_reported_per_lun(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["metrics"] = {
            f"{VM_A}|Data Disk IOPS Consumed Percentage|1": {"base": 40.0, "recent": 97.0, "recentMinutes": 12},
            f"{VM_A}|Data Disk Latency|1": {"base": 30.0},
        }
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("disk-saturation", "vm-a")])
        assert body is not None
        self.assertEqual(body["findings"][0]["observed"]["series"], "LUN 1")
        self.assertEqual(body["findings"][0]["observed"]["latencyMedian"], 30.0)

    def test_saturation_without_latency_is_only_counted(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["metrics"] = {
            f"{VM_A}|Data Disk Bandwidth Consumed Percentage|1": {"base": 40.0, "recent": 100.0, "recentMinutes": 12},
            f"{VM_A}|Data Disk Latency|0": {"base": 30.0},
        }
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None), summary)
        self.assertIn("1 saturated without latency", summary)

    def test_expired_login_wakes_the_agent_without_calling_azure(self) -> None:
        (self.home / "fail-home").write_text("")
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("collector-error", None)])
        assert body is not None
        self.assertEqual(body["findings"][0]["episode"], "collector|AUTH_REQUIRED")
        self.assertEqual(FakeArm.calls, [])

    def test_no_readable_tenant_is_a_collector_error(self) -> None:
        for tenant in (HOME_TENANT, GUEST_TENANT):
            (self.home / f"fail-{tenant}").write_text("")
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        assert body is not None
        self.assertTrue(body["findings"][0]["observed"]["error"].startswith("NO_COVERAGE"))

    def test_resource_graph_failing_everywhere_is_a_collector_error(self) -> None:
        self.world["argFail"] = [HOME_TENANT, GUEST_TENANT]
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        assert body is not None
        self.assertTrue(body["findings"][0]["observed"]["error"].startswith("NO_COVERAGE"))
        self.assertIn("InvalidQuery", body["findings"][0]["observed"]["error"])

    def test_unready_toolchain_names_the_ensure_command(self) -> None:
        (self.tools / "not-ready").write_text("")
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        assert body is not None
        error = body["findings"][0]["observed"]["error"]
        self.assertTrue(error.startswith("AZ_NOT_FOUND"))
        self.assertIn(f"--tools-home {self.tools} --ensure", error)
        self.assertEqual(FakeArm.calls, [])

    def test_tools_home_must_be_absolute(self) -> None:
        result = subprocess.run(
            [sys.executable, str(self.script), "--tools-home", "relative/home"],
            capture_output=True, text=True, env=self.env, timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute", result.stderr)

    def test_missing_metric_is_a_gap_not_a_finding(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["broken"] = ["OS Disk Latency"]
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)
        self.assertIn("1 gap(s)", summary)

    def test_output_stays_within_the_wake_budget_and_counts_the_rest(self) -> None:
        self.world["vms"] = [vm(vm_id(SUB, f"vm-{i:03}")) for i in range(60)]
        self.world["metrics"] = {
            f"{vm_id(SUB, f'vm-{i:03}')}|Data Disk Latency|0": {"base": 2.0, "recent": 100.0 + i, "recentMinutes": 12, "hourly": 2.0}
            for i in range(60)
        }
        code, body, summary, result = self.watch("--disk-burst", "1000")
        self.assertEqual(code, 1)
        self.assertLessEqual(len(result.stdout.strip().splitlines()[0]), 7000)
        assert body is not None
        shown = len(body["findings"])
        self.assertEqual(shown + body["omitted"]["count"], 60)
        self.assertEqual(body["omitted"]["rules"], {"disk-latency": 60 - shown})
        self.assertEqual(body["findings"][0]["observed"]["median"], 159.0)
        self.assertIn("60 finding(s)", summary)

    def test_deadline_skips_remaining_disk_batches_but_still_reports(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["health"] = [health(VM_A, "Unavailable", self.now - timedelta(minutes=3))]
        code, body, summary, _ = self.watch("--deadline-seconds", "0")
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("health-onset", "vm-a")])
        self.assertIn("2 disk batch(es) skipped at deadline", summary)
        self.assertIn("0 disk series", summary)

    def test_tenant_limits_the_sweep_and_hides_other_tenants(self) -> None:
        guest = vm_id(GUEST_SUB, "vm-guest", "guest-rg")
        self.world["tenants"][LOCKED_TENANT] = []
        (self.home / "known-tenants.json").write_text(json.dumps([{"t": HOME_TENANT}, {"t": LOCKED_TENANT}]))
        (self.home / f"fail-{LOCKED_TENANT}").write_text("")
        self.world["vms"] = [vm(VM_A), vm(guest, location="eastasia")]
        self.world["health"] = [
            health(VM_A, "Unavailable", self.now - timedelta(minutes=3)),
            health(guest, "Unavailable", self.now - timedelta(minutes=3)),
        ]
        code, body, summary, _ = self.watch("--tenant", GUEST_TENANT.upper())
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("health-onset", "vm-guest")])
        assert body is not None
        self.assertNotIn("blind", body)
        self.assertIn("1 tenant(s), 1 subscription(s), 0 blind; 1 VM(s)", summary)
        self.assertNotIn(HOME_TENANT, {c[2] for c in FakeArm.calls if c[0] == "POST"})

    def test_unreadable_requested_tenant_is_a_collector_error(self) -> None:
        self.world["tenants"][LOCKED_TENANT] = []
        (self.home / f"fail-{LOCKED_TENANT}").write_text("")
        code, body, _, _ = self.watch("--tenant", LOCKED_TENANT)
        self.assertEqual(code, 1)
        assert body is not None
        error = body["findings"][0]["observed"]["error"]
        self.assertTrue(error.startswith("NO_COVERAGE"))
        self.assertIn(LOCKED_TENANT, error)

    def test_rejects_tenant_that_is_not_a_uuid(self) -> None:
        code, _, _, result = self.watch("--tenant", "contoso.onmicrosoft.com")
        self.assertEqual(code, 2)
        self.assertIn("tenant must be a tenant UUID", result.stderr)

    def slow_disks(self, count: int) -> None:
        self.world["vms"] = [vm(vm_id(SUB, f"vm-{i:03}")) for i in range(count)]
        self.world["metrics"] = {
            f"{vm_id(SUB, f'vm-{i:03}')}|Data Disk Latency|0": {"base": 2.0, "recent": 100.0 + i, "recentMinutes": 12, "hourly": 2.0}
            for i in range(count)
        }

    def test_many_disk_onsets_collapse_into_one_burst(self) -> None:
        self.slow_disks(25)
        self.world["health"] = [health(vm_id(SUB, "vm-000"), "Unavailable", self.now - timedelta(minutes=3))]
        code, body, summary, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("disk-burst", None), ("health-onset", "vm-000")])
        assert body is not None
        burst = next(f for f in body["findings"] if f["rule"] == "disk-burst")["observed"]
        self.assertEqual(burst["count"], 25)
        self.assertEqual(burst["groups"], [{"subscription": SUB, "location": "westeurope", "rule": "disk-latency", "count": 25}])
        self.assertEqual([w["name"] for w in burst["worst"]], ["vm-024", "vm-023", "vm-022", "vm-021", "vm-020"])
        self.assertIn("2 finding(s)", summary)

    def test_saturated_burst_ranks_worst_disks_by_latency(self) -> None:
        names = [f"vm-{i:03}" for i in range(25)]
        self.world["vms"] = [vm(vm_id(SUB, n)) for n in names]
        self.world["metrics"] = {}
        for i, n in enumerate(names):
            vid = vm_id(SUB, n)
            self.world["metrics"][f"{vid}|Data Disk Bandwidth Consumed Percentage|1"] = {"base": 40.0, "recent": 100.0, "recentMinutes": 12}
            self.world["metrics"][f"{vid}|Data Disk Latency|1"] = {"base": 30.0 + (i * 7) % 25}
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        assert body is not None
        burst = next(f for f in body["findings"] if f["rule"] == "disk-burst")["observed"]
        self.assertEqual(burst["count"], 25)
        worst = burst["worst"]
        self.assertEqual([w["latencyMs"] for w in worst], [54.0, 53.0, 52.0, 51.0, 50.0])
        self.assertTrue(all(w["consumedPct"] == 100.0 and w["series"] == "LUN 1" for w in worst))

    def test_few_disk_onsets_stay_individual(self) -> None:
        self.slow_disks(3)
        code, body, _, _ = self.watch("--disk-burst", "3")
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("disk-latency", f"vm-00{i}") for i in range(3)])

    def test_recovery_is_reported_once_when_it_happens(self) -> None:
        self.world["vms"] = [vm(VM_A), vm(VM_B)]
        self.world["health"] = [
            health(VM_A, "Available", self.now - timedelta(minutes=3), previous="Degraded"),
            health(VM_B, "Available", self.now - timedelta(minutes=40), previous="Unavailable"),
        ]
        code, body, summary, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("health-recovered", "vm-a")])
        assert body is not None
        self.assertEqual(body["findings"][0]["severity"], "info")
        self.assertEqual(body["findings"][0]["observed"]["previousAvailabilityState"], "Degraded")
        self.assertIn("0 not Available", summary)

    def test_daily_summary_dates_each_degradation_and_counts_loop_candidates(self) -> None:
        old = vm_id(SUB, "vm-old")
        new = vm_id(SUB, "vm-new")
        young = vm_id(SUB, "vm-young")
        lost = vm_id(SUB, "vm-lost")
        self.world["vms"] = [vm(old), vm(new), vm(young), vm(lost)]
        refreshed = self.now - timedelta(minutes=50)
        self.world["health"] = [
            health(old, "Degraded", refreshed, previous="Degraded"),
            health(new, "Unavailable", refreshed, previous="Unavailable"),
            health(young, "Degraded", refreshed, previous="Degraded"),
            health(lost, "Degraded", refreshed, previous="Degraded"),
        ]
        self.world["history"] = {
            old: [record(None, self.now - timedelta(days=2)), record("Degraded", self.now - timedelta(days=5)),
                  record("Degraded", self.now - timedelta(days=10)), record("Available", self.now - timedelta(days=30))],
            new: [record("Unavailable", self.now - timedelta(hours=2)), record("Available", self.now - timedelta(days=3)),
                  record("Unavailable", self.now - timedelta(days=20))],
            young: [record("Degraded", self.now - timedelta(hours=1)), record("Available", self.now - timedelta(days=1))],
        }
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        assert body is not None
        summary = next(f for f in body["findings"] if f["rule"] == "daily-summary")["observed"]
        self.assertEqual(summary["notAvailableCount"], 4)
        self.assertEqual(summary["newIn24h"], 2)
        degraded = next(g for g in summary["notAvailableGroups"] if g["state"] == "Degraded")
        self.assertEqual((degraded["count"], degraded["newIn24h"]), (3, 1))
        self.assertEqual(degraded["oldestSince"], iso(self.now - timedelta(days=10)))
        shadow = summary["loopShadow"]
        self.assertEqual(shadow["wouldTrack"], 3)
        self.assertEqual(
            [(b["bucket"], b["count"], b["examples"]) for b in shadow["byAge"]],
            [("Degraded >7d", 1, ["vm-old"]), ("Degraded unknown", 1, ["vm-lost"]), ("Unavailable <24h", 1, ["vm-new"])],
        )
        self.assertIn("gaps", body)

    def test_summary_only_skips_detection_and_disks_and_always_reports(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["health"] = [health(VM_A, "Unavailable", self.now - timedelta(minutes=3))]
        self.world["annotations"] = [{
            "target": VM_A, "annotation": "LiveMigrationFailed",
            "occurred": iso(self.now - timedelta(minutes=2)), "context": "Platform Initiated",
        }]
        self.world["metrics"] = {
            f"{VM_A}|Data Disk Latency|0": {"base": 2.0, "recent": 300.0, "recentMinutes": 12, "hourly": 2.0},
        }
        code, body, summary, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("daily-summary", None)])
        self.assertEqual([c for c in FakeArm.calls if "metrics" in c[1]], [])
        self.assertIn("1 VM(s), 1 running, 1 not Available", summary)
        self.world["vms"], self.world["health"], self.world["annotations"] = [vm(VM_A)], [], []
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        assert body is not None
        quiet = body["findings"][0]["observed"]
        self.assertEqual((quiet["notAvailableCount"], quiet["notAvailableGroups"]), (0, []))

    def test_disk_latency_counts_days_over_threshold_in_the_baseline_week(self) -> None:
        self.world["vms"] = [vm(VM_A), vm(VM_B)]
        hours_a = [2.0] * 168
        for day in (1, 3, 5):
            hours_a[-(day * 24) - 2] = 80.0
        self.world["metrics"] = {
            f"{VM_A}|Data Disk Latency|0": {"base": 2.0, "recent": 300.0, "recentMinutes": 12, "hourlySeries": hours_a},
            f"{VM_B}|Data Disk Latency|0": {"base": 2.0, "recent": 300.0, "recentMinutes": 12, "hourly": 2.0},
        }
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        assert body is not None
        observed = {f["resource"]["name"]: f["observed"] for f in body["findings"]}
        self.assertEqual((observed["vm-a"]["daysOverThreshold"], observed["vm-a"]["wouldTrack"]), (3, True))
        self.assertEqual((observed["vm-b"]["daysOverThreshold"], observed["vm-b"]["wouldTrack"]), (0, False))

    def nat(self, name: str) -> str:
        nid = f"/subscriptions/{SUB}/resourcegroups/{RG}/providers/microsoft.network/natgateways/{name}"
        self.world.setdefault("nats", []).append({
            "id": nid, "name": name, "subscriptionId": SUB, "resourceGroup": RG,
            "location": "westeurope", "sku": "Standard", "subnets": 2, "publicIps": 1,
        })
        return nid

    def test_nat_problems_are_counted_in_shadow_without_waking(self) -> None:
        a = self.nat("nat-a")
        ongoing = self.nat("nat-ongoing")
        self.world["metrics"] = {
            f"{a}|DatapathAvailability|": {"base": 100.0, "recent": 50.0, "recentMinutes": 12},
            f"{ongoing}|DatapathAvailability|": {"base": 100.0, "recent": 40.0, "recentMinutes": 200},
        }
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None), summary)
        self.assertIn("2 NAT gateway(s) (nat: datapath 1 (worst 50.0))", summary)

    def test_nat_alerts_report_datapath_snat_failures_and_drops_above_baseline(self) -> None:
        a, b, c, d = (self.nat(n) for n in ("nat-a", "nat-b", "nat-c", "nat-d"))
        self.world["metrics"] = {
            f"{a}|DatapathAvailability|": {"base": 100.0, "recent": 50.0, "recentMinutes": 12},
            f"{b}|SNATConnectionCount|Failed": {"base": 0.0, "recent": 40.0, "recentMinutes": 12, "hourly": 60.0},
            f"{b}|SNATConnectionCount|Attempted": {"base": 5.0, "recent": 400.0, "recentMinutes": 12, "hourly": 300.0},
            f"{c}|PacketDropCount|": {"base": 0.0, "recent": 500.0, "recentMinutes": 12, "hourly": 600.0},
            f"{d}|PacketDropCount|": {"base": 0.0, "recent": 500.0, "recentMinutes": 12, "hourly": 12000.0},
        }
        for n in (a, b, c, d):
            self.world["metrics"].setdefault(f"{n}|DatapathAvailability|", {"base": 100.0})
        code, body, _, _ = self.watch("--nat-alerts")
        self.assertEqual(code, 1)
        self.assertEqual(
            self.rules(body),
            [("nat-datapath", "nat-a"), ("nat-packet-drops", "nat-c"), ("nat-snat-failures", "nat-b")],
        )
        assert body is not None
        datapath = next(f for f in body["findings"] if f["rule"] == "nat-datapath")
        self.assertEqual((datapath["severity"], datapath["observed"]["min"]), ("critical", 50.0))
        snat = next(f for f in body["findings"] if f["rule"] == "nat-snat-failures")
        self.assertEqual((snat["observed"]["series"], snat["observed"]["baselinePerMinute"]), ("ConnectionState Failed", 1.0))

    def test_daily_summary_counts_nat_gateways(self) -> None:
        self.nat("nat-a")
        self.nat("nat-b")
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        assert body is not None
        self.assertEqual(body["findings"][0]["observed"]["natGateways"], 2)

    def net(self, kind: str, name: str, **extra: Any) -> str:
        rid = f"/subscriptions/{SUB}/resourcegroups/{RG}/providers/microsoft.network/{kind}/{name}"
        self.world.setdefault("net", []).append({
            "id": rid, "type": f"microsoft.network/{kind}", "name": name, "subscriptionId": SUB,
            "resourceGroup": RG, "location": "westeurope", **extra,
        })
        return rid

    def circuit(self, name: str) -> str:
        return self.net(
            "expressroutecircuits", name, sku="Premium_MeteredData", providerState="Provisioned",
            circuitState="Enabled", peerings=[
                {"name": "AzurePrivatePeering", "properties": {"peeringType": "AzurePrivatePeering", "state": "Enabled"}},
                {"name": "MicrosoftPeering", "properties": {"peeringType": "MicrosoftPeering", "state": "Disabled"}},
            ],
        )

    def maintenance(self, title: str = "ExpressRoute maintenance", **extra: Any) -> None:
        self.world.setdefault("servicehealth", []).append({
            "subscriptionId": SUB, "eventType": "PlannedMaintenance", "status": "Active",
            "trackingId": "TRK-1", "title": title,
            "start": iso(self.now - timedelta(hours=1)), "end": iso(self.now + timedelta(hours=3)), **extra,
        })

    def test_network_resources_get_health_onset_and_circuits_carry_service_health(self) -> None:
        lb = self.net("loadbalancers", "lb-a", sku="Standard", rules=2)
        agw = self.net("applicationgateways", "agw-a", sku="Standard_v2", tier="Standard_v2",
                       operationalState="Running")
        er = self.circuit("er-a")
        self.maintenance()
        self.world["servicehealth"].append({
            "subscriptionId": SUB, "eventType": "PlannedMaintenance", "status": "Active",
            "trackingId": "TRK-OLD", "title": "finished", "start": "1700000000000", "end": "1700003600000",
        })
        self.world["health"] = [
            health(lb, "Unavailable", self.now - timedelta(minutes=3)),
            health(agw, "Degraded", self.now - timedelta(minutes=3)),
            health(er, "Degraded", self.now - timedelta(minutes=3)),
        ]
        code, body, _, _ = self.watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("health-onset", "agw-a"), ("health-onset", "er-a"), ("health-onset", "lb-a")])
        assert body is not None
        by_name = {f["resource"]["name"]: f for f in body["findings"]}
        self.assertEqual(by_name["lb-a"]["severity"], "critical")
        self.assertEqual(by_name["lb-a"]["resource"]["kind"], "loadBalancer")
        self.assertEqual([e["trackingId"] for e in by_name["er-a"]["observed"]["serviceHealth"]], ["TRK-1"])
        self.assertNotIn("serviceHealth", by_name["agw-a"]["observed"])

    def test_service_health_is_not_queried_without_circuits(self) -> None:
        self.net("loadbalancers", "lb-a", sku="Standard", rules=2)
        self.watch()
        queries = self.world["queries"]
        self.assertTrue(any("loadbalancers" in q for q in queries))
        self.assertFalse(any(q.startswith("ServiceHealthResources") for q in queries))

    def test_daily_summary_groups_unhealthy_by_kind_and_counts_network(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        lb = self.net("loadbalancers", "lb-a", sku="Standard", rules=2)
        self.circuit("er-a")
        self.net("virtualnetworkgateways", "ergw-a", sku="ErGw1AZ")
        self.maintenance()
        self.world["health"] = [
            health(VM_A, "Degraded", self.now - timedelta(minutes=3)),
            health(lb, "Degraded", self.now - timedelta(minutes=3)),
        ]
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        assert body is not None
        observed = body["findings"][0]["observed"]
        self.assertEqual(observed["network"], {"loadBalancer": 1, "expressRouteCircuit": 1, "expressRouteGateway": 1})
        self.assertEqual(sorted(g["kind"] for g in observed["notAvailableGroups"]), ["loadBalancer", "virtualMachine"])
        self.assertEqual([e["trackingId"] for e in observed["expressRouteServiceHealth"]], ["TRK-1"])

    def links(self, circuit: str, metric: str = "BgpAvailability", primary: float = 100.0,
              secondary: float = 100.0, minutes: int = 12) -> None:
        for peer, value in (("Primary", primary), ("Secondary", secondary)):
            for name in ("BgpAvailability", "ArpAvailability"):
                recent = value if name == metric else 100.0
                self.world["metrics"][f"{circuit}|{name}|Private/{peer}"] = {
                    "base": 100.0, "recent": recent, "recentMinutes": minutes,
                }

    def test_one_expressroute_side_down_is_counted_in_shadow_without_waking(self) -> None:
        er = self.circuit("er-a")
        self.links(er, primary=40.0)
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None), summary)
        self.assertIn("1 ER circuit(s) and 0 ER gateway(s) (er: bgp 1 (worst 40.0))", summary)
        self.assertIn("microsoft.network/expressroutecircuits", self.world["namespaces"])

    def test_expressroute_alerts_split_one_side_from_both_sides(self) -> None:
        one = self.circuit("er-one")
        both = self.circuit("er-both")
        ongoing = self.circuit("er-ongoing")
        self.links(one, "ArpAvailability", primary=20.0)
        self.links(both, primary=0.0, secondary=10.0)
        self.links(ongoing, secondary=0.0, minutes=300)
        disabled = self.circuit("er-disabled-peering")
        self.links(disabled)
        for peer in ("Primary", "Secondary"):
            self.world["metrics"][f"{disabled}|BgpAvailability|Microsoft/{peer}"] = {
                "base": 100.0, "recent": 0.0, "recentMinutes": 12,
            }
        code, body, _, _ = self.watch("--er-alerts")
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("er-arp", "er-one"), ("er-bgp", "er-both")])
        assert body is not None
        found = {f["resource"]["name"]: f for f in body["findings"]}
        self.assertEqual(found["er-one"]["severity"], "warning")
        self.assertEqual(found["er-one"]["observed"]["downPeers"], ["Primary"])
        self.assertEqual(found["er-both"]["severity"], "critical")
        self.assertEqual(found["er-both"]["observed"]["downPeers"], ["Primary", "Secondary"])

    def test_one_side_down_during_planned_maintenance_is_only_annotated(self) -> None:
        one = self.circuit("er-one")
        both = self.circuit("er-both")
        self.links(one, primary=20.0)
        self.links(both, primary=0.0, secondary=0.0)
        self.maintenance()
        code, body, summary, _ = self.watch("--er-alerts")
        self.assertEqual(code, 1)
        self.assertEqual(self.rules(body), [("er-bgp", "er-both")])
        assert body is not None
        self.assertEqual(body["findings"][0]["observed"]["serviceHealth"][0]["trackingId"], "TRK-1")
        self.assertIn("maintenance 1 (worst 20.0)", summary)

    def test_unprovisioned_circuits_and_disabled_peerings_are_not_read(self) -> None:
        self.net("expressroutecircuits", "er-new", providerState="NotProvisioned", circuitState="Enabled",
                 peerings=[{"properties": {"peeringType": "AzurePrivatePeering", "state": "Enabled"}}])
        self.net("expressroutecircuits", "er-off", providerState="Provisioned", circuitState="Enabled",
                 peerings=[{"properties": {"peeringType": "AzurePrivatePeering", "state": "Disabled"}}])
        code, _, summary, _ = self.watch("--er-alerts")
        self.assertEqual(code, 0, summary)
        self.assertNotIn("microsoft.network/expressroutecircuits", self.world.get("namespaces", []))

    def test_expressroute_gateway_routes_and_cpu(self) -> None:
        lost = self.net("virtualnetworkgateways", "ergw-lost", sku="ErGw1AZ")
        drop = self.net("virtualnetworkgateways", "ergw-drop", sku="ErGw1AZ")
        busy = self.net("virtualnetworkgateways", "ergw-busy", sku="ErGw1AZ")
        steady = self.net("virtualnetworkgateways", "ergw-steady", sku="ErGw1AZ")
        routes = "ExpressRouteGatewayCountOfRoutesLearnedFromPeer"
        cpu = "ExpressRouteGatewayCpuUtilization"
        self.world["metrics"] = {
            f"{lost}|{routes}|": {"base": 100.0, "recent": 0.0, "recentMinutes": 2},
            f"{drop}|{routes}|": {"base": 100.0, "recent": 30.0, "recentMinutes": 2, "hourly": 100.0},
            f"{busy}|{routes}|": {"base": 100.0},
            f"{steady}|{routes}|": {"base": 40.0, "hourly": 100.0},
            f"{busy}|{cpu}|IN_0": {"base": 20.0, "recent": 95.0, "recentMinutes": 5},
        }
        for g in (lost, drop, busy, steady):
            self.world["metrics"].setdefault(f"{g}|{cpu}|IN_0", {"base": 20.0})
            self.world["metrics"].setdefault(f"{g}|{cpu}|IN_1", {"base": 20.0})
        code, body, _, _ = self.watch("--er-alerts")
        self.assertEqual(code, 1)
        self.assertEqual(
            self.rules(body),
            [("er-gateway-cpu", "ergw-busy"), ("er-routes", "ergw-drop"), ("er-routes", "ergw-lost")],
        )
        assert body is not None
        found = {f["resource"]["name"]: f for f in body["findings"]}
        self.assertEqual(found["ergw-lost"]["severity"], "critical")
        self.assertEqual((found["ergw-drop"]["severity"], found["ergw-drop"]["observed"]["baselineMedian"]),
                         ("warning", 100.0))
        self.assertEqual(found["ergw-busy"]["observed"]["instance"], "IN_0")
        self.assertTrue(all(i == "PT5M" for n, i in zip(self.world["namespaces"], self.world["batch_intervals"])
                            if n == "microsoft.network/virtualnetworkgateways"))

    def test_daily_summary_lists_circuits_with_sustained_bandwidth(self) -> None:
        hot = self.circuit("er-hot")
        spike = self.circuit("er-spike")
        self.world["metrics"] = {
            f"{hot}|EgressBandwidthUtilization|": {"base": 30.0, "recent": 85.0, "recentMinutes": 20},
            f"{spike}|EgressBandwidthUtilization|": {"base": 30.0, "recent": 99.0, "recentMinutes": 3},
        }
        code, body, _, _ = self.watch("--summary-only")
        self.assertEqual(code, 1)
        assert body is not None
        rows = body["findings"][0]["observed"]["expressRouteBandwidth"]
        self.assertEqual([(r["name"], r["egressPeakPct"], r["egressLongestMinutesOver"]) for r in rows],
                         [("er-hot", 85.0, 20)])

    def agw(self, name: str, tier: str = "Standard_v2", state: str = "Running") -> str:
        rid = self.net("applicationgateways", name, sku=tier, tier=tier, operationalState=state)
        for pool in ("pool-a", "pool-b"):
            self.world["metrics"].setdefault(f"{rid}|HealthyHostCount|{pool}", {"base": 2.0})
            self.world["metrics"].setdefault(f"{rid}|UnhealthyHostCount|{pool}", {"base": 0.0})
        self.world["metrics"].setdefault(f"{rid}|ResponseStatus|2xx", {"base": 100.0, "hourly": 6000.0})
        self.world["metrics"].setdefault(f"{rid}|ResponseStatus|5xx", {"base": 1.0, "hourly": 60.0})
        return rid

    def test_app_gateway_pool_loss_is_counted_in_shadow_without_waking(self) -> None:
        a = self.agw("agw-a")
        self.world["metrics"][f"{a}|HealthyHostCount|pool-a"] = {"base": 2.0, "recent": 0.0, "recentMinutes": 12}
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None), summary)
        self.assertIn("1 app gateway(s) (agw: pool-down 1 (worst 0.0))", summary)

    def test_app_gateway_alerts_report_pool_down_unhealthy_hosts_and_5xx(self) -> None:
        down = self.agw("agw-down")
        sick = self.agw("agw-sick")
        empty = self.agw("agw-empty")
        errors = self.agw("agw-errors")
        noisy = self.agw("agw-noisy")
        self.world["metrics"].update({
            f"{down}|HealthyHostCount|pool-a": {"base": 2.0, "recent": 0.0, "recentMinutes": 12},
            f"{down}|UnhealthyHostCount|pool-a": {"base": 0.0, "recent": 2.0, "recentMinutes": 12},
            f"{sick}|UnhealthyHostCount|pool-b": {"base": 0.0, "recent": 1.0, "recentMinutes": 12},
            f"{empty}|HealthyHostCount|pool-a": {"base": 0.0},
            f"{errors}|ResponseStatus|5xx": {"base": 1.0, "recent": 60.0, "recentMinutes": 3, "hourly": 60.0},
            f"{errors}|BackendResponseStatus|5xx": {"base": 0.0, "recent": 50.0, "recentMinutes": 3},
            f"{noisy}|ResponseStatus|5xx": {"base": 1.0, "recent": 60.0, "recentMinutes": 3, "hourly": 1800.0},
        })
        code, body, _, _ = self.watch("--agw-alerts")
        self.assertEqual(code, 1)
        self.assertEqual(
            self.rules(body),
            [("agw-5xx", "agw-errors"), ("agw-pool-down", "agw-down"), ("agw-unhealthy", "agw-sick")],
        )
        assert body is not None
        found = {f["resource"]["name"]: f for f in body["findings"]}
        self.assertEqual((found["agw-down"]["severity"], found["agw-down"]["observed"]["pool"]), ("critical", "pool-a"))
        self.assertEqual(found["agw-sick"]["observed"]["pool"], "pool-b")
        five = found["agw-errors"]["observed"]
        self.assertEqual((five["requests"], five["errors"], five["backend5xx"], five["baselinePct"]),
                         (1692, 192, 150, 0.99))

    def test_v1_and_stopped_app_gateways_are_counted_not_read(self) -> None:
        self.agw("agw-v1", tier="Standard")
        self.agw("agw-stopped", state="Stopped")
        code, _, summary, _ = self.watch("--agw-alerts")
        self.assertEqual(code, 0, summary)
        self.assertNotIn("microsoft.network/applicationgateways", self.world.get("namespaces", []))
        code, body, _, _ = self.watch("--summary-only")
        assert body is not None
        self.assertEqual(body["findings"][0]["observed"]["network"],
                         {"applicationGateway": 2, "applicationGatewayV1": 1})

    def test_rejects_scope_that_could_alter_the_query(self) -> None:
        code, _, _, result = self.watch("--scope", f"/subscriptions/{SUB}/resourceGroups/x') or true //")
        self.assertEqual(code, 2)
        self.assertIn("scope must be", result.stderr)


if __name__ == "__main__":
    unittest.main()
