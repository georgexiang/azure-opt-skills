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

    def series(self, resource: str, names: list[str], luns: list[str], start: datetime,
               end: datetime, hourly: bool) -> list[dict[str, Any]]:
        value = []
        for name in names:
            series = []
            for lun in luns:
                spec = self.world["metrics"].get(f"{resource}|{name}|{lun}", {})
                base = spec.get("base", 1.0)
                points, moment, index = [], end, 0
                while moment > start:
                    if hourly:
                        v = spec.get("hourly", base)
                    else:
                        v = spec.get("recent", base) if index < spec.get("recentMinutes", 0) else base
                    points.append({"timeStamp": iso(moment), "average": v})
                    moment -= timedelta(hours=1) if hourly else timedelta(minutes=1)
                    index += 1
                meta = [{"name": {"value": "lun"}, "value": lun}] if lun else []
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
        resource = url.path.split("/providers/Microsoft.Insights/metrics")[0].lower()
        if self.tenant_of(resource.split("/")[2]) != self.token_tenant():
            return self.reply(401, {"error": {"message": "wrong tenant token"}})
        start, end = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in params["timespan"].split("/"))
        flt = params.get("$filter")
        luns = [flt.split("'")[1]] if flt else [""]
        self.reply(200, {"value": self.series(resource, params["metricnames"].split(","), luns, start, end, True)})

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
            subs = set(body["subscriptions"])
            if query.startswith("Resources"):
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
        luns = ["0", "1"] if params.get("filter") else [""]
        self.world.setdefault("batches", []).append((region, sub, len(body["resourceids"])))
        values = [
            {"resourceid": rid, "value": self.series(rid.lower(), names, luns, start, end, False)}
            for rid in body["resourceids"]
        ]
        self.reply(200, {"values": values})


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
        code, body, _, _ = self.watch("--summary-hour-utc", str(self.now.hour), "--interval-minutes", "60")
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
        code, body, _, _ = self.watch("--summary-hour-utc", str((self.now.hour + 1) % 24))
        self.assertNotIn("daily-summary", [f["rule"] for f in (body or {"findings": []})["findings"]])

    def test_locked_tenant_is_blind_and_reported_once_a_day(self) -> None:
        self.world["tenants"][LOCKED_TENANT] = []
        (self.home / "known-tenants.json").write_text(json.dumps([{"t": HOME_TENANT}, {"t": LOCKED_TENANT}]))
        (self.home / f"fail-{LOCKED_TENANT}").write_text("")
        code, _, summary, _ = self.watch()
        self.assertEqual(code, 0, summary)
        self.assertIn("2 tenant(s), 3 subscription(s), 1 blind", summary)
        code, body, _, _ = self.watch("--summary-hour-utc", str(self.now.hour), "--interval-minutes", "60")
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

    def test_saturation_without_latency_is_only_counted(self) -> None:
        self.world["vms"] = [vm(VM_A)]
        self.world["metrics"] = {
            f"{VM_A}|Data Disk Bandwidth Consumed Percentage|1": {"base": 40.0, "recent": 100.0, "recentMinutes": 12},
            f"{VM_A}|Data Disk Latency|0": {"base": 30.0},
        }
        code, body, summary, _ = self.watch()
        self.assertEqual((code, body), (0, None), summary)
        self.assertIn("1 saturated without latency", summary)
        code, body, _, _ = self.watch("--summary-hour-utc", str(self.now.hour), "--interval-minutes", "60")
        self.assertEqual(code, 1)
        assert body is not None
        self.assertEqual(body["findings"][0]["observed"]["saturatedWithoutLatency"], 1)

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
        code, body, summary, result = self.watch()
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

    def test_rejects_scope_that_could_alter_the_query(self) -> None:
        code, _, _, result = self.watch("--scope", f"/subscriptions/{SUB}/resourceGroups/x') or true //")
        self.assertEqual(code, 2)
        self.assertIn("scope must be", result.stderr)


if __name__ == "__main__":
    unittest.main()
