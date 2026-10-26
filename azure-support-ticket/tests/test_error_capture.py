"""Offline regression tests; no Azure credentials or network calls."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).parents[1] / "scripts" / "capture_azure_error.py"
spec = importlib.util.spec_from_file_location("capture_error", MODULE)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class ErrorCaptureTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "ticketName": "stable-ticket",
            "attemptTime": "fixture-attempt",
            "contact": {
                "firstName": "FixturePerson",
                "lastName": "ExampleSurname",
                "email": "fixture@example.test",
                "phone": "+8612345678900",
            },
        }

    def test_cli_parameter_message_survives(self):
        error = capture.extract_error(
            "ERROR: (InvalidParameterValue) Unsupported preferredTimeZone.\n"
            "Code: InvalidParameterValue\nMessage: Unsupported preferredTimeZone.\n"
            "Target: contactDetails.preferredTimeZone\n",
            self.record,
        )
        self.assertEqual(error["code"], "InvalidParameterValue")
        self.assertIn("preferredTimeZone", error["message"])
        self.assertEqual(error["target"], "contactDetails.preferredTimeZone")
        self.assertFalse(error["detailsMissing"])

    def test_arm_error_details_and_redaction(self):
        raw = json.dumps(
            {
                "error": {
                    "code": "InvalidParameterValue",
                    "message": "Invalid contact fixture@example.test for FixturePerson ExampleSurname +8612345678900",
                    "target": "contactDetails",
                    "details": [
                        {"code": "BadContact", "message": "fixture@example.test"}
                    ],
                }
            }
        )
        error = capture.extract_error(raw, self.record)
        rendered = json.dumps(error)
        for private in self.record["contact"].values():
            self.assertNotIn(private, rendered)
        self.assertEqual(error["details"][0]["code"], "BadContact")

    def test_auth_material_is_not_reported(self):
        error = capture.extract_error(
            "Code: InvalidParameterValue\nMessage: Authorization: Bearer synthetic-private-value\n",
            self.record,
        )
        self.assertNotIn("synthetic-private-value", json.dumps(error))

    def test_quoted_secret_values_are_not_reported(self):
        for text in [
            '{"client_secret": "synthetic-secret with spaces"}',
            "'access_token': 'synthetic-token'",
            '"Authorization": "Bearer synthetic-auth"',
        ]:
            with self.subTest(text=text):
                error = capture.extract_error(
                    json.dumps({"error": {"code": "Test", "message": text}}),
                    self.record,
                )
                self.assertNotIn("synthetic-", json.dumps(error))

    def test_cli_exception_details_are_not_promoted_or_discarded(self):
        error = capture.extract_error(
            "ERROR: (InvalidParameterValue) Request validation failed.\n"
            "Code: InvalidParameterValue\nMessage: Request validation failed.\n"
            "Exception Details: (BadField) Invalid preferredTimeZone.\n"
            "Code: BadField\nMessage: Invalid preferredTimeZone.\n"
            "Target: contactDetails.preferredTimeZone\n",
            self.record,
        )
        self.assertNotIn("target", error)
        self.assertEqual(error["details"][0]["code"], "BadField")
        self.assertEqual(
            error["details"][0]["target"], "contactDetails.preferredTimeZone"
        )

    def test_multiline_message_keeps_parameter_diagnosis(self):
        error = capture.extract_error(
            "Code: InvalidParameterValue\nMessage: Request invalid.\n"
            "The rejected field is preferredTimeZone.\nTarget: contactDetails\n",
            self.record,
        )
        self.assertIn("preferredTimeZone", error["message"])
        self.assertNotIn("Target:", error["message"])

    def test_nested_cli_details_keep_grandchildren_under_child(self):
        error = capture.extract_error(
            "Code: InvalidParameterValue\nMessage: Request invalid.\n"
            "Exception Details: (Child) Child error.\n"
            "Code: Child\nMessage: Child error.\n"
            "  Exception Details:\n"
            "    (FirstGrandchild) First field error.\n"
            "    Code: FirstGrandchild\n    Message: First field error.\n"
            "    (SecondGrandchild) Second field error.\n"
            "    Code: SecondGrandchild\n    Message: Second field error.\n",
            self.record,
        )
        self.assertEqual(len(error["details"]), 1)
        self.assertEqual(
            [item["code"] for item in error["details"][0]["details"]],
            ["FirstGrandchild", "SecondGrandchild"],
        )

    def test_inline_first_grandchild_preserves_sibling_indentation(self):
        error = capture.extract_error(
            "Code: InvalidParameterValue\nMessage: Request invalid.\n"
            "Exception Details: (Child) Child error.\nCode: Child\nMessage: Child error.\n"
            "  Exception Details: (FirstGrandchild) First field error.\n"
            "    Code: FirstGrandchild\n    Message: First field error.\n"
            "    (SecondGrandchild) Second field error.\n"
            "    Code: SecondGrandchild\n    Message: Second field error.\n",
            self.record,
        )
        self.assertEqual(
            [item["code"] for item in error["details"][0]["details"]],
            ["FirstGrandchild", "SecondGrandchild"],
        )

    def test_missing_message_is_explicit(self):
        error = capture.extract_error("InvalidParameterValue", self.record)
        self.assertTrue(error["detailsMissing"])
        self.assertNotIn("timezone", error.get("message", "").lower())

    def test_atomic_record_preserves_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(self.record))
            capture.save_error(
                path,
                "Code: InvalidParameterValue\nMessage: Invalid preferredTimeZone.\n",
                "fixture-attempt",
            )
            saved = json.loads(path.read_text())
            self.assertEqual(saved["ticketName"], "stable-ticket")
            self.assertIn("preferredTimeZone", saved["lastError"]["message"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_error_does_not_overwrite_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(self.record))
            with self.assertRaises(ValueError):
                capture.save_error(path, "InvalidParameterValue", "older-attempt")
            self.assertEqual(json.loads(path.read_text()), self.record)


if __name__ == "__main__":
    unittest.main()
