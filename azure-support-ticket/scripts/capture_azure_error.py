#!/usr/bin/env python3
"""Retain sanitized Azure error evidence without credentials, network calls, or retries."""

import argparse
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path


def redact(text, record):
    private = []
    for key in ("contact", "contactDetails", "contactProfile"):
        contact = record.get(key, {})
        if isinstance(contact, dict):
            for field, value in contact.items():
                if field.lower() in {
                    "firstname",
                    "lastname",
                    "email",
                    "primaryemailaddress",
                    "phone",
                    "phonenumber",
                    "additionalemailaddresses",
                }:
                    private.extend(value if isinstance(value, list) else [value])
    for value in sorted((str(v) for v in private if v), key=len, reverse=True):
        text = re.sub(re.escape(value), "<REDACTED>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "<REDACTED_KEY>",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?im)(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*",
        r"\1: <REDACTED>",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer <REDACTED>", text)
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<REDACTED_TOKEN>", text
    )
    text = re.sub(
        r"(?i)([?&](?:sig|token|access_token|client_secret)=)[^&\s]+",
        r"\1<REDACTED>",
        text,
    )
    text = re.sub(
        r"""(?i)(["']?\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|api[_-]?key|authorization|cookie|set-cookie)\b["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)""",
        r"\1<REDACTED>",
        text,
    )
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<REDACTED_EMAIL>", text)
    return text[:2048]


def split_cli_details(raw):
    lines = raw.splitlines()
    candidates = [
        (index, len(match.group(1).expandtabs(4)))
        for index, line in enumerate(lines)
        if (match := re.match(r"^([ \t]*)\([A-Za-z][A-Za-z0-9_.-]*\)", line))
    ]
    if not candidates:
        return [raw]
    level = min(indent for _, indent in candidates)
    starts = [0] + [index for index, indent in candidates if indent == level][1:]
    return [
        "\n".join(lines[start:end])
        for start, end in zip(starts, starts[1:] + [len(lines)])
    ]


def parse_cli_error(raw, depth=0):
    detail_header = re.search(r"(?im)^([ \t]*)Exception Details:[ \t]*(.*)$", raw)
    parts = [raw]
    if detail_header:
        tail = raw[detail_header.end() :]
        inline_detail = detail_header.group(2).strip()
        if inline_detail:
            direct = re.split(r"(?im)^[ \t]*Exception Details:", tail, maxsplit=1)[0]
            following = re.search(
                r"(?m)^([ \t]*)(?:Code:|Message:|Target:|\([A-Za-z][A-Za-z0-9_.-]*\))",
                direct,
            )
            indentation = following.group(1) if following else detail_header.group(1)
            tail = indentation + inline_detail + tail
        parts = [raw[: detail_header.start()], tail]
    parent = parts[0]
    code = re.search(
        r"(?im)^[ \t]*(?:ERROR:[ \t]*)?Code:[ \t]*([A-Za-z][A-Za-z0-9_.-]*)", parent
    )
    inline = re.search(
        r"(?m)^[ \t]*(?:ERROR:[ \t]*)?\(([A-Za-z][A-Za-z0-9_.-]*)\)[ \t]*(.*)", parent
    )
    message = re.search(
        r"(?ims)^[ \t]*Message:[ \t]*(.*?)(?=^[ \t]*(?:Code|Target|Additional Information):|\Z)",
        parent,
    )
    target = re.search(r"(?im)^[ \t]*Target:[ \t]*(.+)", parent)
    result = {
        "code": code.group(1)
        if code
        else inline.group(1)
        if inline
        else "InvalidParameterValue"
        if raw.strip() == "InvalidParameterValue"
        else "UnknownAzureError"
    }
    if message:
        result["message"] = message.group(1).strip()
    elif inline:
        remainder = parent[inline.start(2) :]
        result["message"] = re.split(
            r"(?im)^[ \t]*(?:Code|Message|Target|Additional Information):",
            remainder,
            maxsplit=1,
        )[0].strip()
    if target:
        result["target"] = target.group(1)
    if len(parts) == 2 and depth < 3:
        blocks = split_cli_details(parts[1])
        result["details"] = [
            parse_cli_error(block, depth + 1) for block in blocks[:10] if block.strip()
        ]
    return result


def extract_error(raw, record):
    error = None
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            error = value.get("error", value)
    except ValueError:
        pass
    if not isinstance(error, dict):
        error = parse_cli_error(raw)

    def project(value, depth=0):
        result = {
            key: redact(str(value[key]), record)
            for key in ("code", "message", "target")
            if value.get(key) is not None
        }
        if depth < 3 and isinstance(value.get("details"), list):
            result["details"] = [
                project(item, depth + 1)
                for item in value["details"][:10]
                if isinstance(item, dict)
            ]
        return result

    result = project(error)
    result.setdefault("code", "UnknownAzureError")
    result["detailsMissing"] = not bool(result.get("message"))
    request_id = re.search(
        r"(?im)^\s*(?:x-ms-request-id|request(?:/correlation)?[ -]?id|correlation[ -]?id):\s*([A-Za-z0-9-]+)",
        raw,
    )
    if request_id:
        result["requestOrCorrelationId"] = request_id.group(1)
    return result


def save_error(record_path, raw, attempt_time):
    """Call after the submission process releases its request lock, not from inside it."""
    record_path = Path(record_path)
    lock_path = record_path.with_suffix(record_path.suffix + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = json.loads(record_path.read_text())
        if not attempt_time or record.get("attemptTime") != attempt_time:
            raise ValueError(
                "request attempt changed; refusing to attach stale error evidence"
            )
        record["lastError"] = {
            **extract_error(raw, record),
            "attemptTime": attempt_time,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=record_path.parent,
            prefix=record_path.name + ".",
            delete=False,
        ) as temp:
            temporary = Path(temp.name)
            try:
                os.fchmod(temp.fileno(), 0o600)
                json.dump(record, temp, ensure_ascii=False)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(temporary, record_path)
                directory = os.open(record_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        return record["lastError"]
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--error-file", required=True, type=Path)
    parser.add_argument("--attempt-time", required=True)
    args = parser.parse_args()
    result = save_error(
        args.record, args.error_file.read_text(errors="replace"), args.attempt_time
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
