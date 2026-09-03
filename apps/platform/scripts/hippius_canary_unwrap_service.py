#!/usr/bin/python3
"""Serve one protected synthetic-canary RSA unwrap authority over AF_UNIX."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

_AUTHORITY_SCHEMA = "dittobench-coding-hippius-canary-unwrap-authority-v1"
_HELPER_REQUEST_SCHEMA = "dittobench-coding-hippius-canary-unwrap-helper-request-v1"
_UNWRAP_REQUEST_SCHEMA = "dittobench-coding-hippius-private-input-unwrap-v1"
_HELPER_RESPONSE_SCHEMA = "dittobench-coding-hippius-canary-unwrap-helper-response-v1"
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")
_MAX_AUTHORITY_BYTES = 64 << 10
_MAX_REQUEST_BYTES = 64 << 10
_MAX_RESPONSE_BYTES = 64 << 10
_MAX_KEY_BYTES = 64 << 10
_HEADER_BYTES = 8
_MIN_WRAPPED_KEY_BYTES = 256
_MAX_WRAPPED_KEY_BYTES = 1024
_OPENSSL_ENV = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}


class UnwrapServiceError(RuntimeError):
    """The exact canary unwrap authority is unavailable or inconsistent."""


@dataclass(frozen=True, repr=False)
class UnwrapServiceConfig:
    socket_path: Path
    authority_path: Path
    private_key_path: Path
    openssl_path: Path
    expected_client_uid: int
    expected_client_gid: int
    socket_timeout_seconds: int
    require_socket_activation: bool = False

    def __repr__(self) -> str:
        return "UnwrapServiceConfig(enabled=True, network=False)"


class CanaryUnwrapService:
    """Exact two-phase RSA unwrap service; never accepts arbitrary ciphertext."""

    def __init__(
        self,
        *,
        config: UnwrapServiceConfig,
        authority: dict[str, Any],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_config(config)
        self._config = config
        self._authority = _validated_authority(deepcopy(authority))
        self._now = now or (lambda: datetime.now(UTC))
        _validate_private_key(config.private_key_path)
        public_key_sha256 = _public_key_sha256(
            openssl_path=config.openssl_path,
            private_key_path=config.private_key_path,
        )
        if public_key_sha256 != self._authority["wrapping_key_sha256"]:
            raise UnwrapServiceError(
                "canary unwrap private key does not match its authority"
            )
        self._responses: dict[str, bytes] = {}

    def handle(self, body: bytes) -> bytes:
        request = _validated_request(
            body,
            authority=self._authority,
            now=self._checked_now(),
        )
        request_sha256 = request["request_sha256"]
        cached = self._responses.get(request_sha256)
        if cached is not None:
            return cached
        if len(self._responses) >= 2:
            raise UnwrapServiceError("canary unwrap request set is exhausted")
        wrapped_data_key = base64.b64decode(
            request["wrapped_data_key_b64"],
            validate=True,
        )
        data_key = _decrypt_data_key(
            openssl_path=self._config.openssl_path,
            private_key_path=self._config.private_key_path,
            wrapped_data_key=wrapped_data_key,
            aad_sha256=request["aad_sha256"],
        )
        response = _canonical_json(
            {
                "data_key_b64": base64.b64encode(data_key).decode("ascii"),
                "expires_at": self._authority["ticket_deadline"],
                "request_sha256": request_sha256,
                "schema": _HELPER_RESPONSE_SCHEMA,
                "weight_eligible": False,
            },
            maximum_bytes=_MAX_RESPONSE_BYTES,
            label="canary unwrap response",
        )
        self._responses[request_sha256] = response
        return response

    def _checked_now(self) -> datetime:
        value = self._now()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise UnwrapServiceError("canary unwrap clock is invalid")
        return value.astimezone(UTC)

    def __repr__(self) -> str:
        return "CanaryUnwrapService(authorized_requests=2)"


def serve(
    config: UnwrapServiceConfig,
    *,
    stop: threading.Event | None = None,
    listener: socket.socket | None = None,
) -> None:
    authority = load_unwrap_authority(config.authority_path)
    service = CanaryUnwrapService(config=config, authority=authority)
    stop_event = threading.Event() if stop is None else stop
    owns_socket_path = listener is None
    resolved_listener = (
        _bind_socket(config.socket_path) if listener is None else listener
    )
    _validate_listener(resolved_listener, config.socket_path)
    try:
        resolved_listener.settimeout(1.0)
        while not stop_event.is_set():
            try:
                connection, _address = resolved_listener.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    connection.settimeout(config.socket_timeout_seconds)
                    _validate_client_peer(
                        connection,
                        expected_uid=config.expected_client_uid,
                        expected_gid=config.expected_client_gid,
                    )
                    request_size = struct.unpack(
                        ">Q", _receive_exact(connection, _HEADER_BYTES)
                    )[0]
                    if not 1 <= request_size <= _MAX_REQUEST_BYTES:
                        raise UnwrapServiceError(
                            "canary unwrap request frame exceeds its bound"
                        )
                    request = _receive_exact(connection, request_size)
                    response = service.handle(request)
                    connection.sendall(struct.pack(">Q", len(response)) + response)
                except (OSError, struct.error, UnwrapServiceError):
                    continue
    finally:
        resolved_listener.close()
        if owns_socket_path:
            _remove_owned_socket(config.socket_path)


def parse_config(
    environ: Mapping[str, str] | None = None,
) -> UnwrapServiceConfig | None:
    values = os.environ if environ is None else environ
    enabled = values.get("DITTO_HIPPIUS_CANARY_UNWRAP_ENABLED", "false").lower()
    if enabled in {"false", "0", "no", "off"}:
        return None
    if enabled not in {"true", "1", "yes", "on"}:
        raise UnwrapServiceError(
            "DITTO_HIPPIUS_CANARY_UNWRAP_ENABLED must be true or false"
        )

    def required_path(name: str) -> Path:
        raw = values.get(name, "")
        path = Path(raw)
        if not raw or not path.is_absolute():
            raise UnwrapServiceError(
                f"required canary unwrap path is missing or relative: {name}"
            )
        return path

    try:
        expected_client_uid = int(
            values["DITTO_HIPPIUS_CANARY_UNWRAP_EXPECTED_CLIENT_UID"]
        )
        expected_client_gid = int(
            values["DITTO_HIPPIUS_CANARY_UNWRAP_EXPECTED_CLIENT_GID"]
        )
        timeout_seconds = int(
            values.get("DITTO_HIPPIUS_CANARY_UNWRAP_SOCKET_TIMEOUT_SECONDS", "30")
        )
    except (KeyError, ValueError) as error:
        raise UnwrapServiceError(
            "canary unwrap peer or timeout setting is invalid"
        ) from error
    require_socket_activation = _parse_bool(
        values.get("DITTO_HIPPIUS_CANARY_UNWRAP_REQUIRE_SOCKET_ACTIVATION", "false"),
        label="canary unwrap socket activation",
    )
    config = UnwrapServiceConfig(
        socket_path=required_path("DITTO_HIPPIUS_CANARY_UNWRAP_SOCKET_PATH"),
        authority_path=required_path("DITTO_HIPPIUS_CANARY_UNWRAP_AUTHORITY_PATH"),
        private_key_path=required_path("DITTO_HIPPIUS_CANARY_UNWRAP_PRIVATE_KEY_PATH"),
        openssl_path=required_path("DITTO_HIPPIUS_CANARY_UNWRAP_OPENSSL_PATH"),
        expected_client_uid=expected_client_uid,
        expected_client_gid=expected_client_gid,
        socket_timeout_seconds=timeout_seconds,
        require_socket_activation=require_socket_activation,
    )
    _validate_config(config)
    return config


def load_unwrap_authority(path: Path) -> dict[str, Any]:
    body = _read_private_file(
        path,
        maximum_bytes=_MAX_AUTHORITY_BYTES,
        label="canary unwrap authority",
    )
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object)
        if (
            not isinstance(raw, dict)
            or _canonical_json(
                raw,
                maximum_bytes=_MAX_AUTHORITY_BYTES,
                label="canary unwrap authority",
            )
            != body
        ):
            raise ValueError("authority is not canonical")
        return _validated_authority(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise UnwrapServiceError("canary unwrap authority is invalid") from error


def _validated_authority(raw: dict[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "allowed_requests",
        "assignment_sha256",
        "authority_sha256",
        "catalog_commitment_sha256",
        "catalog_index",
        "coding_run_id",
        "publication_receipt_payload_sha256",
        "run_manifest_sha256",
        "run_row_id",
        "schema",
        "single_validator",
        "source_sha",
        "synthetic_only",
        "ticket_deadline",
        "ticket_id",
        "transport_manifest_sha256",
        "validator_hotkey",
        "weight_eligible",
        "wrapping_key_sha256",
    }
    if set(raw) != expected_fields:
        raise UnwrapServiceError("canary unwrap authority fields are invalid")
    projection = dict(raw)
    authority_sha256 = projection.pop("authority_sha256")
    allowed = raw["allowed_requests"]
    if (
        raw["schema"] != _AUTHORITY_SCHEMA
        or raw["synthetic_only"] is not True
        or raw["single_validator"] is not True
        or raw["weight_eligible"] is not False
        or not isinstance(authority_sha256, str)
        or _SHA256.fullmatch(authority_sha256) is None
        or hashlib.sha256(
            _canonical_json(
                projection,
                maximum_bytes=_MAX_AUTHORITY_BYTES,
                label="canary unwrap authority projection",
            )
        ).hexdigest()
        != authority_sha256
        or not isinstance(raw["source_sha"], str)
        or _SOURCE_SHA.fullmatch(raw["source_sha"]) is None
        or not _valid_uuid(raw["ticket_id"])
        or not _valid_uuid(raw["run_row_id"])
        or not isinstance(raw["validator_hotkey"], str)
        or _SS58.fullmatch(raw["validator_hotkey"]) is None
        or not _safe_scalar(raw["coding_run_id"], maximum_bytes=256)
        or not _valid_utc(raw["ticket_deadline"])
        or not _strict_int(raw["catalog_index"], minimum=0, maximum=999_999)
        or any(
            not isinstance(raw[field], str) or _SHA256.fullmatch(raw[field]) is None
            for field in (
                "assignment_sha256",
                "catalog_commitment_sha256",
                "publication_receipt_payload_sha256",
                "run_manifest_sha256",
                "transport_manifest_sha256",
                "wrapping_key_sha256",
            )
        )
        or not isinstance(allowed, list)
        or len(allowed) != 2
    ):
        raise UnwrapServiceError("canary unwrap authority is inconsistent")
    expected_phases = ["authoring", "grading"]
    for expected_phase, item in zip(expected_phases, allowed, strict=True):
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "aad_sha256",
                "ciphertext_sha256",
                "delivery_phase",
                "request_sha256",
                "wrapped_data_key_sha256",
            }
            or item["delivery_phase"] != expected_phase
            or any(
                not isinstance(item[field], str)
                or _SHA256.fullmatch(item[field]) is None
                for field in (
                    "aad_sha256",
                    "ciphertext_sha256",
                    "request_sha256",
                    "wrapped_data_key_sha256",
                )
            )
        ):
            raise UnwrapServiceError(
                "canary unwrap allowed-request set is inconsistent"
            )
    if any(
        allowed[0][field] != allowed[1][field]
        for field in (
            "aad_sha256",
            "ciphertext_sha256",
            "wrapped_data_key_sha256",
        )
    ):
        raise UnwrapServiceError(
            "canary unwrap phases do not bind one encrypted object"
        )
    return raw


def _validated_request(
    body: bytes,
    *,
    authority: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    expected_fields = {
        "aad_sha256",
        "assignment_sha256",
        "catalog_commitment_sha256",
        "catalog_index",
        "ciphertext_sha256",
        "coding_run_id",
        "delivery_phase",
        "publication_receipt_payload_sha256",
        "request_sha256",
        "run_manifest_sha256",
        "run_row_id",
        "schema",
        "ticket_deadline",
        "ticket_id",
        "transport_manifest_sha256",
        "validator_hotkey",
        "weight_eligible",
        "wrapped_data_key_b64",
        "wrapping_key_sha256",
    }
    try:
        request = json.loads(body, object_pairs_hook=_unique_object)
        if (
            not isinstance(request, dict)
            or set(request) != expected_fields
            or _canonical_json(
                request,
                maximum_bytes=_MAX_REQUEST_BYTES,
                label="canary unwrap request",
            )
            != body
            or request["schema"] != _HELPER_REQUEST_SCHEMA
            or request["weight_eligible"] is not False
            or request["delivery_phase"] not in {"authoring", "grading"}
        ):
            raise ValueError("request shape is invalid")
        wrapped = base64.b64decode(request["wrapped_data_key_b64"], validate=True)
        if not _MIN_WRAPPED_KEY_BYTES <= len(wrapped) <= _MAX_WRAPPED_KEY_BYTES:
            raise ValueError("wrapped key is invalid")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise UnwrapServiceError("canary unwrap request is invalid") from error
    for field in (
        "assignment_sha256",
        "catalog_commitment_sha256",
        "catalog_index",
        "coding_run_id",
        "publication_receipt_payload_sha256",
        "run_manifest_sha256",
        "run_row_id",
        "ticket_deadline",
        "ticket_id",
        "transport_manifest_sha256",
        "validator_hotkey",
        "wrapping_key_sha256",
    ):
        if request[field] != authority[field]:
            raise UnwrapServiceError("canary unwrap request authority drifted")
    deadline = _parse_utc(request["ticket_deadline"])
    if now >= deadline:
        raise UnwrapServiceError("canary unwrap authority expired")
    wrapped_sha256 = hashlib.sha256(wrapped).hexdigest()
    digest_projection = {
        "aad_sha256": request["aad_sha256"],
        "assignment_sha256": request["assignment_sha256"],
        "catalog_commitment_sha256": request["catalog_commitment_sha256"],
        "catalog_index": request["catalog_index"],
        "ciphertext_sha256": request["ciphertext_sha256"],
        "coding_run_id": request["coding_run_id"],
        "delivery_phase": request["delivery_phase"],
        "publication_receipt_payload_sha256": request[
            "publication_receipt_payload_sha256"
        ],
        "run_manifest_sha256": request["run_manifest_sha256"],
        "run_row_id": request["run_row_id"],
        "schema": _UNWRAP_REQUEST_SCHEMA,
        "ticket_deadline": request["ticket_deadline"],
        "ticket_id": request["ticket_id"],
        "transport_manifest_sha256": request["transport_manifest_sha256"],
        "validator_hotkey": request["validator_hotkey"],
        "weight_eligible": False,
        "wrapped_data_key_sha256": wrapped_sha256,
        "wrapping_key_sha256": request["wrapping_key_sha256"],
    }
    request_sha256 = hashlib.sha256(
        _canonical_json(
            digest_projection,
            maximum_bytes=_MAX_REQUEST_BYTES,
            label="private-input unwrap request",
        )
    ).hexdigest()
    if request_sha256 != request["request_sha256"]:
        raise UnwrapServiceError("canary unwrap request digest is invalid")
    allowed = next(
        (
            item
            for item in authority["allowed_requests"]
            if item["delivery_phase"] == request["delivery_phase"]
        ),
        None,
    )
    if (
        allowed is None
        or allowed["request_sha256"] != request_sha256
        or allowed["aad_sha256"] != request["aad_sha256"]
        or allowed["ciphertext_sha256"] != request["ciphertext_sha256"]
        or allowed["wrapped_data_key_sha256"] != wrapped_sha256
    ):
        raise UnwrapServiceError("canary unwrap request is not allowlisted")
    return request


def _decrypt_data_key(
    *,
    openssl_path: Path,
    private_key_path: Path,
    wrapped_data_key: bytes,
    aad_sha256: str,
) -> bytes:
    try:
        completed = subprocess.run(
            [
                str(openssl_path),
                "pkeyutl",
                "-decrypt",
                "-inkey",
                str(private_key_path),
                "-pkeyopt",
                "rsa_padding_mode:oaep",
                "-pkeyopt",
                "rsa_oaep_md:sha256",
                "-pkeyopt",
                "rsa_mgf1_md:sha256",
                "-pkeyopt",
                f"rsa_oaep_label:{aad_sha256}",
            ],
            input=wrapped_data_key,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_OPENSSL_ENV,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise UnwrapServiceError("canary data-key unwrap failed") from error
    if len(completed.stdout) != 32:
        raise UnwrapServiceError("canary data-key unwrap returned invalid bytes")
    return completed.stdout


def _public_key_sha256(*, openssl_path: Path, private_key_path: Path) -> str:
    try:
        completed = subprocess.run(
            [
                str(openssl_path),
                "pkey",
                "-in",
                str(private_key_path),
                "-pubout",
                "-outform",
                "DER",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_OPENSSL_ENV,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise UnwrapServiceError("canary unwrap key identity is unavailable") from error
    if not 1 <= len(completed.stdout) <= _MAX_KEY_BYTES:
        raise UnwrapServiceError("canary unwrap public key is invalid")
    return hashlib.sha256(completed.stdout).hexdigest()


def _validate_config(config: UnwrapServiceConfig) -> None:
    if (
        not all(
            path.is_absolute()
            for path in (
                config.socket_path,
                config.authority_path,
                config.private_key_path,
                config.openssl_path,
            )
        )
        or len(str(config.socket_path).encode()) > 100
        or not _strict_int(config.expected_client_uid, minimum=1)
        or not _strict_int(config.expected_client_gid, minimum=1)
        or not _strict_int(
            config.socket_timeout_seconds,
            minimum=1,
            maximum=300,
        )
        or not isinstance(config.require_socket_activation, bool)
    ):
        raise UnwrapServiceError("canary unwrap configuration is unsafe")
    try:
        openssl = config.openssl_path.stat()
    except OSError as error:
        raise UnwrapServiceError("canary unwrap OpenSSL is unavailable") from error
    if (
        not stat.S_ISREG(openssl.st_mode)
        or openssl.st_uid != 0
        or stat.S_IMODE(openssl.st_mode) & 0o022
        or not os.access(config.openssl_path, os.X_OK)
    ):
        raise UnwrapServiceError("canary unwrap OpenSSL authority is unsafe")


def _validate_private_key(path: Path) -> None:
    _read_private_file(path, maximum_bytes=_MAX_KEY_BYTES, label="canary unwrap key")


def _read_private_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise UnwrapServiceError(f"{label} is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_nlink != 1
            or not 1 <= info.st_size <= maximum_bytes
        ):
            raise UnwrapServiceError(f"{label} is unsafe")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    except OSError as error:
        raise UnwrapServiceError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not body or len(body) > maximum_bytes:
        raise UnwrapServiceError(f"{label} exceeds its bound")
    return bytes(body)


def _bind_socket(path: Path) -> socket.socket:
    parent = path.parent
    if not parent.is_absolute() or parent.is_symlink() or not parent.is_dir():
        raise UnwrapServiceError("canary unwrap socket directory is unsafe")
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o750:
        raise UnwrapServiceError("canary unwrap socket directory is unsafe")
    if path.exists() or path.is_symlink():
        _remove_owned_socket(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        created = path.lstat()
        if (
            not stat.S_ISSOCK(created.st_mode)
            or created.st_uid != os.getuid()
            or created.st_gid != os.getgid()
            or stat.S_IMODE(created.st_mode) != 0o660
        ):
            raise UnwrapServiceError("canary unwrap socket authority is unsafe")
        listener.listen(4)
    except Exception:
        listener.close()
        _remove_owned_socket(path)
        raise
    return listener


def _systemd_listener(config: UnwrapServiceConfig) -> socket.socket:
    try:
        listen_pid = int(os.environ.get("LISTEN_PID", "0"))
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError as error:
        raise UnwrapServiceError(
            "canary unwrap socket activation is invalid"
        ) from error
    if listen_pid != os.getpid() or listen_fds != 1:
        raise UnwrapServiceError("canary unwrap socket activation is unavailable")
    try:
        listener = socket.socket(fileno=3)
    except OSError as error:
        raise UnwrapServiceError("canary unwrap socket activation failed") from error
    try:
        _validate_listener(listener, config.socket_path)
    except Exception:
        listener.close()
        raise
    return listener


def _validate_listener(listener: socket.socket, expected_path: Path) -> None:
    try:
        socket_path = listener.getsockname()
        accepting = listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    except OSError as error:
        raise UnwrapServiceError("canary unwrap listener is invalid") from error
    if (
        listener.family != socket.AF_UNIX
        or str(socket_path) != str(expected_path)
        or accepting != 1
    ):
        raise UnwrapServiceError("canary unwrap listener authority is invalid")


def _remove_owned_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise UnwrapServiceError("canary unwrap socket state is unavailable") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise UnwrapServiceError("refusing to replace unsafe unwrap socket path")
    try:
        path.unlink()
    except OSError as error:
        raise UnwrapServiceError("canary unwrap socket cleanup failed") from error


def _validate_client_peer(
    connection: socket.socket,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if not hasattr(socket, "SO_PEERCRED"):
        raise UnwrapServiceError("canary unwrap peer credentials are unsupported")
    try:
        _pid, uid, gid = struct.unpack(
            "3i",
            connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
        )
    except (OSError, struct.error) as error:
        raise UnwrapServiceError(
            "canary unwrap peer credentials are unavailable"
        ) from error
    if uid != expected_uid or gid != expected_gid:
        raise UnwrapServiceError("canary unwrap client identity is inconsistent")


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = connection.recv(min(64 << 10, size - len(body)))
        if not chunk:
            raise UnwrapServiceError("canary unwrap request frame is incomplete")
        body.extend(chunk)
    return bytes(body)


def _canonical_json(
    value: dict[str, Any],
    *,
    maximum_bytes: int,
    label: str,
) -> bytes:
    try:
        body = (
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
        raise UnwrapServiceError(f"canonical {label} is invalid") from error
    if len(body) > maximum_bytes:
        raise UnwrapServiceError(f"canonical {label} exceeds its bound")
    return body


def _strict_int(value: object, *, minimum: int, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _safe_scalar(value: object, *, maximum_bytes: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode()) <= maximum_bytes
        and all(
            character.isprintable() and not character.isspace() for character in value
        )
    )


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value and UUID(value).int != 0
    except ValueError:
        return False


def _valid_utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _parse_utc(value)
    except ValueError:
        return False
    return True


def _parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp is not UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(UTC)


def _parse_bool(value: str, *, label: str) -> bool:
    lowered = value.lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise UnwrapServiceError(f"{label} must be true or false")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def main() -> int:
    try:
        config = parse_config()
        if config is None:
            raise UnwrapServiceError("canary unwrap service is disabled")
        stop = threading.Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        listener = (
            _systemd_listener(config) if config.require_socket_activation else None
        )
        serve(config, stop=stop, listener=listener)
    except UnwrapServiceError:
        print("Hippius canary unwrap service failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
