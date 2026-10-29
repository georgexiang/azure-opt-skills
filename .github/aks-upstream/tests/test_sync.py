"""Offline Git fixtures: reproducibility, dependency closure and fail-closed updates."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".github/aks-upstream"))
import sync_aks_upstream as sync  # noqa: E402
import validate_aks_bundle as validator  # noqa: E402


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "target"
        self.source = self.base / "source"
        self.source.mkdir()
        (self.root / ".github/aks-upstream").mkdir(parents=True)
        (self.root / ".github/aks-upstream/aks.sources.json").write_bytes(
            (ROOT / ".github/aks-upstream/aks.sources.json").read_bytes()
        )
        self.settings = sync.config(self.root)
        self.command("init", "-q")
        self.command("config", "user.name", "Fixture")
        self.command("config", "user.email", "fixture@example.invalid")
        for name in self.settings["selected"]:
            self.put(
                f"skills/{name}/SKILL.md",
                "---\nname: fixture\n---\n# Guide\n\n[Evidence](references/evidence.md)\n",
            )
            self.put(
                f"skills/{name}/references/evidence.md",
                "# Evidence\n\n[Back](../SKILL.md)\n",
            )
        self.put("LICENSE", "MIT License\nCopyright Microsoft\n")
        self.put(".mcp.json", "{}\n")
        self.commit = self.save()

    def command(self, *args: str) -> str:
        return (
            subprocess.check_output(
                ["git", "-C", str(self.source), *args], stderr=subprocess.PIPE
            )
            .decode()
            .strip()
        )

    def put(self, path: str, text: str) -> None:
        target = self.source / sync.PLUGIN / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def save(self) -> str:
        self.command("add", ".")
        self.command("commit", "-qm", "fixture")
        return self.command("rev-parse", "HEAD")

    def build(self) -> tuple[dict[str, bytes], dict]:
        return sync.build(self.root, self.source, self.commit)

    def test_reproducible_immutable_blobs_and_local_rewrites(self) -> None:
        before, lock = self.build()
        self.put("skills/aks-troubleshooting/SKILL.md", "uncommitted contamination")
        after, _ = self.build()
        self.assertEqual(before, after)
        self.assertEqual(lock["commit"], self.commit)
        evidence = before[
            sync.GENERATED + "/aks-troubleshooting/references/evidence.md"
        ].decode()
        self.assertIn("[Back](../guide.md)", evidence)
        self.assertNotIn(
            "name: fixture",
            before[sync.GENERATED + "/aks-troubleshooting/guide.md"].decode(),
        )
        sync.publish(self.root, before, False)
        sync.publish(self.root, before, True)

    def test_shell_is_inert_and_never_executed(self) -> None:
        self.put(
            "skills/aks-troubleshooting/scripts/collect.sh",
            "#!/bin/sh\ntouch /should-not-run\n",
        )
        self.put(
            "skills/aks-troubleshooting/references/collector.md",
            "[Collector](../scripts/collect.sh)\n",
        )
        self.commit = self.save()
        output, _ = self.build()
        path = sync.GENERATED + "/aks-troubleshooting/scripts/collect.sh.md"
        self.assertIn(b"````sh", output[path])
        self.assertIn(
            b"collect.sh.md",
            output[sync.GENERATED + "/aks-troubleshooting/references/collector.md"],
        )
        self.assertFalse(any(p.endswith(".sh") for p in output))

    def test_collector_reference_labels_are_preserved(self) -> None:
        self.put("skills/aks-troubleshooting/scripts/collect.sh", "#!/bin/sh\nexit 0\n")
        self.put(
            "skills/aks-troubleshooting/SKILL.md",
            "---\nname: fixture\n---\n[Collector][scripts/collect.sh]\n\n[SCRIPTS/COLLECT.SH]: scripts/collect.sh\n",
        )
        self.commit = self.save()
        output, _ = self.build()
        guide = output[sync.GENERATED + "/aks-troubleshooting/guide.md"].decode()
        self.assertIn("[Collector][scripts/collect.sh]", guide)
        self.assertIn("[SCRIPTS/COLLECT.SH]: scripts/collect.sh.md", guide)

    def test_unselected_sibling_and_metadata_are_monitored_not_installed(self) -> None:
        self.put("skills/aks-new-capability/SKILL.md", "new upstream skill\n")
        self.put("hooks/danger.sh", "exit 1\n")
        self.commit = self.save()
        output, lock = self.build()
        self.assertIn("aks-new-capability", lock["inventory"])
        self.assertIn(sync.PLUGIN + "/hooks/danger.sh", lock["plugin_metadata_blobs"])
        self.assertFalse(
            any("aks-new-capability" in p or "hooks/" in p for p in output)
        )

    def test_missing_selected_skill_is_error(self) -> None:
        (self.source / sync.PLUGIN / "skills/aks-known-issues/SKILL.md").unlink()
        self.commit = self.save()
        with self.assertRaisesRegex(sync.SyncError, "Selected skill missing"):
            self.build()

    def test_new_unknown_file_type_is_error(self) -> None:
        self.put("skills/aks-known-issues/collector.py", "print('no')\n")
        self.commit = self.save()
        with self.assertRaisesRegex(sync.SyncError, "asset type"):
            self.build()

    def test_symlink_and_submodule_assets_are_rejected(self) -> None:
        target = self.source / sync.PLUGIN / "skills/aks-known-issues/escape.md"
        target.symlink_to("/etc/passwd")
        self.commit = self.save()
        with self.assertRaisesRegex(sync.SyncError, "regular file"):
            self.build()
        with self.assertRaisesRegex(sync.SyncError, "regular file"):
            sync.blob(self.source, ("160000", "commit", self.commit), "submodule")

    def test_unbundled_escape_and_unsafe_url_dependencies_fail(self) -> None:
        for link in (
            "missing.md",
            "../../aks-gpu-inference/SKILL.md",
            "../../../../../../etc/passwd",
            "/etc/passwd",
            "javascript:alert",
        ):
            with self.subTest(link=link):
                self.put(
                    "skills/aks-known-issues/references/evidence.md", f"[Bad]({link})\n"
                )
                self.commit = self.save()
                with self.assertRaises(sync.SyncError):
                    self.build()

    def test_titled_markdown_dependency_is_checked(self) -> None:
        self.put(
            "skills/aks-known-issues/references/evidence.md",
            '[Missing](missing.md "title")\n',
        )
        self.commit = self.save()
        with self.assertRaisesRegex(sync.SyncError, "Unbundled dependency"):
            self.build()

    def test_whitespace_newline_and_angle_dependencies_are_checked(self) -> None:
        for link in (
            "[Missing]( missing.md )",
            "[Missing](\nmissing.md\n)",
            '[Missing](<missing.md> "title")',
        ):
            with self.subTest(link=link):
                self.put("skills/aks-known-issues/references/evidence.md", link + "\n")
                self.commit = self.save()
                with self.assertRaisesRegex(sync.SyncError, "Unbundled dependency"):
                    self.build()

    def test_reference_label_is_not_renamed_with_destination(self) -> None:
        self.put(
            "skills/aks-known-issues/SKILL.md",
            "---\nname: fixture\n---\n[Back][SKILL.md]\n\n[SKILL.md]: SKILL.md\n",
        )
        self.commit = self.save()
        output, _ = self.build()
        guide = output[sync.GENERATED + "/aks-known-issues/guide.md"].decode()
        self.assertIn("[Back][SKILL.md]", guide)
        self.assertIn("[SKILL.md]: guide.md", guide)
        self.assertNotIn("[guide.md]:", guide)

    def test_unsupported_markdown_syntax_fails_closed(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "Unsupported Markdown"):
            sync.markdown_targets("[![nested](image.md)](missing.md)")

    def test_container_reference_definitions_fail_closed(self) -> None:
        for text in (
            "> [Missing][x]\n>\n> [x]: missing.md\n",
            "- [x]: missing.md\n",
            "[Missing][x]\n\n[x]:\nmissing.md\n",
            "[Missing][long label]\n\n[long\nlabel]: missing.md\n",
        ):
            with self.subTest(text=text):
                self.put("skills/aks-known-issues/references/evidence.md", text)
                self.commit = self.save()
                with self.assertRaisesRegex(
                    sync.SyncError, "Unsupported Markdown reference"
                ):
                    self.build()

    def test_reference_style_dependency_is_checked(self) -> None:
        self.put(
            "skills/aks-known-issues/references/evidence.md",
            "[Missing][e]\n\n[e]: missing.md\n",
        )
        self.commit = self.save()
        with self.assertRaisesRegex(sync.SyncError, "Unbundled dependency"):
            self.build()

    def test_license_change_requires_attention(self) -> None:
        self.put("LICENSE", "Different License\n")
        self.commit = self.save()
        with self.assertRaisesRegex(sync.SyncError, "License changed"):
            self.build()

    def test_only_immutable_sha_accepted(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "immutable"):
            sync.build(self.root, self.source, "HEAD")

    def test_drift_deleted_files_and_preserve_local_content(self) -> None:
        output, _ = self.build()
        sync.publish(self.root, output, False)
        local = self.root / "azure-aks-ops/SKILL.md"
        local.write_text("local workflow\n")
        generated = self.root / sync.GENERATED / "aks-troubleshooting/guide.md"
        generated.write_text("manual edit\n")
        with self.assertRaisesRegex(sync.SyncError, "drift"):
            sync.publish(self.root, output, True)
        stale = generated.parent / "stale.md"
        stale.write_text("stale\n")
        sync.publish(self.root, output, False)
        self.assertFalse(stale.exists())
        self.assertEqual(local.read_text(), "local workflow\n")

    def test_directory_file_transitions_are_supported(self) -> None:
        self.put("skills/aks-known-issues/references/x.md/child.md", "child\n")
        self.commit = self.save()
        output, _ = self.build()
        sync.publish(self.root, output, False)
        parent = self.source / sync.PLUGIN / "skills/aks-known-issues/references/x.md"
        (parent / "child.md").unlink()
        parent.rmdir()
        parent.write_text("replacement file\n")
        self.commit = self.save()
        output, _ = self.build()
        sync.publish(self.root, output, False)
        self.assertEqual(sync.actual_outputs(self.root), output)
        parent.unlink()
        self.put("skills/aks-known-issues/references/x.md/child.md", "restored child\n")
        self.commit = self.save()
        output, _ = self.build()
        sync.publish(self.root, output, False)
        self.assertEqual(sync.actual_outputs(self.root), output)

    def test_failure_does_not_replace_previous_generated_content(self) -> None:
        output, _ = self.build()
        sync.publish(self.root, output, False)
        self.put(
            "skills/aks-known-issues/references/evidence.md", "[Missing](missing.md)\n"
        )
        self.commit = self.save()
        with self.assertRaises(sync.SyncError):
            new, _ = self.build()
            sync.publish(self.root, new, False)
        self.assertEqual(sync.actual_outputs(self.root), output)

    def test_output_symlink_rejected(self) -> None:
        output, _ = self.build()
        directory = self.root / sync.GENERATED
        directory.parent.mkdir(parents=True)
        directory.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(sync.SyncError, "[Ss]ymlink"):
            sync.publish(self.root, output, False)

    def test_symlink_parent_is_rejected_before_deleting(self) -> None:
        output, _ = self.build()
        (self.root / "azure-aks-ops").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(sync.SyncError, "Symlink"):
            sync.publish(self.root, output, False)

    def test_cli_update_ignores_unrelated_monorepo_commits(self) -> None:
        output, _ = self.build()
        sync.publish(self.root, output, False)
        (self.source / "README.md").write_text("unrelated\n")
        self.save()
        args = ["sync", "--source", str(self.source), "--update"]
        with (
            patch.object(sync, "ROOT", self.root),
            patch.object(sys, "argv", args),
            patch("builtins.print"),
        ):
            sync.main()
        self.assertEqual(sync.actual_outputs(self.root), output)

    def test_cli_update_changes_content_and_writes_review_summary(self) -> None:
        output, _ = self.build()
        sync.publish(self.root, output, False)
        self.put("skills/aks-known-issues/references/evidence.md", "Updated evidence\n")
        candidate = self.save()
        summary = self.base / "summary.md"
        args = [
            "sync",
            "--source",
            str(self.source),
            "--update",
            "--summary",
            str(summary),
        ]
        with (
            patch.object(sync, "ROOT", self.root),
            patch.object(sys, "argv", args),
            patch("builtins.print"),
        ):
            sync.main()
        lock = json.loads((self.root / sync.LOCK).read_text())
        self.assertEqual(lock["commit"], candidate)
        self.assertIn(candidate, summary.read_text())
        self.assertIn("Required human review", summary.read_text())
        sync.publish(self.root, sync.build(self.root, self.source, candidate)[0], True)

    def test_binary_and_oversized_files_fail(self) -> None:
        for text in ("\x00", "x" * (sync.MAX_BLOB + 1)):
            self.put("skills/aks-known-issues/bad.md", text)
            self.commit = self.save()
            with self.assertRaises(sync.SyncError):
                self.build()


class BundleTests(unittest.TestCase):
    def test_checked_in_bundle_integrity(self) -> None:
        validator.validate(ROOT)

    def test_one_skill_entry_and_no_runtime_collectors(self) -> None:
        bundle = ROOT / "azure-aks-ops"
        self.assertEqual(
            [p.relative_to(bundle).as_posix() for p in bundle.rglob("SKILL.md")],
            ["SKILL.md"],
        )
        self.assertEqual(list(bundle.rglob("*.sh")), [])
        self.assertEqual(
            {p.relative_to(bundle).as_posix() for p in bundle.rglob("*.py")},
            {
                f"scripts/{name}.py"
                for name in ("aks_auth", "aks_common", "aks_runtime", "aks_toolchain")
            },
        )
        manifest = json.loads((bundle / "UPSTREAM.json").read_text())
        self.assertEqual(manifest["mode"], "knowledge-only")
        self.assertEqual(len(manifest["selected"]), 3)


if __name__ == "__main__":
    unittest.main()
