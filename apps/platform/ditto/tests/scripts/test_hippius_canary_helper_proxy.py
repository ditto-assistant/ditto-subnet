from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import struct
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[5]
SCRIPT = ROOT / "apps/platform/scripts/hippius_canary_helper_proxy.py"
SPEC = importlib.util.spec_from_file_location("hippius_canary_helper_proxy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
proxy: ModuleType = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)

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


def _canonical(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _write_config(
    path: Path,
    *,
    role: str,
    socket_path: Path,
    uid: int | None = None,
    gid: int | None = None,
    max_response_bytes: int = 4096,
) -> Path:
    path.write_bytes(
        _canonical(
            {
                "expected_peer_gid": os.getgid() if gid is None else gid,
                "expected_peer_uid": os.getuid() if uid is None else uid,
                "max_request_bytes": 4096,
                "max_response_bytes": max_response_bytes,
                "role": role,
                "schema": ("dittobench-coding-hippius-canary-helper-proxy-config-v1"),
                "socket_path": str(socket_path),
                "timeout_seconds": 5,
            }
        )
    )
    path.chmod(0o600)
    return path.resolve()


def _serve_once(
    socket_path: Path,
    *,
    response: bytes,
    framed_size: int | None = None,
) -> tuple[threading.Thread, list[bytes]]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o660)
    listener.listen(1)
    observed: list[bytes] = []

    def serve() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                header = _receive(connection, 8)
                size = struct.unpack(">Q", header)[0]
                observed.append(_receive(connection, size))
                response_size = len(response) if framed_size is None else framed_size
                connection.sendall(struct.pack(">Q", response_size))
                if framed_size is None:
                    connection.sendall(response)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, observed


def _receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("test socket closed early")
        result.extend(chunk)
    return bytes(result)


@pytest.mark.parametrize("role", ["unwrap", "authoring", "grading"])
def test_proxy_forwards_one_canonical_frame_to_exact_peer(
    tmp_path: Path,
    role: str,
) -> None:
    socket_path = tmp_path / f"{role}.sock"
    response = _canonical(
        {
            "ready": True,
            "schema": _RESPONSE_SCHEMA[role],
            "weight_eligible": False,
        }
    )
    thread, observed = _serve_once(socket_path, response=response)
    config = _write_config(
        tmp_path / f"{role}.json",
        role=role,
        socket_path=socket_path,
    )
    request = _canonical(
        {
            "schema": _REQUEST_SCHEMA[role],
            "synthetic_only": True,
            "weight_eligible": False,
        }
    )

    assert proxy.proxy_request(role=role, config_path=config, body=request) == response
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert observed == [request]


def test_proxy_rejects_socket_authority_and_noncanonical_input(tmp_path: Path) -> None:
    socket_path = tmp_path / "unwrap.sock"
    response = _canonical({"schema": _RESPONSE_SCHEMA["unwrap"]})
    thread, _observed = _serve_once(socket_path, response=response)
    config = _write_config(
        tmp_path / "unwrap.json",
        role="unwrap",
        socket_path=socket_path,
        uid=os.getuid() + 1,
    )
    request = _canonical({"schema": _REQUEST_SCHEMA["unwrap"]})
    with pytest.raises(proxy.HelperProxyError, match="authority"):
        proxy.proxy_request(role="unwrap", config_path=config, body=request)
    socket_path.chmod(0o600)
    config = _write_config(
        tmp_path / "unwrap-mode.json",
        role="unwrap",
        socket_path=socket_path,
    )
    with pytest.raises(proxy.HelperProxyError, match="authority"):
        proxy.proxy_request(role="unwrap", config_path=config, body=request)
    with pytest.raises(proxy.HelperProxyError, match="request"):
        proxy.proxy_request(
            role="unwrap",
            config_path=config,
            body=request.rstrip(b"\n"),
        )
    socket_path.chmod(0o660)
    assert (
        proxy.proxy_request(role="unwrap", config_path=config, body=request) == response
    )
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_proxy_rejects_oversized_response_frame(tmp_path: Path) -> None:
    socket_path = tmp_path / "grading.sock"
    thread, _observed = _serve_once(
        socket_path,
        response=b"",
        framed_size=4097,
    )
    config = _write_config(
        tmp_path / "grading.json",
        role="grading",
        socket_path=socket_path,
        max_response_bytes=4096,
    )
    request = _canonical({"schema": _REQUEST_SCHEMA["grading"]})
    with pytest.raises(proxy.HelperProxyError, match="bound"):
        proxy.proxy_request(role="grading", config_path=config, body=request)
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_proxy_main_uses_installed_name_and_redacts_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    socket_path = tmp_path / "authoring.sock"
    response = _canonical({"schema": _RESPONSE_SCHEMA["authoring"]})
    thread, _observed = _serve_once(socket_path, response=response)
    _write_config(
        config_root / "authoring.json",
        role="authoring",
        socket_path=socket_path,
    )
    request = _canonical({"schema": _REQUEST_SCHEMA["authoring"]})
    output = io.BytesIO()
    assert (
        proxy.main(
            [],
            program_name="hippius-canary-authoring",
            config_root=config_root,
            stdin=io.BytesIO(request),
            stdout=output,
        )
        == 0
    )
    assert output.getvalue() == response
    thread.join(timeout=5)

    assert (
        proxy.main(
            [],
            program_name="wrong-name",
            config_root=config_root,
            stdin=io.BytesIO(request),
            stdout=io.BytesIO(),
        )
        == 2
    )
    assert "invalid" in capsys.readouterr().err


def test_proxy_configuration_is_owner_only_canonical_and_role_bound(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "unused.sock"
    config = _write_config(
        tmp_path / "unwrap.json",
        role="unwrap",
        socket_path=socket_path,
    )
    config.chmod(0o644)
    with pytest.raises(proxy.HelperProxyError, match="unsafe"):
        proxy.proxy_request(
            role="unwrap",
            config_path=config,
            body=_canonical({"schema": _REQUEST_SCHEMA["unwrap"]}),
        )
    config.chmod(0o600)
    with pytest.raises(proxy.HelperProxyError, match="role"):
        proxy.proxy_request(
            role="grading",
            config_path=config,
            body=_canonical({"schema": _REQUEST_SCHEMA["grading"]}),
        )
