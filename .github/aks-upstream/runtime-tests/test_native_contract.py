"""Optional real Core HTTP/keychain/staging contract, without Azure or live state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "azure-aks-ops/scripts"))
import aks_auth as auth  # noqa: E402
from aks_common import AKSError, Keychain, authorize  # noqa: E402

TENANT = "11111111-1111-4111-8111-111111111111"
SUB = "22222222-2222-4222-8222-222222222222"
OBJECT = "33333333-3333-4333-8333-333333333333"


@unittest.skipUnless(
    os.environ.get("QM_SOURCE_FOR_AKS_TESTS"),
    "Optional pinned Core source not supplied; not a native-platform acceptance pass",
)
class NativeContractTests(unittest.TestCase):
    def test_real_http_save_ask_grant_scope_materialization_and_cleanup(self):
        temp = tempfile.TemporaryDirectory(prefix="aks-native-contract-")
        self.addCleanup(temp.cleanup)
        fixture = Path(temp.name)
        process = subprocess.Popen(
            [
                "node",
                str(ROOT / ".github/aks-upstream/native_contract_fixture.mjs"),
                os.environ["QM_SOURCE_FOR_AKS_TESTS"],
                str(fixture),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def cleanup():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stderr.close()

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 30
        ready = None
        while time.monotonic() < deadline and process.poll() is None:
            try:
                ready = json.loads((fixture / "ready.json").read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.1)
        self.assertIsNotNone(
            ready,
            "Pinned Core contract fixture failed to start; no live platform was contacted",
        )
        with patch.dict(
            os.environ,
            {"AGENT_API_URL": ready["url"], "AGENT_API_TOKEN": ready["ownerToken"]},
        ):
            owner = Keychain()
            login_home = fixture / "interactive-login-home"
            (login_home / ".azure").mkdir(parents=True, mode=0o700)
            for name in ("azureProfile.json", "msal_token_cache.json"):
                path = login_home / ".azure" / name
                path.write_text('{"fixtureLogin":"interactive-login"}')
                path.chmod(0o600)
            saved = subprocess.run(
                [
                    "node",
                    str(
                        Path(os.environ["QM_SOURCE_FOR_AKS_TESTS"])
                        / "skills-seed/interactive-login/scripts/save-login.mjs"
                    ),
                    "azure",
                    str(login_home),
                ],
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                saved.returncode, 0, "Native interactive-login save failed"
            )
            self.assertFalse(login_home.exists())
            credential = next(
                c
                for c in owner.call("/v1/keychain/credentials")["credentials"]
                if c["service"] == "azure"
            )
            handle = credential["credentialHandle"]
            self.assertEqual(
                owner.identity(), {"scopeId": "personal:alice", "actorId": "alice"}
            )
            self.assertNotIn(
                "contentBase64", json.dumps(owner.call("/v1/keychain/credentials"))
            )
            self.assertEqual(
                authorize(owner, handle, "personal:alice", None, None)["mode"],
                "personal-owner",
            )
            with self.assertRaises(AKSError):
                owner.call("/v1/keychain/use", {"grant": "not-allowed"})
        with patch.dict(
            os.environ,
            {"AGENT_API_URL": ready["url"], "AGENT_API_TOKEN": ready["groupToken"]},
        ):
            group = Keychain()
            self.assertEqual(
                group.identity(),
                {"scopeId": "group:contract-project", "actorId": "alice"},
            )
            args = auth.parser().parse_args(
                [
                    "request-access",
                    "--credential-id",
                    credential["id"],
                    "--tenant",
                    TENANT,
                    "--object-id",
                    OBJECT,
                    "--subscription",
                    SUB,
                    "--resource-group",
                    "rg-test",
                    "--cluster",
                    "aks-test",
                    "--namespace",
                    "app",
                ]
            )
            requested = auth.request_access(args, group)
            self.assertEqual(requested["status"], "pending")
            purpose = json.dumps(
                requested["purpose"], sort_keys=True, separators=(",", ":")
            )
            approve = urllib.request.Request(
                ready["url"] + "/__fixture/approve",
                data=json.dumps(
                    {"askId": requested["askId"], "purpose": purpose}
                ).encode(),
                headers={
                    "x-agent-capability": ready["ownerToken"],
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(approve, timeout=15) as response:
                grant = json.load(response)["grant"]
            self.assertEqual(
                authorize(
                    group,
                    handle,
                    "group:contract-project",
                    grant["id"],
                    requested["purpose"],
                )["mode"],
                "project-shared",
            )
            with self.assertRaises(AKSError):
                authorize(group, handle, "personal:alice", None, None)
            stage = urllib.request.Request(
                ready["url"] + "/__fixture/stage",
                data=json.dumps(
                    {"grant": grant["id"], "bundleRoot": str(ROOT)}
                ).encode(),
                headers={
                    "x-agent-capability": ready["groupToken"],
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(stage, timeout=15) as response:
                staged = json.load(response)
            self.assertTrue(staged["nativeStaging"] and staged["cleanup"])
            self.assertEqual(staged["executed"], ["azure-support-ticket", "azure-ops"])
            self.assertEqual(
                sorted(staged["savedPaths"]),
                [".azure/azureProfile.json", ".azure/msal_token_cache.json"],
            )
        with patch.dict(
            os.environ,
            {"AGENT_API_URL": ready["url"], "AGENT_API_TOKEN": "invalid-fixture-token"},
        ):
            with self.assertRaisesRegex(AKSError, "HTTP 401"):
                Keychain().identity()


if __name__ == "__main__":
    unittest.main()
