"""``ditto readmit``: wire format, refuse-before-paying, the payment ledger, outcomes.

The ledger invariant: a miner never pays twice and never loses track of a
payment already made, across reruns, era changes, explicit recovery flags and
transfers whose outcome is unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace as replace_state
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import bittensor
import pytest

from ditto.miner_cli.commands import readmit as readmit_cmd
from ditto.miner_cli.errors import MinerCliError, ReadmissionPaymentRejectedError
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.payment import ChainTransfer
from ditto.miner_cli.preferences import (
    PreferencesUnreadableError,
    ReadmissionBusyError,
    ReadmissionRecord,
    load_pending_payments_for_hotkey,
    load_readmission_record,
    save_readmission_record,
)
from ditto.miner_cli.signing import build_readmission_payload

_KEY = bittensor.Keypair.create_from_uri("//Bob")
_COLD = bittensor.Keypair.create_from_uri("//Alice").ss58_address
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
    wallet = SimpleNamespace(
        hotkey=_KEY, coldkeypub=SimpleNamespace(ss58_address=_COLD)
    )
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

    def _no_chain(**_kw: Any) -> ChainTransfer:
        raise AssertionError("no chain read expected")

    monkeypatch.setattr(readmit_cmd, "read_transfer", _no_chain)
    return calls


def _transfer(**overrides: Any) -> ChainTransfer:
    """The on-chain transfer the fake check reserved, finalized just now."""
    values: dict[str, Any] = {
        "call": "Balances.transfer_keep_alive",
        "signer": _COLD,
        "dest": "5Dest",
        "amount_rao": 75_000_000,
        "timestamp_ms": int(time.time() * 1000),
        "succeeded": True,
    }
    values.update(overrides)
    return ChainTransfer(**values)


def _chain(
    monkeypatch: pytest.MonkeyPatch, transfer: ChainTransfer | Exception
) -> None:
    def _read(**_kw: Any) -> ChainTransfer:
        if isinstance(transfer, Exception):
            raise transfer
        return transfer

    monkeypatch.setattr(readmit_cmd, "read_transfer", _read)


def _paying() -> ReadmissionRecord:
    return ReadmissionRecord(
        agent_id=str(_AGENT),
        bench_version=19,
        state="paying",
        token="00000000-0000-4000-8000-000000000009",
        amount_rao=75_000_000,
        send_address="5Dest",
    )


def _store(record: ReadmissionRecord) -> None:
    assert save_readmission_record(
        network="local", hotkey=_KEY.ss58_address, record=record
    )


def _record() -> ReadmissionRecord | None:
    return load_readmission_record(
        network="local", hotkey=_KEY.ss58_address, agent_id=str(_AGENT)
    )


def _saved() -> PaymentReceipt | None:
    record = _record()
    return record.payment if record is not None else None


_PROOF = PaymentReceipt(block_hash=_BLOCK, block_number=77, extrinsic_index=3)
_OTHER = PaymentReceipt(block_hash="0x" + "cd" * 32, block_number=78, extrinsic_index=1)


def _flags(receipt: PaymentReceipt) -> dict[str, Any]:
    return {
        "payment_block_hash": receipt.block_hash,
        "payment_block_number": receipt.block_number,
        "payment_extrinsic_index": receipt.extrinsic_index,
    }


def _active(version: int) -> dict[str, Any]:
    terms = dict(_TERMS)
    terms["readmission"] = {
        **_TERMS["readmission"],  # type: ignore[dict-item]
        "activation": {
            "active": True,
            "inactive_reasons": [],
            "active_bench_version": version,
        },
    }
    return terms


def test_the_signature_payload_is_the_pinned_wire_format() -> None:
    """Pinned literally here and in Platform's readmission endpoint tests."""
    payload = build_readmission_payload(
        operation="commit",
        hotkey_ss58="5Hot",
        agent_id=_AGENT,
        bench_version=19,
        signature_timestamp=1700000000,
        signature_nonce=UUID("00000000-0000-4000-8000-000000000001"),
    )
    assert payload == (
        b"ditto-readmit-v1:commit:5Hot:7c9e6679-7425-40de-944b-e07fc1f90ae7:19:"
        b"1700000000:00000000-0000-4000-8000-000000000001"
    )
    with pytest.raises(ValueError):
        build_readmission_payload(
            operation="upload",
            hotkey_ss58="5Hot",
            agent_id=_AGENT,
            bench_version=19,
            signature_timestamp=1700000000,
            signature_nonce=UUID("00000000-0000-4000-8000-000000000001"),
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
    # The check reserved, so it was signed as a reservation.
    reserve_payload = build_readmission_payload(
        operation="reserve",
        hotkey_ss58=_KEY.ss58_address,
        agent_id=_AGENT,
        bench_version=19,
        signature_timestamp=check["signature_timestamp"],
        signature_nonce=UUID(check["signature_nonce"]),
    )
    assert _KEY.verify(reserve_payload, bytes.fromhex(check["signature"]))
    # The commit is signed as a commit, over the readmission domain.
    payload = build_readmission_payload(
        operation="commit",
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

    # A rerun reuses the saved proof, and the reservation token it was paid
    # for, without paying again.
    calls = _install(monkeypatch)
    assert readmit_cmd.run(_args()) == 0
    assert not any("pay" in call for call in calls)
    assert _FakeClient.checks == []
    assert _FakeClient.commits[0]["payment_block_hash"] == _BLOCK
    assert _FakeClient.commits[0]["admission_token"] == (
        "00000000-0000-4000-8000-000000000009"
    )
    assert _saved() is None


def test_explicit_recovery_flags_also_restore_the_saved_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args())
    _install(monkeypatch)
    assert (
        readmit_cmd.run(
            _args(
                payment_block_hash=_BLOCK,
                payment_block_number=77,
                payment_extrinsic_index=3,
            )
        )
        == 0
    )
    [commit] = _FakeClient.commits
    assert commit["admission_token"] == "00000000-0000-4000-8000-000000000009"


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


def test_a_malformed_commit_response_keeps_the_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    _FakeClient.commit = {"status": "ok"}
    with pytest.raises(MinerCliError, match="invalid readmission response"):
        readmit_cmd.run(_args())
    assert _saved() == PaymentReceipt(
        block_hash=_BLOCK, block_number=77, extrinsic_index=3
    )


def test_nothing_is_paid_when_the_reservation_cannot_be_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch)
    monkeypatch.setattr(readmit_cmd, "save_readmission_record", lambda **_kw: False)
    with pytest.raises(MinerCliError, match="no payment was sent"):
        readmit_cmd.run(_args())
    assert not any("pay" in call for call in calls)
    assert _FakeClient.commits == []


def test_unsaved_proof_guidance_never_says_rerun_the_same_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing saved, a plain rerun would pay again: insist on the flags."""
    _install(monkeypatch)
    real_save = readmit_cmd.save_readmission_record

    def _save(**kw: Any) -> bool:
        # The reserved and paying records persist; the paid one does not.
        return kw["record"].state != "paid" and real_save(**kw)

    monkeypatch.setattr(readmit_cmd, "save_readmission_record", _save)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args())
    err = capsys.readouterr().err
    assert "Do not rerun without these payment flags" in err
    assert "Rerun the same command" not in err
    assert "--payment-block-hash " + _BLOCK in err


@pytest.mark.parametrize("activation", ["not-a-dict", ["v19"], 19])
def test_a_malformed_activation_is_refused_before_paying(
    monkeypatch: pytest.MonkeyPatch, activation: object
) -> None:
    calls = _install(monkeypatch)
    terms = {**_TERMS, "readmission": {"activation": activation}}
    monkeypatch.setattr(
        _FakeClient, "get_submission_terms", lambda _self, _competition: terms
    )
    with pytest.raises(MinerCliError, match="does not offer readmission"):
        readmit_cmd.run(_args())
    assert calls == []


# --- the ledger invariant, one test per review finding (SN-485 #3563) -------


def test_a_rerun_after_the_era_changes_commits_the_saved_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607089935: the record is keyed by agent, not era; the paid era wins."""
    _install(monkeypatch)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args())
    calls = _install(monkeypatch)
    monkeypatch.setattr(
        _FakeClient, "get_submission_terms", lambda _self, _competition: _active(20)
    )
    _FakeClient.commit = {
        "outcome": "credited",
        "refusal_reason": "the active era moved to v20",
        "credit_expires_at": "2026-12-30T00:00:00Z",
    }
    assert readmit_cmd.run(_args()) == 1
    assert not any("pay" in call for call in calls)
    assert _FakeClient.checks == []
    [commit] = _FakeClient.commits
    assert commit["bench_version"] == 19
    assert commit["payment_block_hash"] == _BLOCK
    assert _record() is None


def test_recovery_flags_never_drop_a_different_saved_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607090550: a different saved proof is refused, not left or lost."""
    _install(monkeypatch)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args())
    calls = _install(monkeypatch)
    with pytest.raises(MinerCliError, match="differs from these --payment"):
        readmit_cmd.run(_args(**_flags(_OTHER)))
    assert _FakeClient.commits == [] and not any("pay" in c for c in calls)
    assert _saved() == _PROOF
    # The matching flags resolve it and leave nothing stale behind.
    _install(monkeypatch)
    assert readmit_cmd.run(_args(**_flags(_PROOF))) == 0
    assert _record() is None


def test_an_unconfirmed_transfer_is_never_paid_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """4607091041: a timeout with no receipt blocks any second transfer."""
    calls = _install(monkeypatch)

    def _timeout(**kw: Any) -> PaymentReceipt:
        calls.append({"pay": kw})
        raise TimeoutError("finalization timed out")

    monkeypatch.setattr(readmit_cmd, "submit_eval_payment", _timeout)
    with pytest.raises(TimeoutError):
        readmit_cmd.run(_args())
    record = _record()
    assert record is not None and record.state == "paying"
    assert "Do not pay again" in capsys.readouterr().err

    calls = _install(monkeypatch)
    with pytest.raises(MinerCliError, match="never confirmed"):
        readmit_cmd.run(_args())
    assert not any("pay" in c for c in calls) and _FakeClient.checks == []

    # Its proof, once found in the coldkey history and matched on chain,
    # resolves it.
    _install(monkeypatch)
    _chain(monkeypatch, _transfer())
    assert readmit_cmd.run(_args(**_flags(_PROOF))) == 0
    assert _FakeClient.commits[0]["admission_token"] == (
        "00000000-0000-4000-8000-000000000009"
    )
    assert _record() is None


def test_discarding_an_unconfirmed_transfer_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(_paying())
    calls = _install(monkeypatch)
    assert readmit_cmd.run(_args(discard_unconfirmed_payment=True)) == 0
    assert any("pay" in c for c in calls) and len(_FakeClient.checks) == 1


def test_explicit_recovery_proofs_are_saved_before_the_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """4607333170: a flags-supplied proof is really saved, so a plain rerun
    reuses it and never reserves or pays again."""
    _install(monkeypatch)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args(**_flags(_PROOF)))
    assert _saved() == _PROOF
    assert "Rerun the same command" in capsys.readouterr().err
    calls = _install(monkeypatch)
    assert readmit_cmd.run(_args()) == 0
    assert not any("pay" in c for c in calls) and _FakeClient.checks == []
    assert _FakeClient.commits[0]["payment_block_hash"] == _BLOCK


def test_a_rejected_proof_supplied_by_flags_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607333593: a definitive 32xx clears that exact proof however supplied."""
    _install(monkeypatch)
    _FakeClient.commit = RuntimeError("gateway timeout")
    with pytest.raises(RuntimeError):
        readmit_cmd.run(_args())
    _install(monkeypatch)
    _FakeClient.commit = ReadmissionPaymentRejectedError("3208 recovery expired")
    with pytest.raises(ReadmissionPaymentRejectedError):
        readmit_cmd.run(_args(**_flags(_PROOF)))
    assert _record() is None


def test_tokenless_flag_recovery_commits_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607334065: with the local config gone the commit carries no token, and
    Platform falls back to the reservation it holds for this agent and era
    (pinned in Platform's test_a_tokenless_commit_keeps_the_reserved_price)."""
    _install(monkeypatch)
    assert readmit_cmd.run(_args(**_flags(_PROOF))) == 0
    [commit] = _FakeClient.commits
    assert "admission_token" not in commit
    assert commit["bench_version"] == 19


# --- second review round (SN-485 #3563): locking, matching, truthful state --


def test_two_concurrent_readmits_send_exactly_one_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607476522: two runs racing past the first read cannot both pay.

    The first run is held inside its eligibility check, after it read "no
    record" and before it writes anything, which is exactly the window in
    which a second run used to read "no record" too and go on to pay.
    """
    calls = _install(monkeypatch)
    checking = threading.Event()
    release = threading.Event()
    real_check = _FakeClient.post_readmission_check

    def _held_check(self: _FakeClient, body: dict[str, Any]) -> dict[str, Any]:
        if not checking.is_set():
            checking.set()
            assert release.wait(10)
        return real_check(self, body)

    monkeypatch.setattr(_FakeClient, "post_readmission_check", _held_check)
    outcomes: dict[str, object] = {}

    def _race(name: str) -> None:
        try:
            outcomes[name] = readmit_cmd.run(_args())
        except BaseException as error:  # noqa: BLE001 - recorded for the assert
            outcomes[name] = error

    first = threading.Thread(target=_race, args=("first",))
    first.start()
    assert checking.wait(10), "the first run never reached its check"
    second = threading.Thread(target=_race, args=("second",))
    second.start()
    second.join(10)
    release.set()
    first.join(10)

    assert sum("pay" in c for c in calls) == 1
    assert outcomes["first"] == 0
    assert isinstance(outcomes["second"], ReadmissionBusyError)
    assert "nothing was sent" in str(outcomes["second"])
    assert len(_FakeClient.checks) == 1 and len(_FakeClient.commits) == 1
    assert _record() is None


def test_another_process_holding_the_agent_lock_blocks_payment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lock is an OS lock: a separate process holding it stops this run."""
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, sys, time\n"
            "from ditto.miner_cli.preferences import readmission_lock\n"
            "with readmission_lock(network='local', hotkey=sys.argv[1], "
            "agent_id=sys.argv[2]):\n"
            "    pathlib.Path(sys.argv[3]).touch()\n"
            "    time.sleep(30)\n",
            _KEY.ss58_address,
            str(_AGENT),
            str(ready),
        ],
        env=os.environ.copy(),
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert holder.poll() is None, "the lock holder exited early"
            assert time.monotonic() < deadline, "the lock holder never started"
            time.sleep(0.05)
        calls = _install(monkeypatch)
        with pytest.raises(ReadmissionBusyError, match="nothing was sent"):
            readmit_cmd.run(_args())
        assert not any("pay" in c for c in calls) and _FakeClient.checks == []
    finally:
        holder.kill()
        holder.wait()
    # Released by the kernel when the holder dies.
    _install(monkeypatch)
    assert readmit_cmd.run(_args()) == 0


@pytest.mark.parametrize(
    "mismatch",
    [
        {"amount_rao": 50_000_000},
        {"dest": "5Elsewhere"},
        {"signer": _KEY.ss58_address},
        {"call": "Balances.transfer_all"},
        {"timestamp_ms": int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)},
        # 4607620558: a finalized but dispatch-failed transfer moved no funds.
        {"succeeded": False},
        {"succeeded": None},
        "unreachable",
    ],
)
def test_recovery_flags_must_be_the_unconfirmed_transfer(
    monkeypatch: pytest.MonkeyPatch, mismatch: dict[str, Any] | str
) -> None:
    """4607477001: a `paying` record is resolved only by its own transfer."""
    _store(_paying())
    calls = _install(monkeypatch)
    _chain(
        monkeypatch,
        readmit_cmd.PaymentSubmissionError("node down")
        if mismatch == "unreachable"
        else _transfer(**mismatch),  # type: ignore[arg-type]
    )
    with pytest.raises(MinerCliError, match="unconfirmed transfer record is kept"):
        readmit_cmd.run(_args(**_flags(_OTHER)))
    assert _FakeClient.commits == [] and not any("pay" in c for c in calls)
    record = _record()
    assert record is not None and record.state == "paying"
    assert record.payment is None


def test_an_unrelated_proof_can_never_clear_the_unconfirmed_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607477001: even if Platform would reject it, an unrelated proof never
    reaches the commit, so its rejection cannot clear the `paying` record."""
    _store(_paying())
    _install(monkeypatch)
    _chain(monkeypatch, _transfer(amount_rao=1))
    _FakeClient.commit = ReadmissionPaymentRejectedError("3207 spent proof")
    with pytest.raises(MinerCliError):
        readmit_cmd.run(_args(**_flags(_OTHER)))
    record = _record()
    assert record is not None and record.state == "paying"


def test_an_era_mismatch_states_the_saved_payment_truthfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4607477458: never "no payment was sent" when a payment is saved."""
    _store(replace_state(_paying(), state="paid", payment=_PROOF))
    calls = _install(monkeypatch)
    with pytest.raises(MinerCliError) as caught:
        readmit_cmd.run(_args(bench_version=20))
    message = str(caught.value)
    assert "finalized readmission payment is saved for v19" in message
    assert "no payment was sent" not in message
    assert "record is kept" in message
    assert _saved() == _PROOF and not any("pay" in c for c in calls)

    _store(_paying())
    _install(monkeypatch)
    with pytest.raises(MinerCliError) as caught:
        readmit_cmd.run(_args(bench_version=20, **_flags(_PROOF)))
    assert "unconfirmed readmission transfer" in str(caught.value)
    assert "it may have gone through" in str(caught.value)


def test_a_rejected_own_proof_is_removed_even_when_its_save_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """4607478277: the `paying` record of this run's own transfer is resolved
    by Platform's rejection, and the message says what really happened."""
    _install(monkeypatch)
    real_save = readmit_cmd.save_readmission_record
    monkeypatch.setattr(
        readmit_cmd,
        "save_readmission_record",
        lambda **kw: kw["record"].state != "paid" and real_save(**kw),
    )
    _FakeClient.commit = ReadmissionPaymentRejectedError("3201 not found")
    with pytest.raises(ReadmissionPaymentRejectedError):
        readmit_cmd.run(_args())
    assert _record() is None
    assert "its local record was removed" in capsys.readouterr().err


def test_a_rejected_proof_that_was_not_removed_is_reported_as_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """4607478277: when the record survives, the CLI says so and why."""
    _install(monkeypatch)
    monkeypatch.setattr(readmit_cmd, "clear_readmission_record", lambda **_kw: False)
    _FakeClient.commit = ReadmissionPaymentRejectedError("3201 not found")
    with pytest.raises(ReadmissionPaymentRejectedError):
        readmit_cmd.run(_args())
    err = capsys.readouterr().err
    assert "was not removed" in err and "removed. Rerunning" not in err
    assert "finalized readmission payment is saved for v19" in err
    assert _saved() == _PROOF


@pytest.mark.parametrize(
    "corrupt",
    [
        {"state": "paid", "block_hash": "0x12"},
        {"state": "paid"},
        {"state": "paying"},
        {"state": "settled"},
        "not an object",
    ],
)
def test_a_malformed_record_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, corrupt: dict[str, Any] | str
) -> None:
    """4607478844: a malformed record is never read as "no record"."""
    key = f"local:{_KEY.ss58_address}:{_AGENT}"
    value: object = (
        {"agent_id": str(_AGENT), "bench_version": 19, **corrupt}
        if isinstance(corrupt, dict)
        else corrupt
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"pending_readmissions": {key: value}}))
    before = config.read_text()
    calls = _install(monkeypatch)
    with pytest.raises(PreferencesUnreadableError, match=str(config)):
        readmit_cmd.run(_args())
    with pytest.raises(PreferencesUnreadableError):
        readmit_cmd.run(_args(discard_unconfirmed_payment=True))
    assert not any("pay" in c for c in calls) and _FakeClient.checks == []
    assert config.read_text() == before


def test_an_unparseable_config_fails_closed_and_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"pending_readmissions": {')
    calls = _install(monkeypatch)
    with pytest.raises(PreferencesUnreadableError, match="not valid JSON"):
        readmit_cmd.run(_args())
    assert not any("pay" in c for c in calls)
    assert config.read_text() == '{"pending_readmissions": {'
