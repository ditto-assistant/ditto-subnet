"""``ditto readmit``: wire format, refuse-before-paying, proof storage, outcomes."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import bittensor
import pytest

from ditto.miner_cli.commands import readmit as readmit_cmd
from ditto.miner_cli.errors import MinerCliError, ReadmissionPaymentRejectedError
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.preferences import (
    READMISSION_PAYMENTS_BUCKET,
    load_pending_payment,
    load_pending_payments_for_hotkey,
)
from ditto.miner_cli.signing import build_readmission_payload

_KEY = bittensor.Keypair.create_from_uri("//Bob")
_AGENT = UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
_BLOCK = "0x" + "ab" * 32
_TERMS = {
    "competition": "memory",
    "deliverable": {},
    "fee": {},
    "cooldown": {},
    "readmission": {
        "activation": {
            "active": True,
            "inactive_reasons": [],
            "active_bench_version": 19,
        },
        "cost_basis": {
            "bench_version": 19,
            "agents_sampled": 12,
            "median_agent_cost_usd": 1.05,
        },
    },
}


@pytest.fixture(autouse=True)
def _isolated_cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(tmp_path / "config.json"))


class _FakeClient:
    check: dict[str, Any] = {}
    commit: dict[str, Any] | Exception = {}
    checks: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get_submission_terms(self, competition: str) -> dict[str, Any] | None:
        assert competition == "memory"
        return _TERMS

    def post_readmission_check(self, body: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.checks.append(body)
        return _FakeClient.check

    def post_readmission_commit(self, body: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.commits.append(body)
        if isinstance(_FakeClient.commit, Exception):
            raise _FakeClient.commit
        return _FakeClient.commit


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "network": "local",
        "agent_id": _AGENT,
        "bench_version": None,
        "coldkey_name": "cold",
        "hotkey_name": "hot",
        "yes": True,
        "payment_block_hash": None,
        "payment_block_number": None,
        "payment_extrinsic_index": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _install(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    handle = SimpleNamespace(hotkey_ss58=_KEY.ss58_address)
    wallet = SimpleNamespace(hotkey=_KEY)
    monkeypatch.setattr(readmit_cmd, "load_wallet", lambda **_kw: (handle, wallet))
    monkeypatch.setattr(readmit_cmd, "ApiClient", _FakeClient)
    _FakeClient.checks = []
    _FakeClient.commits = []
    _FakeClient.check = {
        "ok": True,
        "payment_amount_rao": 75_000_000,
        "payment_send_address": "5Dest",
        "admission_token": "00000000-0000-4000-8000-000000000009",
        "eligibility": {"owner_best_stranded": True},
    }
    _FakeClient.commit = {"outcome": "admitted", "bench_version": 19}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        readmit_cmd, "preflight_payment_signer", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(readmit_cmd, "confirm_payment", lambda **kw: calls.append(kw))

    def _pay(**kw: Any) -> PaymentReceipt:
        calls.append({"pay": kw})
        return PaymentReceipt(block_hash=_BLOCK, block_number=77, extrinsic_index=3)

    monkeypatch.setattr(readmit_cmd, "submit_eval_payment", _pay)
    return calls


def _saved() -> PaymentReceipt | None:
    return load_pending_payment(
        network="local",
        hotkey=_KEY.ss58_address,
        name=readmit_cmd.PENDING_PAYMENT_NAME,
        sha256=readmit_cmd._pending_key(_AGENT, 19),
        bucket=READMISSION_PAYMENTS_BUCKET,
    )


def test_the_signature_payload_is_the_pinned_wire_format() -> None:
    """Pinned literally here and in Platform's readmission endpoint tests."""
    payload = build_readmission_payload(
        hotkey_ss58="5Hot",
        agent_id=_AGENT,
        bench_version=19,
        signature_timestamp=1700000000,
        signature_nonce=UUID("00000000-0000-4000-8000-000000000001"),
    )
    assert payload == (
        b"ditto-readmit-v1:5Hot:7c9e6679-7425-40de-944b-e07fc1f90ae7:19:"
        b"1700000000:00000000-0000-4000-8000-000000000001"
    )


def test_a_refused_check_pays_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch)
    _FakeClient.check = {"ok": False, "messages": ["readmission is not active"]}
    with pytest.raises(MinerCliError, match="no payment was sent"):
        readmit_cmd.run(_args())
    assert not any("pay" in call for call in calls)
    assert _FakeClient.commits == []


def test_admits_after_paying_the_reserved_price_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch)
    assert readmit_cmd.run(_args()) == 0
    [check] = _FakeClient.checks
    assert check["reserve_slot"] is True and check["bench_version"] == 19
    pay = next(call["pay"] for call in calls if "pay" in call)
    assert pay["amount_rao"] == 75_000_000 and pay["dest_address"] == "5Dest"
    [commit] = _FakeClient.commits
    assert commit["admission_token"] == "00000000-0000-4000-8000-000000000009"
    assert commit["payment_block_hash"] == _BLOCK
    # The signature is over the readmission domain, never the upload one.
    payload = build_readmission_payload(
        hotkey_ss58=_KEY.ss58_address,
        agent_id=_AGENT,
        bench_version=19,
        signature_timestamp=commit["signature_timestamp"],
        signature_nonce=UUID(commit["signature_nonce"]),
    )
    assert _KEY.verify(payload, bytes.fromhex(commit["signature"]))
    assert _saved() is None


def test_the_proof_lives_in_its_own_bucket_and_survives_a_failed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args())
    assert _saved() == PaymentReceipt(
        block_hash=_BLOCK, block_number=77, extrinsic_index=3
    )
    # An upload never sees a readmission proof as a reusable credit.
    assert (
        load_pending_payments_for_hotkey(network="local", hotkey=_KEY.ss58_address)
        == ()
    )

    # A rerun reuses the saved proof without paying again.
    calls = _install(monkeypatch)
    assert readmit_cmd.run(_args()) == 0
    assert not any("pay" in call for call in calls)
    assert _FakeClient.checks == []
    assert _FakeClient.commits[0]["payment_block_hash"] == _BLOCK
    assert _saved() is None


def test_a_definitively_refused_proof_is_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    _FakeClient.commit = ReadmissionPaymentRejectedError("3203 amount mismatch")
    with pytest.raises(ReadmissionPaymentRejectedError):
        readmit_cmd.run(_args())
    assert _saved() is None


def test_a_credited_outcome_explains_the_credit_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch)
    _FakeClient.commit = {
        "outcome": "credited",
        "refusal_reason": "a benchmark rollout opened after payment",
        "credit_expires_at": "2026-12-30T00:00:00Z",
    }
    assert readmit_cmd.run(_args()) == 1
    err = capsys.readouterr().err
    assert "a benchmark rollout opened after payment" in err
    assert "upload credit" in err and "No TAO is refunded" in err
    assert "Readmission: active" in err
    assert "$1.05" in err


def test_a_platform_without_readmission_is_refused_before_paying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch)
    monkeypatch.setattr(
        _FakeClient, "get_submission_terms", lambda _self, _competition: None
    )
    with pytest.raises(MinerCliError, match="does not offer readmission"):
        readmit_cmd.run(_args())
    assert calls == []


def test_the_parser_registers_readmit() -> None:
    from ditto.miner_cli.__main__ import _build_parser

    args = _build_parser().parse_args(["readmit", str(_AGENT), "--yes"])
    assert args.agent_id == _AGENT
    assert args.func is readmit_cmd.run
