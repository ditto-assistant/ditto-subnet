from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).parents[5]
SERVICE_PATH = ROOT / "apps/platform/scripts/hippius_canary_unwrap_service.py"
PROXY_PATH = ROOT / "apps/platform/scripts/hippius_canary_helper_proxy.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


service = _load(SERVICE_PATH, "hippius_canary_unwrap_service")
proxy = _load(PROXY_PATH, "hippius_canary_helper_proxy_for_unwrap")

_NOW = datetime.now(UTC).replace(microsecond=0)
_DEADLINE = _NOW + timedelta(hours=1)
_DATA_KEY = b"k" * 32
_VALIDATOR = "5" + "A" * 47


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


def _private_key(tmp_path: Path) -> tuple[rsa.RSAPrivateKey, Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    private = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
    path = tmp_path / "unwrap-private.pem"
    path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o400)
    public_der = private.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, path.resolve(), hashlib.sha256(public_der).hexdigest()


def _request(
    private: rsa.RSAPrivateKey,
    *,
    phase: str,
    wrapping_key_sha256: str,
    wrapped_data_key: bytes | None = None,
    aad_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_aad = (
        hashlib.sha256(b"one synthetic object").hexdigest()
        if aad_sha256 is None
        else aad_sha256
    )
    wrapped = wrapped_data_key
    if wrapped is None:
        wrapped = private.public_key().encrypt(
            _DATA_KEY,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=bytes.fromhex(resolved_aad),
            ),
        )
    static = {
        "assignment_sha256": "1" * 64,
        "catalog_commitment_sha256": "2" * 64,
        "catalog_index": 0,
        "ciphertext_sha256": "3" * 64,
        "coding_run_id": "hippius-canary-run-001",
        "delivery_phase": phase,
        "publication_receipt_payload_sha256": "4" * 64,
        "run_manifest_sha256": "5" * 64,
        "run_row_id": "22222222-2222-4222-8222-222222222222",
        "ticket_deadline": _DEADLINE.isoformat().replace("+00:00", "Z"),
        "ticket_id": "11111111-1111-4111-8111-111111111111",
        "transport_manifest_sha256": "6" * 64,
        "validator_hotkey": _VALIDATOR,
        "weight_eligible": False,
        "wrapping_key_sha256": wrapping_key_sha256,
    }
    wrapped_sha256 = hashlib.sha256(wrapped).hexdigest()
    digest_projection = {
        **static,
        "aad_sha256": resolved_aad,
        "schema": "dittobench-coding-hippius-private-input-unwrap-v1",
        "wrapped_data_key_sha256": wrapped_sha256,
    }
    request_sha256 = hashlib.sha256(_canonical(digest_projection)).hexdigest()
    request = {
        **static,
        "aad_sha256": resolved_aad,
        "request_sha256": request_sha256,
        "schema": "dittobench-coding-hippius-canary-unwrap-helper-request-v1",
        "wrapped_data_key_b64": base64.b64encode(wrapped).decode("ascii"),
    }
    allowed = {
        "aad_sha256": resolved_aad,
        "ciphertext_sha256": static["ciphertext_sha256"],
        "delivery_phase": phase,
        "request_sha256": request_sha256,
        "wrapped_data_key_sha256": wrapped_sha256,
    }
    return request, allowed


def _authority(
    private: rsa.RSAPrivateKey,
    *,
    wrapping_key_sha256: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    requests: dict[str, dict[str, Any]] = {}
    allowed: list[dict[str, Any]] = []
    authoring, authoring_entry = _request(
        private,
        phase="authoring",
        wrapping_key_sha256=wrapping_key_sha256,
    )
    grading, grading_entry = _request(
        private,
        phase="grading",
        wrapping_key_sha256=wrapping_key_sha256,
        wrapped_data_key=base64.b64decode(authoring["wrapped_data_key_b64"]),
        aad_sha256=authoring["aad_sha256"],
    )
    requests.update(authoring=authoring, grading=grading)
    allowed.extend((authoring_entry, grading_entry))
    first = requests["authoring"]
    projection = {
        "allowed_requests": allowed,
        "assignment_sha256": first["assignment_sha256"],
        "catalog_commitment_sha256": first["catalog_commitment_sha256"],
        "catalog_index": first["catalog_index"],
        "coding_run_id": first["coding_run_id"],
        "publication_receipt_payload_sha256": first[
            "publication_receipt_payload_sha256"
        ],
        "run_manifest_sha256": first["run_manifest_sha256"],
        "run_row_id": first["run_row_id"],
        "schema": "dittobench-coding-hippius-canary-unwrap-authority-v1",
        "single_validator": True,
        "source_sha": "a" * 40,
        "synthetic_only": True,
        "ticket_deadline": first["ticket_deadline"],
        "ticket_id": first["ticket_id"],
        "transport_manifest_sha256": first["transport_manifest_sha256"],
        "validator_hotkey": first["validator_hotkey"],
        "weight_eligible": False,
        "wrapping_key_sha256": wrapping_key_sha256,
    }
    authority_sha256 = hashlib.sha256(_canonical(projection)).hexdigest()
    return {**projection, "authority_sha256": authority_sha256}, requests


def _write_authority(path: Path, authority: dict[str, Any]) -> Path:
    path.write_bytes(_canonical(authority))
    path.chmod(0o400)
    return path.resolve()


def _config(
    tmp_path: Path,
    *,
    authority_path: Path,
    private_key_path: Path,
) -> Any:
    openssl = shutil.which("openssl")
    assert openssl is not None
    socket_root = tmp_path / "socket"
    socket_root.mkdir(exist_ok=True)
    socket_root.chmod(0o750)
    return service.UnwrapServiceConfig(
        socket_path=(socket_root / "unwrap.sock").resolve(),
        authority_path=authority_path,
        private_key_path=private_key_path,
        openssl_path=Path(openssl).resolve(),
        expected_client_uid=os.getuid(),
        expected_client_gid=os.getgid(),
        socket_timeout_seconds=5,
    )


def _fixture(tmp_path: Path) -> tuple[Any, dict[str, Any], dict[str, dict[str, Any]]]:
    private, private_path, public_sha256 = _private_key(tmp_path)
    authority, requests = _authority(private, wrapping_key_sha256=public_sha256)
    authority_path = _write_authority(tmp_path / "authority.json", authority)
    config = _config(
        tmp_path,
        authority_path=authority_path,
        private_key_path=private_path,
    )
    return config, authority, requests


def test_service_unwraps_only_two_exact_requests_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    config, authority, requests = _fixture(tmp_path)
    unwrap = service.CanaryUnwrapService(
        config=config,
        authority=authority,
        now=lambda: _NOW,
    )

    first = unwrap.handle(_canonical(requests["authoring"]))
    replay = unwrap.handle(_canonical(requests["authoring"]))
    second = unwrap.handle(_canonical(requests["grading"]))

    assert replay == first
    for body, phase in ((first, "authoring"), (second, "grading")):
        response = json.loads(body)
        assert base64.b64decode(response["data_key_b64"]) == _DATA_KEY
        assert response["request_sha256"] == requests[phase]["request_sha256"]
        assert response["expires_at"] == authority["ticket_deadline"]
        assert response["weight_eligible"] is False
    assert "private" not in repr(unwrap).lower()


@pytest.mark.parametrize(
    "mutation",
    ["ticket", "digest", "wrapped", "aad", "ciphertext", "phase"],
)
def test_service_rejects_authority_or_allowlist_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    config, authority, requests = _fixture(tmp_path)
    unwrap = service.CanaryUnwrapService(
        config=config,
        authority=authority,
        now=lambda: _NOW,
    )
    request = deepcopy(requests["authoring"])
    if mutation == "ticket":
        request["ticket_id"] = "33333333-3333-4333-8333-333333333333"
    elif mutation == "digest":
        request["request_sha256"] = "f" * 64
    elif mutation == "wrapped":
        wrapped = base64.b64decode(request["wrapped_data_key_b64"])
        request["wrapped_data_key_b64"] = base64.b64encode(
            bytes([wrapped[0] ^ 1]) + wrapped[1:]
        ).decode()
    elif mutation == "aad":
        request["aad_sha256"] = "f" * 64
    elif mutation == "ciphertext":
        request["ciphertext_sha256"] = "f" * 64
    elif mutation == "phase":
        request["delivery_phase"] = "grading"

    with pytest.raises(service.UnwrapServiceError):
        unwrap.handle(_canonical(request))


def test_authority_file_and_private_key_are_exact_owner_only_inputs(
    tmp_path: Path,
) -> None:
    config, authority, _requests = _fixture(tmp_path)
    assert service.load_unwrap_authority(config.authority_path) == authority
    config.authority_path.chmod(0o600)
    with pytest.raises(service.UnwrapServiceError, match="unsafe"):
        service.load_unwrap_authority(config.authority_path)
    config.authority_path.chmod(0o400)
    forged = deepcopy(authority)
    forged["source_sha"] = "b" * 40
    forged_path = _write_authority(tmp_path / "forged.json", forged)
    with pytest.raises(service.UnwrapServiceError, match="authority"):
        service.load_unwrap_authority(forged_path)

    private, other_key, _digest = _private_key(tmp_path / "other")
    assert private.key_size == 3072
    drifted = service.UnwrapServiceConfig(
        **{**config.__dict__, "private_key_path": other_key}
    )
    with pytest.raises(service.UnwrapServiceError, match="does not match"):
        service.CanaryUnwrapService(
            config=drifted,
            authority=authority,
            now=lambda: _NOW,
        )


def test_service_expires_exact_authority(tmp_path: Path) -> None:
    config, authority, requests = _fixture(tmp_path)
    unwrap = service.CanaryUnwrapService(
        config=config,
        authority=authority,
        now=lambda: _DEADLINE,
    )
    with pytest.raises(service.UnwrapServiceError, match="expired"):
        unwrap.handle(_canonical(requests["authoring"]))


def test_config_is_default_off_and_fail_closed(tmp_path: Path) -> None:
    assert service.parse_config({}) is None
    with pytest.raises(service.UnwrapServiceError, match="true or false"):
        service.parse_config({"DITTO_HIPPIUS_CANARY_UNWRAP_ENABLED": "maybe"})
    config, _authority, _requests = _fixture(tmp_path)
    values = {
        "DITTO_HIPPIUS_CANARY_UNWRAP_ENABLED": "true",
        "DITTO_HIPPIUS_CANARY_UNWRAP_SOCKET_PATH": str(config.socket_path),
        "DITTO_HIPPIUS_CANARY_UNWRAP_AUTHORITY_PATH": str(config.authority_path),
        "DITTO_HIPPIUS_CANARY_UNWRAP_PRIVATE_KEY_PATH": str(config.private_key_path),
        "DITTO_HIPPIUS_CANARY_UNWRAP_OPENSSL_PATH": str(config.openssl_path),
        "DITTO_HIPPIUS_CANARY_UNWRAP_EXPECTED_CLIENT_UID": str(os.getuid()),
        "DITTO_HIPPIUS_CANARY_UNWRAP_EXPECTED_CLIENT_GID": str(os.getgid()),
        "DITTO_HIPPIUS_CANARY_UNWRAP_SOCKET_TIMEOUT_SECONDS": "5",
    }
    assert service.parse_config(values) == config
    assert "private" not in repr(config).lower()


def test_socket_service_interoperates_with_protected_proxy(tmp_path: Path) -> None:
    config, _authority, requests = _fixture(tmp_path)
    stop = threading.Event()
    thread = threading.Thread(
        target=service.serve,
        kwargs={"config": config, "stop": stop},
        daemon=True,
    )
    thread.start()
    for _attempt in range(100):
        if config.socket_path.exists():
            break
        time.sleep(0.01)
    assert config.socket_path.exists()
    proxy_config = tmp_path / "proxy.json"
    proxy_config.write_bytes(
        _canonical(
            {
                "expected_peer_gid": os.getgid(),
                "expected_peer_uid": os.getuid(),
                "max_request_bytes": 65536,
                "max_response_bytes": 65536,
                "role": "unwrap",
                "schema": ("dittobench-coding-hippius-canary-helper-proxy-config-v2"),
                "socket_gid": os.getgid(),
                "socket_path": str(config.socket_path),
                "timeout_seconds": 5,
            }
        )
    )
    proxy_config.chmod(0o600)

    response = proxy.proxy_request(
        role="unwrap",
        config_path=proxy_config.resolve(),
        body=_canonical(requests["authoring"]),
    )
    assert base64.b64decode(json.loads(response)["data_key_b64"]) == _DATA_KEY
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not config.socket_path.exists()
