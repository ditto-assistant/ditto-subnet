#!/usr/bin/python3
"""Forward one bounded Hippius canary helper exchange over a protected socket."""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

_CONFIG_ROOT = Path("/etc/ditto-platform/coding/hippius-canary")
_CONFIG_SCHEMA = "dittobench-coding-hippius-canary-helper-proxy-config-v1"
_ROLE_BY_NAME = {
    "hippius-canary-unwrap": "unwrap",
    "hippius-canary-authoring": "authoring",
    "hippius-canary-grading": "grading",
}
_REQUEST_SCHEMA = {
    "unwrap": "dittobench-coding-hippius-canary-unwrap-helper-request-v1",
    "authoring": "dittobench-coding-hippius-canary-authoring-helper-request-v1",
    "grading": "dittobench-coding-hippius-canary-grading-helper-request-v1",
}
_RESPONSE_SCHEMA = {
    "unwrap": "dittobench-coding-hippius-canary-unwrap-helper-response-v1",
    "authoring": "dittobench-coding-hippius-canary-authoring-helper-response-v1",
    "grading": "dittobench-coding-hippius-canary-grading-helper-response-v1",
}
_MAX_CONFIG_BYTES = 16 << 10
_MAX_FRAME_BYTES = 24 << 20
_HEADER_BYTES = 8


class HelperProxyError(RuntimeError):
    """The local protected helper boundary is unavailable or inconsistent."""


def proxy_request(*, role: str, config_path: Path, body: bytes) -> bytes:
    if role not in _REQUEST_SCHEMA:
        raise HelperProxyError("helper proxy role is invalid")
    config = _load_config(config_path, role=role)
    request = _canonical_object(
        body,
        maximum_bytes=config["max_request_bytes"],
        expected_schema=_REQUEST_SCHEMA[role],
        label="request",
    )
    request_body = _canonical_json(request)
    socket_path = Path(config["socket_path"])
    _validate_socket_path(
        socket_path,
        expected_uid=config["expected_peer_uid"],
        expected_gid=config["expected_peer_gid"],
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(config["timeout_seconds"])
        client.connect(str(socket_path))
        _validate_connected_peer(
            client,
            expected_uid=config["expected_peer_uid"],
            expected_gid=config["expected_peer_gid"],
        )
        client.sendall(struct.pack(">Q", len(request_body)) + request_body)
        client.shutdown(socket.SHUT_WR)
        response_size = struct.unpack(">Q", _receive_exact(client, _HEADER_BYTES))[0]
        if not 1 <= response_size <= config["max_response_bytes"]:
            raise HelperProxyError("helper response frame exceeds its bound")
        response_body = _receive_exact(client, response_size)
    except (OSError, struct.error) as error:
        raise HelperProxyError("helper socket exchange failed") from error
    finally:
        client.close()
    response = _canonical_object(
        response_body,
        maximum_bytes=config["max_response_bytes"],
        expected_schema=_RESPONSE_SCHEMA[role],
        label="response",
    )
    return _canonical_json(response)


def main(
    argv: list[str] | None = None,
    *,
    program_name: str | None = None,
    config_root: Path = _CONFIG_ROOT,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    name = Path(sys.argv[0] if program_name is None else program_name).name
    if arguments or name not in _ROLE_BY_NAME:
        print("Hippius canary helper proxy invocation is invalid", file=sys.stderr)
        return 2
    role = _ROLE_BY_NAME[name]
    source = sys.stdin.buffer if stdin is None else stdin
    sink = sys.stdout.buffer if stdout is None else stdout
    try:
        body = source.read(_MAX_FRAME_BYTES + 1)
        if not body or len(body) > _MAX_FRAME_BYTES:
            raise HelperProxyError("helper request exceeds its bound")
        response = proxy_request(
            role=role,
            config_path=config_root / f"{role}.json",
            body=body,
        )
        sink.write(response)
        sink.flush()
    except HelperProxyError:
        print("Hippius canary helper proxy failed", file=sys.stderr)
        return 2
    return 0


def _load_config(path: Path, *, role: str) -> dict[str, Any]:
    body = _read_protected_config(path)
    config = _canonical_object(
        body,
        maximum_bytes=_MAX_CONFIG_BYTES,
        expected_schema=_CONFIG_SCHEMA,
        label="configuration",
    )
    if set(config) != {
        "expected_peer_gid",
        "expected_peer_uid",
        "max_request_bytes",
        "max_response_bytes",
        "role",
        "schema",
        "socket_path",
        "timeout_seconds",
    }:
        raise HelperProxyError("helper proxy configuration fields are invalid")
    if config["role"] != role:
        raise HelperProxyError("helper proxy role is inconsistent")
    socket_path = config["socket_path"]
    if (
        not isinstance(socket_path, str)
        or not socket_path.startswith("/")
        or "\x00" in socket_path
        or len(socket_path.encode()) > 100
        or not _strict_int(config["expected_peer_uid"], minimum=1)
        or not _strict_int(config["expected_peer_gid"], minimum=1)
        or not _strict_int(
            config["max_request_bytes"], minimum=1, maximum=_MAX_FRAME_BYTES
        )
        or not _strict_int(
            config["max_response_bytes"], minimum=1, maximum=_MAX_FRAME_BYTES
        )
        or not _strict_int(config["timeout_seconds"], minimum=1, maximum=7200)
    ):
        raise HelperProxyError("helper proxy configuration is unsafe")
    return config


def _read_protected_config(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HelperProxyError("helper proxy configuration is unavailable") from error
    try:
        info = os.fstat(descriptor)
        self_owned = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) in {
            0o400,
            0o600,
        }
        root_group_owned = (
            info.st_uid == 0
            and info.st_gid in os.getgroups()
            and stat.S_IMODE(info.st_mode) == 0o440
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not (self_owned or root_group_owned)
            or not 1 <= info.st_size <= _MAX_CONFIG_BYTES
        ):
            raise HelperProxyError("helper proxy configuration is unsafe")
        body = _read_descriptor(descriptor, maximum_bytes=_MAX_CONFIG_BYTES)
    except OSError as error:
        raise HelperProxyError("helper proxy configuration is unreadable") from error
    finally:
        os.close(descriptor)
    return body


def _validate_socket_path(path: Path, *, expected_uid: int, expected_gid: int) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise HelperProxyError("helper socket is unavailable") from error
    if (
        not path.is_absolute()
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != expected_uid
        or info.st_gid != expected_gid
        or stat.S_IMODE(info.st_mode) != 0o660
    ):
        raise HelperProxyError("helper socket authority is unsafe")


def _validate_connected_peer(
    client: socket.socket,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if not hasattr(socket, "SO_PEERCRED"):
        raise HelperProxyError("helper peer credentials are unsupported")
    try:
        _pid, uid, gid = struct.unpack(
            "3i",
            client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
        )
    except (OSError, struct.error) as error:
        raise HelperProxyError("helper peer credentials are unavailable") from error
    if uid != expected_uid or gid != expected_gid:
        raise HelperProxyError("helper peer identity is inconsistent")


def _receive_exact(client: socket.socket, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = client.recv(min(64 << 10, size - len(body)))
        if not chunk:
            raise HelperProxyError("helper response frame is incomplete")
        body.extend(chunk)
    return bytes(body)


def _read_descriptor(descriptor: int, *, maximum_bytes: int) -> bytes:
    body = bytearray()
    while len(body) < maximum_bytes + 1:
        chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
        if not chunk:
            break
        body.extend(chunk)
    if not body or len(body) > maximum_bytes:
        raise HelperProxyError("helper proxy configuration exceeds its bound")
    return bytes(body)


def _canonical_object(
    body: bytes,
    *,
    maximum_bytes: int,
    expected_schema: str,
    label: str,
) -> dict[str, Any]:
    if not 1 <= len(body) <= maximum_bytes:
        raise HelperProxyError(f"helper {label} exceeds its bound")
    try:
        value = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or value.get("schema") != expected_schema:
            raise ValueError("schema is invalid")
        if _canonical_json(value) != body:
            raise ValueError("document is not canonical")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HelperProxyError(f"helper {label} is invalid") from error
    return value


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise HelperProxyError("helper canonical JSON is invalid") from error


def _strict_int(value: object, *, minimum: int, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
