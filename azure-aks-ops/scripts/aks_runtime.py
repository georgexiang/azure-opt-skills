#!/usr/bin/env python3
"""Fixed, target-bound Azure/AKS reads using a natively staged file credential."""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import ssl
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aks_common import (  # noqa: E402
    AKSError,
    Keychain,
    authorize,
    credential_home,
    clean_profile,
    require,
    result_error,
    run,
    uuid_value,
    verified_azure_claims,
)
from aks_toolchain import inspect  # noqa: E402

CONTEXT = "qm-aks-bound"
AKS_SERVER_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"
CLUSTER_QUERY = "{id:id,name:name,resourceGroup:resourceGroup,location:location,kubernetesVersion:kubernetesVersion,provisioningState:provisioningState,powerState:powerState.code,fqdn:fqdn,privateFqdn:privateFqdn,aadProfile:aadProfile}"


def environment(home: Path, work: Path, tools: dict) -> dict[str, str]:
    binary = work / "bin"
    binary.mkdir(mode=0o700)
    launcher = binary / "az"
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(str(tools["azure_python"]))
        + ' -I -m azure.cli "$@"\n'
    )
    launcher.chmod(0o700)
    extensions = work / "extensions"
    extensions.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "PATH": f"{binary}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "AZURE_CONFIG_DIR": str(home / ".azure"),
        "AZURE_EXTENSION_DIR": str(extensions),
        "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
        "AZURE_CORE_COLLECT_TELEMETRY": "0",
        "AZURE_CORE_NO_COLOR": "true",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(work / "cache"),
    }


def az(tools: dict, env: dict[str, str], args: list[str], as_json: bool = True) -> Any:
    raw = run(
        [
            str(tools["azure_python"]),
            "-I",
            "-m",
            "azure.cli",
            *args,
            "--only-show-errors",
            "--output",
            "json" if as_json else "none",
        ],
        env,
    )
    return json.loads(raw) if as_json else None


def verify_azure_identity(
    tools: dict,
    env: dict[str, str],
    handle: str,
    tenant: str,
    object_id: str | None = None,
) -> dict:
    from urllib.parse import unquote

    service = unquote(handle.rsplit(":", 1)[0])
    match = re.fullmatch(r"azure-(?:aks-)?([0-9a-f-]{36})-([0-9a-f-]{36})", service)
    if match:
        require(
            uuid_value(match[1]) == tenant
            and (not object_id or uuid_value(object_id) == uuid_value(match[2])),
            "IDENTITY_MISMATCH",
            "Selected credential was captured for another Azure identity",
        )
        object_id = uuid_value(match[2])
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
    parts = token.get("accessToken", "").split(".")
    require(
        len(parts) == 3,
        "IDENTITY_MISMATCH",
        "Azure did not return an identifiable access token",
    )
    claims = verified_azure_claims(token["accessToken"], tenant)
    require(
        uuid_value(claims.get("tid", "")) == tenant
        and (not object_id or uuid_value(claims.get("oid", "")) == object_id),
        "IDENTITY_MISMATCH",
        "Staged Azure token identity differs from the selected credential; no cluster read was attempted",
    )
    return {"tenantId": tenant, "objectId": uuid_value(claims.get("oid", ""))}


def target_from(args: argparse.Namespace) -> dict:
    require(
        bool(args.subscription and args.resource_group and args.cluster),
        "TARGET_REQUIRED",
        "Confirm subscription, resource group and cluster; no default target is inferred",
    )
    subscription = uuid_value(args.subscription)
    require(
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}", args.resource_group)),
        "TARGET_INVALID",
        "Invalid resource group name",
    )
    require(
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", args.cluster)),
        "TARGET_INVALID",
        "Invalid cluster name",
    )
    namespaces = sorted(set(args.namespace))
    require(
        len(namespaces) <= 16
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", ns)
            for ns in namespaces
        ),
        "TARGET_INVALID",
        "Use at most 16 explicit namespaces, not wildcard/all-namespaces",
    )
    require(
        args.action not in {"pods", "events"} or bool(namespaces),
        "NAMESPACE_REQUIRED",
        "Pod/event reads require explicit namespace selection",
    )
    require(
        args.action != "nodes" or args.cluster_read,
        "TARGET_DENIED",
        "Node reads require explicit cluster-read selection",
    )
    target = {
        "tenantId": uuid_value(args.tenant),
        "subscriptionId": subscription,
        "resourceGroup": args.resource_group,
        "clusterName": args.cluster,
        "namespaces": namespaces,
        "clusterRead": args.cluster_read,
    }
    if args.object_id:
        target["objectId"] = uuid_value(args.object_id)
    return target


def select_kubectl(tools: dict, server_version: str) -> Path:
    match = re.match(r"^v?1\.(\d+)(?:\.|$)", server_version)
    require(
        match is not None,
        "VERSION_UNSUPPORTED",
        "Cluster Kubernetes version is unavailable",
    )
    assert match is not None
    minor = int(match[1])
    choices = sorted(
        (
            (abs(int(version.split(".")[1]) - minor), version, path)
            for version, path in tools["kubectl"].items()
        )
    )
    require(
        bool(choices) and choices[0][0] <= 1,
        "VERSION_UNSUPPORTED",
        "No pinned kubectl is within one minor version; update the reviewed tool lock",
    )
    return Path(choices[0][2])


def safe_kubeconfig(raw: bytes, metadata: dict, target: dict, tools: dict) -> dict:
    import yaml  # type: ignore[import-untyped]

    require(
        len(raw) <= 1024 * 1024,
        "KUBECONFIG_DENIED",
        "Cluster connection configuration is oversized",
    )
    original = yaml.safe_load(raw)
    require(
        isinstance(original, dict),
        "KUBECONFIG_DENIED",
        "Invalid cluster connection configuration",
    )
    clusters = original.get("clusters", [])
    contexts = original.get("contexts", [])
    users = original.get("users", [])
    require(
        len(clusters) == len(contexts) == len(users) == 1,
        "KUBECONFIG_DENIED",
        "An isolated single-cluster configuration is required",
    )
    cluster = clusters[0]["cluster"]
    endpoint = urlsplit(cluster.get("server", ""))
    expected_hosts = {
        str(metadata.get("fqdn") or "").lower(),
        str(metadata.get("privateFqdn") or "").lower(),
    } - {""}
    require(
        endpoint.scheme == "https"
        and endpoint.hostname in expected_hosts
        and endpoint.port in {None, 443}
        and endpoint.path in {"", "/"}
        and not endpoint.username
        and not endpoint.password
        and not endpoint.query
        and not endpoint.fragment,
        "ENDPOINT_DENIED",
        "Kubeconfig endpoint does not match the selected AKS control plane",
    )
    require(
        not cluster.get("insecure-skip-tls-verify")
        and "certificate-authority" not in cluster
        and "proxy-url" not in cluster,
        "KUBECONFIG_DENIED",
        "External CA paths, proxy overrides and insecure TLS are not accepted",
    )
    ca = cluster.get("certificate-authority-data", "")
    certificate = base64.b64decode(ca, validate=True)
    require(
        0 < len(certificate) <= 256 * 1024,
        "KUBECONFIG_DENIED",
        "Missing or oversized cluster CA",
    )
    ssl.create_default_context(cadata=certificate.decode("ascii"))
    aad = metadata.get("aadProfile") or {}
    require(
        aad.get("managed") is True
        and str(aad.get("tenantID") or aad.get("tenantId") or "").lower()
        == target["tenantId"],
        "AUTH_MODE_UNSUPPORTED",
        "This runtime supports managed Entra AKS in the selected tenant, not admin/static-certificate logins",
    )
    # Never execute, convert, or copy an exec/auth-provider stanza from downloaded
    # kubeconfig. Reconstruct the one permitted Azure CLI token provider ourselves.
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": CONTEXT,
        "clusters": [
            {
                "name": CONTEXT,
                "cluster": {
                    "server": cluster["server"],
                    "certificate-authority-data": ca,
                },
            }
        ],
        "contexts": [
            {"name": CONTEXT, "context": {"cluster": CONTEXT, "user": CONTEXT}}
        ],
        "users": [
            {
                "name": CONTEXT,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": str(tools["kubelogin"]),
                        "args": [
                            "get-token",
                            "--login",
                            "azurecli",
                            "--server-id",
                            AKS_SERVER_ID,
                            "--tenant-id",
                            target["tenantId"],
                            "--environment",
                            "AzurePublicCloud",
                        ],
                        "interactiveMode": "Never",
                        "provideClusterInfo": False,
                    }
                },
            }
        ],
    }


def projected(kind: str, data: dict) -> dict:
    items = data.get("items", [])
    require(
        isinstance(items, list),
        "RESPONSE_INVALID",
        "Expected a Kubernetes list response",
    )
    rows = []
    for item in items[:200]:
        metadata, status = item.get("metadata", {}), item.get("status", {})
        row = {"name": metadata.get("name"), "namespace": metadata.get("namespace")}
        if kind == "nodes":
            row.update(
                {
                    "ready": next(
                        (
                            c.get("status")
                            for c in status.get("conditions", [])
                            if c.get("type") == "Ready"
                        ),
                        "Unknown",
                    ),
                    "kubeletVersion": status.get("nodeInfo", {}).get("kubeletVersion"),
                }
            )
        elif kind == "pods":
            row.update(
                {
                    "phase": status.get("phase"),
                    "reason": status.get("reason"),
                    "containers": [
                        {
                            "name": c.get("name"),
                            "ready": c.get("ready"),
                            "restartCount": c.get("restartCount"),
                            "waitingReason": c.get("state", {})
                            .get("waiting", {})
                            .get("reason"),
                            "terminationReason": c.get("state", {})
                            .get("terminated", {})
                            .get("reason"),
                            "exitCode": c.get("state", {})
                            .get("terminated", {})
                            .get("exitCode"),
                        }
                        for c in status.get("containerStatuses", [])
                    ],
                }
            )
        else:
            involved = item.get("involvedObject", {})
            row.update(
                {
                    "type": item.get("type"),
                    "reason": item.get("reason"),
                    "count": item.get("count"),
                    "lastTimestamp": item.get("lastTimestamp"),
                    "object": {
                        "kind": involved.get("kind"),
                        "name": involved.get("name"),
                    },
                }
            )
        rows.append(row)
    # No annotations, pod specs/env, event messages, raw logs or Secrets enter model context.
    return {
        "items": rows,
        "returned": len(rows),
        "truncated": len(items) > 200 or bool(data.get("metadata", {}).get("continue")),
    }


def execute(
    args: argparse.Namespace, api: Keychain | None = None, tools: dict | None = None
) -> dict:
    api = api or Keychain()
    tools = tools or inspect(Path(args.tools_root))
    home = credential_home()
    tenant = uuid_value(args.tenant)
    target = None if args.action == "accounts" else target_from(args)
    authorization = authorize(api, args.handle, args.scope, args.grant, target)
    approved_identity = authorization.get("approved", {}).get("objectId")
    if authorization["mode"] == "project-shared":
        from urllib.parse import unquote

        service = unquote(args.handle.rsplit(":", 1)[0])
        require(
            re.fullmatch(r"azure-(?:aks-)?[0-9a-f-]{36}-[0-9a-f-]{36}", service)
            or approved_identity,
            "IDENTITY_APPROVAL_REQUIRED",
            "A shared interactive-login handle requires the owner's approved Azure object ID in its AKS purpose",
        )
    requested_identity = uuid_value(args.object_id) if args.object_id else None
    object_id = (
        uuid_value(approved_identity) if approved_identity else requested_identity
    )
    require(
        not requested_identity or requested_identity == object_id,
        "IDENTITY_MISMATCH",
        "Requested Azure object ID differs from the approved purpose",
    )
    with (
        clean_profile(home),
        tempfile.TemporaryDirectory(prefix="aks-", dir=home.parent) as temp,
    ):
        work = Path(temp)
        env = environment(home, work, tools)
        if args.action == "accounts":
            approved = authorization.get("approved")
            require(
                not approved or str(approved.get("tenantId", "")).lower() == tenant,
                "TARGET_DENIED",
                "Account discovery tenant differs from the grant purpose",
            )
            azure_identity = verify_azure_identity(
                tools, env, args.handle, tenant, object_id
            )
            accounts = az(
                tools,
                env,
                [
                    "account",
                    "list",
                    "--all",
                    "--query",
                    "[].{tenantId:tenantId,subscriptionId:id,name:name,state:state,cloud:environmentName}",
                ],
            )
            approved = authorization.get("approved")
            visible = [
                a
                for a in accounts
                if a.get("tenantId", "").lower() == tenant
                and a.get("cloud") == "AzureCloud"
                and (
                    not approved
                    or a.get("subscriptionId", "").lower()
                    == str(approved.get("subscriptionId", "")).lower()
                )
            ]
            return {
                "ok": True,
                "mode": "bound-readonly",
                "azureIdentity": azure_identity,
                "authorization": {
                    k: v for k, v in authorization.items() if k != "approved"
                },
                "accounts": visible,
            }
        assert target is not None

        def check() -> None:
            authorize(api, args.handle, args.scope, args.grant, target)

        account = az(
            tools,
            env,
            [
                "account",
                "show",
                "--subscription",
                target["subscriptionId"],
                "--query",
                "{tenantId:tenantId,subscriptionId:id,state:state,cloud:environmentName,user:user}",
            ],
        )
        require(
            account.get("tenantId", "").lower() == tenant
            and account.get("subscriptionId", "").lower() == target["subscriptionId"]
            and account.get("state") == "Enabled"
            and account.get("cloud") == "AzureCloud",
            "IDENTITY_MISMATCH",
            "Live Azure account does not match the approved tenant/subscription/cloud",
        )
        check()
        az(
            tools,
            env,
            ["account", "set", "--subscription", target["subscriptionId"]],
            as_json=False,
        )
        azure_identity = verify_azure_identity(
            tools, env, args.handle, tenant, object_id
        )
        check()
        base = [
            "--subscription",
            target["subscriptionId"],
            "--resource-group",
            target["resourceGroup"],
        ]
        metadata = az(
            tools,
            env,
            [
                "aks",
                "show",
                *base,
                "--name",
                target["clusterName"],
                "--query",
                CLUSTER_QUERY,
            ],
        )
        expected_id = f"/subscriptions/{target['subscriptionId']}/resourceGroups/{target['resourceGroup']}/providers/Microsoft.ContainerService/managedClusters/{target['clusterName']}"
        require(
            metadata.get("id", "").lower() == expected_id.lower(),
            "TARGET_MISMATCH",
            "Azure returned a different cluster resource",
        )
        report: dict[str, Any] = {
            "ok": True,
            "mode": "bound-readonly",
            "target": target,
            "authorization": {
                k: v for k, v in authorization.items() if k != "approved"
            },
            "azureIdentity": {
                **azure_identity,
                "name": account.get("user", {}).get("name"),
                "type": account.get("user", {}).get("type"),
            },
            "controlPlane": {
                k: metadata.get(k)
                for k in (
                    "id",
                    "name",
                    "resourceGroup",
                    "location",
                    "kubernetesVersion",
                    "provisioningState",
                    "powerState",
                )
            },
            "assessment": "Evidence only; missing/denied reads are not a healthy result",
        }
        if args.action == "cluster":
            return report
        if args.action == "nodepools":
            check()
            report["nodepools"] = az(
                tools,
                env,
                [
                    "aks",
                    "nodepool",
                    "list",
                    *base,
                    "--cluster-name",
                    target["clusterName"],
                    "--query",
                    "[].{name:name,count:count,vmSize:vmSize,mode:mode,provisioningState:provisioningState,powerState:powerState.code,orchestratorVersion:orchestratorVersion}",
                ],
            )
            return report
        if (
            args.action == "status"
            and not target["clusterRead"]
            and not target["namespaces"]
        ):
            report["kubernetes"] = {
                "unavailable": "No node or namespace read was selected; only ARM state was inspected"
            }
            return report
        config = work / "kubeconfig"
        check()
        az(
            tools,
            env,
            [
                "aks",
                "get-credentials",
                *base,
                "--name",
                target["clusterName"],
                "--file",
                str(config),
                "--context",
                CONTEXT,
                "--format",
                "exec",
            ],
            as_json=False,
        )
        require(
            config.is_file() and not config.is_symlink(),
            "KUBECONFIG_DENIED",
            "Connection configuration was not created safely",
        )
        require(
            config.stat().st_size <= 1024 * 1024,
            "KUBECONFIG_DENIED",
            "Cluster connection configuration is oversized",
        )
        config.chmod(0o600)
        safe = safe_kubeconfig(config.read_bytes(), metadata, target, tools)
        config.write_text(json.dumps(safe))
        config.chmod(0o600)
        kubectl = select_kubectl(tools, metadata.get("kubernetesVersion", ""))
        command = [
            str(kubectl),
            "--kubeconfig",
            str(config),
            "--context",
            CONTEXT,
            "--request-timeout=30s",
            "--cache-dir",
            str(work / "discovery"),
        ]
        check()
        version = json.loads(run([*command, "version", "--output=json"], env))
        select_kubectl(
            {
                "kubectl": {
                    next(
                        v for v, p in tools["kubectl"].items() if Path(p) == kubectl
                    ): kubectl
                }
            },
            version.get("serverVersion", {}).get("gitVersion", ""),
        )
        report["connection"] = {
            "verified": True,
            "serverVersion": version.get("serverVersion", {}).get("gitVersion"),
            "credentialPersistence": "Azure login only in native keychain; kubeconfig removed with this execution",
        }
        if args.action == "connect":
            return report
        datasets = (
            [("nodes", None)]
            if args.action == "nodes"
            else [(args.action, ns) for ns in target["namespaces"]]
            if args.action in {"pods", "events"}
            else ([("nodes", None)] if target["clusterRead"] else [])
            + [(kind, ns) for ns in target["namespaces"] for kind in ("pods", "events")]
        )
        report["evidence"] = {}
        for kind, namespace in datasets:
            check()
            label = kind + (":" + namespace if namespace else "")
            read = [*command, "get", kind, "--output=json", "--chunk-size=100"]
            if namespace:
                read += ["--namespace", namespace]
            try:
                report["evidence"][label] = projected(kind, json.loads(run(read, env)))
            except AKSError as error:
                if args.action != "status":
                    raise
                report["evidence"][label] = {"unavailable": error.code}
        return report


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tools-root", required=True)
    p.add_argument("--handle", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--grant")
    p.add_argument("--tenant", required=True)
    p.add_argument("--object-id")
    p.add_argument("--subscription")
    p.add_argument("--resource-group")
    p.add_argument("--cluster")
    p.add_argument("--namespace", action="append", default=[])
    p.add_argument("--cluster-read", action="store_true")
    p.add_argument(
        "action",
        choices=[
            "accounts",
            "cluster",
            "nodepools",
            "connect",
            "nodes",
            "pods",
            "events",
            "status",
        ],
    )
    return p


if __name__ == "__main__":
    try:
        print(json.dumps(execute(parser().parse_args()), ensure_ascii=False))
    except Exception as error:
        result_error(error)
        sys.exit(1)
