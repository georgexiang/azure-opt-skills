"""Offline installer contract tests; no Azure credentials or public downloads."""

import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[3] / "azure-aks-ops/scripts/aks_toolchain.py"
SPEC = importlib.util.spec_from_file_location("aks_toolchain", SCRIPT)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
REAL_RUN = tool._run


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.lock = self.home / "requirements.lock"
        self.lock.write_text("azure-cli==2.89.1 --hash=sha256:fixture\n")
        self.manifest = self.home / "tools.json"
        self.artifacts = {"kubectl": b"kubectl fixture", "login": b"login fixture"}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("bin/linux_amd64/kubelogin", self.artifacts["login"])
        self.artifacts["archive"] = archive.getvalue()
        self.pins = {
            "kubectl": {
                v: {"url": "kubectl", "sha256": digest(self.artifacts["kubectl"])}
                for v in ("1.33.5", "1.34.1", "1.35.0")
            },
            "kubelogin": {
                "url": "archive",
                "sha256": digest(self.artifacts["archive"]),
                "binarySha256": digest(self.artifacts["login"]),
                "member": "bin/linux_amd64/kubelogin",
            },
        }
        self.manifest.write_text(json.dumps({"platforms": {"amd64": self.pins}}))
        for patcher in (
            patch.object(tool, "LOCK", self.lock),
            patch.object(tool, "MANIFEST", self.manifest),
            patch.object(tool, "_platform", return_value="amd64"),
            patch.dict(os.environ, {"HOME": str(self.home)}),
            patch.object(tool.shutil, "which", return_value="/usr/bin/python3.11"),
            patch.object(tool, "_run", side_effect=self.run_fixture),
            patch.object(tool, "_download", side_effect=self.download),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls: list[list[str]] = []

    def download(self, url: str, target: Path) -> None:
        target.write_bytes(self.artifacts[url])

    def run_fixture(self, args: list[str]) -> None:
        self.calls.append(args)
        if "venv" in args:
            venv = Path(args[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "lib").mkdir()
            (venv / "lib64").symlink_to("lib")
            (venv / "bin/python").write_bytes(b"python fixture")
            (venv / "bin/python").chmod(0o700)

    def test_install_and_check_explicit_root_with_temporary_home(self) -> None:
        root, result = tool.ensure()
        self.assertEqual(root, self.home / ".local/share/azure-aks-ops/toolchain-v1")
        self.assertEqual(set(result["kubectl"]), {"1.33.5", "1.34.1", "1.35.0"})
        pip = next(args for args in self.calls if "pip" in args)
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
            self.assertIn(flag, pip)
        self.assertEqual(Path(pip[-1]).name, self.lock.name)
        self.assertFalse((root / "venv/lib64").exists())
        with patch.dict(os.environ, {"HOME": "/nonexistent"}):
            self.assertEqual(tool.inspect(root), result)
        self.assertIn(str(root / "venv/bin/python"), self.calls[-1])
        self.assertIn("azure.cli.core", self.calls[-1][-1])
        self.assertFalse((self.home / ".azure").exists())
        self.assertFalse((self.home / ".kube").exists())
        with patch.object(
            tool, "_populate", side_effect=AssertionError("unexpected reinstall")
        ):
            self.assertEqual(tool.ensure(), (root, result))

    def test_reviewed_pin_upgrade_and_failed_upgrade_preserve_old_install(self) -> None:
        root, result = tool.ensure()
        original_marker = (root / tool.MARKER).read_bytes()
        original_binary = result["kubelogin"].read_bytes()
        pins = json.loads(self.manifest.read_text())
        pins["reviewedRevision"] = 2
        self.manifest.write_text(json.dumps(pins))
        with patch.object(
            tool, "_download", side_effect=OSError("failed fixture download")
        ):
            with self.assertRaises(OSError):
                tool.ensure()
        self.assertEqual((root / tool.MARKER).read_bytes(), original_marker)
        self.assertEqual(result["kubelogin"].read_bytes(), original_binary)
        new_root, _ = tool.ensure()
        self.assertEqual(new_root, root)
        self.assertNotEqual((root / tool.MARKER).read_bytes(), original_marker)
        tool.inspect(root)

    def test_binary_and_lock_tampering(self) -> None:
        root, result = tool.ensure()
        for target in (
            result["kubelogin"],
            *result["kubectl"].values(),
            root / self.lock.name,
        ):
            old = target.read_bytes()
            target.write_bytes(b"tampered")
            with self.assertRaises(tool.ToolchainError):
                tool.inspect(root)
            target.write_bytes(old)

    def test_archive_and_extracted_binary_hashes(self) -> None:
        for field in ("sha256", "binarySha256"):
            with self.subTest(field=field):
                pins = json.loads(self.manifest.read_text())
                original = pins["platforms"]["amd64"]["kubelogin"][field]
                pins["platforms"]["amd64"]["kubelogin"][field] = "0" * 64
                self.manifest.write_text(json.dumps(pins))
                with self.assertRaises(tool.ToolchainError):
                    tool.ensure()
                self.assertFalse((tool._parent() / "toolchain-v1").exists())
                pins["platforms"]["amd64"]["kubelogin"][field] = original
                self.manifest.write_text(json.dumps(pins))

    def test_symlink_and_unrelated_directory_rejection(self) -> None:
        parent = tool._parent()
        root = parent / "toolchain-v1"
        root.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(tool.ToolchainError):
            tool.ensure()
        root.unlink()
        root.mkdir()
        sentinel = root / "unrelated"
        sentinel.write_text("preserve")
        with self.assertRaises(Exception):
            tool.ensure()
        self.assertEqual(sentinel.read_text(), "preserve")
        sentinel.unlink()
        root.rmdir()
        root, result = tool.ensure()
        python = result["azure_python"]
        python.unlink()
        python.symlink_to("/usr/bin/python3.11")
        with self.assertRaises(tool.ToolchainError):
            tool.inspect(root)

    def test_parent_symlink_and_permissions(self) -> None:
        (self.home / ".local").symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(tool.ToolchainError):
            tool.ensure()
        (self.home / ".local").unlink()
        root, result = tool.ensure()
        result["kubelogin"].chmod(0o777)
        with self.assertRaises(tool.ToolchainError):
            tool.inspect(root)
        with self.assertRaises(tool.ToolchainError):
            tool.inspect(Path("relative"))

    def test_failed_repair_and_post_move_validation_roll_back(self) -> None:
        root, result = tool.ensure()
        original = result["kubelogin"].read_bytes()
        inspect = tool.inspect
        for fail_download in (True, False):
            checks = 0

            def check(path: Path) -> dict:
                nonlocal checks
                if path == root:
                    checks += 1
                    if checks == 1 or not fail_download:
                        raise tool.ToolchainError("fixture failure")
                return inspect(path)

            with patch.object(tool, "inspect", side_effect=check):
                if fail_download:
                    with patch.object(tool, "_download", side_effect=OSError("secret")):
                        with self.assertRaises(OSError):
                            tool.ensure()
                else:
                    with self.assertRaises(tool.ToolchainError):
                        tool.ensure()
            self.assertEqual(result["kubelogin"].read_bytes(), original)
            inspect(root)
            self.assertFalse(list(root.parent.glob(".toolchain-stage-*")))
            self.assertFalse(list(root.parent.glob(".toolchain-old-*")))

    def test_concurrent_install_populates_once(self) -> None:
        context = multiprocessing.get_context("fork")
        count = context.Value("i", 0)
        populate = tool._populate

        def slow_populate(path: Path) -> None:
            with count.get_lock():
                count.value += 1
            time.sleep(0.1)
            populate(path)

        with patch.object(tool, "_populate", side_effect=slow_populate):
            processes = [context.Process(target=tool.ensure) for _ in range(3)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join()
                self.assertEqual(process.exitcode, 0)
        self.assertEqual(count.value, 1)

    def test_runtime_failure_is_generic(self) -> None:
        root, _ = tool.ensure()
        with patch.object(tool, "_run", side_effect=RuntimeError("secret details")):
            with self.assertRaisesRegex(
                tool.ToolchainError, "^toolchain_validation_failed$"
            ):
                tool.inspect(root)

    def test_subprocess_environment_excludes_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AZURE_CLIENT_SECRET": "secret",
                "PYTHONPATH": "bad",
                "PIP_EXTRA_INDEX_URL": "secret",
                "KUBECONFIG": "secret",
            },
        ):
            with patch.object(tool.subprocess, "run") as run:
                REAL_RUN(["/fixture/python", "-I", "-c", "pass"])
        env = run.call_args.kwargs["env"]
        for key in (
            "AZURE_CLIENT_SECRET",
            "PYTHONPATH",
            "PIP_EXTRA_INDEX_URL",
            "KUBECONFIG",
        ):
            self.assertNotIn(key, env)
        self.assertEqual(env["HOME"], "/nonexistent")
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_cli_generic_error(self) -> None:
        result = subprocess.run(
            ["python3", str(SCRIPT), "check", "--root", "/missing-secret"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout), {"ok": False, "error": "toolchain_unavailable"}
        )
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
