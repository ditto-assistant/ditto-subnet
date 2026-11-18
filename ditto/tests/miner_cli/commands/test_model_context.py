"""``ditto model-context``: wire format, manifest hashing, commit, submit.

Contract: ``docs/research/cpm/competition-v1.md`` sections 2.3, 3.1, 3.2 and
5.3. The wire checks run against Platform's own models
(:mod:`ditto.tests.platform_model_context`), not a copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.miner_cli.__main__ import _build_parser
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands import model_context as cmd
from ditto.miner_cli.errors import ApiResponseError, TransientApiError
from ditto.tests import platform_model_context

_KEY = bittensor.Keypair.create_from_uri("//Bob")
_REPO = "ditto-miner/cpm-lora"
_REVISION = "0123456789abcdef0123456789abcdef01234567"
_NONCE = UUID("4b0d7e8e-5f3c-4c4e-9a8b-1e2f3a4b5c6d")

_ADAPTER_CONFIG = b'{"r": 8, "target_modules": ["q_proj", "v_proj"]}\n'
_ADAPTER_WEIGHTS = b"stand-in adapter weights"
# The contract section 2.3 test vector: these two files, base qwen3.5-0.8b,
# kind lora, license apache-2.0, attestation "synthetic test vector".
_VECTOR_MANIFEST = (
    "{\n"
    '  "schema": "ditto-cpm-manifest-v1",\n'
    '  "track": "model_context",\n'
    '  "base_id": "qwen3.5-0.8b",\n'
    '  "kind": "lora",\n'
    '  "files": [\n'
    "    {\n"
    '      "path": "adapter_config.json",\n'
    f'      "sha256": "{hashlib.sha256(_ADAPTER_CONFIG).hexdigest()}",\n'
    f'      "bytes": {len(_ADAPTER_CONFIG)}\n'
    "    },\n"
    "    {\n"
    '      "path": "adapter_model.safetensors",\n'
    f'      "sha256": "{hashlib.sha256(_ADAPTER_WEIGHTS).hexdigest()}",\n'
    f'      "bytes": {len(_ADAPTER_WEIGHTS)}\n'
    "    }\n"
    "  ],\n"
    '  "license": "apache-2.0",\n'
    '  "training_data_attestation": "synthetic test vector"\n'
    "}\n"
).encode()
_VECTOR_SHA256 = "6e7cec86453a9826e976d38be00c8bccdef29299e79d21f2bdc4fe857a95ac6e"


def _lora_dir(root: Path) -> Path:
    model = root / "model"
    model.mkdir()
    (model / "adapter_config.json").write_bytes(_ADAPTER_CONFIG)
    (model / "adapter_model.safetensors").write_bytes(_ADAPTER_WEIGHTS)
    # The tokenizer is always the base's; its files are never listed.
    (model / "tokenizer.json").write_text("{}")
    (model / "README.md").write_text("# model\n")
    return model


# --- wire format -----------------------------------------------------------------


def test_signature_payload_is_the_contract_bytes() -> None:
    payload = cmd.build_signature_payload(
        hotkey=_KEY.ss58_address,
        hf_repo=_REPO,
        revision=_REVISION,
        manifest_sha256="ab" * 32,
        ts=1_700_000_000,
        nonce=_NONCE,
    )
    assert payload == (
        f"ditto-model-context-v1:{_KEY.ss58_address}:{_REPO}:{_REVISION}:"
        f"{'ab' * 32}:1700000000:{_NONCE}"
    ).encode("ascii")
    platform = platform_model_context.load()
    assert payload == platform.signature_payload(
        hotkey=_KEY.ss58_address,
        hf_repo=_REPO,
        revision=_REVISION,
        manifest_sha256="ab" * 32,
        ts=1_700_000_000,
        nonce=_NONCE,
    )


def test_commitment_string_matches_platform() -> None:
    digest = "cd" * 32
    assert cmd.commitment_value(digest) == f"dcpm1:{digest}"
    assert cmd.commitment_value(digest) == (
        platform_model_context.load().commitment_value(digest)
    )
    # 70 ASCII bytes: one Raw70 field, well under the pallet's 128-byte cap.
    assert len(cmd.commitment_value(digest).encode("ascii")) == 70
    with pytest.raises(cmd.ModelContextError):
        cmd.commitment_value("not-a-digest")


def test_cli_constants_match_platform() -> None:
    platform = platform_model_context.load()
    assert cmd.SIGNATURE_DOMAIN == platform.SIGNATURE_DOMAIN
    assert cmd.COMMITMENT_PREFIX == platform.COMMITMENT_PREFIX
    assert cmd.MANIFEST_SCHEMA == platform.MANIFEST_SCHEMA
    assert cmd.MANIFEST_FILENAME == platform.MANIFEST_FILENAME
    assert cmd.ALLOWED_LICENSES == platform.ALLOWED_LICENSES
    assert cmd.MAX_ATTESTATION_CHARS == platform.MAX_ATTESTATION_CHARS
    assert set(cmd.KNOWN_BASES) == {b.base_id for b in platform.DEFAULT_BASES}


# --- manifest ----------------------------------------------------------------------


def test_lora_manifest_matches_the_test_vector(tmp_path: Path) -> None:
    raw = cmd.build_manifest(
        _lora_dir(tmp_path),
        base_id="qwen3.5-0.8b",
        kind="lora",
        license_id="apache-2.0",
        attestation="synthetic test vector",
    )
    assert raw == _VECTOR_MANIFEST
    assert cmd.manifest_sha256(raw) == hashlib.sha256(_VECTOR_MANIFEST).hexdigest()
    assert cmd.manifest_sha256(raw) == _VECTOR_SHA256
    manifest = platform_model_context.load().CpmManifest.model_validate(json.loads(raw))
    assert [f.path for f in manifest.files] == list(cmd.LORA_FILES)
    assert manifest.total_bytes == len(_ADAPTER_CONFIG) + len(_ADAPTER_WEIGHTS)


def test_full_manifest_lists_config_index_and_shards_only(tmp_path: Path) -> None:
    model = tmp_path / "full"
    model.mkdir()
    for name in (
        "config.json",
        "model.safetensors.index.json",
        "model-00002-of-00002.safetensors",
        "model-00001-of-00002.safetensors",
        "tokenizer.json",
        "generation_config.json",
    ):
        (model / name).write_bytes(name.encode())
    raw = cmd.build_manifest(
        model,
        base_id="qwen3.5-2b",
        kind="full",
        license_id="mit",
        attestation="",
    )
    manifest = platform_model_context.load().CpmManifest.model_validate(json.loads(raw))
    assert [f.path for f in manifest.files] == [
        "config.json",
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
        "model.safetensors.index.json",
    ]
    assert manifest.kind == "full" and manifest.license == "mit"


def test_manifest_refuses_missing_files_and_code(tmp_path: Path) -> None:
    model = _lora_dir(tmp_path)
    (model / "adapter_model.safetensors").unlink()
    with pytest.raises(cmd.ModelContextError, match="adapter_model.safetensors"):
        cmd.build_manifest(
            model, base_id="qwen3.5-0.8b", kind="lora", license_id="mit", attestation=""
        )
    (model / "adapter_model.safetensors").write_bytes(b"w")
    (model / "training_args.bin").write_bytes(b"pickle")
    with pytest.raises(cmd.ModelContextError, match="training_args.bin"):
        cmd.build_manifest(
            model, base_id="qwen3.5-0.8b", kind="lora", license_id="mit", attestation=""
        )


def test_manifest_command_writes_the_file_and_prints_the_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model = _lora_dir(tmp_path)
    args = _build_parser().parse_args(
        [
            "model-context",
            "manifest",
            str(model),
            "--base",
            "qwen3.5-0.8b",
            "--kind",
            "lora",
            "--attestation",
            "synthetic test vector",
        ]
    )
    assert args.func(args) == 0
    written = (model / "cpm-manifest.json").read_bytes()
    assert written == _VECTOR_MANIFEST
    out = capsys.readouterr()
    assert out.out.strip() == _VECTOR_SHA256
    assert f"dcpm1:{_VECTOR_SHA256}" in out.err


def test_read_manifest_hashes_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "cpm-manifest.json"
    # CRLF must survive: text-mode reads would translate it.
    raw = b'{\r\n  "schema": "ditto-cpm-manifest-v1"\r\n}\r\n'
    path.write_bytes(raw)
    text, digest = cmd.read_manifest(path)
    assert text.encode("utf-8") == raw
    assert digest == hashlib.sha256(raw).hexdigest()
    path.write_bytes(b'{"schema": "other"}')
    with pytest.raises(cmd.ModelContextError, match="not a ditto-cpm-manifest-v1"):
        cmd.read_manifest(path)


# --- commit ------------------------------------------------------------------------


def _commit_args(manifest: Path, *, yes: bool) -> argparse.Namespace:
    return argparse.Namespace(
        network="local",
        chain_endpoint=None,
        model_context_command="commit",
        manifest=manifest,
        netuid=118,
        coldkey_name="cold",
        hotkey_name="hot",
        yes=yes,
    )


def _install_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = SimpleNamespace(hotkey_ss58=_KEY.ss58_address)
    wallet = SimpleNamespace(hotkey=_KEY)
    monkeypatch.setattr(cmd, "load_wallet", lambda **_kw: (handle, wallet))


def test_commit_asks_first_and_writes_nothing_when_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "cpm-manifest.json"
    manifest.write_bytes(_VECTOR_MANIFEST)
    _install_wallet(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _set(**kw: Any) -> int:
        calls.append(kw)
        return 1

    monkeypatch.setattr(cmd, "set_commitment", _set)
    monkeypatch.setattr(cmd, "prompt_input", lambda _prompt: "n")
    with pytest.raises(cmd.ModelContextError, match="no commitment was written"):
        cmd.run(_commit_args(manifest, yes=False))
    assert calls == []


def test_commit_writes_the_manifest_digest_and_prints_the_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "cpm-manifest.json"
    manifest.write_bytes(_VECTOR_MANIFEST)
    _install_wallet(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _set(**kw: Any) -> int:
        calls.append(kw)
        return 4_242_424

    monkeypatch.setattr(cmd, "set_commitment", _set)
    monkeypatch.setattr(cmd, "prompt_input", lambda _prompt: "y")
    assert cmd.run(_commit_args(manifest, yes=False)) == 0
    (call,) = calls
    assert call["value"] == f"dcpm1:{_VECTOR_SHA256}"
    assert call["netuid"] == 118 and call["chain_target"] == "local"
    assert capsys.readouterr().out.strip() == "4242424"


@pytest.mark.parametrize(
    "fields",
    [
        [{"Raw70": "0x" + b"dcpm1:abc".hex()}],
        [[{"Raw70": "0x" + b"dcpm1:abc".hex()}]],
        ([{"Raw70": tuple(b"dcpm1:abc")}],),
        [{"Raw70": b"dcpm1:abc"}],
    ],
)
def test_decode_commitment_reads_every_decoder_shape(fields: Any) -> None:
    value = {"deposit": 0, "block": 77, "info": {"fields": fields}}
    assert cmd.decode_commitment(value) == (77, b"dcpm1:abc")


def test_set_commitment_writes_raw_text_and_reads_the_block_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No chain: a fake ``Subtensor`` records the call the SDK would sign."""
    value = f"dcpm1:{'ef' * 32}"
    calls: list[dict[str, Any]] = []

    class _FakeSubtensor:
        def __init__(self, network: str) -> None:
            assert network == "test"

        def set_commitment(self, **kw: Any) -> SimpleNamespace:
            calls.append(kw)
            return SimpleNamespace(success=True, message="")

        def get_commitment_metadata(self, netuid: int, hotkey: str) -> dict[str, Any]:
            assert (netuid, hotkey) == (118, _KEY.ss58_address)
            return {
                "deposit": 0,
                "block": 9001,
                "info": {"fields": [[{"Raw70": "0x" + value.encode().hex()}]]},
            }

    monkeypatch.setattr(bittensor, "Subtensor", _FakeSubtensor)
    wallet = SimpleNamespace(hotkey=_KEY)
    block = cmd.set_commitment(
        wallet=wallet, chain_target="test", netuid=118, value=value
    )
    assert block == 9001
    (call,) = calls
    assert call["data"] == value and call["netuid"] == 118
    assert call["wallet"] is wallet and call["wait_for_finalization"] is True


def test_decode_commitment_rejects_non_registrations() -> None:
    assert cmd.decode_commitment("") is None
    assert cmd.decode_commitment({"block": -1, "info": {}}) is None
    assert cmd.decode_commitment({"block": 5, "info": {"fields": []}}) == (5, b"")


# --- submit ------------------------------------------------------------------------


class _FakeClient:
    terms: dict[str, Any] = {}
    posted: list[dict[str, Any]] = []
    terms_hotkeys: list[str | None] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get_model_context_upload_terms(
        self, *, hotkey: str | None = None
    ) -> dict[str, Any]:
        _FakeClient.terms_hotkeys.append(hotkey)
        return _FakeClient.terms

    def post_model_context_submission(self, body: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.posted.append(body)
        return {
            "submission_id": "00000000-0000-4000-8000-000000000001",
            "status": "screening",
            "manifest_sha256": hashlib.sha256(body["manifest"].encode()).hexdigest(),
            "commitment_block": 1234,
            "base_id": "qwen3.5-0.8b",
            "kind": "lora",
        }


def _submit_args(manifest: Path, *, revision: str = _REVISION) -> argparse.Namespace:
    return argparse.Namespace(
        network="local",
        model_context_command="submit",
        repo=_REPO,
        revision=revision,
        manifest=manifest,
        coldkey_name="cold",
        hotkey_name="hot",
        yes=True,
    )


@pytest.fixture
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _install_wallet(monkeypatch)
    monkeypatch.setattr(cmd, "ApiClient", _FakeClient)
    _FakeClient.terms = {"accepting": True, "blockers": []}
    _FakeClient.posted = []
    _FakeClient.terms_hotkeys = []
    return _FakeClient


def test_submit_body_is_the_platform_request_and_its_signature_verifies(
    tmp_path: Path, fake_platform: type[_FakeClient]
) -> None:
    manifest = tmp_path / "cpm-manifest.json"
    manifest.write_bytes(_VECTOR_MANIFEST)
    assert cmd.run(_submit_args(manifest)) == 0
    (body,) = fake_platform.posted
    assert fake_platform.terms_hotkeys == [_KEY.ss58_address]
    assert set(body) == {
        "hotkey",
        "hf_repo",
        "revision",
        "manifest",
        "signature",
        "ts",
        "nonce",
    }
    platform = platform_model_context.load()
    request = platform.ModelContextSubmissionRequest.model_validate(
        json.loads(json.dumps(body))
    )
    # Platform hashes the manifest text it receives; it is the file's bytes.
    assert request.manifest_sha256 == _VECTOR_SHA256
    assert request.hf_repo == _REPO and request.revision == _REVISION
    # Platform's verifier: Keypair(ss58).verify(payload, bytes.fromhex(sig)).
    message = platform.signature_payload(
        hotkey=request.hotkey,
        hf_repo=request.hf_repo,
        revision=request.revision,
        manifest_sha256=request.manifest_sha256,
        ts=request.ts,
        nonce=request.nonce,
    )
    verifier = bittensor.Keypair(ss58_address=request.hotkey)
    assert verifier.verify(message, bytes.fromhex(request.signature.removeprefix("0x")))


def test_submit_stops_on_platform_blockers(
    tmp_path: Path, fake_platform: type[_FakeClient]
) -> None:
    manifest = tmp_path / "cpm-manifest.json"
    manifest.write_bytes(_VECTOR_MANIFEST)
    fake_platform.terms = {
        "accepting": False,
        "blockers": ["the model_context competition is not accepting submissions"],
    }
    with pytest.raises(cmd.ModelContextError, match="not accepting"):
        cmd.run(_submit_args(manifest))
    assert fake_platform.posted == []


@pytest.mark.parametrize("revision", ["main", "0123abc", "0" * 39 + "G"])
def test_submit_requires_a_full_commit_sha(
    tmp_path: Path, fake_platform: type[_FakeClient], revision: str
) -> None:
    manifest = tmp_path / "cpm-manifest.json"
    manifest.write_bytes(_VECTOR_MANIFEST)
    with pytest.raises(cmd.ModelContextError, match="40-hex"):
        cmd.run(_submit_args(manifest, revision=revision))
    assert fake_platform.posted == []


# --- practice ----------------------------------------------------------------------


def _practice_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "model_context_command": "practice",
        "model": "./out",
        "base": "qwen3.5-0.8b",
        "seeds": None,
        "device": None,
        "limit_cases": None,
        "scorer_bin": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_practice_explains_how_to_install_the_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cmd.shutil, "which", lambda _name: None)
    with pytest.raises(cmd.ModelContextError, match="cpm-scorer is not installed"):
        cmd.practice_command(_practice_args())


def test_practice_runs_the_scorer_score_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd.shutil, "which", lambda _name: "/usr/bin/cpm-scorer")
    assert cmd.practice_command(
        _practice_args(seeds="1,2,3", device="cpu", limit_cases=4)
    ) == [
        "/usr/bin/cpm-scorer",
        "score",
        "--model",
        "./out",
        "--base",
        "qwen3.5-0.8b",
        "--seeds",
        "1,2,3",
        "--device",
        "cpu",
        "--limit-cases",
        "4",
    ]


def test_every_subcommand_is_registered() -> None:
    parser = _build_parser()
    for argv in (
        ["model-context", "board"],
        ["model-context", "status", "--ss58", _KEY.ss58_address],
        ["model-context", "practice", "base"],
        ["model-context", "commit", "--manifest", "m.json"],
        [
            "model-context",
            "submit",
            "--repo",
            _REPO,
            "--revision",
            _REVISION,
            "--manifest",
            "m.json",
        ],
    ):
        args = parser.parse_args(argv)
        assert args.func is cmd.run
    assert parser.parse_args(["model-context", "commit", "--manifest", "m"]).netuid == (
        118
    )


# --- api client --------------------------------------------------------------------


def test_api_client_reads_and_posts_the_public_routes() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        if request.method == "POST":
            return httpx.Response(200, json={"submission_id": "x"})
        return httpx.Response(200, json={"ok": True})

    http = httpx.Client(
        base_url="https://platform.test", transport=httpx.MockTransport(handler)
    )
    with ApiClient(client=http) as client:
        client.get_model_context_upload_terms(hotkey=_KEY.ss58_address)
        client.post_model_context_submission({"hotkey": _KEY.ss58_address})
        client.list_model_context_submissions(hotkey=_KEY.ss58_address)
        client.get_model_context_board()
    assert [(method, path) for method, path, _ in seen] == [
        ("GET", "/api/v1/model-context/upload-terms"),
        ("POST", "/api/v1/model-context/submissions"),
        ("GET", "/api/v1/model-context/submissions"),
        ("GET", "/api/v1/model-context/board"),
    ]
    assert seen[0][2] == f"hotkey={_KEY.ss58_address}"


@pytest.mark.parametrize(
    ("status", "error"), [(409, ApiResponseError), (429, TransientApiError)]
)
def test_api_client_maps_submit_failures(
    status: int, error: type[ApiResponseError]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "refused"})

    http = httpx.Client(
        base_url="https://platform.test", transport=httpx.MockTransport(handler)
    )
    with ApiClient(client=http) as client, pytest.raises(error, match=str(status)):
        client.post_model_context_submission({})
