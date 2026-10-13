"""``ditto upload --track reranker`` (Season 2 shadow intake, SN-207).

The wallet and HTTP layers are replaced, so these pin argument parsing, track
selection, local artifact validation, the signed payload, and the request
shape without any network or chain access.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from dittobench_reranker.artifact import artifact_digest, pack
from dittobench_reranker.contract import CONTRACT_ID

from ditto.miner_cli.__main__ import _build_parser
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands import upload as upload_cmd
from ditto.miner_cli.errors import (
    RerankerArtifactError,
    RerankerUploadRejectedError,
    SubmissionCooldownError,
    TransientApiError,
)
from ditto.miner_cli.models import RerankerUploadResult, WalletHandle
from ditto.miner_cli.reranker import prepare_reranker_artifact
from ditto.miner_cli.signing import (
    build_reranker_upload_payload,
    build_upload_payload,
    sign_reranker_upload_payload,
)

_ALICE = bittensor.Keypair.create_from_uri("//Alice")
_MODEL = b"model bytes (unchecked without onnx)"


def _vocab() -> bytes:
    tokens = ["[PAD]"] + [f"[unused{i}]" for i in range(1, 100)]
    tokens += ["[UNK]", "[CLS]", "[SEP]", "needle"]
    return ("\n".join(tokens) + "\n").encode()


def _manifest(**overrides: object) -> bytes:
    doc: dict[str, object] = {
        "contract": CONTRACT_ID,
        "base_model": "none",
        "base_model_license": "apache-2.0",
    }
    doc.update(overrides)
    return json.dumps(doc).encode()


def _artifact_dir(root: Path, *, manifest: bytes | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.onnx").write_bytes(_MODEL)
    (root / "vocab.txt").write_bytes(_vocab())
    (root / "manifest.json").write_bytes(manifest or _manifest())
    return root


@pytest.fixture(autouse=True)
def _no_onnx(monkeypatch: pytest.MonkeyPatch) -> None:
    # The base miner install does not carry onnx; pin that path by default.
    monkeypatch.setattr("ditto.miner_cli.reranker._onnx_available", lambda: False)


def _handle() -> WalletHandle:
    return WalletHandle(
        coldkey_name="miner", hotkey_name="default", hotkey_ss58=_ALICE.ss58_address
    )


def _wallet() -> MagicMock:
    wallet = MagicMock()
    wallet.hotkey = _ALICE
    return wallet


# ---- argument parsing and track selection ---------------------------------


def test_track_defaults_to_memory() -> None:
    args = _build_parser().parse_args(["upload", "--path", "agent.tar.gz"])
    assert args.track == "memory"


def test_track_accepts_reranker_and_rejects_unknown() -> None:
    args = _build_parser().parse_args(
        ["upload", "--track", "reranker", "--path", "artifact"]
    )
    assert args.track == "reranker"
    assert args.tar_path == Path("artifact")
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["upload", "--track", "router", "--path", "x"])


def _args(path: Path, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "tar_path": path,
        "name": None,
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "yes": False,
        "network": "local",
        "verbose": False,
        "track": "reranker",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_reranker_track_never_enters_the_paid_memory_flow(tmp_path: Path) -> None:
    with (
        patch.object(upload_cmd, "_run_reranker_upload", return_value=0) as reranker,
        patch.object(upload_cmd, "_run_upload") as memory,
    ):
        assert upload_cmd.run(_args(tmp_path)) == 0
    reranker.assert_called_once()
    memory.assert_not_called()


def test_memory_track_is_unchanged(tmp_path: Path) -> None:
    with (
        patch.object(upload_cmd, "_run_reranker_upload") as reranker,
        patch.object(upload_cmd, "_run_upload", return_value=0) as memory,
    ):
        assert upload_cmd.run(_args(tmp_path, track="memory", name="alpha")) == 0
    memory.assert_called_once()
    reranker.assert_not_called()


def test_namespace_without_track_stays_on_memory(tmp_path: Path) -> None:
    args = _args(tmp_path, name="alpha")
    del args.track
    with patch.object(upload_cmd, "_run_upload", return_value=0) as memory:
        assert upload_cmd.run(args) == 0
    memory.assert_called_once()


# ---- local artifact preparation -------------------------------------------


def test_directory_is_packed_deterministically(tmp_path: Path) -> None:
    directory = _artifact_dir(tmp_path / "artifact")

    prepared = prepare_reranker_artifact(directory)

    assert prepared.data == pack(directory)
    assert prepared.sha256 == hashlib.sha256(prepared.data).hexdigest()
    assert prepared.artifact_sha256 == artifact_digest(_MODEL, _vocab(), _manifest())
    assert prepared.model_checked is False


def test_tarball_is_uploaded_as_is(tmp_path: Path) -> None:
    tarball = tmp_path / "artifact.tar.gz"
    tarball.write_bytes(pack(_artifact_dir(tmp_path / "artifact")))

    prepared = prepare_reranker_artifact(tarball)

    assert prepared.data == tarball.read_bytes()


def test_model_is_checked_when_onnx_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ditto.miner_cli.reranker._onnx_available", lambda: True)
    seen: list[bytes] = []

    def _validate_model(raw: bytes) -> list[str]:
        seen.append(raw)
        return ["extra outputs ignored: ['x']"]

    monkeypatch.setattr("dittobench_reranker.artifact.validate_model", _validate_model)

    prepared = prepare_reranker_artifact(_artifact_dir(tmp_path / "a"))

    assert seen == [_MODEL]
    assert prepared.model_checked is True
    assert prepared.warnings == ("extra outputs ignored: ['x']",)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda d: (d / "manifest.json").write_bytes(
                _manifest(base_model_license="cc-by-nc-4.0")
            ),
            "base_model_license",
        ),
        (lambda d: (d / "vocab.txt").write_bytes(b"[PAD]\n"), "vocab.txt"),
        (lambda d: (d / "notes.md").write_text("hi"), "unexpected files"),
        (lambda d: (d / "model.onnx").unlink(), "missing model.onnx"),
    ],
)
def test_contract_violations_fail_locally(
    tmp_path: Path,
    mutate,
    reason: str,  # type: ignore[no-untyped-def]
) -> None:
    directory = _artifact_dir(tmp_path / "artifact")
    mutate(directory)

    with pytest.raises(RerankerArtifactError, match=reason):
        prepare_reranker_artifact(directory)


def test_unreadable_tarball_fails_locally(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"\x1f\x8bnot gzip")
    with pytest.raises(RerankerArtifactError):
        prepare_reranker_artifact(bad)


# ---- signing ---------------------------------------------------------------


def test_reranker_payload_is_domain_separated_from_memory_uploads() -> None:
    nonce = UUID("6f1c9a52-7d3e-4b8a-9c2f-1e5d7a3b9c40")
    kwargs = {
        "hotkey_ss58": "5Hot",
        "sha256_hex": "ab" * 32,
        "signature_timestamp": 7,
        "signature_nonce": nonce,
    }
    reranker = build_reranker_upload_payload(**kwargs)  # type: ignore[arg-type]
    memory = build_upload_payload(**kwargs)  # type: ignore[arg-type]
    # Byte-for-byte what Platform's reranker_upload.build_reranker_upload_payload
    # verifies.
    assert reranker == f"reranker-season-v1:upload:5Hot:{'ab' * 32}:7:{nonce}".encode()
    assert reranker != memory


def test_signature_verifies_against_the_hotkey() -> None:
    nonce = uuid4()
    signature = sign_reranker_upload_payload(
        handle=_handle(),
        live_wallet=_wallet(),
        sha256_hex="cd" * 32,
        signature_timestamp=123,
        signature_nonce=nonce,
    )
    payload = build_reranker_upload_payload(
        hotkey_ss58=_ALICE.ss58_address,
        sha256_hex="cd" * 32,
        signature_timestamp=123,
        signature_nonce=nonce,
    )
    assert _ALICE.verify(payload, bytes.fromhex(signature))


# ---- API client ------------------------------------------------------------


def _client(handler) -> ApiClient:  # type: ignore[no-untyped-def]
    client = ApiClient(base_url="http://test")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    return client


def _post(client: ApiClient) -> RerankerUploadResult:
    return client.post_upload_reranker(
        artifact=b"tarball",
        artifact_filename="artifact.tar.gz",
        hotkey=_ALICE.ss58_address,
        sha256="ef" * 32,
        signature="00" * 64,
        signature_timestamp=1,
        signature_nonce=UUID(int=4),
    )


def test_client_posts_the_multipart_shape_platform_expects() -> None:
    seen: dict[str, object] = {}
    submission_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        seen["type"] = request.headers["content-type"]
        return httpx.Response(
            201,
            json={
                "submission_id": str(submission_id),
                "status": "accepted",
                "artifact_sha256": "aa" * 32,
                "upload_sha256": "ef" * 32,
                "contract_id": CONTRACT_ID,
                "contract_sha256": "bb" * 32,
                "created_at": "2026-10-13T00:00:00Z",
                "warnings": ["w"],
                "future_field": True,
            },
        )

    with _client(handler) as client:
        result = _post(client)

    assert seen["path"] == "/api/v1/upload/reranker"
    assert str(seen["type"]).startswith("multipart/form-data")
    body = seen["body"]
    assert isinstance(body, bytes)
    for field in (
        b'name="artifact"; filename="artifact.tar.gz"',
        b'name="hotkey"',
        b'name="sha256"',
        b'name="signature"',
        b'name="signature_timestamp"',
        b'name="signature_nonce"',
    ):
        assert field in body
    for absent in (b"payment_block_hash", b"admission_token", b'name="name"'):
        assert absent not in body
    assert result == RerankerUploadResult(
        submission_id=submission_id,
        status="accepted",
        artifact_sha256="aa" * 32,
        contract_id=CONTRACT_ID,
        warnings=("w",),
    )


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, RerankerUploadRejectedError),
        (403, RerankerUploadRejectedError),
        (409, RerankerUploadRejectedError),
        (413, RerankerUploadRejectedError),
        (422, RerankerUploadRejectedError),
        (429, SubmissionCooldownError),
        (503, TransientApiError),
    ],
)
def test_client_maps_rejections(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            status,
            json={"error_code": 3002, "message": "artifact rejected: nope"},
            headers={"Retry-After": "60"} if status == 429 else {},
        )

    with _client(handler) as client, pytest.raises(error) as caught:
        _post(client)
    if status == 429:
        assert caught.value.retry_after_seconds == 60  # type: ignore[attr-defined]


# ---- end-to-end orchestration (mocked wallet + client) --------------------


def test_run_validates_signs_and_uploads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _artifact_dir(tmp_path / "artifact")
    submission_id = uuid4()
    client = MagicMock()
    client.post_upload_reranker.return_value = RerankerUploadResult(
        submission_id=submission_id,
        status="accepted",
        artifact_sha256="aa" * 32,
        contract_id=CONTRACT_ID,
    )
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False

    with (
        patch.object(upload_cmd, "load_wallet", return_value=(_handle(), _wallet())),
        patch.object(upload_cmd, "ApiClient", ctor),
    ):
        code = upload_cmd.run(_args(directory))

    assert code == 0
    out = capsys.readouterr()
    assert out.out.strip() == str(submission_id)
    assert "no payment" in out.err
    kwargs = client.post_upload_reranker.call_args.kwargs
    assert kwargs["artifact"] == pack(directory)
    assert kwargs["sha256"] == hashlib.sha256(pack(directory)).hexdigest()
    assert kwargs["hotkey"] == _ALICE.ss58_address
    payload = build_reranker_upload_payload(
        hotkey_ss58=_ALICE.ss58_address,
        sha256_hex=kwargs["sha256"],
        signature_timestamp=kwargs["signature_timestamp"],
        signature_nonce=kwargs["signature_nonce"],
    )
    assert _ALICE.verify(payload, bytes.fromhex(kwargs["signature"]))


def test_run_stops_before_any_request_on_a_bad_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _artifact_dir(
        tmp_path / "artifact", manifest=_manifest(contract="other")
    )
    ctor = MagicMock()
    with (
        patch.object(upload_cmd, "load_wallet", return_value=(_handle(), _wallet())),
        patch.object(upload_cmd, "ApiClient", ctor),
    ):
        code = upload_cmd.run(_args(directory))

    assert code == 1
    assert "contract" in capsys.readouterr().err
    ctor.assert_not_called()


def test_run_reports_a_server_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = MagicMock()
    client.post_upload_reranker.side_effect = RerankerUploadRejectedError(
        "upload-reranker failed: HTTP 409 code=3002 duplicate"
    )
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client
    ctor.return_value.__exit__.return_value = False
    with (
        patch.object(upload_cmd, "load_wallet", return_value=(_handle(), _wallet())),
        patch.object(upload_cmd, "ApiClient", ctor),
    ):
        code = upload_cmd.run(_args(_artifact_dir(tmp_path / "a")))

    assert code == 1
    assert "HTTP 409" in capsys.readouterr().err


def test_pack_round_trips_through_tarfile(tmp_path: Path) -> None:
    data = pack(_artifact_dir(tmp_path / "artifact"))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert tar.getnames() == ["model.onnx", "vocab.txt", "manifest.json"]
