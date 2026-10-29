"""Private execution helpers shared by the fixed AKS login and read interfaces."""

from __future__ import annotations

import json
import base64
import zlib
import shutil
from contextlib import contextmanager
from collections.abc import Iterator
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_BYTES = 2 * 1024 * 1024
PURPOSE_KIND = "azure-aks-readonly/v1"
PROFILE_FILES = {"azureProfile.json", "msal_token_cache.json"}


class AKSError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition: Any, code: str, message: str) -> None:
    if not condition:
        raise AKSError(code, message)


def uuid_value(value: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise AKSError(
            "TARGET_INVALID", "Tenant and subscription require UUIDs"
        ) from error
    require(parsed == value.lower(), "TARGET_INVALID", "Use canonical UUIDs")
    return parsed


def real_private(path: Path, directory: bool = True) -> None:
    require(path.is_absolute(), "PATH_DENIED", "An absolute private path is required")
    for component in (path, *path.parents):
        require(
            not component.is_symlink(),
            "PATH_DENIED",
            "Symlink credential paths are forbidden",
        )
    info = path.stat()
    require(
        info.st_uid == os.getuid(), "PATH_DENIED", "Credential path has another owner"
    )
    require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "PATH_DENIED",
        "Unexpected credential path type",
    )
    require(
        not info.st_mode & 0o077,
        "PATH_DENIED",
        "Credential path must be private (0700/0600)",
    )


def credential_home() -> Path:
    home = Path(os.environ.get("HOME", ""))
    require(
        home.name == "home"
        and bool(re.fullmatch(r"qm-credentials\.[A-Za-z0-9]+", home.parent.name))
        and home.parent.parent == Path("/tmp"),
        "CREDENTIAL_NOT_STAGED",
        "Use the platform execute.credentials file handle; resident HOME/cache is not accepted",
    )
    real_private(home.parent)
    real_private(home)
    config = home / ".azure"
    real_private(config)
    require(
        all(
            p.name in PROFILE_FILES and p.is_file() and not p.is_symlink()
            for p in config.iterdir()
        ),
        "PROFILE_DENIED",
        "Re-login with aks_auth.py; staged profile contains unsupported cloud/config/plugin or binary cache files",
    )
    require(
        os.environ.get("AZURE_CONFIG_DIR", str(config)) == str(config),
        "PROFILE_DENIED",
        "Azure profile must remain inside this credential execution",
    )
    return home


@contextmanager
def clean_profile(home: Path) -> Iterator[None]:
    try:
        yield
    finally:
        # Never persist cloud overrides, executable extensions or MSAL pickle
        # caches introduced by the CLI. Keep only the two captured JSON roots.
        config = home / ".azure"
        real_private(config)
        for path in config.iterdir():
            if path.name not in PROFILE_FILES:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


class Keychain:
    """Only metadata, approval requests and encrypted-login saves; never /use."""

    def __init__(self) -> None:
        self.url = os.environ.get("AGENT_API_URL", "").rstrip("/")
        self.token = os.environ.get("AGENT_API_TOKEN", "")
        parsed = urlsplit(self.url)
        require(
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment,
            "KEYCHAIN_UNAVAILABLE",
            "The platform must supply its agent API endpoint",
        )
        require(
            bool(self.token),
            "KEYCHAIN_UNAVAILABLE",
            "The platform must supply its agent capability",
        )
        self.opener = urllib.request.build_opener(NoRedirect())

    def identity(self) -> dict:
        # The same token must first be accepted by the authoritative Core API.
        # Only then read its signed scope/actor claims locally; we never possess
        # the Core signing key or trust a caller-provided --scope as authority.
        self.call("/v1/keychain/credentials")
        parts = self.token.split(".")
        require(
            len(parts) in {2, 3},
            "PLATFORM_INCOMPATIBLE",
            "Unknown native capability envelope",
        )
        encoded = parts[1] if len(parts) == 3 else parts[0]
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if payload.get("encoding") == "deflate-raw":
            value = payload.get("claims", "")
            packed = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            decoder = zlib.decompressobj(-15)
            raw = decoder.decompress(packed, 1024 * 1024 + 1)
            require(
                len(raw) <= 1024 * 1024 and not decoder.unconsumed_tail,
                "PLATFORM_INCOMPATIBLE",
                "Oversized native capability",
            )
            payload = json.loads(raw)
        require(
            isinstance(payload.get("scopeId"), str)
            and isinstance(payload.get("actorId"), str),
            "PLATFORM_INCOMPATIBLE",
            "Native capability lacks execution identity",
        )
        return {"scopeId": payload["scopeId"], "actorId": payload["actorId"]}

    def call(self, path: str, body: dict | None = None) -> dict:
        require(
            path
            in {"/v1/keychain/credentials", "/v1/keychain/grants", "/v1/keychain/asks"},
            "KEYCHAIN_DENIED",
            "Unsupported keychain operation",
        )
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "x-agent-capability": self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(10 * MAX_BYTES + 1)
            require(
                len(data) <= 10 * MAX_BYTES,
                "KEYCHAIN_ERROR",
                "Keychain response exceeds its limit",
            )
            value = json.loads(data)
            require(
                isinstance(value, dict), "KEYCHAIN_ERROR", "Invalid keychain response"
            )
            return value
        except urllib.error.HTTPError as error:
            raise AKSError(
                "KEYCHAIN_DENIED",
                f"Keychain refused this operation (HTTP {error.code}); no alternate credential was tried",
            ) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise AKSError(
                "KEYCHAIN_UNAVAILABLE",
                "Keychain request failed; authorization cannot be verified",
            ) from None


def read_azure_keys(tenant: str) -> dict:
    # Public keys only, no capability/token headers, redirects or profile-defined
    # cloud endpoints. This is intentionally a replaceable network test boundary.
    url = f"https://login.microsoftonline.com/{uuid_value(tenant)}/discovery/v2.0/keys"
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(url, timeout=20) as response:
            raw = response.read(256 * 1024 + 1)
        require(
            len(raw) <= 256 * 1024,
            "IDENTITY_UNVERIFIED",
            "Microsoft signing-key response exceeds its limit",
        )
        return json.loads(raw)
    except (OSError, ValueError):
        raise AKSError(
            "IDENTITY_UNVERIFIED",
            "Microsoft signing keys are unavailable; token identity was not accepted",
        ) from None


def verified_azure_claims(token: str, tenant: str) -> dict:
    import jwt

    try:
        header = jwt.get_unverified_header(token)
        require(
            header.get("alg") == "RS256" and isinstance(header.get("kid"), str),
            "IDENTITY_UNVERIFIED",
            "Unsupported Azure signing algorithm/key",
        )
        keys = read_azure_keys(tenant).get("keys", [])
        matches = [
            key
            for key in keys
            if key.get("kid") == header["kid"] and key.get("kty") == "RSA"
        ]
        require(
            len(matches) == 1,
            "IDENTITY_UNVERIFIED",
            "Azure token signing key was not found uniquely",
        )
        key = jwt.PyJWK.from_dict(matches[0]).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=[
                "https://management.azure.com/",
                "https://management.azure.com",
                "https://management.core.windows.net/",
                "https://management.core.windows.net",
            ],
            issuer=[
                f"https://sts.windows.net/{tenant}/",
                f"https://login.microsoftonline.com/{tenant}/v2.0",
            ],
            leeway=60,
            options={"require": ["exp", "iss", "aud", "tid", "oid"]},
        )
        require(
            uuid_value(claims["tid"]) == tenant,
            "IDENTITY_MISMATCH",
            "Verified Azure token tenant differs from the selected tenant",
        )
        uuid_value(claims["oid"])
        return claims
    except AKSError:
        raise
    except Exception:
        raise AKSError(
            "IDENTITY_UNVERIFIED",
            "Azure token signature, audience, issuer or lifetime could not be verified",
        ) from None


def purpose_target(purpose: str) -> dict:
    try:
        value = json.loads(purpose)
    except (ValueError, TypeError):
        raise AKSError(
            "PURPOSE_DENIED",
            "Existing grant lacks the structured AKS read-only purpose; request owner approval for the new purpose",
        ) from None
    require(
        isinstance(value, dict) and value.get("kind") == PURPOSE_KIND,
        "PURPOSE_DENIED",
        "The grant is not an AKS read-only authorization",
    )
    return value


def authorize(
    api: Keychain, handle: str, scope: str, grant_id: str | None, target: dict | None
) -> dict:
    require(
        bool(re.fullmatch(r"[A-Za-z0-9%._-]+:[A-Za-z0-9_-]+", handle)),
        "CREDENTIAL_DENIED",
        "Use an execution handle from the current platform credential catalog",
    )
    service, credential_id = handle.rsplit(":", 1)
    identity = api.identity()
    require(
        scope == identity["scopeId"],
        "SCOPE_MISMATCH",
        "Requested scope differs from the Core-validated capability scope",
    )
    if grant_id:
        grants = api.call("/v1/keychain/grants").get("grants", [])
        grant = next((g for g in grants if g.get("id") == grant_id), None)
        require(
            grant
            and grant.get("credentialId") == credential_id
            and grant.get("audienceScopeId") == scope,
            "GRANT_DENIED",
            "The grant must match this credential and current conversation scope",
        )
        assert grant is not None
        require(
            grant.get("mode") == "standing"
            and grant.get("status") == "active"
            and (not grant.get("expiresAt") or grant["expiresAt"] > time.time() * 1000),
            "GRANT_DENIED",
            "Shared AKS access requires a current standing grant; consumed once-grants are not accepted",
        )
        approved = purpose_target(grant.get("purpose", ""))
        if target:
            for key in ("tenantId", "subscriptionId", "resourceGroup", "clusterName"):
                require(
                    str(approved.get(key, "")).lower() == str(target[key]).lower(),
                    "TARGET_DENIED",
                    "Requested cluster is outside the approved purpose",
                )
            namespaces = approved.get("namespaces")
            require(
                isinstance(namespaces, list)
                and set(target["namespaces"]) <= set(namespaces),
                "TARGET_DENIED",
                "Requested namespaces are outside the approved purpose",
            )
            require(
                not target["clusterRead"] or approved.get("clusterRead") is True,
                "TARGET_DENIED",
                "Cluster-scoped node reads were not approved",
            )
        return {
            "mode": "project-shared",
            "credential": handle,
            "grant": grant_id,
            "approved": approved,
        }
    own = api.call("/v1/keychain/credentials").get("credentials", [])
    credential = next(
        (
            c
            for c in own
            if c.get("id") == credential_id and c.get("service") == unquote(service)
        ),
        None,
    )
    require(
        credential and credential.get("kind") == "file",
        "CREDENTIAL_DENIED",
        "A current owner Azure file-login credential is required",
    )
    assert credential is not None
    require(
        scope == "personal:" + str(credential.get("ownerId"))
        and credential.get("ownerId") == identity["actorId"],
        "GRANT_REQUIRED",
        "Shared conversations require an explicit AKS standing grant; use the owner's personal conversation for unshared access",
    )
    require(
        not credential.get("expiresAt") or credential["expiresAt"] > time.time() * 1000,
        "AUTH_REQUIRED",
        "The saved login has expired",
    )
    return {"mode": "personal-owner", "credential": handle}


def run(
    argv: list[str], env: dict[str, str], timeout: int = 120, challenge: bool = False
) -> bytes:
    """Bound stdout/stderr together, kill process groups, never expose raw errors."""
    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    errors = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, output)
    selector.register(process.stderr, selectors.EVENT_READ, errors)
    deadline = time.monotonic() + timeout
    challenge_buffer = ""
    try:
        while selector.get_map():
            require(
                time.monotonic() < deadline,
                "TIMEOUT",
                "AKS operation timed out; no automatic retry was made",
            )
            for key, _ in selector.select(0.2):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                require(
                    len(output) + len(errors) <= MAX_BYTES,
                    "OUTPUT_LIMIT",
                    "AKS output exceeded its limit; narrow the request",
                )
                if challenge:
                    challenge_buffer += chunk.decode("utf-8", errors="replace")
                    for match in re.finditer(
                        r"https://(?:microsoft\.com/devicelogin|login\.microsoftonline\.com/[^\s]+).*?\bcode\s+([A-Z0-9]{6,16})\b",
                        challenge_buffer,
                        re.IGNORECASE,
                    ):
                        print(
                            json.dumps(
                                {
                                    "verificationUrl": match[0].split()[0],
                                    "userCode": match[1],
                                }
                            ),
                            flush=True,
                        )
                        challenge_buffer = ""
                        break
        result = process.wait(timeout=max(1, deadline - time.monotonic()))
        if result:
            detail = errors.decode("utf-8", errors="replace").lower()
            code = (
                "FORBIDDEN"
                if "forbidden" in detail
                or "authorizationfailed" in detail
                or "403" in detail
                else "AUTH_REQUIRED"
                if "unauthorized" in detail
                or "aadsts" in detail
                or "please run" in detail
                else "NETWORK_UNAVAILABLE"
                if "timeout" in detail
                or "no such host" in detail
                or "connection refused" in detail
                else "TOOL_FAILED"
            )
            raise AKSError(
                code,
                "The approved operation failed; raw stdout/stderr was withheld to protect credentials",
            )
        return bytes(output)
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            stream.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def result_error(error: Exception) -> None:
    if isinstance(error, AKSError):
        print(
            json.dumps(
                {"ok": False, "error": {"code": error.code, "message": str(error)}}
            )
        )
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "LOCAL_FAILURE",
                        "message": "The local runtime failed safely; no raw credential/error data is included",
                    },
                }
            )
        )
