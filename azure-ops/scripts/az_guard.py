#!/usr/bin/env python3

import argparse
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


MAX_REQUEST_BYTES = 65_536
MAX_OUTPUT_CHARS = 150_000
MAX_PROCESS_FILE_BYTES = MAX_OUTPUT_CHARS + 1
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 120
RESOURCE_GRAPH_PATH = "/providers/Microsoft.ResourceGraph/resources"
AZURE_CLI_VERSION = "2.89.1"
AZURE_SUPPORT_EXTENSION_VERSION = "2.0.1"
USER_AZURE_INSTALL_ROOT = Path.home() / ".local" / "share" / "azure-cli" / AZURE_CLI_VERSION
USER_AZURE_CLI = Path.home() / ".local" / "bin" / "az"
USER_AZURE_EXTENSION_DIR = Path.home() / ".local" / "share" / "azure-cli" / "extensions"

READ_COMMANDS = {
    ("account", "show"),
    ("disk", "show"),
    ("monitor", "metrics", "list"),
    ("network", "nic", "show"),
    ("resource", "list"),
    ("resource", "show"),
    ("support", "services", "list"),
    ("support", "services", "problem-classifications", "list"),
    ("vm", "get-instance-view"),
    ("vm", "show"),
}

DENIED_TOKENS = {
    "add",
    "assign-identity",
    "capture",
    "convert",
    "create",
    "deallocate",
    "delete",
    "execute",
    "extension",
    "generalize",
    "invoke",
    "login",
    "logout",
    "migrate",
    "patch",
    "perform-maintenance",
    "redeploy",
    "reimage",
    "remove",
    "restart",
    "run-command",
    "set",
    "ssh",
    "start",
    "stop",
    "update",
}

DENIED_FLAGS = {
    "--debug",
    "--file",
    "--headers",
    "--input-file",
    "--output-file",
    "--verbose",
}

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


class GuardError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def emit(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_OUTPUT_CHARS:
        encoded = json.dumps(
            {"error": "OUTPUT_TOO_LARGE", "message": "Guard output exceeded the response limit"},
            separators=(",", ":"),
        )
    print(encoded)


def redact(value: str) -> str:
    text = value
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[REDACTED_JWT]", text)
    text = re.sub(
        r'(?i)("(?:accessToken|refreshToken|clientSecret|client_secret|id_token|password|token)"\s*:\s*")[^"]*(")',
        r"\1[REDACTED]\2",
        text,
    )
    text = re.sub(
        r"(?i)((?:client[_-]?secret|access[_-]?token|refresh[_-]?token|password)\s*[=:]\s*)\S+",
        r"\1[REDACTED]",
        text,
    )
    return text


def command_path(args: list[str]) -> tuple[str, ...]:
    path: list[str] = []
    for value in args:
        if value.startswith("-"):
            break
        path.append(value.lower())
    return tuple(path)


def flag_value(args: list[str], *flags: str) -> str | None:
    matches: list[str] = []
    for index, value in enumerate(args):
        for flag in flags:
            if value == flag and index + 1 < len(args):
                matches.append(args[index + 1])
            if value.startswith(f"{flag}="):
                matches.append(value.split("=", 1)[1])
    if len(matches) > 1:
        raise GuardError("OPTION_DUPLICATE", f"Azure CLI option may appear only once: {'/'.join(flags)}")
    return matches[0] if matches else None


def validate_output(args: list[str]) -> None:
    output = flag_value(args, "-o", "--output")
    if output is not None and output.lower() != "json":
        raise GuardError("OUTPUT_FORMAT_DENIED", "Azure CLI output must be JSON")


def validate_common_args(args: list[str]) -> None:
    if not args:
        raise GuardError("EMPTY_COMMAND", "Azure CLI argument list is empty")
    if any(value.startswith("@") for value in args):
        raise GuardError("FILE_REFERENCE_DENIED", "Azure CLI @file arguments are not allowed")
    if len(args) > 200 or sum(len(value) for value in args) > 50_000:
        raise GuardError("COMMAND_TOO_LARGE", "Azure CLI argument list exceeds the guard limit")
    for value in args:
        lowered = value.lower()
        if lowered in DENIED_FLAGS or any(lowered.startswith(f"{flag}=") for flag in DENIED_FLAGS):
            raise GuardError("FLAG_DENIED", f"Azure CLI flag is not allowed: {value}")
    validate_output(args)


def validate_rest(args: list[str]) -> None:
    for value in args[1:]:
        if value == "--uri" or value.startswith("--uri=") or (
            value.startswith("-") and not value.startswith("--") and value != "-o"
        ):
            raise GuardError("REST_OPTION_DENIED", "az rest requires canonical long options; short options and --uri are denied")
    method = (flag_value(args, "--method") or "get").lower()
    raw_url = flag_value(args, "--url")
    if not raw_url:
        raise GuardError("REST_URL_REQUIRED", "az rest requires --url")
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or parsed.hostname != "management.azure.com" or parsed.username or parsed.password:
        raise GuardError("REST_URL_DENIED", "az rest is restricted to https://management.azure.com")
    if parsed.port not in (None, 443) or parsed.fragment:
        raise GuardError("REST_URL_DENIED", "az rest URL contains a denied port or fragment")
    if "api-version" not in parse_qs(parsed.query):
        raise GuardError("REST_API_VERSION_REQUIRED", "az rest URL requires api-version")

    input_file = flag_value(args, "--input-file")
    headers = flag_value(args, "--headers")
    if input_file or headers:
        raise GuardError("REST_OPTION_DENIED", "az rest input files and custom headers are not allowed")

    body = flag_value(args, "--body")
    if method == "get":
        if body is not None:
            raise GuardError("REST_BODY_DENIED", "read-only ARM GET requests cannot include a body")
        return

    if method != "post" or parsed.path.lower() != RESOURCE_GRAPH_PATH.lower():
        raise GuardError("REST_METHOD_DENIED", "only ARM GET and the fixed Resource Graph query POST are allowed")
    if body is None or len(body.encode("utf-8")) > 20_000:
        raise GuardError("RESOURCE_GRAPH_BODY_INVALID", "Resource Graph POST requires a bounded JSON body")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise GuardError("RESOURCE_GRAPH_BODY_INVALID", f"Resource Graph body is not valid JSON: {error.msg}") from error
    if not isinstance(payload, dict) or set(payload) - {"subscriptions", "query", "options"}:
        raise GuardError("RESOURCE_GRAPH_BODY_INVALID", "Resource Graph body contains unsupported fields")
    subscriptions = payload.get("subscriptions")
    query = payload.get("query")
    if not isinstance(subscriptions, list) or not subscriptions or len(subscriptions) > 20:
        raise GuardError("RESOURCE_GRAPH_BODY_INVALID", "Resource Graph subscriptions must be a non-empty bounded list")
    if not all(isinstance(value, str) and UUID_PATTERN.fullmatch(value) for value in subscriptions):
        raise GuardError("RESOURCE_GRAPH_BODY_INVALID", "Resource Graph subscription IDs must be UUIDs")
    if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
        raise GuardError("RESOURCE_GRAPH_BODY_INVALID", "Resource Graph query must be a bounded non-empty string")


def validate_read(args: list[str]) -> None:
    validate_common_args(args)
    path = command_path(args)
    if path == ("rest",):
        validate_rest(args)
        return
    if path not in READ_COMMANDS:
        raise GuardError("COMMAND_DENIED", f"Azure CLI command is not in the read-only allowlist: {' '.join(path)}")
    for value in args:
        if value.lower() in DENIED_TOKENS:
            raise GuardError("MUTATION_DENIED", f"Mutation token is not allowed in a read command: {value}")


def workspace_file(raw_path: str) -> Path:
    root = Path.cwd().resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise GuardError("REQUEST_FILE_DENIED", "Request file must be a regular file inside the current workspace") from error
    if candidate.is_symlink() or not resolved.is_file() or resolved.stat().st_size > MAX_REQUEST_BYTES:
        raise GuardError("REQUEST_FILE_DENIED", "Request file must be a bounded non-symlink regular file")
    return resolved


def read_request(raw_path: str) -> list[str]:
    path = workspace_file(raw_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GuardError("REQUEST_INVALID", "Request file must contain valid UTF-8 JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"args"}:
        raise GuardError("REQUEST_INVALID", "Request JSON must contain only an args field")
    args = payload["args"]
    if not isinstance(args, list) or not args or not all(isinstance(value, str) and value for value in args):
        raise GuardError("REQUEST_INVALID", "Request args must be a non-empty array of non-empty strings")
    if len(args) > 200 or sum(len(value) for value in args) > 50_000:
        raise GuardError("REQUEST_INVALID", "Request argument list exceeds the guard limit")
    return args


def timeout_seconds() -> int:
    raw = os.environ.get("AZURE_OPS_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        value = int(raw)
    except ValueError as error:
        raise GuardError("TIMEOUT_INVALID", "AZURE_OPS_TIMEOUT_SECONDS must be an integer") from error
    return max(5, min(value, MAX_TIMEOUT_SECONDS))


def normalize_output_args(args: list[str]) -> list[str]:
    if flag_value(args, "-o", "--output") is not None:
        return list(args)
    return [*args, "-o", "json"]


def classify_cli_error(stderr: str) -> str:
    lowered = stderr.lower()
    if any(value in lowered for value in ("az login", "aadsts", "credential", "expired", "unauthorized", "authentication")):
        return "AUTH_REQUIRED"
    if any(value in lowered for value in ("resourcenotfound", "was not found", "not found")):
        return "NOT_FOUND"
    if any(value in lowered for value in ("authorizationfailed", "forbidden", "does not have authorization")):
        return "FORBIDDEN"
    return "AZURE_ERROR"


def azure_cli_path() -> str | None:
    expected = USER_AZURE_INSTALL_ROOT / "bin" / "az"
    marker = USER_AZURE_INSTALL_ROOT / ".complete"
    try:
        for directory in (USER_AZURE_INSTALL_ROOT, expected.parent, USER_AZURE_EXTENSION_DIR):
            if not directory.is_dir() or directory.is_symlink() or directory.stat().st_uid != os.getuid():
                return None
        if expected.is_symlink() or marker.is_symlink():
            return None
        if not USER_AZURE_CLI.is_symlink():
            return None
        if os.readlink(USER_AZURE_CLI) != f"../share/azure-cli/{AZURE_CLI_VERSION}/bin/az":
            return None
        if USER_AZURE_CLI.resolve(strict=True) != expected.resolve(strict=True):
            return None
        if marker.read_text(encoding="utf-8").strip() != AZURE_CLI_VERSION:
            return None
        if not expected.is_file() or not os.access(expected, os.X_OK):
            return None
        with tempfile.TemporaryDirectory() as config_dir:
            check_env = {
                "HOME": str(Path.home()),
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                "AZURE_CORE_COLLECT_TELEMETRY": "0",
                "AZURE_CONFIG_DIR": config_dir,
                "AZURE_EXTENSION_DIR": str(USER_AZURE_EXTENSION_DIR),
            }
            cli = subprocess.run(
                [str(expected), "version", "--query", '"azure-cli"', "-o", "tsv"],
                capture_output=True,
                check=False,
                env=check_env,
                text=True,
                timeout=15,
            )
            support = subprocess.run(
                [str(expected), "extension", "show", "--name", "support", "--query", "version", "-o", "tsv"],
                capture_output=True,
                check=False,
                env=check_env,
                text=True,
                timeout=15,
            )
            if cli.returncode != 0 or cli.stdout.strip() != AZURE_CLI_VERSION:
                return None
            if support.returncode != 0 or support.stdout.strip() != AZURE_SUPPORT_EXTENSION_VERSION:
                return None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return str(expected)


def read_process_file(handle, limit: int, code: str, message: str) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size > limit:
        raise GuardError(code, message)
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace")


def limit_process_files() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PROCESS_FILE_BYTES, MAX_PROCESS_FILE_BYTES))


def execute(args: list[str]) -> Any:
    az_path = azure_cli_path()
    if not az_path:
        raise GuardError(
            "AZ_NOT_FOUND",
            "Azure CLI is not installed in this Scope; run skills/azure-ops/scripts/install_azure_cli.sh",
        )
    command = [az_path, *normalize_output_args(args), "--only-show-errors"]
    environment = {
        "HOME": str(Path.home()),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "AZURE_CORE_COLLECT_TELEMETRY": "0",
        "AZURE_CONFIG_DIR": str(Path.home() / ".azure"),
        "AZURE_EXTENSION_DIR": str(USER_AZURE_EXTENSION_DIR),
    }
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            result = subprocess.run(
                command,
                check=False,
                env=environment,
                shell=False,
                stdout=stdout_file,
                stderr=stderr_file,
                preexec_fn=limit_process_files,
                timeout=timeout_seconds(),
            )
        except subprocess.TimeoutExpired as error:
            raise GuardError("TIMEOUT", "Azure CLI command timed out") from error
        except OSError as error:
            raise GuardError("EXECUTION_FAILED", f"Azure CLI could not start: {error}") from error

        if result.returncode != 0:
            stderr = read_process_file(stderr_file, MAX_OUTPUT_CHARS, "ERROR_TOO_LARGE", "Azure CLI error exceeded the guard limit")
            stdout = read_process_file(stdout_file, MAX_OUTPUT_CHARS, "ERROR_TOO_LARGE", "Azure CLI error exceeded the guard limit")
            detail = redact((stderr or stdout or "Azure CLI command failed").strip())[-2_000:]
            raise GuardError(classify_cli_error(detail), detail)
        output = read_process_file(
            stdout_file,
            MAX_OUTPUT_CHARS,
            "OUTPUT_TOO_LARGE",
            "Azure CLI output exceeded the guard limit; narrow the query",
        ).strip()
    if not output:
        return None
    try:
        return json.loads(redact(output))
    except json.JSONDecodeError as error:
        raise GuardError("OUTPUT_INVALID", "Azure CLI returned non-JSON output") from error


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="az_guard.py")
    parser.add_argument("--request-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_cli(argv if argv is not None else sys.argv[1:])
    try:
        if options.request_file:
            if options.command:
                raise GuardError("USAGE_INVALID", "Request-file mode does not accept direct command arguments")
            args = read_request(options.request_file)
        else:
            args = list(options.command)
            if args[:1] == ["--"]:
                args = args[1:]
        validate_read(args)
        emit(execute(args))
        return 0
    except GuardError as error:
        emit({"error": error.code, "message": redact(error.message)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())