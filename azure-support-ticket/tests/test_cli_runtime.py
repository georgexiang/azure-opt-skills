"""Execute the packaged runtime in fresh processes; no Azure credentials or calls."""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "azure-support-ticket/scripts"
RUNTIME = SCRIPTS / "azure_cli.sh"


class CliRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.cli = self.home / ".local/share/azure-cli/2.89.1/bin/az"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "a = sys.argv[1:]\n"
            "ext = Path(os.environ.get('AZURE_EXTENSION_DIR', str(Path.home()/'.azure/cliextensions')))\n"
            "if a[:1] == ['version']:\n"
            " print('2.89.1'); sys.exit(0)\n"
            "if a[:2] == ['extension', 'add']:\n"
            " (ext/'support').mkdir(parents=True, exist_ok=True)\n"
            " (ext/'support/version').write_text('2.0.1\\n')\n"
            " (ext/'support/loadable').touch(); sys.exit(0)\n"
            "if a[:2] == ['extension', 'show']:\n"
            " if not (ext/'support/version').exists(): sys.exit(1)\n"
            " print((ext/'support/version').read_text().strip()); sys.exit(0)\n"
            "if a[:5] == ['support', 'in-subscription', 'tickets', 'create', '--help']:\n"
            " sys.exit(0 if (ext/'support/loadable').exists() else 1)\n"
            "if os.environ.get('FIXTURE_READY'):\n"
            " Path(os.environ['FIXTURE_READY']).touch()\n"
            " while not Path(os.environ['FIXTURE_RELEASE']).exists(): time.sleep(0.02)\n"
            "print(json.dumps({'args':a,'profile':os.environ.get('AZURE_CONFIG_DIR'),"
            "'extensions':str(ext),'dynamic':os.environ.get('AZURE_EXTENSION_USE_DYNAMIC_INSTALL')}))\n"
            "sys.exit(int(os.environ.get('FIXTURE_EXIT', '0')))\n"
        )
        self.cli.chmod(0o755)
        (self.cli.parent.parent / ".complete").write_text("2.89.1\n")
        self.extension = self.home / ".local/share/azure-cli/extensions/support"
        self.extension.mkdir(parents=True)
        (self.extension / "version").write_text("2.0.1\n")
        (self.extension / "loadable").touch()
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}

    def invoke(self, *args, **env):
        return subprocess.run(
            ["bash", str(RUNTIME), *args],
            check=False,
            env={**self.env, **env},
            text=True,
            capture_output=True,
            timeout=20,
        )

    def test_new_process_finds_nondefault_extension_without_export(self):
        # The old bare CLI fails with the same on-disk fixture.
        bare = subprocess.run(
            [str(self.cli), "extension", "show", "--name", "support"],
            check=False,
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(bare.returncode, 0)
        for _ in range(2):
            result = self.invoke("--check")
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = self.invoke("extension", "show", "--name", "support")
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(actual.stdout.strip(), "2.0.1")
        self.assertFalse((self.home / ".azure").exists())

    def test_profile_switch_does_not_change_extension_directory(self):
        for profile in ("tenant-one", "tenant-two"):
            path = str(self.home / profile)
            result = self.invoke("account", "show", AZURE_CONFIG_DIR=path)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = json.loads(result.stdout)
            self.assertEqual(out["profile"], path)
            self.assertEqual(out["extensions"], str(self.extension.parent))
            self.assertEqual(out["dynamic"], "no")
            self.assertFalse(Path(path).exists())

    def test_ignores_path_shadow_and_extension_override(self):
        shadow = self.home / "shadow"
        shadow.mkdir()
        az = shadow / "az"
        az.write_text("#!/bin/sh\nexit 99\n")
        az.chmod(0o755)
        result = self.invoke(
            "account",
            "show",
            PATH=f"{shadow}:/usr/bin:/bin",
            AZURE_EXTENSION_DIR="/wrong",
            AZURE_EXTENSION_USE_DYNAMIC_INSTALL="yes_without_prompt",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["extensions"], str(self.extension.parent)
        )

    def test_preserves_arguments_and_exit_code_without_retry(self):
        text = "title with spaces; $(touch NEVER)\n'quoted'"
        result = self.invoke(
            "support",
            "in-subscription",
            "tickets",
            "create",
            "--title",
            text,
            FIXTURE_EXIT="43",
        )
        self.assertEqual(result.returncode, 43)
        self.assertEqual(json.loads(result.stdout)["args"][-1], text)
        self.assertFalse((self.home / "NEVER").exists())

    def test_check_detects_missing_wrong_and_unloadable_extension(self):
        version = self.extension / "version"
        version.unlink()
        result = self.invoke("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SUPPORT_TOOLCHAIN_NOT_READY", result.stderr)
        version.write_text("wrong\n")
        self.assertNotEqual(self.invoke("--check").returncode, 0)
        version.write_text("2.0.1\n")
        (self.extension / "loadable").unlink()
        self.assertNotEqual(self.invoke("--check").returncode, 0)

    def test_ensure_reuses_existing_install_without_authentication(self):
        result = self.invoke(
            "--ensure", AZURE_CONFIG_DIR=str(self.home / "private-profile")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "private-profile").exists())
        self.assertEqual(self.invoke("--check").returncode, 0)

    def mock_download(self, fails=False):
        fake_bin = self.home / "download-bin"
        fake_bin.mkdir()
        for name, body in {
            "curl": "exit 45"
            if fails
            else 'while [ "$1" != --output ]; do shift; done; printf wheel >"$2"',
            "sha256sum": "exit 0",
        }.items():
            path = fake_bin / name
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)
        return f"{fake_bin}:/usr/bin:/bin"

    def test_ensure_repairs_correct_version_but_unloadable_extension(self):
        (self.extension / "loadable").unlink()
        self.assertNotEqual(self.invoke("--check").returncode, 0)
        result = self.invoke("--ensure", PATH=self.mock_download())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invoke("--check").returncode, 0)
        self.assertFalse((self.home / ".azure").exists())

    def test_failed_repair_preserves_old_files_and_does_not_load_profile(self):
        (self.extension / "loadable").unlink()
        sentinel = self.extension / "sentinel"
        sentinel.write_text("keep")
        result = self.invoke(
            "--ensure",
            PATH=self.mock_download(fails=True),
            AZURE_CONFIG_DIR=str(self.home / "untouched"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((self.home / "untouched").exists())

    def test_install_waits_for_active_command_and_concurrent_ensures_finish(self):
        ready, release = self.home / "ready", self.home / "release"
        active = subprocess.Popen(
            ["bash", str(RUNTIME), "account", "show"],
            env={
                **self.env,
                "FIXTURE_READY": str(ready),
                "FIXTURE_RELEASE": str(release),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        installers = []
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            for _ in range(2):
                installers.append(
                    subprocess.Popen(
                        ["bash", str(RUNTIME), "--ensure"],
                        env=self.env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            time.sleep(0.2)
            self.assertTrue(all(p.poll() is None for p in installers))
            release.touch()
            active.communicate(timeout=10)
            self.assertEqual(active.returncode, 0)
            for process in installers:
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
        finally:
            release.touch()
            for process in [active, *installers]:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_missing_cli_never_falls_back_to_system_or_installs_during_create(self):
        self.cli.unlink()
        result = self.invoke("support", "in-subscription", "tickets", "create")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SUPPORT_TOOLCHAIN_NOT_READY", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_rejects_symlinked_toolchain(self):
        outside = self.home / "outside"
        self.cli.parent.rename(outside)
        self.cli.parent.symlink_to(outside, target_is_directory=True)
        result = self.invoke("version")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class PackagedToolchainTests(unittest.TestCase):
    def test_support_bundle_is_self_contained_and_shared_assets_match(self):
        for name in (
            "azure_cli_env.sh",
            "azure_cli.sh",
            "install_azure_cli.sh",
            "azure-cli-2.89.1.lock",
        ):
            support = SCRIPTS / name
            ops = ROOT / "azure-ops/scripts" / name
            with self.subTest(asset=name):
                self.assertTrue(support.is_file())
                self.assertFalse(support.is_symlink())
                self.assertEqual(support.read_bytes(), ops.read_bytes())


if __name__ == "__main__":
    unittest.main()
