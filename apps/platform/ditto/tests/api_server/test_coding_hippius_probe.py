from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_PROBE_CONFIRMATION,
    HIPPIUS_REVIEWED_REVISION,
    AiobotoHippiusProbeTransport,
    HippiusCredentialRole,
    HippiusProbeAccessDenied,
    HippiusProbeCheck,
    HippiusProbeCheckStatus,
    HippiusProbeConfig,
    HippiusProbeConfigurationError,
    HippiusProbeCredential,
    HippiusProbeHttpResponse,
    HippiusProbeNotFound,
    HippiusProbeObjectMetadata,
    HippiusProbeReceipt,
    HippiusProbeReceiptError,
    hippius_private_input_authority_sha256,
    load_hippius_probe_receipt,
    parse_hippius_probe_config,
    run_hippius_capability_probe,
    write_hippius_probe_receipt,
)

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]


def _load_probe_script() -> ModuleType:
    path = _PLATFORM_ROOT / "scripts/probe_hippius_coding_storage.py"
    spec = importlib.util.spec_from_file_location("probe_hippius_coding_storage", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _credential(name: str) -> HippiusProbeCredential:
    return HippiusProbeCredential(
        access_key=f"hip_{name}_access", secret_key=f"{name}-secret"
    )


def _config(**overrides: object) -> HippiusProbeConfig:
    values: dict[str, object] = {
        "endpoint_url": "https://s3.hippius.com",
        "private_input_bucket": "coding-private-inputs",
        "sealed_evidence_bucket": "coding-sealed-evidence",
        "private_input_curator": _credential("curator"),
        "private_input_reader": _credential("reader"),
        "evidence_mediator": _credential("evidence"),
        "region": "decentralized",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return HippiusProbeConfig(**values)  # type: ignore[arg-type]


class _FakeTransport:
    def __init__(self, *, reader_write_allowed: bool = False) -> None:
        self.reader_write_allowed = reader_write_allowed
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.presigned: dict[str, tuple[str, str, bytes]] = {}
        self.expired = False

    def _allows(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        operation: str,
    ) -> bool:
        if role is None:
            return False
        if role is HippiusCredentialRole.PRIVATE_INPUT_CURATOR:
            return bucket == "coding-private-inputs"
        if role is HippiusCredentialRole.PRIVATE_INPUT_READER:
            if bucket != "coding-private-inputs":
                return False
            return operation in {"get", "head", "list"} or (
                operation == "put" and self.reader_write_allowed
            )
        return bucket == "coding-sealed-evidence" and operation in {
            "put",
            "get",
            "head",
            "list",
            "delete",
        }

    async def put_object(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        if not self._allows(role=role, bucket=bucket, operation="put"):
            raise HippiusProbeAccessDenied("denied")
        self.objects[(bucket, key)] = (body, dict(metadata))

    async def get_object(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> bytes:
        if not self._allows(role=role, bucket=bucket, operation="get"):
            raise HippiusProbeAccessDenied("denied")
        try:
            body = self.objects[(bucket, key)][0]
        except KeyError as error:
            raise HippiusProbeNotFound("missing") from error
        if len(body) > max_bytes:
            raise AssertionError("fake object exceeded bound")
        return body

    async def head_object(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        key: str,
    ) -> HippiusProbeObjectMetadata:
        if not self._allows(role=role, bucket=bucket, operation="head"):
            raise HippiusProbeAccessDenied("denied")
        try:
            body, metadata = self.objects[(bucket, key)]
        except KeyError as error:
            raise HippiusProbeNotFound("missing") from error
        return HippiusProbeObjectMetadata(size_bytes=len(body), metadata=metadata)

    async def list_prefix(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        prefix: str,
    ) -> None:
        del prefix
        if not self._allows(role=role, bucket=bucket, operation="list"):
            raise HippiusProbeAccessDenied("denied")

    async def delete_object(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
    ) -> None:
        if not self._allows(role=role, bucket=bucket, operation="delete"):
            raise HippiusProbeAccessDenied("denied")
        self.objects.pop((bucket, key), None)

    async def presigned_get_url(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
        expires_in: int,
    ) -> str:
        if not self._allows(role=role, bucket=bucket, operation="get"):
            raise HippiusProbeAccessDenied("denied")
        body = self.objects[(bucket, key)][0]
        token = str(len(self.presigned) + 1)
        url = (
            f"https://s3.hippius.com/presigned/{token}"
            f"?X-Amz-Expires={expires_in}&X-Amz-Signature={'a' * 64}"
        )
        self.presigned[url] = ("GET", key, body)
        return url

    async def request_presigned(
        self, *, method: str, url: str
    ) -> HippiusProbeHttpResponse:
        if self.expired:
            raise HippiusProbeAccessDenied("expired")
        try:
            expected_method, _key, body = self.presigned[url]
        except KeyError as error:
            raise HippiusProbeAccessDenied("tampered") from error
        if method != expected_method:
            raise HippiusProbeAccessDenied("wrong method")
        return HippiusProbeHttpResponse(status_code=200, body=body)


def _entropy() -> Callable[[int], bytes]:
    counter = 0

    def generate(size: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter % 251]) * size

    return generate


async def _run_probe(
    transport: _FakeTransport,
):
    async def expire(_seconds: float) -> None:
        transport.expired = True

    return await run_hippius_capability_probe(
        config=_config(),
        transport=transport,
        source_sha="a" * 40,
        now=lambda: datetime(2026, 9, 2, 15, 0, tzinfo=UTC),
        synthetic_bytes=_entropy(),
        sleep=expire,
    )


async def test_probe_accepts_expected_hippius_scope_and_records_list_behavior():
    receipt = await _run_probe(_FakeTransport())

    assert receipt.ready is True
    assert receipt.synthetic_only is True
    assert receipt.retained_synthetic_objects == 2
    assert receipt.weight_eligible is False
    assert receipt.reviewed_hippius_revision == HIPPIUS_REVIEWED_REVISION
    assert receipt.private_input_authority_sha256 == (
        hippius_private_input_authority_sha256(
            endpoint_url="https://s3.hippius.com",
            region="decentralized",
            bucket="coding-private-inputs",
            curator_access_key="hip_curator_access",
            reader_access_key="hip_reader_access",
        )
    )
    checks = {check.name: check for check in receipt.checks}
    assert checks["private_input_reader_list_scope"].status is (
        HippiusProbeCheckStatus.OBSERVED
    )
    assert checks["private_input_reader_list_scope"].detail == "allowed"
    assert checks["evidence_mediator_list_scope"].detail == "allowed"
    assert (
        checks["private_input_curator_cross_bucket_write_denied"].status
        is HippiusProbeCheckStatus.PASS
    )
    assert (
        checks["evidence_mediator_cross_bucket_write_denied"].status
        is HippiusProbeCheckStatus.PASS
    )
    assert all(
        check.status is not HippiusProbeCheckStatus.FAIL for check in checks.values()
    )


async def test_probe_fails_if_reader_can_write():
    receipt = await _run_probe(_FakeTransport(reader_write_allowed=True))

    assert receipt.ready is False
    assert receipt.retained_synthetic_objects == 3
    checks = {check.name: check for check in receipt.checks}
    assert checks["private_input_reader_write_denied"].status is (
        HippiusProbeCheckStatus.FAIL
    )
    assert checks["private_input_reader_write_denied"].detail == "allowed"


async def test_live_adapter_pins_path_style_and_rejects_wrong_origin_locally():
    async with AiobotoHippiusProbeTransport(_config()) as transport:
        assert transport._signed_config.s3["addressing_style"] == "path"  # type: ignore[attr-defined]
        with pytest.raises(HippiusProbeAccessDenied):
            await transport.request_presigned(
                method="GET",
                url=("https://not-hippius.invalid/object?X-Amz-Signature=synthetic"),
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint_url": "http://s3.hippius.com"},
        {"endpoint_url": "https://example.com"},
        {"endpoint_url": "https://s3.hippius.com/path"},
        {"region": "us-east-1"},
        {"sealed_evidence_bucket": "coding-private-inputs"},
        {"private_input_reader": _credential("curator")},
    ],
)
def test_probe_config_rejects_unsafe_or_shared_authority(overrides: dict[str, object]):
    with pytest.raises(HippiusProbeConfigurationError):
        _config(**overrides)


def test_probe_config_repr_and_errors_do_not_expose_authority():
    config = _config()

    rendered = repr(config)
    for sensitive in (
        config.endpoint_url,
        config.private_input_bucket,
        config.sealed_evidence_bucket,
        config.private_input_curator.access_key,
        config.private_input_curator.secret_key,
    ):
        assert sensitive not in rendered

    with pytest.raises(HippiusProbeConfigurationError) as caught:
        parse_hippius_probe_config({})
    assert "PRIVATE_INPUT_BUCKET" not in str(caught.value)
    assert "ENDPOINT_URL" in str(caught.value)


def test_parse_probe_config_accepts_only_environment_credentials():
    config = parse_hippius_probe_config(
        {
            "DITTO_CODING_HIPPIUS_ENDPOINT_URL": "https://s3.hippius.com",
            "DITTO_CODING_HIPPIUS_REGION": "decentralized",
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET": "coding-private-inputs",
            "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": "coding-sealed-evidence",
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": "hip_curator",
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY": "curator-key",
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": "hip_reader",
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": "reader-key",
            "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": "hip_evidence",
            "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": "evidence-key",
        }
    )

    assert config.region == "decentralized"
    assert config.timeout_seconds == 10.0
    assert config.private_input_reader.access_key == "hip_reader"


async def test_receipt_is_exclusive_mode_0600_and_redacted(
    tmp_path: Path,
):
    config = _config()
    receipt = await _run_probe(_FakeTransport())
    output = (tmp_path / "receipt.json").resolve()
    previous_umask = os.umask(0o002)
    try:
        payload_sha256 = write_hippius_probe_receipt(receipt=receipt, output=output)
    finally:
        os.umask(previous_umask)

    document = json.loads(output.read_text())
    assert len(payload_sha256) == 64
    assert document["receipt_payload_sha256"] == payload_sha256
    payload = {
        key: value for key, value in document.items() if key != "receipt_payload_sha256"
    }
    expected_payload_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert payload_sha256 == expected_payload_sha256
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    loaded, loaded_sha256 = load_hippius_probe_receipt(output)
    assert loaded == receipt
    assert loaded_sha256 == payload_sha256
    raw = output.read_text()
    for sensitive in (
        config.endpoint_url,
        config.private_input_bucket,
        config.sealed_evidence_bucket,
        config.private_input_curator.access_key,
        config.private_input_curator.secret_key,
        config.private_input_reader.access_key,
        config.private_input_reader.secret_key,
        config.evidence_mediator.access_key,
        config.evidence_mediator.secret_key,
        "X-Amz-Signature",
        "coding-capability-probe/v1",
    ):
        assert sensitive not in raw
    with pytest.raises(HippiusProbeReceiptError):
        write_hippius_probe_receipt(receipt=receipt, output=output)


def test_receipt_requires_absolute_path(tmp_path: Path):
    del tmp_path
    receipt = HippiusProbeReceipt(
        schema="dittobench-coding-hippius-capability-probe-v1",
        source_sha="a" * 40,
        reviewed_hippius_revision=HIPPIUS_REVIEWED_REVISION,
        checked_at="2026-09-02T15:00:00Z",
        provider="hippius",
        private_input_authority_sha256="b" * 64,
        sealed_evidence_authority_sha256="c" * 64,
        synthetic_only=True,
        retained_synthetic_objects=0,
        ready=False,
        weight_eligible=False,
        checks=(
            HippiusProbeCheck(
                name="synthetic",
                status=HippiusProbeCheckStatus.FAIL,
                detail="not_run",
            ),
        ),
    )
    with pytest.raises(HippiusProbeReceiptError):
        write_hippius_probe_receipt(receipt=receipt, output=Path("receipt.json"))


def test_cli_rejects_inexact_confirmation_before_reading_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    output = (tmp_path / "receipt.json").resolve()
    script = _load_probe_script()
    with pytest.raises(SystemExit) as caught:
        script.main(
            [
                "--confirm",
                "PROBE HIPPIUS STORAGE",
                "--output",
                str(output),
            ]
        )

    assert caught.value.code == 2
    assert HIPPIUS_PROBE_CONFIRMATION in capsys.readouterr().err
    assert not output.exists()


def test_cli_exact_confirmation_fails_safely_without_secret_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    output = (tmp_path / "receipt.json").resolve()
    script = _load_probe_script()
    for name in tuple(os.environ):
        if name.startswith("DITTO_CODING_HIPPIUS_"):
            monkeypatch.delenv(name)
    result = script.main(
        [
            "--confirm",
            HIPPIUS_PROBE_CONFIRMATION,
            "--output",
            str(output),
        ]
    )

    assert result == 2
    stderr = capsys.readouterr().err
    assert "required Hippius probe setting is missing" in stderr
    assert "Traceback" not in stderr
    assert not output.exists()
