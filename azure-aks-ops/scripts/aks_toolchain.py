#!/usr/bin/env python3
"""Pinned, credential-free AKS toolchain installation and inspection."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, NoReturn
import urllib.request
import zipfile

BUNDLE = Path(__file__).resolve().parent
LOCK = BUNDLE / "azure-cli-2.89.1.lock"
MANIFEST = BUNDLE / "tools.lock.json"
MARKER = ".aks-toolchain.json"


class ToolchainError(Exception):
    """A deliberately non-sensitive public error."""


def _require(condition: bool) -> None:
    if not condition:
        raise ToolchainError("toolchain_validation_failed")


def _platform() -> str:
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    _require(platform.system() == "Linux" and arch is not None)
    return str(arch)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe(path: Path, *, owned: bool = False, directory: bool = False) -> None:
    _require(path.is_absolute() and ".." not in path.parts)
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        _require(not stat.S_ISLNK(info.st_mode))
        _require(not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX))
    info = path.lstat()
    if owned:
        _require(info.st_uid == os.getuid() and not info.st_mode & 0o022)
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def _tree(root: Path) -> None:
    _safe(root, owned=True, directory=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            _safe(path, owned=True, directory=name in dirs)


def _marker() -> dict[str, Any]:
    return {
        "schema": 1,
        "arch": _platform(),
        "lock": _digest(LOCK),
        "manifest": _digest(MANIFEST),
    }


def _run(args: list[str]) -> None:
    # Never inherit credentials, Python injection, pip configuration, or Azure state.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "AZURE_CONFIG_DIR": "/nonexistent",
        "PYTHONDONTWRITEBYTECODE": "1",
        "AZURE_CORE_COLLECT_TELEMETRY": "0",
        "LANG": "C.UTF-8",
    }
    subprocess.run(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=1200,
    )


def inspect(root: Path) -> dict[str, Any]:
    """Validate an explicit persistent root; return absolute executable paths.

    Invoke Azure commands as [azure_python, '-m', 'azure.cli', ...].
    A relocated venv's generated az/pip entry-point shebangs are not usable.
    """
    try:
        _tree(root)
        _require(json.loads((root / MARKER).read_text()) == _marker())
        _require(_digest(root / LOCK.name) == _digest(LOCK))
        pins = json.loads(MANIFEST.read_text())["platforms"][_platform()]
        kubectl = {
            version: root / "bin" / f"kubectl-{version}" for version in pins["kubectl"]
        }
        login = root / "bin" / "kubelogin"
        for version, path in kubectl.items():
            _require(_digest(path) == pins["kubectl"][version]["sha256"])
            _require(os.access(path, os.X_OK))
        _require(_digest(login) == pins["kubelogin"]["binarySha256"])
        _require(os.access(login, os.X_OK))
        python = root / "venv" / "bin" / "python"
        _safe(python, owned=True)
        _run(
            [
                str(python),
                "-I",
                "-B",
                "-c",
                "import sys, azure.cli.core; from importlib.metadata import version; "
                "assert sys.version_info[:2] == (3, 11); "
                "assert azure.cli.core.__version__ == '2.89.1'; "
                "assert version('azure-cli') == '2.89.1'",
            ]
        )
        return {"azure_python": python, "kubelogin": login, "kubectl": kubectl}
    except Exception:
        raise ToolchainError("toolchain_validation_failed") from None


def _download(url: str, target: Path) -> None:
    deadline = time.monotonic() + 180
    count = 0
    with urllib.request.urlopen(url, timeout=30) as response, target.open("xb") as out:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            _require(count <= 128 * 1024 * 1024 and time.monotonic() < deadline)
            out.write(chunk)


def _populate(root: Path) -> None:
    pins = json.loads(MANIFEST.read_text())["platforms"][_platform()]
    (root / "bin").mkdir(mode=0o700)
    shutil.copyfile(LOCK, root / LOCK.name)
    python = shutil.which("python3.11", path="/usr/local/bin:/usr/bin:/bin")
    _require(python is not None)
    _run([str(python), "-I", "-m", "venv", "--copies", str(root / "venv")])
    _run(
        [
            str(root / "venv/bin/python"),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-cache-dir",
            "--require-hashes",
            "--no-deps",
            "--only-binary=:all:",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            str(root / LOCK.name),
        ]
    )
    # CPython venv creates this redundant convenience symlink even with --copies.
    lib64 = root / "venv/lib64"
    if lib64.is_symlink() and os.readlink(lib64) == "lib":
        lib64.unlink()
    for version, pin in pins["kubectl"].items():
        target = root / "bin" / f"kubectl-{version}"
        _download(pin["url"], target)
        _require(_digest(target) == pin["sha256"])
        target.chmod(0o700)
    pin = pins["kubelogin"]
    archive = root / "kubelogin.zip"
    _download(pin["url"], archive)
    _require(_digest(archive) == pin["sha256"])
    with zipfile.ZipFile(archive) as zipped:
        info = zipped.getinfo(pin["member"])
        _require(not stat.S_ISLNK(info.external_attr >> 16))
        with zipped.open(info) as source, (root / "bin/kubelogin").open("xb") as out:
            shutil.copyfileobj(source, out)
    _require(_digest(root / "bin/kubelogin") == pin["binarySha256"])
    (root / "bin/kubelogin").chmod(0o700)
    archive.unlink()
    (root / MARKER).write_text(json.dumps(_marker()))


def _parent() -> Path:
    home = Path(os.environ.get("HOME", ""))
    _require(
        not any(
            part.startswith(("qm-credentials.", "qm-aks-login-")) for part in home.parts
        )
    )
    _safe(home, owned=True, directory=True)
    parent = home
    for name in (".local", "share", "azure-aks-ops"):
        parent = parent / name
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _safe(parent, owned=True, directory=True)
    return parent


def ensure() -> tuple[Path, dict[str, Any]]:
    """Install only beneath the current persistent HOME, under an exclusive lock."""
    parent = _parent()
    root = parent / "toolchain-v1"
    lock = parent / ".toolchain-v1.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        _safe(lock, owned=True)
        _require(os.fstat(stream.fileno()).st_nlink == 1)
        fcntl.flock(stream, fcntl.LOCK_EX)
        if root.exists() or root.is_symlink():
            _tree(root)
            # Refuse to overwrite a directory that is not this pinned toolchain.
            prior = json.loads((root / MARKER).read_text())
            _require(
                isinstance(prior, dict)
                and set(prior) == {"schema", "arch", "lock", "manifest"}
                and prior["schema"] == 1
                and prior["arch"] == _platform()
                and all(
                    isinstance(prior[k], str)
                    and re.fullmatch(r"[0-9a-f]{64}", prior[k])
                    for k in ("lock", "manifest")
                )
            )
            try:
                return root, inspect(root)
            except ToolchainError:
                pass
        stage = Path(tempfile.mkdtemp(prefix=".toolchain-stage-", dir=parent))
        backup = stage / "previous"
        try:
            _populate(stage)
            inspect(stage)
            # Reserve a unique backup name without touching unrelated directories.
            backup = Path(tempfile.mkdtemp(prefix=".toolchain-old-", dir=parent))
            backup.rmdir()
            if root.exists():
                root.rename(backup)
            try:
                stage.rename(root)
                result = inspect(root)
            except Exception:
                if root.exists():
                    root.rename(stage)
                if backup.exists():
                    backup.rename(root)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return root, result
        finally:
            if stage.exists():
                shutil.rmtree(stage)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ToolchainError("invalid_arguments")


def main() -> int:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ensure")
    check = commands.add_parser("check")
    check.add_argument("--root", type=Path, required=True)
    try:
        args = parser.parse_args()
        if args.command == "ensure":
            root, result = ensure()
        else:
            root, result = args.root, inspect(args.root)
        print(json.dumps({"ok": True, "root": str(root), **result}, default=str))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error": "toolchain_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
