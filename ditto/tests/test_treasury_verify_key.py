"""The public-address check must never disclose the stored mnemonic."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import treasury_verify_key as verify  # noqa: E402


def test_pinned_secret_address_matches_without_disclosing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "test mnemonic must stay private"
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(argv, 0, stdout=secret.encode())

    monkeypatch.setattr(verify, "_instance_name", lambda: verify.HOST_NAME)
    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    monkeypatch.setattr(
        verify.bt.Keypair,
        "create_from_mnemonic",
        lambda mnemonic: (
            SimpleNamespace(ss58_address="public-address")
            if mnemonic == secret
            else pytest.fail("wrong secret")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "treasury_verify_key.py",
            "--project",
            "project",
            "--expected-address",
            "public-address",
        ],
    )

    verify.main()

    assert calls == [
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "1",
            "--secret=sn118-treasury-signing-key",
            "--project=project",
        ]
    ]
    assert capsys.readouterr().out == "public-address\n"


def test_mismatched_address_prints_no_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "test mnemonic must stay private"
    monkeypatch.setattr(verify, "_instance_name", lambda: verify.HOST_NAME)
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *args, **_kwargs: subprocess.CompletedProcess(
            args, 0, stdout=secret.encode()
        ),
    )
    monkeypatch.setattr(
        verify.bt.Keypair,
        "create_from_mnemonic",
        lambda _mnemonic: SimpleNamespace(ss58_address="different-address"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "treasury_verify_key.py",
            "--project",
            "project",
            "--expected-address",
            "public-address",
        ],
    )

    with pytest.raises(RuntimeError, match="stored key does not match"):
        verify.main()
    assert secret not in capsys.readouterr().out
