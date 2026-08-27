from __future__ import annotations

import importlib.util
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra/ansible/scripts/generate_validator_hotkey.py"
MATERIALIZER = (
    ROOT / "infra/ansible/roles/validator_stack/templates/materialize_wallet.py.j2"
)
ENV_TEMPLATE = ROOT / "infra/ansible/roles/validator_stack/templates/validator.env.j2"


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_validator_hotkey", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_streams_mnemonic_only_to_secret_manager(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    mnemonic = "sensitive mnemonic that must never be printed"
    calls: list[tuple[list[str], str | None]] = []

    class FakeKeypair:
        ss58_address = "5" + "A" * 47

        @staticmethod
        def generate_mnemonic(*, n_words: int) -> str:
            assert n_words == 24
            return mnemonic

        @staticmethod
        def create_from_mnemonic(value: str) -> FakeKeypair:
            assert value == mnemonic
            return FakeKeypair()

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert text and capture_output and not check
        calls.append((command, input))
        if command[1:4] == ["secrets", "versions", "list"]:
            stdout = ""
        elif command[1:4] == ["secrets", "versions", "add"]:
            stdout = "projects/p/secrets/s/versions/1\n"
        else:
            stdout = "projects/p/secrets/s\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(module.bt, "Keypair", FakeKeypair)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--confirm", module.CONFIRMATION],
    )

    assert module.main() == 0
    output = capsys.readouterr()
    assert FakeKeypair.ss58_address in output.out
    assert "versions/1" in output.out
    assert mnemonic not in output.out
    assert mnemonic not in output.err
    assert all(mnemonic not in " ".join(command) for command, _ in calls)
    assert [stdin for _, stdin in calls] == [None, None, f"{mnemonic}\n"]


def test_generator_refuses_to_replace_any_existing_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()

    def fake_run(
        command: list[str],
        *,
        input: str | None,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, capture_output, check
        stdout = (
            "1\n" if command[1:4] == ["secrets", "versions", "list"] else "secret\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--confirm", module.CONFIRMATION],
    )

    with pytest.raises(RuntimeError, match="already has a version"):
        module.main()


def test_materializer_writes_only_the_hotkey_and_verifies_expected_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bittensor as bt

    mnemonic = bt.Keypair.generate_mnemonic(n_words=24)
    expected = bt.Keypair.create_from_mnemonic(mnemonic).ss58_address
    wallet_path = tmp_path / "wallets"
    environment = {
        "VALIDATOR_MNEMONIC": mnemonic,
        "EXPECTED_HOTKEY": expected,
        "BT_WALLET_PATH": str(wallet_path),
        "BT_WALLET_NAME": "validator",
        "BT_HOTKEY_NAME": "gcp-prod",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    runpy.run_path(str(MATERIALIZER), run_name="__main__")

    hotkey_file = wallet_path / "validator/hotkeys/gcp-prod"
    assert hotkey_file.is_file()
    assert not (wallet_path / "validator/coldkey").exists()

    monkeypatch.delenv("VALIDATOR_MNEMONIC")
    monkeypatch.setenv("VERIFY_ONLY", "1")
    runpy.run_path(str(MATERIALIZER), run_name="__main__")


def test_production_environment_never_contains_signing_seed_or_coldkey() -> None:
    template = ENV_TEMPLATE.read_text()

    assert "MNEMONIC" not in template
    assert "COLDKEY" not in template
    assert "VALIDATOR_MNEMONIC" not in template
