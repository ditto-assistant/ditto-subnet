"""``ditto generator``: signing wire format, local validation, upload flow."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import bittensor
import pytest

from ditto.miner_cli.commands import generator as gen_cmd

_KEY = bittensor.Keypair.create_from_uri("//Bob")
_DATAGEN = Path(__file__).resolve().parents[4] / "research" / "dittobench-datagen"
_HAS_GO = shutil.which("go") is not None


def test_signature_payload_is_the_pinned_wire_format() -> None:
    """Pinned literally here and in Platform's ``test_generator_competition``.

    The root package cannot import Platform's models, so both sides pin the
    same literal instead of importing each other.
    """
    nonce = "4b0d7e8e-5f3c-4c4e-9a8b-1e2f3a4b5c6d"
    payload = gen_cmd.build_pack_payload(
        hotkey_ss58=_KEY.ss58_address,
        pack_sha256="ab" * 32,
        signature_timestamp=1_700_000_000,
        nonce=nonce,
    )
    expected = f"ditto-generator-pack-v1:{_KEY.ss58_address}:{'ab' * 32}"
    assert payload == f"{expected}:1700000000:{nonce}".encode()


def _example(tmp_path: Path, hotkey: str) -> Path:
    out = tmp_path / "pack.json"
    subprocess.run(
        [
            "go",
            "run",
            "./cmd/challenge",
            "example",
            "-seed",
            "5",
            "-hotkey",
            hotkey,
            "-out",
            str(out),
        ],
        cwd=_DATAGEN,
        check=True,
    )
    return out


@pytest.mark.skipif(not _HAS_GO, reason="needs the Go toolchain")
def test_validate_runs_the_contract_validator(tmp_path: Path) -> None:
    pack = _example(tmp_path, _KEY.ss58_address)
    report = gen_cmd.validate_pack(pack, validator=None)
    assert report["valid"] is True and report["case_count"] == 16
    assert report["pack_sha256"] == hashlib.sha256(pack.read_bytes()).hexdigest()

    broken = json.loads(pack.read_text())
    broken["cases"][0]["expected_answer"] = "not in evidence"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(broken))
    report = gen_cmd.validate_pack(bad, validator=None)
    assert report["valid"] is False
    assert {issue["rule"] for issue in report["issues"]} >= {"grounding"}


def test_missing_validator_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gen_cmd, "_validator_command", lambda _explicit: None)
    with pytest.raises(gen_cmd.PackValidationError, match="no pack validator"):
        gen_cmd.validate_pack(tmp_path / "x.json", validator=None)


class _FakeClient:
    posted: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def post_generator_pack(self, body: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.posted.append(body)
        return {
            "pack_id": "00000000-0000-4000-8000-000000000001",
            "status": "queued",
            "case_count": 16,
            "pack_sha256": body["pack_sha256"],
        }


def _upload_args(pack: Path) -> argparse.Namespace:
    return argparse.Namespace(
        network="local",
        generator_command="upload",
        pack=pack,
        validator=None,
        skip_validate=True,
        coldkey_name="cold",
        hotkey_name="hot",
        yes=True,
    )


def _install_wallet(monkeypatch: pytest.MonkeyPatch, key: bittensor.Keypair) -> None:
    handle = SimpleNamespace(hotkey_ss58=key.ss58_address)
    wallet = SimpleNamespace(hotkey=key)
    monkeypatch.setattr(gen_cmd, "load_wallet", lambda **_kw: (handle, wallet))
    monkeypatch.setattr(gen_cmd, "ApiClient", _FakeClient)


def test_upload_signs_the_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    raw = json.dumps(
        {"contract": gen_cmd.PACK_CONTRACT_V1, "miner_hotkey": _KEY.ss58_address}
    ).encode()
    pack = tmp_path / "pack.json"
    pack.write_bytes(raw)
    _FakeClient.posted.clear()
    assert gen_cmd.run(_upload_args(pack)) == 0
    body = _FakeClient.posted[0]
    assert base64.b64decode(body["pack_base64"]) == raw
    assert body["pack_sha256"] == hashlib.sha256(raw).hexdigest()
    payload = gen_cmd.build_pack_payload(
        hotkey_ss58=_KEY.ss58_address,
        pack_sha256=body["pack_sha256"],
        signature_timestamp=body["signature_timestamp"],
        nonce=body["signature_nonce"],
    )
    assert _KEY.verify(payload, bytes.fromhex(body["signature"]))


def test_upload_refuses_a_pack_bound_to_another_hotkey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    pack = tmp_path / "pack.json"
    pack.write_text(
        json.dumps({"contract": gen_cmd.PACK_CONTRACT_V1, "miner_hotkey": "5Other"})
    )
    _FakeClient.posted.clear()
    with pytest.raises(gen_cmd.PackValidationError, match="not the signing"):
        gen_cmd.run(_upload_args(pack))
    assert _FakeClient.posted == []
