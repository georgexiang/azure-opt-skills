#!/usr/bin/env python3
"""Offline integrity and packaging checks for the AKS knowledge bundle."""

from __future__ import annotations

import json
from pathlib import Path

from sync_aks_upstream import (
    GENERATED,
    LICENSE,
    LOCK,
    MANIFEST,
    NOTICE,
    ROOT,
    TRANSFORM_VERSION,
    actual_outputs,
    config,
    digest,
    require,
    validate_links,
)


def validate(root: Path) -> None:
    settings = config(root)
    outputs = actual_outputs(root)
    manifest = json.loads(outputs[MANIFEST])
    lock = json.loads(outputs[LOCK])
    for key, value in settings.items():
        require(lock.get(key) == value, f"Lock/config mismatch: {key}")
    for key in ("commit", "mode", "selected", "repository", "transform_version"):
        require(manifest.get(key) == lock.get(key), f"Manifest/lock mismatch: {key}")
    require(lock["transform_version"] == TRANSFORM_VERSION, "Transform version drift")
    require(manifest["license"]["path"] == LICENSE, "Unexpected license path")
    require(
        digest(outputs[LICENSE])
        == manifest["license"]["sha256"]
        == lock["license_sha256"],
        "License drift",
    )
    expected = {LICENSE, MANIFEST, LOCK}
    sources = {}
    for entry in manifest["files"]:
        path = entry["path"]
        require(
            path.startswith(GENERATED + "/") and path.endswith(".md"),
            f"Unexpected asset: {path}",
        )
        require(path not in expected, f"Duplicate asset: {path}")
        expected.add(path)
        require(path in outputs, f"Missing asset: {path}")
        require(digest(outputs[path]) == entry["sha256"], f"Asset drift: {path}")
        require(
            outputs[path].decode().startswith(NOTICE),
            f"Missing capability notice: {path}",
        )
        require(Path(path).name != "SKILL.md", "Nested skill discovery risk")
        require(
            not (root / path).stat().st_mode & 0o111,
            f"Executable upstream asset: {path}",
        )
        sources[entry["source"]] = entry["source_sha256"]
    require(sources == lock["source_files"], "Source inventory mismatch")
    require(set(outputs) == expected, "Unexpected generated files")
    for name in settings["selected"]:
        require(
            f"{GENERATED}/{name}/guide.md" in expected,
            f"Missing selected guide: {name}",
        )
    skill_path = root / "azure-aks-ops/SKILL.md"
    require(not skill_path.is_symlink(), "Skill entry is a symlink")
    skill = skill_path.read_bytes()
    require(
        skill.startswith(b"---\nname: azure-aks-ops\n"), "Unexpected Skill identity"
    )
    require(
        b"knowledge-only" in skill and b"bound-readonly" in skill,
        "Missing dual-mode capability contract",
    )
    bundle = root / "azure-aks-ops"
    local = json.loads((root / ".github/aks-upstream/runtime-assets.json").read_text())
    require(
        isinstance(local, list)
        and all(
            isinstance(p, str)
            and not Path(p).is_absolute()
            and ".." not in Path(p).parts
            for p in local
        ),
        "Unsafe local runtime asset declaration",
    )
    local_paths = {"azure-aks-ops/" + p for p in local}
    for path in local_paths:
        require(
            (root / path).is_file() and not (root / path).is_symlink(),
            f"Missing local runtime asset: {path}",
        )
    allowed = expected | {"azure-aks-ops/SKILL.md"} | local_paths
    for path in bundle.rglob("*"):
        require(not path.is_symlink(), f"Symlink in bundle: {path}")
        if path.is_file():
            require(
                path.relative_to(root).as_posix() in allowed,
                f"Unreviewed runtime asset: {path}",
            )
    validate_links(
        {
            **outputs,
            "azure-aks-ops/SKILL.md": skill,
            **{p: (root / p).read_bytes() for p in local_paths if p.endswith(".md")},
        }
    )


if __name__ == "__main__":
    validate(ROOT)
    print(
        "AKS bundle integrity, local links and explicit runtime asset inventory verified"
    )
