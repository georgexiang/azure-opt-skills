"""Public runtime flows with Azure/Kubernetes/API boundaries replaced by fixtures."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib
import time
import jwt
from cryptography.hazmat.primitives import serialization
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "azure-aks-ops/scripts"))
import aks_auth as auth  # noqa: E402
import aks_runtime as runtime  # noqa: E402
from aks_common import AKSError, Keychain, PURPOSE_KIND, run  # noqa: E402

TENANT = "11111111-1111-4111-8111-111111111111"
SUB = "22222222-2222-4222-8222-222222222222"
OBJECT = "33333333-3333-4333-8333-333333333333"
RESOURCE_ID = f"/subscriptions/{SUB}/resourceGroups/rg-test/providers/Microsoft.ContainerService/managedClusters/aks-test"
SERVICE = f"azure-aks-{TENANT}-{OBJECT}"
HANDLE = SERVICE + ":credential-one"
SECRET = "fixture-sensitive-token-do-not-print"


class API(Keychain):
    def __init__(self) -> None:
        self.requests = []
        self.scope_id = "personal:alice"
        self.own = [
            {
                "id": "credential-one",
                "service": SERVICE,
                "ownerId": "alice",
                "kind": "file",
            }
        ]
        self.grants = []
        self.ask_response = None

    def identity(self) -> dict:
        return {"scopeId": self.scope_id, "actorId": "alice"}

    def call(self, path: str, body: dict | None = None) -> dict:
        self.requests.append((path, body))
        if path.endswith("/grants"):
            return {"grants": self.grants}
        if path.endswith("/asks"):
            return self.ask_response or {
                "ask": {
                    "id": "ask-one",
                    "status": "pending",
                    "purpose": body["purpose"],
                    "requestedMode": body["requestedMode"],
                }
            }
        if body:
            return {
                "credential": {
                    "id": "saved-id",
                    "credentialHandle": body["service"] + ":saved-id",
                }
            }
        return {"credentials": self.own}


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cert_temp = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.cert_temp.name) / "ca.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=AKS-Fixture",
                "-days",
                "1",
                "-keyout",
                str(Path(cls.cert_temp.name) / "key.pem"),
                "-out",
                str(cls.cert),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.ca = base64.b64encode(cls.cert.read_bytes()).decode()
        cls.key_pem = (Path(cls.cert_temp.name) / "key.pem").read_bytes()
        public_key = serialization.load_pem_private_key(
            cls.key_pem, password=None
        ).public_key()
        cls.jwk = jwt.algorithms.RSAAlgorithm.to_jwk(public_key, as_dict=True)
        cls.jwk["kid"] = "fixture-key"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cert_temp.cleanup()

    def setUp(self) -> None:
        self.directory = Path("/tmp/qm-credentials." + uuid.uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.home = self.directory / "home"
        self.home.mkdir(mode=0o700)
        (self.home / ".azure").mkdir(mode=0o700)
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "AZURE_CONFIG_DIR": str(self.home / ".azure"),
                "AGENT_API_TOKEN": SECRET,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.api = API()
        self.tools = {
            "azure_python": Path("/trusted/python"),
            "kubelogin": Path("/trusted/kubelogin"),
            "kubectl": {"1.34.1": Path("/trusted/kubectl")},
        }
        self.commands = []
        self.endpoint = "https://aks.example.test:443"
        self.account_tenant = TENANT
        self.token_oid = OBJECT
        self.token_expiry = int(time.time()) + 600
        self.invalid_signature = False
        self.jwks = patch(
            "aks_common.read_azure_keys", return_value={"keys": [self.jwk]}
        )
        self.jwks.start()
        self.addCleanup(self.jwks.stop)
        self.metadata_id = RESOURCE_ID
        self.metadata = {
            "id": RESOURCE_ID,
            "name": "aks-test",
            "resourceGroup": "rg-test",
            "kubernetesVersion": "1.34.2",
            "fqdn": "aks.example.test",
            "privateFqdn": None,
            "provisioningState": "Succeeded",
            "powerState": "Running",
            "aadProfile": {"managed": True, "tenantID": TENANT},
        }
        self.deny_nodes = False

    def args(self, action: str = "status", extra: list[str] | None = None):
        return runtime.parser().parse_args(
            [
                "--tools-root",
                "/trusted",
                "--handle",
                HANDLE,
                "--scope",
                "personal:alice",
                "--tenant",
                TENANT,
                "--subscription",
                SUB,
                "--resource-group",
                "rg-test",
                "--cluster",
                "aks-test",
                *(extra or []),
                action,
            ]
        )

    def command(self, argv: list[str], env: dict, **kwargs) -> bytes:
        self.commands.append(argv)
        self.assertNotIn("AGENT_API_TOKEN", env)
        self.assertNotIn(SECRET, json.dumps(env))
        if "azure.cli" in argv:
            self.assertIn("-I", argv)
            args = argv[argv.index("azure.cli") + 1 :]
            if args[:2] == ["account", "show"]:
                value = {
                    "tenantId": self.account_tenant,
                    "subscriptionId": SUB,
                    "state": "Enabled",
                    "cloud": "AzureCloud",
                    "user": {"name": "azure-user@example.invalid", "type": "user"},
                }
            elif args[:2] == ["account", "list"]:
                value = [
                    {
                        "tenantId": TENANT,
                        "subscriptionId": SUB,
                        "name": "Fixture",
                        "state": "Enabled",
                        "cloud": "AzureCloud",
                    }
                ]
            elif args[:2] == ["account", "set"]:
                return b""
            elif args[:2] == ["aks", "show"]:
                value = {**self.metadata, "id": self.metadata_id}
            elif args[:2] == ["aks", "get-credentials"]:
                self.assertNotIn("--admin", args)
                original = {
                    "clusters": [
                        {
                            "cluster": {
                                "server": self.endpoint,
                                "certificate-authority-data": self.ca,
                            }
                        }
                    ],
                    "contexts": [{}],
                    "users": [
                        {
                            "user": {
                                "exec": {"command": "RUN_EVIL", "args": [SECRET]},
                                "token": SECRET,
                            }
                        }
                    ],
                }
                Path(args[args.index("--file") + 1]).write_text(json.dumps(original))
                return b""
            elif args[:3] == ["aks", "nodepool", "list"]:
                value = [{"name": "system", "count": 2}]
            elif args[:2] == ["account", "get-access-token"]:
                claims = {
                    "oid": self.token_oid,
                    "tid": TENANT,
                    "iss": f"https://sts.windows.net/{TENANT}/",
                    "aud": "https://management.azure.com/",
                    "exp": self.token_expiry,
                }
                token = jwt.encode(
                    claims,
                    self.key_pem,
                    algorithm="RS256",
                    headers={"kid": "fixture-key"},
                )
                if self.invalid_signature:
                    parts = token.split(".")
                    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
                    token = ".".join(parts)
                value = {"accessToken": token}
            else:
                self.fail(f"Unexpected Azure command {args}")
            return json.dumps(value).encode()
        config = json.loads(Path(argv[argv.index("--kubeconfig") + 1]).read_text())
        self.assertNotIn("RUN_EVIL", json.dumps(config))
        self.assertNotIn(SECRET, json.dumps(config))
        self.assertEqual(
            config["users"][0]["user"]["exec"]["command"], "/trusted/kubelogin"
        )
        self.assertEqual(config["clusters"][0]["cluster"]["server"], self.endpoint)
        if "version" in argv:
            return json.dumps({"serverVersion": {"gitVersion": "v1.34.2"}}).encode()
        kind = argv[argv.index("get") + 1]
        if kind == "nodes" and self.deny_nodes:
            raise AKSError("FORBIDDEN", "Denied")
        if kind == "nodes":
            item = {
                "metadata": {"name": "node-one", "annotations": {"secret": SECRET}},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "nodeInfo": {"kubeletVersion": "v1.34.2"},
                },
            }
        elif kind == "pods":
            self.assertIn("--namespace", argv)
            item = {
                "metadata": {"name": "app", "namespace": "app"},
                "spec": {"containers": [{"env": [{"value": SECRET}]}]},
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [
                        {
                            "name": "main",
                            "ready": False,
                            "restartCount": 3,
                            "state": {
                                "waiting": {
                                    "reason": "ImagePullBackOff",
                                    "message": SECRET,
                                }
                            },
                        }
                    ],
                },
            }
        else:
            item = {
                "metadata": {"name": "event", "namespace": "app"},
                "type": "Warning",
                "reason": "Failed",
                "message": SECRET,
                "involvedObject": {"kind": "Pod", "name": "app"},
            }
        return json.dumps({"items": [item], "metadata": {}}).encode()

    def execute(self, args):
        with patch.object(runtime, "run", side_effect=self.command):
            return runtime.execute(args, self.api, self.tools)

    def test_owner_status_binds_target_projects_evidence_and_cleans_kubeconfig(self):
        result = self.execute(self.args(extra=["--namespace", "app", "--cluster-read"]))
        self.assertEqual(result["evidence"]["nodes"]["items"][0]["ready"], "True")
        self.assertEqual(
            result["evidence"]["pods:app"]["items"][0]["containers"][0][
                "waitingReason"
            ],
            "ImagePullBackOff",
        )
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(list(self.directory.iterdir()), [self.home])
        self.assertFalse((self.home / ".kube").exists())
        self.assertTrue(all("--all-namespaces" not in cmd for cmd in self.commands))
        self.assertIn("azure-user@example.invalid", json.dumps(result))

    def test_default_status_only_reads_arm_not_kube_credentials(self):
        result = self.execute(self.args())
        self.assertIn("No node or namespace read", result["kubernetes"]["unavailable"])
        self.assertFalse(any("get-credentials" in cmd for cmd in self.commands))

    def test_symlinked_login_files_are_rejected_before_cli(self):
        path = self.home / ".azure/azureProfile.json"
        path.symlink_to(self.directory / "another-identity")
        with self.assertRaisesRegex(AKSError, "unsafe login files"):
            self.execute(self.args("cluster"))
        self.assertEqual(self.commands, [])

    def test_resident_azure_cache_is_not_an_authorization(self):
        with patch.dict(os.environ, {"HOME": "/home/alice"}):
            with self.assertRaisesRegex(AKSError, "execute.credentials"):
                self.execute(self.args())
        self.assertEqual(self.commands, [])

    def test_live_identity_and_cluster_mismatch_fail_before_kubernetes(self):
        self.account_tenant = OBJECT
        with self.assertRaisesRegex(AKSError, "account does not match"):
            self.execute(self.args("connect"))
        self.assertFalse(any("get-credentials" in cmd for cmd in self.commands))
        self.account_tenant = TENANT
        self.metadata_id = RESOURCE_ID + "-other"
        with self.assertRaisesRegex(AKSError, "different cluster"):
            self.execute(self.args("connect"))

    def test_endpoint_injection_fails_and_cleans_raw_configuration(self):
        self.endpoint = "https://unapproved.example.test"
        with self.assertRaisesRegex(AKSError, "endpoint does not match"):
            self.execute(self.args("connect"))
        self.assertEqual(list(self.directory.iterdir()), [self.home])
        self.assertFalse(any(cmd[0] == "/trusted/kubectl" for cmd in self.commands))

    def test_unsupported_version_and_auth_mode_are_not_bypassed(self):
        self.metadata["kubernetesVersion"] = "1.20.1"
        with self.assertRaisesRegex(AKSError, "one minor"):
            self.execute(self.args("connect"))
        self.metadata["kubernetesVersion"] = "1.34.2"
        self.metadata["aadProfile"]["managed"] = False
        with self.assertRaisesRegex(AKSError, "managed Entra"):
            self.execute(self.args("connect"))

    def shared(self):
        self.api.scope_id = "group:project"
        target = runtime.target_from(
            self.args(extra=["--namespace", "app", "--cluster-read"])
        )
        self.api.grants = [
            {
                "id": "grant-one",
                "credentialId": "credential-one",
                "audienceScopeId": "group:project",
                "mode": "standing",
                "status": "active",
                "purpose": json.dumps({"kind": PURPOSE_KIND, **target}),
            }
        ]
        args = self.args(
            extra=[
                "--namespace",
                "app",
                "--cluster-read",
                "--scope",
                "group:project",
                "--grant",
                "grant-one",
            ]
        )
        return args

    def test_shared_grant_requires_exact_target_namespace_and_purpose(self):
        args = self.shared()
        result = self.execute(args)
        self.assertEqual(result["authorization"]["mode"], "project-shared")
        args.namespace = ["other"]
        with self.assertRaisesRegex(AKSError, "namespaces"):
            self.execute(args)
        args.namespace = ["app"]
        self.api.grants[0]["purpose"] = "Support ticket submission"
        with self.assertRaisesRegex(AKSError, "purpose"):
            self.execute(args)

    def test_interactive_login_owner_handle_uses_only_json_profile(self):
        self.api.own[0]["service"] = "azure"
        args = self.args("cluster")
        args.handle = "azure:credential-one"
        config = self.home / ".azure"
        (config / "config").write_text("[cloud]\nname=untrusted\n")
        (config / "clouds.config").write_text("untrusted override")
        (config / "msal_http_cache.bin").write_bytes(b"not-a-safe-pickle")
        (config / "commands").mkdir()
        (config / "commands/login.log").write_text(SECRET)
        with patch.object(runtime, "run", side_effect=self.command) as command:

            def sanitized(argv, env, **kwargs):
                self.assertFalse((config / "config").exists())
                self.assertFalse((config / "clouds.config").exists())
                self.assertFalse((config / "msal_http_cache.bin").exists())
                self.assertFalse((config / "commands").exists())
                return self.command(argv, env, **kwargs)

            command.side_effect = sanitized
            result = runtime.execute(args, self.api, self.tools)
        self.assertEqual(result["azureIdentity"]["objectId"], OBJECT)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_interactive_login_shared_handle_requires_approved_immutable_identity(self):
        args = self.shared()
        args.handle = "azure:credential-one"
        with self.assertRaisesRegex(AKSError, "object ID"):
            self.execute(args)
        self.assertEqual(self.commands, [])
        purpose = json.loads(self.api.grants[0]["purpose"])
        purpose["objectId"] = OBJECT
        self.api.grants[0]["purpose"] = json.dumps(purpose)
        self.assertEqual(self.execute(args)["azureIdentity"]["objectId"], OBJECT)
        self.commands.clear()
        self.token_oid = SUB
        with self.assertRaisesRegex(AKSError, "token identity differs"):
            self.execute(args)
        self.assertFalse(any("aks" in command for command in self.commands))

    def test_generic_login_request_binds_object_id_without_changing_legacy_purpose(
        self,
    ):
        args = auth.parser().parse_args(
            [
                "request-access",
                "--credential-id",
                "credential-one",
                "--tenant",
                TENANT,
                "--subscription",
                SUB,
                "--resource-group",
                "rg-test",
                "--cluster",
                "aks-test",
                "--object-id",
                OBJECT,
            ]
        )
        self.assertEqual(
            auth.request_access(args, self.api)["purpose"]["objectId"], OBJECT
        )

    def test_account_discovery_does_not_replace_an_explicitly_requested_identity(self):
        args = self.shared()
        args.action = "accounts"
        args.handle = "azure:credential-one"
        args.object_id = SUB
        purpose = json.loads(self.api.grants[0]["purpose"])
        purpose["objectId"] = OBJECT
        self.api.grants[0]["purpose"] = json.dumps(purpose)
        with self.assertRaisesRegex(AKSError, "object ID differs"):
            self.execute(args)
        self.assertEqual(self.commands, [])

    def test_revoked_expired_and_once_grants_stop_without_fallback(self):
        for field, value in [("status", "revoked"), ("expiresAt", 1), ("mode", "once")]:
            args = self.shared()
            self.commands.clear()
            self.api.grants[0][field] = value
            with self.assertRaises(AKSError):
                self.execute(args)
            self.assertEqual(self.commands, [])

    def test_unverifiable_and_expired_tokens_stop_before_cluster_reads(self):
        self.invalid_signature = True
        with self.assertRaisesRegex(AKSError, "could not be verified"):
            self.execute(self.args("cluster"))
        self.invalid_signature = False
        self.token_expiry = int(time.time()) - 120
        with self.assertRaisesRegex(AKSError, "could not be verified"):
            self.execute(self.args("cluster"))
        self.assertFalse(any("aks" in command for command in self.commands))

    def test_scope_and_actual_staged_azure_identity_cannot_be_substituted(self):
        self.api.scope_id = "group:project"
        with self.assertRaisesRegex(AKSError, "Core-validated capability scope"):
            self.execute(self.args("cluster"))
        self.api.scope_id = "personal:alice"
        self.token_oid = SUB
        with self.assertRaisesRegex(AKSError, "token identity differs"):
            self.execute(self.args("cluster"))
        self.assertFalse(any("aks" in command for command in self.commands))

    def test_shared_scope_cannot_reuse_owner_login_without_grant(self):
        self.api.scope_id = "group:project"
        with self.assertRaisesRegex(AKSError, "explicit AKS standing grant"):
            self.execute(self.args(extra=["--scope", "group:project"]))
        self.assertEqual(self.commands, [])

    def test_status_preserves_forbidden_as_missing_not_healthy(self):
        self.deny_nodes = True
        result = self.execute(self.args(extra=["--cluster-read"]))
        self.assertEqual(result["evidence"]["nodes"], {"unavailable": "FORBIDDEN"})
        self.assertIn("not a healthy result", result["assessment"])

    def test_mutation_raw_flags_and_injection_are_not_interfaces(self):
        for action in ("exec", "delete", "logs", "get-credentials", "runcommand"):
            with (
                self.subTest(action=action),
                patch("sys.stderr"),
                self.assertRaises(SystemExit),
            ):
                self.args(action)
        args = self.args("cluster")
        args.resource_group = "rg;touch /tmp/evil"
        with self.assertRaises(AKSError):
            self.execute(args)

    def test_request_access_uses_native_approval_without_use_or_secret(self):
        args = auth.parser().parse_args(
            [
                "request-access",
                "--credential-id",
                "credential-one",
                "--tenant",
                TENANT,
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
        response = auth.request_access(args, self.api)
        self.assertEqual(response["status"], "pending")
        self.assertEqual(self.api.requests[0][0], "/v1/keychain/asks")
        self.assertEqual(self.api.requests[0][1]["requestedMode"], "standing")
        self.api.ask_response = {
            "existing": True,
            "ask": {"purpose": "other use", "requestedMode": "standing"},
        }
        with self.assertRaisesRegex(AKSError, "different purpose"):
            auth.request_access(args, self.api)

    def test_device_login_saves_native_files_not_kubeconfig_and_removes_profile(self):
        args = auth.parser().parse_args(
            ["login", "--tools-root", "/trusted", "--tenant", TENANT]
        )
        homes = []

        def login_command(argv, env, **kwargs):
            homes.append(Path(env["HOME"]))
            self.assertNotIn("AGENT_API_TOKEN", env)
            self.assertIn("--tenant", argv)
            (Path(env["HOME"]) / ".azure/msal_token_cache.json").write_text(SECRET)
            return b""

        with (
            patch.object(auth, "run", side_effect=login_command),
            patch.object(runtime, "run", side_effect=self.command),
        ):
            response = auth.login(args, self.api, self.tools)
        self.assertTrue(response["ok"])
        self.assertIn(OBJECT, response["credentialHandle"])
        self.assertNotIn(SECRET, json.dumps(response))
        self.assertTrue(all(not home.exists() for home in homes))
        save = self.api.requests[-1][1]
        self.assertEqual(
            [f["path"] for f in save["files"]], [".azure/msal_token_cache.json"]
        )


class CapabilityTests(unittest.TestCase):
    def test_identity_uses_same_core_validated_token_plain_and_compressed(self):
        claims = {"scopeId": "group:project", "actorId": "alice"}
        packed = zlib.compressobj(wbits=-15)
        raw = packed.compress(json.dumps(claims).encode()) + packed.flush()
        for value in (
            claims,
            {
                "encoding": "deflate-raw",
                "claims": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            },
        ):
            encoded = (
                base64.urlsafe_b64encode(json.dumps(value).encode())
                .decode()
                .rstrip("=")
            )
            api = Keychain.__new__(Keychain)
            api.token = "header." + encoded + ".fixture-signature"
            with patch.object(
                api, "call", return_value={"credentials": []}
            ) as boundary:
                self.assertEqual(api.identity(), claims)
            boundary.assert_called_once_with("/v1/keychain/credentials")
        with patch.object(
            api,
            "call",
            side_effect=AKSError("KEYCHAIN_DENIED", "Native verification refused"),
        ):
            with self.assertRaisesRegex(AKSError, "Native verification"):
                api.identity()


class ProcessTests(unittest.TestCase):
    def test_raw_error_is_withheld(self):
        with self.assertRaises(AKSError) as caught:
            run(
                [
                    sys.executable,
                    "-c",
                    f"import sys;sys.stderr.write('{SECRET} Forbidden');sys.exit(1)",
                ],
                {"PATH": "/usr/bin:/bin"},
            )
        self.assertEqual(caught.exception.code, "FORBIDDEN")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_timeout_and_output_limit_are_bounded(self):
        for code, program in [
            ("TIMEOUT", "import time;time.sleep(20)"),
            ("OUTPUT_LIMIT", "import sys;sys.stdout.write('x'*3000000)"),
        ]:
            with self.subTest(code=code), self.assertRaises(AKSError) as caught:
                run(
                    [sys.executable, "-c", program],
                    {"PATH": "/usr/bin:/bin"},
                    timeout=1,
                )
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
