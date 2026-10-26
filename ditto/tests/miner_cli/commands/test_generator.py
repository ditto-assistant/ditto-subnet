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
from ditto.miner_cli.errors import (
    ApiResponseError,
    GeneratorPaymentRejectedError,
    MinerCliError,
)
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.preferences import load_pending_payments_for_hotkey

_KEY = bittensor.Keypair.create_from_uri("//Bob")
_KIT = (
    Path(__file__).resolve().parents[4] / "miners" / "dittobench-generator-starter-kit"
)
_HAS_GO = shutil.which("go") is not None


@pytest.fixture(autouse=True)
def _isolated_cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saved payment proofs go to a per-test config, never the real one."""
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(tmp_path / "config.json"))


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
    # None models an older Platform with no terms endpoint.
    terms: dict[str, Any] | None = None
    reject: Exception | None = None
    malformed = False

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def get_generator_upload_terms(self, **query: str) -> dict[str, Any] | None:
        assert set(query) == {"hotkey", "source_sha256"}
        return _FakeClient.terms

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def post_generator_submission(self, body: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.posted.append(body)
        if _FakeClient.reject is not None:
            raise _FakeClient.reject
        if _FakeClient.malformed:
            return {"status": "active"}
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
    _FakeClient.posted = []
    _FakeClient.terms = None
    _FakeClient.reject = None
    _FakeClient.malformed = False


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


_BLOCK = "0x" + "ef" * 32
_FEE_TERMS = {
    "accepting": True,
    "payment_required": True,
    "payment_amount_rao": 40_000_000,
    "payment_send_address": "5FeeAddress",
    "upload_cooldown_seconds": 3600,
    "retry_at": None,
    "blockers": [],
}


def _fake_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    raw = b"\x1f\x8b generator bytes"
    archive = tmp_path / "gen.tar.gz"
    archive.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        gen_cmd,
        "inspect_archive",
        lambda _path, **_kw: {"sha256": digest, "files": 2, "module": "m"},
    )
    return archive


def _install_payment(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        gen_cmd,
        "preflight_payment_signer",
        lambda **kw: calls.append({"preflight": kw}),
    )
    monkeypatch.setattr(
        gen_cmd, "confirm_payment", lambda **kw: calls.append({"confirm": kw})
    )

    def _pay(**kw: Any) -> PaymentReceipt:
        calls.append({"pay": kw})
        return PaymentReceipt(block_hash=_BLOCK, block_number=77, extrinsic_index=3)

    monkeypatch.setattr(gen_cmd, "submit_eval_payment", _pay)
    return calls


def test_upload_pays_nothing_when_the_fee_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    for terms in (None, {**_FEE_TERMS, "payment_required": False}):
        _FakeClient.terms = terms
        _FakeClient.posted = []
        assert gen_cmd.run(_upload_args(archive)) == 0
        assert "payment_block_hash" not in _FakeClient.posted[0]
    assert calls == []


def test_upload_pays_the_quoted_fee_and_attaches_the_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    _FakeClient.terms = dict(_FEE_TERMS)
    assert gen_cmd.run(_upload_args(archive)) == 0
    pay = next(c["pay"] for c in calls if "pay" in c)
    assert pay["amount_rao"] == 40_000_000
    assert pay["dest_address"] == "5FeeAddress"
    assert [next(iter(c)) for c in calls] == ["preflight", "confirm", "pay"]
    body = _FakeClient.posted[0]
    assert body["payment_block_hash"] == _BLOCK
    assert body["payment_block_number"] == 77
    assert body["payment_extrinsic_index"] == 3


def test_upload_refuses_before_paying_when_platform_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    _FakeClient.terms = {**_FEE_TERMS, "blockers": ["generator upload cooldown"]}
    with pytest.raises(MinerCliError, match="no payment was sent.*cooldown"):
        gen_cmd.run(_upload_args(archive))
    assert calls == []
    assert _FakeClient.posted == []


@pytest.mark.parametrize(
    "failure",
    [ApiResponseError("generator upload failed: HTTP 503"), ValueError("not json")],
)
def test_a_failed_upload_after_payment_keeps_the_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    _FakeClient.terms = dict(_FEE_TERMS)
    _FakeClient.reject = failure
    with pytest.raises(type(failure)):
        gen_cmd.run(_upload_args(archive))
    err = capsys.readouterr().err
    assert f"--payment-block-hash {_BLOCK}" in err
    assert "--payment-extrinsic-index 3" in err
    # Saved in its own bucket: agent uploads never see it as a credit.
    assert (
        load_pending_payments_for_hotkey(network="local", hotkey=_KEY.ss58_address)
        == ()
    )

    # Rerunning the same archive reuses the saved proof without paying again,
    # and a confirmed upload clears it.
    paid = len(calls)
    _FakeClient.reject = None
    _FakeClient.malformed = False
    _FakeClient.posted = []
    assert gen_cmd.run(_upload_args(archive)) == 0
    assert len(calls) == paid
    assert _FakeClient.posted[0]["payment_block_hash"] == _BLOCK
    _FakeClient.posted = []
    assert gen_cmd.run(_upload_args(archive)) == 0
    assert len(calls) == paid + 3


def test_a_malformed_success_keeps_the_saved_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    _FakeClient.terms = dict(_FEE_TERMS)
    _FakeClient.malformed = True
    with pytest.raises(KeyError):
        gen_cmd.run(_upload_args(archive))
    _FakeClient.malformed = False
    _FakeClient.posted = []
    assert gen_cmd.run(_upload_args(archive)) == 0
    assert len(calls) == 3  # paid once; the rerun reused the saved proof
    assert _FakeClient.posted[0]["payment_block_hash"] == _BLOCK


def test_a_definitively_refused_proof_is_forgotten_and_repaid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    _FakeClient.terms = dict(_FEE_TERMS)
    _FakeClient.reject = GeneratorPaymentRejectedError(
        "generator upload failed: HTTP 402 code=3208 payment recovery expired"
    )
    with pytest.raises(GeneratorPaymentRejectedError):
        gen_cmd.run(_upload_args(archive))
    assert "removed from local storage" in capsys.readouterr().err
    # The rerun reads the terms and pays again rather than resending the proof.
    _FakeClient.reject = None
    assert gen_cmd.run(_upload_args(archive)) == 0
    assert [next(iter(c)) for c in calls] == ["preflight", "confirm", "pay"] * 2


def test_recovery_flags_reuse_a_proof_without_paying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_wallet(monkeypatch, _KEY)
    calls = _install_payment(monkeypatch)
    archive = _fake_archive(tmp_path, monkeypatch)
    _FakeClient.terms = dict(_FEE_TERMS)
    args = _upload_args(archive)
    args.payment_block_hash = _BLOCK
    args.payment_block_number = 77
    args.payment_extrinsic_index = 3
    assert gen_cmd.run(args) == 0
    assert calls == []
    assert _FakeClient.posted[0]["payment_extrinsic_index"] == 3

    args.payment_block_number = None
    with pytest.raises(MinerCliError, match="together"):
        gen_cmd.run(args)


def test_the_upload_parser_accepts_recovery_flags() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    gen_cmd.add_subparser(subs)
    args = parser.parse_args(
        [
            "generator",
            "upload",
            "gen.tar.gz",
            "--payment-block-hash",
            _BLOCK,
            "--payment-block-number",
            "77",
            "--payment-extrinsic-index",
            "3",
        ]
    )
    assert (args.payment_block_number, args.payment_extrinsic_index) == (77, 3)
