"""Offline packet integrity controls using synthetic public test approvals."""

import json
import subprocess
import sys
from dataclasses import asdict

import pytest
from bittensor_wallet import Keypair

from ditto.tests.test_collector_automation import policy
from ditto.treasury.collector import canonical
from ditto.treasury.service_allocation import ServiceDestination
from scripts.prepare_treasury_manual_packet import prepare


def approval(tmp_path):
    key = Keypair.create_from_uri("//Alice")
    p = policy(
        collector_coldkey=key.ss58_address,
        collector_hotkey=Keypair.create_from_uri("//Bob").ss58_address,
        registration_delegate=Keypair.create_from_uri("//Charlie").ss58_address,
        transfer_delegate=Keypair.create_from_uri("//Dave").ss58_address,
        destinations=(
            ServiceDestination(
                "gm", 600, Keypair.create_from_uri("//Eve").ss58_address
            ),
            ServiceDestination(
                "bitsec", 400, Keypair.create_from_uri("//Ferdie").ss58_address
            ),
        ),
    )
    path = tmp_path / "public-collector.json"
    path.write_text(
        json.dumps(
            {
                "policy": asdict(p),
                "signature": "0x"
                + key.sign(("ditto-collector-policy-v1:" + p.digest).encode()).hex(),
            }
        )
    )
    return path, p


def check(packet):
    return subprocess.run(
        [sys.executable, "-I", str(packet / "sign-manual-limit.py"), "--check-request"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_public_packet_checks_without_wallet_access_and_refuses_overwrite(tmp_path):
    path, p = approval(tmp_path)
    packet = tmp_path / "packet"
    result = prepare(path, p.digest, packet)
    verified = check(packet)
    assert verified.returncode == 0
    assert json.loads(verified.stdout)["digest"] == result["approval_digest"]
    assert json.loads(verified.stdout)["transactions_signed_or_sent"] == 0
    assert list((packet / "public").iterdir()) == []
    with pytest.raises(FileExistsError):
        prepare(path, p.digest, packet)


@pytest.mark.parametrize("fault", ["amount", "collector", "symlink"])
def test_usb_request_tamper_refuses_before_crypto_and_logs_only_reason(tmp_path, fault):
    path, p = approval(tmp_path)
    packet = tmp_path / "packet"
    prepare(path, p.digest, packet)
    request = packet / "manual-limit-policy.json"
    raw = json.loads(request.read_text())
    if fault == "symlink":
        request.unlink()
        request.symlink_to(path)
    else:
        raw["max_amount_rao" if fault == "amount" else "collector_coldkey"] = (
            2_500_000_000_001 if fault == "amount" else "unapproved-wallet"
        )
        request.write_text(canonical(raw))
    refused = check(packet)
    assert refused.returncode == 1
    assert not (packet / "public/manual-limit-approval.json").exists()
    status = json.loads((packet / "public/signing-result.json").read_text())
    assert status["status"] == "refused" and status["transactions_signed_or_sent"] == 0
    assert status["code"] in {"input-file-mismatch", "symlink-path"}


def test_unapproved_base_policy_never_creates_packet(tmp_path):
    path, p = approval(tmp_path)
    raw = json.loads(path.read_text())
    raw["signature"] = "0x" + "00" * 64
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        prepare(path, p.digest, tmp_path / "packet")
    assert not (tmp_path / "packet").exists()
