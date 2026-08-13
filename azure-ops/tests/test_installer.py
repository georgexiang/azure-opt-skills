#!/usr/bin/env python3

import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
INSTALLER = (ROOT / "scripts" / "install_azure_cli.sh").read_text(encoding="utf-8")
LOCK = (ROOT / "scripts" / "azure-cli-2.89.1.lock").read_text(encoding="utf-8")
INSTALLER_PATH = ROOT / "scripts" / "install_azure_cli.sh"


class InstallerContractTests(unittest.TestCase):
    def existing_install(
        self,
        home: Path,
        reported_version: str = "2.89.1",
        support_version: str = "2.0.1",
    ) -> tuple[Path, Path]:
        install_root = home / ".local" / "share" / "azure-cli" / "2.89.1"
        executable = install_root / "bin" / "az"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/usr/bin/env bash\n"
            f"if [[ \"$1\" == version ]]; then printf '{reported_version}\\n'; exit 0; fi\n"
            f"if [[ \"$1 $2\" == 'extension show' ]]; then printf '{support_version}\\n'; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        (install_root / ".complete").write_text("2.89.1\n", encoding="utf-8")
        command = home / ".local" / "bin" / "az"
        command.parent.mkdir(parents=True)
        command.symlink_to("../share/azure-cli/2.89.1/bin/az")
        extension = home / ".local" / "share" / "azure-cli" / "extensions"
        extension.mkdir()
        (extension / "sentinel").write_text("keep\n", encoding="utf-8")
        return install_root, extension

    def test_versions_and_paths_are_fixed(self) -> None:
        self.assertIn('azure_cli_version="2.89.1"', INSTALLER)
        self.assertIn('support_extension_version="2.0.1"', INSTALLER)
        self.assertIn('install_base="$HOME/.local/share/azure-cli"', INSTALLER)
        self.assertNotRegex(INSTALLER, r"AZURE_CLI_(?:VERSION|INSTALL_ROOT|BIN_DIR|STATE_DIR):-")

    def test_cli_dependencies_require_hashes(self) -> None:
        self.assertIn("--require-hashes", INSTALLER)
        self.assertIn('azure-cli==2.89.1 \\', LOCK)
        self.assertRegex(LOCK, r"azure-cli==2\.89\.1[\s\\]+--hash=sha256:[0-9a-f]{64}")

    def test_support_extension_artifact_is_hash_pinned(self) -> None:
        self.assertIn("support-2.0.1-py2.py3-none-any.whl", INSTALLER)
        self.assertRegex(INSTALLER, r'support_extension_sha256="[0-9a-f]{64}"')
        self.assertIn("sha256sum --check --status", INSTALLER)

    def test_installer_has_check_and_lock_modes(self) -> None:
        self.assertIn('if [[ "${1:-}" == "--check" ]]', INSTALLER)
        self.assertIn('flock 9', INSTALLER)
        self.assertIn('mktemp -d "$install_base/.backup-$azure_cli_version.', INSTALLER)
        self.assertIn('mktemp -d "$install_base/.extensions-$support_extension_version.', INSTALLER)

    def test_installer_rejects_unsafe_install_directories(self) -> None:
        self.assertIn("assert_owned_real_dir", INSTALLER)
        self.assertIn('[[ -d "$path" && ! -L "$path" ]]', INSTALLER)
        self.assertNotIn('find "$install_base"', INSTALLER)

    def test_installer_does_not_write_auth_directory(self) -> None:
        self.assertNotIn("$HOME/.azure", INSTALLER)
        self.assertNotIn("az login", INSTALLER)

    def test_refuses_to_replace_unowned_az_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            command = home / ".local" / "bin" / "az"
            command.parent.mkdir(parents=True)
            command.write_text("do not replace\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(INSTALLER_PATH)],
                capture_output=True,
                env={"HOME": str(home), "PATH": os.environ["PATH"]},
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to replace", result.stderr)
            self.assertEqual(command.read_text(encoding="utf-8"), "do not replace\n")

    def test_refuses_to_replace_unowned_az_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / "other-az"
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)
            command = home / ".local" / "bin" / "az"
            command.parent.mkdir(parents=True)
            command.symlink_to(target)
            result = subprocess.run(
                ["bash", str(INSTALLER_PATH)],
                capture_output=True,
                env={"HOME": str(home), "PATH": os.environ["PATH"]},
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to replace unowned", result.stderr)
            self.assertEqual(command.resolve(), target)

    def test_rejects_symlinked_local_directory_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            home = Path(temporary)
            sentinel = Path(outside) / "sentinel"
            sentinel.write_text("keep\n", encoding="utf-8")
            (home / ".local").symlink_to(outside, target_is_directory=True)
            result = subprocess.run(
                ["bash", str(INSTALLER_PATH)],
                capture_output=True,
                env={"HOME": str(home), "PATH": os.environ["PATH"]},
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe Azure CLI install directory", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_cli_update_failure_restores_previous_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            install_root, _ = self.existing_install(home, reported_version="wrong")
            sentinel = install_root / "sentinel"
            sentinel.write_text("old\n", encoding="utf-8")
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == -c ]]; then printf '3.11\\n'; exit 0; fi\n"
                "exit 44\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            result = subprocess.run(["bash", str(INSTALLER_PATH)], capture_output=True, env=env, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "old\n")

    def test_extension_download_failure_preserves_previous_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            _, extension = self.existing_install(home, support_version="old")
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text("#!/usr/bin/env bash\nexit 45\n", encoding="utf-8")
            fake_curl.chmod(0o755)
            env = {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            result = subprocess.run(["bash", str(INSTALLER_PATH)], capture_output=True, env=env, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((extension / "sentinel").read_text(encoding="utf-8"), "keep\n")

    def test_final_verification_failure_restores_promoted_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            install_root, extension = self.existing_install(home, support_version="old")
            executable = install_root / "bin" / "az"
            executable.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == version ]]; then printf '2.89.1\\n'; exit 0; fi\n"
                "if [[ \"$1 $2\" == 'extension add' ]]; then mkdir -p \"$AZURE_EXTENSION_DIR\"; touch \"$AZURE_EXTENSION_DIR/installed\"; exit 0; fi\n"
                "if [[ \"$1 $2\" == 'extension show' ]]; then\n"
                "  case \"$AZURE_EXTENSION_DIR\" in\n"
                "    *'.extensions-2.0.1.'*) printf '2.0.1\\n' ;;\n"
                "    *) if [[ -f \"$AZURE_EXTENSION_DIR/installed\" ]]; then printf 'wrong\\n'; else printf 'old\\n'; fi ;;\n"
                "  esac\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env bash\nout=''\nwhile [[ $# -gt 0 ]]; do if [[ \"$1\" == --output ]]; then out=\"$2\"; shift 2; else shift; fi; done\nprintf wheel >\"$out\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_sha = fake_bin / "sha256sum"
            fake_sha.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_sha.chmod(0o755)
            result = subprocess.run(
                ["bash", str(INSTALLER_PATH)],
                capture_output=True,
                env={"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((extension / "sentinel").read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((extension / "installed").exists())


if __name__ == "__main__":
    unittest.main()