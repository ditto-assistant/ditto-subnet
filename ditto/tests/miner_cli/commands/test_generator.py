"""``ditto generator``: signing wire format, packaging, rehearsal, upload flow."""

from __future__ import annotations

import argparse
import base64
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import bittensor
import pytest

from ditto.miner_cli.commands import generator as gen_cmd

_KEY = bittensor.Keypair.create_from_uri("//Bob")
_KIT = (
    Path(__file__).resolve().parents[4] / "miners" / "dittobench-generator-starter-kit"
)
_HAS_GO = shutil.which("go") is not None


def test_signature_payload_is_the_pinned_wire_format() -> None:
    """Pinned literally here and in Platform's ``test_generator_competition``.

    The root package cannot import Platform's models, so both sides pin the
    same literal instead of importing each other.
    """
    nonce = "4b0d7e8e-5f3c-4c4e-9a8b-1e2f3a4b5c6d"
    payload = gen_cmd.build_source_payload(
        hotkey_ss58=_KEY.ss58_address,
        source_sha256="ab" * 32,
        signature_timestamp=1_700_000_000,
        nonce=nonce,
    )
    expected = f"ditto-generator-v1:{_KEY.ss58_address}:{'ab' * 32}"
    assert payload == f"{expected}:1700000000:{nonce}".encode()


@pytest.mark.skipif(not _HAS_GO, reason="needs the Go toolchain")
def test_package_and_rehearse_the_starter_kit(tmp_path: Path) -> None:
    archive = tmp_path / "kit.tar.gz"
    gen_cmd.package_dir(_KIT, archive, explicit=None)
    info = gen_cmd.inspect_archive(archive, explicit=None)
    assert info["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert info["vendored"] is True and info["module"].endswith("starter-kit")
    # Packaging never writes into the miner's checkout.
    assert not (_KIT / "vendor").exists()

    proc = gen_cmd.run_tool(
        [
            "rehearse",
            "-archive",
            str(archive),
            "-hotkey",
            _KEY.ss58_address,
            "-seeds",
            "1",
            "-cases",
            "8",
        ],
        explicit=None,
        capture=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "deterministic" in proc.stdout


def test_missing_tool_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_cmd.shutil, "which", lambda _name: None)
    with pytest.raises(gen_cmd.GeneratorToolError, match="no generator tool"):
        gen_cmd.run_tool(["inspect", "/x"], explicit=None)


class _FakeClient:
    posted: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def post_generator_submission(self, body: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.posted.append(body)
        return {
            "submission_id": "00000000-0000-4000-8000-000000000001",
            "status": "active",
            "source_sha256": body["source_sha256"],
            "tree_sha256": "cd" * 32,
            "superseded": [],
        }


def _upload_args(
    archive: Path | None, directory: Path | None = None
) -> argparse.Namespace:
    return argparse.Namespace(
        network="local",
        generator_command="upload",
        archive=archive,
        dir=directory,
        challenge_bin=None,
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
    raw = b"\x1f\x8b fake archive bytes"
    archive = tmp_path / "gen.tar.gz"
    archive.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        gen_cmd,
        "inspect_archive",
        lambda _path, **_kw: {"sha256": digest, "files": 2, "module": "m"},
    )
    _FakeClient.posted.clear()
    assert gen_cmd.run(_upload_args(archive)) == 0
    body = _FakeClient.posted[0]
    assert base64.b64decode(body["source_base64"]) == raw
    assert body["source_sha256"] == digest
    payload = gen_cmd.build_source_payload(
        hotkey_ss58=_KEY.ss58_address,
        source_sha256=digest,
        signature_timestamp=body["signature_timestamp"],
        nonce=body["signature_nonce"],
    )
    assert _KEY.verify(payload, bytes.fromhex(body["signature"]))


def test_upload_refuses_an_archive_the_contract_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    archive = tmp_path / "gen.tar.gz"
    archive.write_bytes(b"bytes")

    def _reject(_path: Path, **_kw: object) -> dict:
        raise gen_cmd.GeneratorToolError("archive rejected: no go.mod")

    monkeypatch.setattr(gen_cmd, "inspect_archive", _reject)
    _FakeClient.posted.clear()
    with pytest.raises(gen_cmd.GeneratorToolError, match="no go.mod"):
        gen_cmd.run(_upload_args(archive))
    assert _FakeClient.posted == []


def test_upload_needs_exactly_one_source(tmp_path: Path) -> None:
    with pytest.raises(gen_cmd.GeneratorToolError, match="exactly one"):
        gen_cmd.run(_upload_args(None))
    with pytest.raises(gen_cmd.GeneratorToolError, match="exactly one"):
        gen_cmd.run(_upload_args(tmp_path / "a", tmp_path))
