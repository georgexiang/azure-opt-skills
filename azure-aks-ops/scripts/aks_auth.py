#!/usr/bin/env python3
"""Tenant-specific device login/save and owner-approved AKS access requests."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aks_common import (  # noqa: E402
    Keychain,
    PURPOSE_KIND,
    PROFILE_FILES,
    real_private,
    require,
    result_error,
    run,
    uuid_value,
    verified_azure_claims,
)
from aks_runtime import az, environment, target_from  # noqa: E402
from aks_toolchain import inspect  # noqa: E402

LOGIN_FILES = PROFILE_FILES


def save_files(home: Path) -> list[dict]:
    files: list[dict[str, Any]] = []
    total = 0
    config = home / ".azure"
    real_private(config)
    for name in sorted(LOGIN_FILES):
        path = config / name
        if not path.exists():
            continue
        require(
            not path.is_symlink() and path.is_file(),
            "LOGIN_SAVE_DENIED",
            "Login cache contains an unsafe file",
        )
        require(
            path.stat().st_size <= 8 * 1024 * 1024,
            "LOGIN_SAVE_DENIED",
            "Login cache exceeds native keychain limits",
        )
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        total += len(raw)
        require(
            total <= 8 * 1024 * 1024,
            "LOGIN_SAVE_DENIED",
            "Login cache exceeds native keychain limits",
        )
        files.append(
            {
                "path": ".azure/" + name,
                "contentBase64": base64.b64encode(raw).decode(),
                "mode": 0o600,
            }
        )
    require(
        any(
            f["path"].endswith(("msal_token_cache.json", "msal_token_cache.bin"))
            for f in files
        ),
        "LOGIN_INCOMPLETE",
        "No usable MSAL login cache was created",
    )
    return files


def login(
    args: argparse.Namespace, api: Keychain | None = None, tools: dict | None = None
) -> dict:
    api = api or Keychain()
    tools = tools or inspect(Path(args.tools_root))
    tenant = uuid_value(args.tenant)
    old_mask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="qm-aks-login-") as temp:
            directory = Path(temp)
            home = directory / "home"
            home.mkdir(mode=0o700)
            (home / ".azure").mkdir(mode=0o700)
            work = directory / "work"
            work.mkdir(mode=0o700)
            env = environment(home, work, tools)
            run(
                [
                    str(tools["azure_python"]),
                    "-I",
                    "-m",
                    "azure.cli",
                    "login",
                    "--tenant",
                    tenant,
                    "--use-device-code",
                    "--allow-no-subscriptions",
                    "--output",
                    "none",
                    "--only-show-errors",
                ],
                env,
                timeout=600,
                challenge=True,
            )
            accounts = az(
                tools,
                env,
                [
                    "account",
                    "list",
                    "--all",
                    "--query",
                    "[].{tenantId:tenantId,subscriptionId:id,name:name,state:state,cloud:environmentName,user:user}",
                ],
            )
            visible = [
                a
                for a in accounts
                if a.get("tenantId", "").lower() == tenant
                and a.get("cloud") == "AzureCloud"
            ]
            require(
                bool(visible),
                "LOGIN_INCOMPLETE",
                "The requested AzureCloud tenant was not authenticated",
            )
            # Get identity claims locally from the trusted CLI result. Token bytes
            # never reach model output or the save response. The immutable oid in
            # the service slot prevents replacing another Azure account while
            # accidentally retaining that account's existing sharing grants.
            token = az(
                tools,
                env,
                [
                    "account",
                    "get-access-token",
                    "--tenant",
                    tenant,
                    "--resource",
                    "https://management.azure.com/",
                ],
            )
            encoded = token.get("accessToken", "").split(".")
            require(
                len(encoded) == 3,
                "LOGIN_INCOMPLETE",
                "Azure did not return an identifiable access token",
            )
            claims = verified_azure_claims(token["accessToken"], tenant)
            object_id = uuid_value(claims.get("oid", ""))
            require(
                uuid_value(claims.get("tid", "")) == tenant,
                "IDENTITY_MISMATCH",
                "Authenticated token tenant differs from the requested tenant",
            )
            service = f"azure-aks-{tenant}-{object_id}"
            label = f"Azure AKS tenant {tenant}; object {object_id}"
            response = api.call(
                "/v1/keychain/credentials",
                {
                    "service": service,
                    "files": save_files(home),
                    "accountLabel": label,
                    "origin": "azure-aks-ops/device-code/v1",
                },
            )
            credential = response.get("credential", {})
            handle = credential.get("credentialHandle", "")
            require(
                handle.startswith(service + ":"),
                "LOGIN_SAVE_FAILED",
                "Keychain did not return the saved Azure execution handle",
            )
            return {
                "ok": True,
                "credentialHandle": handle,
                "credentialId": credential.get("id"),
                "azureIdentity": {"tenantId": tenant, "objectId": object_id},
                "accounts": [
                    {
                        k: a.get(k)
                        for k in (
                            "tenantId",
                            "subscriptionId",
                            "name",
                            "state",
                            "cloud",
                        )
                    }
                    for a in visible
                ],
                "notice": "Saved in the native keychain; temporary login files removed. This is the Azure login identity, not proof it equals the AgentOps web identity.",
            }
    finally:
        os.umask(old_mask)


def request_access(args: argparse.Namespace, api: Keychain | None = None) -> dict:
    api = api or Keychain()
    target = target_from(args)
    purpose = {"kind": PURPOSE_KIND, **target}
    text = json.dumps(purpose, sort_keys=True, separators=(",", ":"))
    response = api.call(
        "/v1/keychain/asks",
        {
            "credential": args.credential_id,
            "purpose": text,
            "requestedMode": "standing",
        },
    )
    ask = response.get("ask", {})
    require(
        ask.get("purpose") == text and ask.get("requestedMode") == "standing",
        "ASK_PURPOSE_CONFLICT",
        "A frozen existing ask has a different purpose/mode; its owner must resolve it before requesting AKS access",
    )
    return {
        "ok": True,
        "askId": ask.get("id"),
        "status": ask.get("status"),
        "existing": bool(response.get("existing")),
        "purpose": purpose,
        "notice": "Wait for the native owner approval card. A pending/approved ask is not itself an execution grant; recheck the active grant and current handle catalog before using credentials.",
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    start = sub.add_parser("login")
    start.add_argument("--tools-root", required=True)
    start.add_argument("--tenant", required=True)
    ask = sub.add_parser("request-access")
    ask.add_argument("--credential-id", required=True)
    ask.add_argument("--tenant", required=True)
    ask.add_argument("--object-id")
    ask.add_argument("--subscription", required=True)
    ask.add_argument("--resource-group", required=True)
    ask.add_argument("--cluster", required=True)
    ask.add_argument("--namespace", action="append", default=[])
    ask.add_argument("--cluster-read", action="store_true")
    return p


if __name__ == "__main__":
    try:
        args = parser().parse_args()
        print(
            json.dumps(
                login(args) if args.action == "login" else request_access(args),
                ensure_ascii=False,
            )
        )
    except Exception as error:
        result_error(error)
        sys.exit(1)
