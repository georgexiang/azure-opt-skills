#!/usr/bin/env python3

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "az_guard.py"
SPEC = importlib.util.spec_from_file_location("az_guard", SCRIPT)
assert SPEC and SPEC.loader
az_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(az_guard)

SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"


def support_args() -> list[str]:
    return ["support", "in-subscription", "tickets", "create", "--title", "blocked"]


class GuardValidationTests(unittest.TestCase):
    def assert_guard_error(self, code: str, callback) -> None:
        with self.assertRaises(az_guard.GuardError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_allows_expected_read_command(self) -> None:
        az_guard.validate_read(["vm", "show", "-g", "rg", "-n", "vm", "-o", "json"])

    def test_denies_destructive_command(self) -> None:
        self.assert_guard_error("COMMAND_DENIED", lambda: az_guard.validate_read(["vm", "delete", "-g", "rg", "-n", "vm"]))

    def test_denies_direct_support_creation(self) -> None:
        self.assert_guard_error("COMMAND_DENIED", lambda: az_guard.validate_read(support_args()))

    def test_denies_support_ticket_reads(self) -> None:
        for operation in ("list", "show"):
            with self.subTest(operation=operation):
                self.assert_guard_error(
                    "COMMAND_DENIED",
                    lambda operation=operation: az_guard.validate_read(
                        ["support", "in-subscription", "tickets", operation]
                    ),
                )

    def test_allows_support_classification_reads(self) -> None:
        az_guard.validate_read(["support", "services", "list", "-o", "json"])
        az_guard.validate_read(
            [
                "support",
                "services",
                "problem-classifications",
                "list",
                "--service-name",
                "service-id",
                "-o",
                "json",
            ]
        )

    def test_allows_arm_get(self) -> None:
        az_guard.validate_read(
            [
                "rest",
                "--method",
                "get",
                "--url",
                "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                "-o",
                "json",
            ]
        )

    def test_denies_external_rest_url(self) -> None:
        self.assert_guard_error(
            "REST_URL_DENIED",
            lambda: az_guard.validate_read(
                ["rest", "--method", "get", "--url", "https://example.com/?api-version=1", "-o", "json"]
            ),
        )

    def test_denies_general_arm_post(self) -> None:
        self.assert_guard_error(
            "REST_METHOD_DENIED",
            lambda: az_guard.validate_read(
                [
                    "rest",
                    "--method",
                    "post",
                    "--url",
                    "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                    "--body",
                    "{}",
                    "-o",
                    "json",
                ]
            ),
        )

    def test_denies_duplicate_rest_method_aliases(self) -> None:
        self.assert_guard_error(
            "REST_OPTION_DENIED",
            lambda: az_guard.validate_read(
                [
                    "rest",
                    "--method",
                    "get",
                    "-m",
                    "post",
                    "--url",
                    "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                ]
            ),
        )

    def test_denies_duplicate_rest_url_aliases(self) -> None:
        self.assert_guard_error(
            "REST_OPTION_DENIED",
            lambda: az_guard.validate_read(
                [
                    "rest",
                    "--url",
                    "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                    "-u",
                    "https://example.com/?api-version=1",
                ]
            ),
        )

    def test_denies_rest_uri_alias(self) -> None:
        self.assert_guard_error(
            "REST_OPTION_DENIED",
            lambda: az_guard.validate_read(
                [
                    "rest",
                    "--url",
                    "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                    "--uri",
                    "https://example.com/?api-version=1",
                ]
            ),
        )

    def test_denies_attached_rest_short_options(self) -> None:
        for option in ("-mpost", "-uhttps://example.com/?api-version=1", "-b{}"):
            with self.subTest(option=option):
                self.assert_guard_error(
                    "REST_OPTION_DENIED",
                    lambda option=option: az_guard.validate_read(
                        [
                            "rest",
                            "--url",
                            "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                            option,
                        ]
                    ),
                )

    def test_denies_separated_rest_body_short_option(self) -> None:
        self.assert_guard_error(
            "REST_OPTION_DENIED",
            lambda: az_guard.validate_read(
                [
                    "rest",
                    "--url",
                    "https://management.azure.com/subscriptions/abc/resources?api-version=2021-04-01",
                    "-b",
                    "{}",
                ]
            ),
        )

    def test_denies_repeated_canonical_rest_options(self) -> None:
        base_url = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
        body = json.dumps({"subscriptions": [SUBSCRIPTION], "query": "Resources | limit 1"})
        cases = [
            ["rest", "--method", "get", "--method", "post", "--url", base_url],
            ["rest", "--url", base_url, "--url", base_url],
            ["rest", "--method", "post", "--url", base_url, "--body", body, "--body", body],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assert_guard_error("OPTION_DUPLICATE", lambda args=args: az_guard.validate_read(args))

    def test_allows_bounded_resource_graph_post(self) -> None:
        body = json.dumps({"subscriptions": [SUBSCRIPTION], "query": "Resources | limit 1"})
        az_guard.validate_read(
            [
                "rest",
                "--method",
                "post",
                "--url",
                "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01",
                "--body",
                body,
                "-o",
                "json",
            ]
        )

    def test_denies_resource_graph_body_with_bad_subscription(self) -> None:
        body = json.dumps({"subscriptions": ["not-a-subscription"], "query": "Resources | limit 1"})
        self.assert_guard_error(
            "RESOURCE_GRAPH_BODY_INVALID",
            lambda: az_guard.validate_read(
                [
                    "rest",
                    "--method",
                    "post",
                    "--url",
                    "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01",
                    "--body",
                    body,
                ]
            ),
        )

    def test_denies_non_json_output(self) -> None:
        self.assert_guard_error(
            "OUTPUT_FORMAT_DENIED",
            lambda: az_guard.validate_read(["account", "show", "-o", "tsv"]),
        )

    def test_denies_duplicate_output_aliases(self) -> None:
        self.assert_guard_error(
            "OPTION_DUPLICATE",
            lambda: az_guard.validate_read(["account", "show", "-o", "json", "--output", "json"]),
        )

    def test_redacts_secret_material(self) -> None:
        value = 'Bearer abc.def.ghi password=hunter2 "accessToken":"secret"'
        redacted = az_guard.redact(value)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn('"secret"', redacted)
        self.assertNotIn("abc.def.ghi", redacted)


class RequestFileTests(unittest.TestCase):
    def test_reads_workspace_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.json"
            request.write_text(json.dumps({"args": ["account", "show"]}), encoding="utf-8")
            with patch("pathlib.Path.cwd", return_value=root):
                self.assertEqual(az_guard.read_request("request.json"), ["account", "show"])

    def test_denies_request_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as outside:
            request = Path(outside) / "request.json"
            request.write_text(json.dumps({"args": ["account", "show"]}), encoding="utf-8")
            with patch("pathlib.Path.cwd", return_value=Path(workspace)):
                with self.assertRaises(az_guard.GuardError) as raised:
                    az_guard.read_request(str(request))
            self.assertEqual(raised.exception.code, "REQUEST_FILE_DENIED")

    def test_request_file_cannot_enable_support_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "support.json"
            request.write_text(json.dumps({"args": support_args()}), encoding="utf-8")
            with patch("pathlib.Path.cwd", return_value=root):
                args = az_guard.read_request("support.json")
                with self.assertRaises(az_guard.GuardError) as raised:
                    az_guard.validate_read(args)
            self.assertEqual(raised.exception.code, "COMMAND_DENIED")


class ExecutionTests(unittest.TestCase):
    def test_executes_argv_without_shell(self) -> None:
        def complete(command, **kwargs):
            kwargs["stdout"].write(b'{"ok":true}\n')
            return subprocess.CompletedProcess(command, 0)

        with patch.object(az_guard.shutil, "which", return_value="/usr/bin/az"), patch.object(
            az_guard.subprocess, "run", side_effect=complete
        ) as run:
            result = az_guard.execute(["account", "show"])
        self.assertEqual(result, {"ok": True})
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/az", "account", "show"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_rejects_large_output_before_reading_json(self) -> None:
        def complete(command, **kwargs):
            kwargs["stdout"].write(b"x" * (az_guard.MAX_OUTPUT_CHARS + 1))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(az_guard.shutil, "which", return_value="/usr/bin/az"), patch.object(
            az_guard.subprocess, "run", side_effect=complete
        ):
            with self.assertRaises(az_guard.GuardError) as raised:
                az_guard.execute(["account", "show"])
        self.assertEqual(raised.exception.code, "OUTPUT_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()