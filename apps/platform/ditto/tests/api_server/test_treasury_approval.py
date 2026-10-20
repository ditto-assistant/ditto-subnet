"""Optional public proposal proof; synthetic signatures never approve funding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bittensor_wallet import Keypair

from ditto.api_server.config import parse_treasury_shadow_approval
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.treasury_shadow import shadow_readiness
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    approval_message,
)

ENV = (
    "DITTO_TREASURY_SHADOW_APPROVAL_FILE",
    "DITTO_TREASURY_APPROVED_POLICY_DIGEST",
    "DITTO_TREASURY_COLLECTOR_POLICY_DIGEST",
)


def fixture():
    coldkey = Keypair.create_from_uri("//Alice")
    policy = TreasuryEmissionPolicy(
        revision=1,
        genesis_hash="0x" + "11" * 32,
        collector_hotkey=Keypair.create_from_uri("//Bob").ss58_address,
        collector_coldkey=coldkey.ss58_address,
        collector_policy_digest="c" * 64,
        buckets=[
            {
                "bucket_id": "gamma",
                "allocation_bps": 1000,
                "holding_coldkey": Keypair.create_from_uri("//Charlie").ss58_address,
            }
        ],
    )
    return TreasuryPolicyApproval(
        policy=policy,
        signature="0x" + coldkey.sign(approval_message(policy)).hex(),
    )


def configure(monkeypatch, tmp_path, approval):
    path = tmp_path / "public-proposal-approval.json"
    path.write_text(approval.model_dump_json())
    for name, value in zip(
        ENV,
        (str(path), approval.policy.digest, approval.policy.collector_policy_digest),
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    return path


def test_absent_approval_never_opens_a_file(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    assert parse_treasury_shadow_approval(None) == (None, None, None)


def test_exact_public_approval_verifies_without_changing_proposal(
    monkeypatch, tmp_path
):
    approval = fixture()
    configure(monkeypatch, tmp_path, approval)
    result = parse_treasury_shadow_approval(approval.policy)
    assert result == (
        approval,
        approval.policy.digest,
        approval.policy.collector_policy_digest,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_file",
        "oversized",
        "wrong_digest",
        "wrong_collector_digest",
        "bad_signature",
        "no_proposal",
    ],
)
def test_bad_approval_refuses_boot_without_echoing_input(monkeypatch, tmp_path, fault):
    approval = fixture()
    path = configure(monkeypatch, tmp_path, approval)
    if fault == "missing_file":
        path.unlink()
    elif fault == "oversized":
        path.write_text("never-echo-public-input" * 1000)
    elif fault == "wrong_digest":
        monkeypatch.setenv(ENV[1], "d" * 64)
    elif fault == "wrong_collector_digest":
        monkeypatch.setenv(ENV[2], "d" * 64)
    elif fault == "bad_signature":
        path.write_text(
            approval.model_copy(
                update={"signature": "0x" + "00" * 64}
            ).model_dump_json()
        )
    with pytest.raises(ApiServerConfigError) as caught:
        parse_treasury_shadow_approval(
            None if fault == "no_proposal" else approval.policy
        )
    assert str(path) not in str(caught.value)
    assert "never-echo" not in str(caught.value)
    assert approval.signature not in str(caught.value)


@pytest.mark.parametrize("missing", ENV)
def test_partial_approval_configuration_refuses(monkeypatch, tmp_path, missing):
    approval = fixture()
    configure(monkeypatch, tmp_path, approval)
    monkeypatch.delenv(missing)
    with pytest.raises(ApiServerConfigError):
        parse_treasury_shadow_approval(approval.policy)


def state(approval):
    return SimpleNamespace(
        config=SimpleNamespace(
            treasury_shadow_policy=approval.policy,
            treasury_shadow_approval=approval,
            treasury_approved_policy_digest=approval.policy.digest,
            treasury_approved_collector_policy_digest=approval.policy.collector_policy_digest,
        ),
        chain=SimpleNamespace(get_treasury_collector_pin=AsyncMock()),
    )


def test_proposal_signature_does_not_approve_epoch_or_enforcement():
    approval = fixture()
    app = state(approval)
    result = shadow_readiness(app, None)
    assert result.proposal_approval_status == "verified"
    assert result.proposal_approved_policy_digest == approval.policy.digest
    assert result.offline_policy_verified is False
    assert result.can_enforce_weights is False
    assert result.weight_effect == "none"
    assert result.stored_shadow_pin is None
    assert "offline_policy_unverified" in result.blocking_reasons
    app.chain.get_treasury_collector_pin.assert_not_called()


@pytest.mark.parametrize("fault", ["latest_proposal", "signature", "deployment_pin"])
def test_readiness_reverifies_exact_proposal_and_never_substitutes_latest(fault):
    approval = fixture()
    app = state(approval)
    if fault == "latest_proposal":
        app.config.treasury_shadow_policy = approval.policy.model_copy(
            update={"revision": 2}
        )
    elif fault == "signature":
        app.config.treasury_shadow_approval = approval.model_copy(
            update={"signature": "0x" + "00" * 64}
        )
    else:
        app.config.treasury_approved_policy_digest = "d" * 64
    result = shadow_readiness(app, None)
    assert result.proposal_approval_status == "invalid"
    assert result.proposal_approved_policy_digest is None
    assert result.can_enforce_weights is False
    app.chain.get_treasury_collector_pin.assert_not_called()
